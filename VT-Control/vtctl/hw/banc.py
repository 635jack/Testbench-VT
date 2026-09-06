#!/usr/bin/env python3
"""
banc.py — les quatre propriétaires assemblés, et l'ordre dans lequel on sort.

Ce module ne pilote rien lui-même. Il monte les propriétaires, les branche
entre eux, et garantit la séquence de clôture. Tout le pilotage vient des
briques de VT-Tactile et de VT-Light, importées telles quelles.

**Le branchement qui compte** : ``Plateau`` et ``Asservissement`` de VT-Tactile
attendent un tracker capable de leur rendre des images. Le vrai
``ArUcoTracker.get_frame`` ouvre **sa propre caméra**, ce qui entrerait en
conflit avec le ``CameraOwner``. On lui substitue donc une méthode qui tire ses
images du propriétaire, en mode ArUco. Le code d'asservissement n'y voit que du
feu, et il n'y a plus qu'un pipeline dans tout le processus.

**La séquence de clôture n'est pas dans un ordre arbitraire** :

1. le flux brut, parce que c'est la seule pièce qui ne se rejoue pas ;
2. la main rouverte et le couple coupé — elle n'est pas rétro-entraînable,
   couper l'alimentation fige les doigts sur l'objet au lieu de le relâcher ;
3. le plateau arrêté, en vérifiant à chaque bascule ;
4. la lampe éteinte et le port rendu ;
5. la caméra fermée.
"""
from __future__ import annotations

import logging
import time

from .. import config
from .angle import AngleMeter
from .bench import BenchOwner
from .camera import CameraOwner
from .hand import HandOwner
from .resources import ResourceManager

log = logging.getLogger("vtctl.banc")


class Banc:
    """
    Le banc complet : caméra, port série, EtherCAT, et la mesure d'angle.

    Args:
        simulation: monte le banc simulé. Aucun matériel n'est touché, ni
            ``pyrealsense2`` ni ``pyserial`` importés, mais **le ``Backend``
            réel tourne au-dessus de la main simulée** : la logique de commande
            est donc bien exercée, pas contournée.
        avec_main: monter la main. La phase visuelle n'en a pas besoin, et le
            bus EtherCAT demande les droits root.
        settle: temps d'établissement de la lampe. Ne le réduire qu'en test.
    """

    def __init__(self, simulation: bool = False, avec_main: bool = True,
                 avec_camera: bool = True, avec_plateau: bool = True,
                 avec_profondeur: bool = True,
                 settle: float = config.SETTLE_LAMPE,
                 exposition_us: "int | None" = None,
                 pwm_aruco: "int | None" = None,
                 expo_aruco: "int | None" = None,
                 gain_aruco: "int | None" = None,
                 iface_index: "int | None" = None,
                 verrous: bool = True, seed: int = 0,
                 ignorer_demons: bool = False,
                 moteurs_exclus: "tuple | None" = None):
        self.simulation = simulation
        self.avec_main = avec_main
        #: Le banc se monte **par morceaux**. Les trois ressources sont
        #: indépendantes, et elles ne sont pas toujours toutes branchées : un
        #: diagnostic de la main n'a que faire de la caméra, et exiger les trois
        #: fait échouer un contrôle qui aurait parfaitement pu aboutir.
        self.avec_camera = avec_camera
        self.avec_plateau = avec_plateau
        self.mgr = ResourceManager(lock_dir=config.lock_dir() if verrous else None)

        faux_dim = faux_cam = faux_main = None
        if simulation:
            from .fake import FakeD405, FakeDimmer, FakeHand  # noqa: PLC0415

            faux_dim = FakeDimmer(settle_s=settle, seed=seed)
            faux_cam = FakeD405(faux_dim, seed=seed)
            faux_main = FakeHand(camera=faux_cam, seed=seed)

        self.bench = BenchOwner(self.mgr, fake=faux_dim, settle=settle)
        self.pwm_aruco = pwm_aruco if pwm_aruco is not None else config.PWM_ARUCO
        #: La profondeur double le trafic isochrone sur le lien USB, et c'est
        #: lui qui met QEMU en défaut : le 2026-08-23 la machine virtuelle est
        #: tombée sur ``usb_packet_complete_one``, une assertion de la file
        #: d'endpoint que les transferts isochrones volumineux déclenchent. La
        #: mesure d'angle n'a besoin que de la couleur : la couper pour un
        #: travail purement ArUco réduit d'autant l'exposition au défaut.
        self.avec_profondeur = avec_profondeur
        self.camera = CameraOwner(self.mgr, fake=faux_cam,
                                  exposition_dataset=exposition_us,
                                  exposition_aruco=expo_aruco,
                                  gain_aruco=gain_aruco,
                                  avec_profondeur=avec_profondeur)
        self.angle = AngleMeter(self.camera)
        self.hand = (HandOwner(self.mgr, fake=faux_main, iface_index=iface_index,
                               ignorer_demons=ignorer_demons,
                               moteurs_exclus=moteurs_exclus)
                     if avec_main else None)
        self.plateau = None
        self.asservissement = None
        self._ouvert = False

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def open(self, tolerance_deg: float = config.TOLERANCE_DEG) -> "Banc":
        """
        Monte le banc, dans l'ordre : port série, caméra, détecteur, main.

        Le port série d'abord parce que le firmware démarre **lampe à fond** et
        qu'il faut la baisser avant toute mesure : à PWM 255 le blanc des
        marqueurs sort de la bande de détection et l'angle devient introuvable.
        """
        if self._ouvert:
            return self
        try:
            if self.avec_plateau:
                self.bench.open()
                self.bench.lumiere(self.pwm_aruco)
            if self.avec_camera:
                self.camera.open()
                self.angle.open()
            if self.avec_camera and self.avec_plateau:
                self._brancher_plateau(tolerance_deg)
            if self.hand is not None:
                self.hand.open()
            self._ouvert = True
        except Exception:
            self.close()
            raise
        return self

    def _brancher_plateau(self, tolerance_deg: float) -> None:
        """
        Monte ``Plateau`` et ``Asservissement``, alimentés par le CameraOwner.

        Le point délicat est ici : ``calibrer_centre`` et les boucles internes
        de VT-Tactile appellent ``tracker.get_frame()``. Laissé tel quel, le
        tracker ouvrirait un second pipeline RealSense — le conflit que tout ce
        dépôt existe pour supprimer. On remplace donc la méthode sur l'instance.
        """
        from vt_tactile.plateau import Asservissement, Plateau  # noqa: PLC0415

        cam = self.camera

        def get_frame():
            couleur, _prof, _t = cam.grab_aruco()
            return couleur

        def gris(couleur: bool = False):
            image = get_frame()
            if couleur:
                return image
            import cv2  # noqa: PLC0415

            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # Substitution sur l'instance, pas sur la classe : les autres outils du
        # dépôt continuent d'ouvrir leur caméra comme avant.
        self.angle.tracker.get_frame = get_frame

        self.plateau = Plateau(self.bench.dim, self.angle.tracker, gris,
                               config.CONFIG_TELECOMMANDE)
        self.asservissement = Asservissement(self.plateau,
                                             tolerance_deg=tolerance_deg)

    def close(self) -> None:
        """Clôture ordonnée. Chaque étape est protégée : aucune n'empêche la suivante."""
        for etape, fn in (
            ("main", self._fermer_main),
            ("plateau", self._arreter_plateau),
            ("serie", self.bench.close),
            ("camera", self.camera.close),
        ):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.warning("clôture « %s » en échec", etape, exc_info=True)
        self.angle.close()
        self.mgr.release_all()
        self._ouvert = False

    def _fermer_main(self) -> None:
        if self.hand is not None:
            self.hand.close()

    def _arreter_plateau(self) -> None:
        """
        Immobilise le plateau **en vérifiant à chaque coup**.

        ``START_PAUSE`` étant une bascule, on ne la renvoie que tant que la
        mesure montre encore du mouvement. En envoyer une « pour être sûr »
        relance le plateau — c'est ce qui a fait tourner le banc pendant une
        heure de mesures sans que personne ne le sache.
        """
        if self.plateau is None or not self.camera.ouverte:
            return
        self.plateau.arreter()

    def __enter__(self) -> "Banc":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def ouvert(self) -> bool:
        return self._ouvert

    # ── Opérations ────────────────────────────────────────────────────────────

    def arreter_plateau(self, essais: int = 5) -> bool:
        """
        Immobilise le plateau, en deux temps.

        **1. Si nous l'avons lancé**, une bascule part sans discussion : c'est
        une connaissance positive, et elle ne dépend pas de l'éclairage. Sur le
        banc réel, s'en remettre à la mesure a laissé le plateau tourner —
        ArUco était aveugle et répondait « immobile ».

        **2. Ensuite seulement**, on vérifie par la mesure, et on renvoie une
        bascule *tant que* le mouvement est confirmé. Jamais « pour être sûr » :
        une bascule de trop relance le plateau.
        """
        if self.plateau is None:
            return True
        envoyee = self.bench.arret_certain()
        arrete = bool(self.plateau.arreter(essais=essais))
        if envoyee and not arrete:
            # La mesure ne confirme rien : elle est peut-être seulement aveugle.
            # On a envoyé la bascule que nous devions ; on le dit plutôt que de
            # prétendre à une certitude qu'on n'a pas.
            log.warning("bascule d'arrêt envoyée, mais l'immobilité n'a pas pu "
                        "être confirmée par la mesure")
        return arrete or envoyee

    def aller_a(self, cible_deg: float, timeout: float = 45.0,
                essais: int = 3) -> dict:
        """
        Amène le plateau à un angle, et rend ce qui a été **mesuré**.

        L'écart à la consigne vaut 2 à 5° en régime établi ; la première
        consigne d'une série est en revanche fausse de 20 à 35°, le sens de
        rotation n'étant pas encore fixé. D'où :meth:`amorcer`.
        """
        if self.asservissement is None:
            raise RuntimeError("banc non ouvert")
        self.bench.lumiere(self.pwm_aruco)
        return self.asservissement.aller_a(cible_deg, timeout=timeout, essais=essais)

    def amorcer(self, depart: float = 30.0, timeout: float = 45.0) -> dict:
        """
        Consigne à vide, jetée — elle fixe le sens de rotation.

        La première consigne d'une série est systématiquement fausse de 20 à
        35°, parce que ``ROTATION_x`` démarre le plateau autant qu'elle choisit
        son sens. Une consigne sacrifiée en début de session suffit à s'en
        débarrasser.
        """
        r = self.aller_a(depart, timeout=timeout, essais=1)
        r["amorce"] = True
        return r

    def calibrer_centre(self, duree: float = 40.0, ecrire: bool = True) -> dict:
        """
        Réestime le centre de rotation. **À faire au début de chaque session.**

        Le centre est en pixels : il ne vaut que pour la pose caméra où il a été
        mesuré. Une caméra déplacée de quelques centimètres décale tous les
        angles, sans que rien ne le signale.
        """
        from vt_tactile.plateau import calibrer_centre  # noqa: PLC0415

        self.bench.lumiere(self.pwm_aruco)
        return calibrer_centre(self.plateau, duree=duree, ecrire=ecrire)

    # ── État ──────────────────────────────────────────────────────────────────

    def infos(self) -> dict:
        return {
            "simulation": self.simulation,
            "ouvert": self._ouvert,
            "camera": self.camera.infos(),
            "banc": self.bench.infos(),
            "aruco": self.angle.infos(),
            "main": self.hand.infos() if self.hand else {"ouverte": False},
            "ressources": self.mgr.tenues(),
        }

    def selftest(self, verbeux: bool = True) -> dict:
        """
        Le banc répond-il ? Une vérification par ressource, sans acquisition.

        Chaque contrôle est indépendant : un port série muet ne doit pas
        empêcher de savoir si la caméra marche.
        """
        r: dict = {"simulation": self.simulation, "controles": [], "ok": True}

        def controle(nom, fn):
            t0 = time.perf_counter()
            try:
                detail = fn()
                ok = True
            except Exception as e:  # noqa: BLE001
                detail, ok = f"{type(e).__name__}: {e}", False
            r["controles"].append({"nom": nom, "ok": ok, "detail": detail,
                                   "duree_s": round(time.perf_counter() - t0, 2)})
            r["ok"] = r["ok"] and ok
            if verbeux:
                print(f"  [{'OK ' if ok else 'ÉCHEC'}] {nom:26s} {detail}")

        if self.avec_plateau:
            controle("port série", lambda: f"acquittement {self.bench.dim.set_pwm(0)}")
            controle("lampe (0 → 200)", self._test_lampe)
        if self.avec_camera:
            controle("caméra couleur+profondeur", self._test_camera)
        if self.avec_camera and self.avec_plateau:
            controle("détection ArUco", self._test_aruco)
            controle("plateau : rotation puis arrêt", self._test_plateau)
        if self.hand is not None:
            controle("EtherCAT : trame capteur", self._test_main)
            controle("moteurs : un doigt bouge", self._test_moteur)
        return r

    #: Écart de luminance minimal, entre lampe éteinte et lampe à 200, pour
    #: qu'on puisse dire qu'elle éclaire. Volontairement bas : il ne s'agit pas
    #: de juger le niveau, seulement de distinguer « elle agit » de « elle
    #: n'agit pas ».
    ECART_LUMIERE_MIN = 4.0

    def _test_lampe(self) -> str:
        """
        La lampe **éclaire-t-elle** ? Mesuré à la caméra, pas relu du variateur.

        La version précédente relisait la consigne et annonçait « PWM 200 établi
        à 200 » — vrai, et sans rapport avec la question. Le 2026-08-23 elle a
        déclaré la lampe bonne alors qu'aucune lumière n'en sortait : l'ESP32
        acquitte la trame quoi qu'il arrive.

        On compare donc deux images, lampe éteinte puis à 200. Sans caméra, on
        se contente d'acquitter et on le dit.
        """
        if not self.camera.ouverte:
            self.bench.lumiere(200)
            self.bench.lumiere(self.pwm_aruco)
            return ("acquittements reçus — sans caméra, on ne peut pas vérifier "
                    "que la lampe éclaire vraiment")

        import cv2  # noqa: PLC0415

        def luminance() -> float:
            self.camera.set_mode("dataset")
            self.camera.flush(6)
            couleur, _p, _t = self.camera.grab_dataset()
            return float(cv2.cvtColor(couleur, cv2.COLOR_BGR2GRAY).mean())

        self.bench.lumiere(0)
        eteinte = luminance()
        self.bench.lumiere(200)
        allumee = luminance()
        self.bench.lumiere(self.pwm_aruco)
        ecart = allumee - eteinte

        if ecart < self.ECART_LUMIERE_MIN:
            raise RuntimeError(
                f"la lampe n'éclaire pas : luminance {eteinte:.1f} éteinte contre "
                f"{allumee:.1f} à PWM 200, soit {ecart:+.1f}. Le variateur "
                f"acquitte pourtant. Vérifier le contact — ou refermer le caisson "
                f"si la lumière ambiante noie sa contribution.")
        return (f"luminance {eteinte:.1f} → {allumee:.1f} ({ecart:+.1f}) "
                f"entre PWM 0 et 200")

    def _test_camera(self) -> str:
        couleur, prof, _t = self.camera.grab_dataset()
        if prof is None:
            if not self.avec_profondeur:
                # Coupée exprès, pour alléger le lien USB : ce n'est pas un défaut.
                return (f"{couleur.shape[1]}x{couleur.shape[0]} en couleur seule "
                        f"(profondeur désactivée)")
            raise RuntimeError("aucune image de profondeur : la caméra est-elle "
                               "sur le concentrateur ? En USB 3 la profondeur "
                               "tombe à 1,3 fps et la combinaison ne rend rien.")
        rempli = 100.0 * float((prof > 0).mean())
        return (f"{couleur.shape[1]}x{couleur.shape[0]}, profondeur "
                f"remplie à {rempli:.0f} %, échelle {self.camera.cam.depth_scale:g}")

    def _test_aruco(self) -> str:
        """
        Un angle se mesure-t-il ? Et si non, **pourquoi** ?

        La distinction que rend :meth:`Mesure.diagnostic` est celle qui fait
        gagner du temps : « aucun motif » envoie chercher du côté de
        l'éclairage, « des motifs mais aucun du plateau » envoie corriger la
        pose de la caméra. Ce sont deux pannes différentes, et elles se
        ressemblent tant qu'on ne regarde pas les identifiants décodés.
        """
        m = self.angle.mesurer(40)
        if not m.connu:
            raise RuntimeError(
                m.diagnostic() + f" (lumière PWM {self.pwm_aruco}, exposition "
                f"{self.camera.exposition_aruco()} µs)")
        avert = ("" if not m.hors_table else
                 f"  ⚠ identifiants hors table lus {sorted(m.hors_table)} — "
                 f"pose caméra à surveiller")
        return (f"angle {m.angle:.1f}° ± {m.dispersion:.1f}°, "
                f"{m.images}/40 images, carreaux {sorted(m.marqueurs)}{avert}")

    def _test_plateau(self) -> str:
        avant = self.angle.mesurer(30)
        self.bench.demarrer(horaire=True)
        try:
            time.sleep(3.0)
            tourne, d = self.angle.tourne(pause=2.0, n=20)
        finally:
            # Quoi qu'il arrive — mesure aveugle, exception, interruption — le
            # plateau que nous avons lancé est arrêté. Le laisser tourner
            # invalide silencieusement tout ce qui suit.
            arrete = self.arreter_plateau()
        apres = self.angle.mesurer(30)
        if not tourne:
            raise RuntimeError(
                f"rotation non confirmée ({d:.1f}° entre deux mesures). "
                f"Portée infrarouge, ou marqueurs indétectables — le plateau a "
                f"bien reçu l'ordre et a été arrêté dans tous les cas.")
        return (f"a tourné ({d:.1f}°), arrêt {'confirmé' if arrete else 'NON CONFIRMÉ'}, "
                f"{_deg(avant)} → {_deg(apres)}")

    def _test_moteur(self) -> str:
        """
        Un doigt part-il vraiment, et revient-il ?

        Le contrôle qui manquait : une trame capteur prouve que le bus vit, pas
        qu'un moteur exécute quoi que ce soit. Le variateur accepte les
        consignes, les relit correctement, et peut ne rien émettre sur le bus —
        c'est le défaut qui a coûté le plus cher sur ce banc.
        """
        from vt_tactile import hardware as hw  # noqa: PLC0415

        moteur = hw.WORKING_FLEXORS[-1]        # l'index : le plus franc des quatre
        depart = self.hand.positions((moteur,))[moteur]
        self.hand.aller_a({moteur: 2500}, velocity=1500,
                          max_current=hw.APPROACH_CURRENT)
        arrivee = self.hand.positions((moteur,))[moteur]
        self.hand.ouvrir()
        retour = self.hand.positions((moteur,))[moteur]
        if abs(arrivee - depart) < 300:
            raise RuntimeError(
                f"{hw.MOTOR_NAMES[moteur]} n'a pas bougé ({depart} → {arrivee}). "
                f"Courant {self.hand.courants((moteur,))[moteur]} ‰ — immobile à "
                f"courant élevé signifie obstacle, pas ordre perdu.")
        return (f"{hw.MOTOR_NAMES[moteur]} : {depart} → {arrivee} → {retour} counts, "
                f"courant max {self.hand.courants((moteur,))[moteur]} ‰")

    def _test_main(self) -> str:
        brut = self.hand.hand.latest_tactile()
        if brut is None:
            raise RuntimeError("aucune trame capteur : la main est-elle réveillée ?")
        if not any(brut[2:]):
            raise RuntimeError("trame capteur à charge utile nulle — la main "
                               "n'a pas reçu set_enable PUIS home_motors")
        n = self.hand.zero(1.5)
        if n < 5:
            raise RuntimeError(f"seulement {n} trame(s) capteur en 1,5 s — la "
                               f"main émet à peine, le réveil est incomplet")
        pos = self.hand.positions()
        return f"ligne de base sur {n} trames, positions {pos}"


def _deg(m) -> str:
    return "—" if not m.connu else f"{m.angle:.1f}°"
