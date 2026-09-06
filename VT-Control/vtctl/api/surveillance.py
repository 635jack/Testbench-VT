#!/usr/bin/env python3
"""
surveillance.py — ce qui ne va plus, et depuis quand.

Le banc tombe en panne par morceaux, et chaque morceau tombe **en silence** :

* la caméra bouge, et les angles deviennent faux sans qu'aucune alarme ne se
  déclenche — le centre du plateau est stocké en pixels ;
* la main cesse d'émettre, et les zones tactiles restent figées sur leur
  dernière valeur, ce qui ressemble à un objet immobile ;
* la lampe ne s'allume plus, mais le variateur acquitte quand même la trame :
  il accuse réception, il ne mesure rien ;
* le plateau ne tourne plus, et l'angle mesuré reste le même — indiscernable
  d'un positionnement parfait.

Chacune de ces pannes produit un jeu de données faux plutôt qu'un jeu de
données manquant. C'est ce qui les rend coûteuses, et c'est pourquoi elles
méritent d'être dites fort.

**Deux registres, et la distinction compte.** Les *alertes actives* sont ce qui
ne va pas **maintenant** : elles s'affichent en bandeau et disparaissent quand
la panne se résout. Le *journal* garde la trace horodatée de chaque apparition
et de chaque résolution, et il ne s'efface jamais. Une panne intermittente ne
laisse rien dans le premier registre et tout dans le second — c'est précisément
celle qu'on cherche à attraper.
"""
from __future__ import annotations

import threading
import time

#: Niveaux, du plus anodin au plus grave. ``erreur`` signifie « les captures
#: qui suivent seront fausses », pas « quelque chose a échoué ».
INFO, AVERT, ERREUR = "info", "avert", "erreur"

#: Âge maximal d'une trame tactile avant de déclarer la main muette. La main
#: émet une trentaine de trames capteur par seconde ; trois secondes de silence
#: ne sont pas un ralentissement, c'est un arrêt.
AGE_TACTILE_MAX = 3.0

#: Idem pour l'image : la caméra tient 30 fps, et l'aperçu la relit à 4 Hz.
AGE_IMAGE_MAX = 12.0

#: Écart de luminance en dessous duquel un changement de lampe est jugé sans
#: effet. Comparé à l'écart de consigne, pas dans l'absolu.
EFFET_LAMPE_MIN = 3.0

#: Délai de grâce au démarrage. La caméra met une seconde ou deux à rendre sa
#: première image, et crier pendant ce temps apprend au lecteur à ignorer le
#: bandeau — le pire service qu'on puisse lui rendre.
GRACE_DEMARRAGE = 12.0


class Alerte:
    """Une panne, avec son début et — si elle est passée — sa fin."""

    __slots__ = ("cle", "niveau", "message", "depuis", "details", "occurrences")

    def __init__(self, cle: str, niveau: str, message: str, details: dict):
        self.cle = cle
        self.niveau = niveau
        self.message = message
        self.depuis = time.time()
        self.details = details
        self.occurrences = 1

    def to_dict(self) -> dict:
        return {"cle": self.cle, "niveau": self.niveau, "message": self.message,
                "depuis": round(self.depuis, 3),
                "duree_s": round(time.time() - self.depuis, 1),
                "occurrences": self.occurrences, "details": self.details}


class Surveillance:
    """
    Le registre des pannes en cours, et le fil qui les cherche.

    Args:
        banc: le :class:`~vtctl.hw.banc.Banc`.
        noter: la fonction qui écrit au journal — c'est elle qui horodate.
        runner: fonction rendant le ``Runner`` courant, ou ``None``. Les
            contrôles s'espacent pendant une acquisition : on ne va pas
            interroger le matériel pendant qu'il travaille.
    """

    def __init__(self, banc, noter, runner=None, periode: float = 2.0):
        self.banc = banc
        self._noter = noter
        self._runner = runner or (lambda: None)
        self.periode = periode
        self._actives: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fil: "threading.Thread | None" = None
        #: Luminance de la dernière image, pour juger l'effet de la lampe.
        self._luminance: "float | None" = None
        self._demarre = time.time()

    # ── Registre ──────────────────────────────────────────────────────────────

    def lever(self, cle: str, niveau: str, message: str, **details) -> bool:
        """
        Signale une panne.

        Ne journalise **que la première fois** : une panne qui dure ne doit pas
        remplir le journal de la même ligne chaque seconde, sans quoi on ne voit
        plus les autres. Le compteur d'occurrences garde la trace de sa
        persistance.

        Returns:
            ``True`` si l'alerte est nouvelle.
        """
        with self._lock:
            deja = self._actives.get(cle)
            if deja is not None:
                deja.occurrences += 1
                deja.message = message
                deja.details = details
                return False
            self._actives[cle] = Alerte(cle, niveau, message, details)
        self._noter(message, niveau)
        return True

    def resoudre(self, cle: str, message: "str | None" = None) -> bool:
        """
        Déclare une panne terminée.

        Journalise le retour à la normale : savoir *quand* c'est reparti vaut
        autant que savoir quand ça s'est cassé, surtout pour une panne
        intermittente.
        """
        with self._lock:
            a = self._actives.pop(cle, None)
        if a is None:
            return False
        duree = time.time() - a.depuis
        self._noter(message or f"Résolu : {a.message} (après {duree:.0f} s)", INFO)
        return True

    def etat(self, cle: str, ok: bool, niveau: str, message: str, **details) -> None:
        """Lève ou résout selon ``ok``. La forme la plus commode en boucle."""
        if ok:
            self.resoudre(cle)
        else:
            self.lever(cle, niveau, message, **details)

    def actives(self) -> list:
        """Les pannes en cours, la plus grave d'abord."""
        ordre = {ERREUR: 0, AVERT: 1, INFO: 2}
        with self._lock:
            return sorted((a.to_dict() for a in self._actives.values()),
                          key=lambda a: (ordre.get(a["niveau"], 3), a["depuis"]))

    # ── Fil de surveillance ───────────────────────────────────────────────────

    def demarrer(self) -> None:
        if self._fil is not None:
            return
        self._stop.clear()
        self._fil = threading.Thread(target=self._boucle, name="surveillance",
                                     daemon=True)
        self._fil.start()

    def arreter(self) -> None:
        self._stop.set()
        self._fil = None

    def _boucle(self) -> None:
        while not self._stop.is_set():
            try:
                self._controler()
            except Exception:  # noqa: BLE001 — la surveillance ne tue jamais le serveur
                pass
            self._stop.wait(self.periode)

    def _controler(self) -> None:
        """
        Les contrôles **passifs** : ceux qui ne touchent pas au matériel.

        Rien ici ne commande quoi que ce soit. Interroger le banc pour savoir
        s'il va bien, c'est risquer de le déranger pendant qu'il travaille — et
        les pannes qui comptent se voient dans ce qu'il émet déjà.
        """
        self._controler_main()
        self._controler_camera()
        self._controler_ressources()

    def _controler_main(self) -> None:
        main = self.banc.hand
        if main is None or not main.ouverte:
            self.resoudre("main_muette")
            self.resoudre("main_alarme")
            return
        s = main.snapshot()

        age = s.get("tactile_age")
        self.etat("main_muette", age is not None and age <= AGE_TACTILE_MAX, ERREUR,
                  f"La main n'émet plus : dernière trame tactile il y a "
                  f"{age:.0f} s." if age else
                  "La main n'émet aucune trame tactile.",
                  age_s=age)

        alarmes = {m: c for m, c in (s.get("alarms") or {}).items() if c}
        self.etat("main_alarme", not alarmes, ERREUR,
                  f"Variateur en alarme : {alarmes}. Un moteur en défaut refuse "
                  f"les consignes en silence.", alarmes=alarmes)

    def _controler_camera(self) -> None:
        cam = self.banc.camera
        if not cam.ouverte:
            self.resoudre("camera_muette")
            return
        derniere = cam.derniere_image()
        if derniere is None:
            if time.time() - self._demarre > GRACE_DEMARRAGE:
                self.lever("camera_muette", ERREUR,
                           "La caméra n'a rendu aucune image depuis le démarrage.")
            return
        age = time.perf_counter() - derniere[2]
        self.etat("camera_muette", age <= AGE_IMAGE_MAX, ERREUR,
                  f"La caméra ne rend plus d'image : la dernière date de "
                  f"{age:.0f} s.", age_s=round(age, 1))
        if age <= AGE_IMAGE_MAX:
            self._memoriser_luminance(derniere[0])

    def _memoriser_luminance(self, couleur) -> None:
        try:
            import cv2  # noqa: PLC0415

            self._luminance = float(cv2.cvtColor(couleur, cv2.COLOR_BGR2GRAY).mean())
        except Exception:  # noqa: BLE001
            pass

    def _controler_ressources(self) -> None:
        tenues = set(self.banc.mgr.tenues())
        attendues = set()
        if self.banc.avec_camera:
            attendues.add("camera")
        if self.banc.avec_plateau:
            attendues.add("serie")
        if self.banc.hand is not None:
            attendues.add("ethercat")
        perdues = attendues - tenues
        self.etat("ressource_perdue", not perdues, ERREUR,
                  f"Ressource(s) libérée(s) sans qu'on l'ait demandé : "
                  f"{', '.join(sorted(perdues))}.", perdues=sorted(perdues))

    # ── Contrôles actifs, appelés par ceux qui savent ─────────────────────────
    #
    # Ces pannes ne se voient pas passivement : il faut avoir commandé quelque
    # chose pour constater qu'il ne s'est rien passé. Ce sont donc les
    # commandes elles-mêmes qui les signalent.

    def juger_lampe(self, avant: float, apres: float, pwm_avant: int,
                    pwm_apres: int) -> None:
        """
        La lampe a-t-elle **suivi** la consigne ?

        Le variateur acquitte la trame quoi qu'il arrive : son ACK dit que la
        liaison série marche, pas que la lampe éclaire. Seule une image le dit.
        """
        if abs(pwm_apres - pwm_avant) < 40:
            return                       # écart trop faible pour conclure
        bouge = abs(apres - avant) >= EFFET_LAMPE_MIN
        self.etat("lampe_inerte", bouge, AVERT,
                  f"La lampe ne suit pas : PWM {pwm_avant} → {pwm_apres} et la "
                  f"luminance ne bouge pas ({avant:.1f} → {apres:.1f}). Contact "
                  f"défectueux, ou caisson ouvert qui noie sa contribution.",
                  pwm=[pwm_avant, pwm_apres], luminance=[round(avant, 1), round(apres, 1)])

    def juger_pose(self, mesure) -> None:
        """
        La caméra a-t-elle bougé ?

        Appelé après chaque mesure d'angle. Le signe qui ne trompe pas n'est pas
        l'absence de marqueur — elle est normale, la moitié des positions sont
        aveugles — mais des motifs **lus de travers** : le détecteur trouve les
        quadrilatères et rend des identifiants qui ne sont pas ceux du plateau.
        """
        if getattr(mesure, "pose_suspecte", False):
            self.lever("camera_deplacee", ERREUR,
                       f"La caméra a bougé : {mesure.diagnostic()}",
                       identifiants=sorted(mesure.hors_table))
        elif mesure.connu:
            self.resoudre("camera_deplacee")
            if mesure.hors_table:
                self.lever("pose_degradee", AVERT,
                           f"Identifiants hors table lus {sorted(mesure.hors_table)} — "
                           f"la pose caméra se dégrade.")
            else:
                self.resoudre("pose_degradee")

    def juger_plateau(self, tourne: bool, degres: float, demande: bool) -> None:
        """
        Le plateau a-t-il bougé quand on le lui a demandé ?

        ``demande`` distingue les deux questions. Un plateau immobile alors
        qu'on ne lui a rien demandé est le cas nominal ; immobile après un ordre
        de rotation, c'est une panne — portée infrarouge, ou alimentation.
        """
        if not demande:
            return
        self.etat("plateau_immobile", tourne, ERREUR,
                  f"Le plateau n'a pas tourné après l'ordre ({degres:.1f}° entre "
                  f"deux mesures). Portée infrarouge, ou plateau hors tension.",
                  degres=round(degres, 1))

    @property
    def luminance(self) -> "float | None":
        return self._luminance
