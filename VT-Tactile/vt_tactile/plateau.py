#!/usr/bin/env python3
"""
plateau.py — le plateau tournant et la lampe, qui sont le même ESP32.

Ce module existe parce que trois pièges ont coûté cher, et qu'aucun n'est
visible depuis les bibliothèques d'origine :

**Un seul port série.** Le variateur de lumière et le pont infrarouge du
plateau sont la même carte sur ``/dev/ttyACM0``. Ouvrir un second port la
réinitialise, et le firmware redémarre **lampe à fond** — le blanc des
marqueurs monte alors hors de la bande de détection et l'asservissement
devient aveugle. Le ``Dimmer`` garde donc le port, et les trames du plateau
passent par ``send_ir``.

**``START_PAUSE`` est une bascule.** Ne l'envoyer que si le plateau tourne
*réellement* : sur un plateau immobile elle le **relance**. Le contrôleur
d'origine décide à partir de la vitesse mesurée par ArUco, or celle-ci est
dérivée d'un angle vu par un seul marqueur et peut annoncer 40 °/s sur un
plateau à l'arrêt. On tranche donc à l'image, pas à l'ArUco.

**La détection ArUco est marginale et intermittente.** Marqueurs de 10 mm à
29,5° d'élévation : sur un tour complet, la moitié des images seulement rendent
un marqueur, et jamais plus de deux à la fois. Un angle ne se lit donc pas sur
une image mais sur une centaine, par médiane circulaire — et il peut
légitimement rester introuvable à certaines positions.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: Réglages sous lesquels les marqueurs se décodent le mieux dans la pose
#: caméra courante. La détection est gouvernée par la **luminance du blanc du
#: marqueur** : au-dessus d'une certaine valeur le motif noir est noyé.
#:
#: À luminance de marqueur égale, plusieurs couples lampe/exposition sont
#: possibles, et ils ne se valent pas — plus l'exposition est courte, plus la
#: mesure est fine. Mesuré à l'arrêt, sur 80 images, plateau immobile :
#:
#:     PWM 120 / 600 µs   -> 96 % d'images utiles, écart-type  4,45°
#:     PWM  60 / 1000 µs  -> 90 %,                 écart-type 10,15°
#:     PWM  28 / 2200 µs  -> 100 %,                écart-type 25,46°
#:     PWM  15 / 4000 µs  -> 25 %,                 écart-type 11,82°
#:
#: On prend donc le plus court. Le gain vient de la netteté du motif, pas du
#: flou de filé — les marqueurs se lisent plateau arrêté.
PWM_ARUCO = 120
EXPO_ARUCO = 600

#: Temps d'établissement de la lampe après un changement de consigne. Mesuré :
#: un saut vers PWM 255 met plus de 2 s à se stabiliser, et les 0,9 s employées
#: au début ont produit des tableaux entiers de valeurs fausses.
SETTLE_LAMPE = 3.5

#: Écart d'angle, en degrés, au-delà duquel deux mesures successives signent
#: une rotation réelle. La dispersion d'une mesure d'angle est de l'ordre du
#: degré quand un seul marqueur est vu ; 3° la couvre largement, et le plateau
#: parcourt plus de 30° en 2,5 s à sa vitesse minimale.
SEUIL_ROTATION_DEG = 3.0


class IRviaDimmer:
    """
    Interface d'un ``TurntableController``, mais les trames sortent par le Dimmer.

    Expose uniquement ce dont ``TurntablePositionController`` se sert. Les
    méthodes de cycle de vie sont neutres : le port appartient au ``Dimmer``,
    c'est lui qui l'ouvre et le referme.
    """

    def __init__(self, dimmer, config_path: str | Path):
        self.dim = dimmer
        self.commands_map = json.loads(Path(config_path).read_text())
        self.simulation = False
        self.ser = dimmer.ser

    def _envoyer(self, nom: str):
        code = self.commands_map[nom]
        return self.dim.send_ir(code.replace("RCV", "SEND"))

    # cycle de vie — sans effet, le port n'est pas à nous
    def start_listener(self) -> None: ...
    def stop_listener(self) -> None: ...
    def disconnect(self) -> None: ...

    def rotation_droite(self):  return self._envoyer("COMMANDE_ROTATION_DROITE")
    def rotation_gauche(self):  return self._envoyer("COMMANDE_ROTATION_GAUCHE")
    def start_pause(self):      return self._envoyer("COMMANDE_START_PAUSE")
    def vitesse_moins(self):    return self._envoyer("COMMANDE_VITESSE_MOINS")
    def vitesse_plus(self):     return self._envoyer("COMMANDE_VITESSE_PLUS")
    def angle_45(self):         return self._envoyer("COMMANDE_ANGLE_45")
    def angle_90(self):         return self._envoyer("COMMANDE_ANGLE_90")
    def angle_180(self):        return self._envoyer("COMMANDE_ANGLE_180")


class Plateau:
    """
    Le plateau et sa lampe, vus comme une seule ressource.

    Args:
        dimmer: ``vt_light.dimmer.Dimmer`` **déjà connecté**.
        tracker: ``ArUcoTracker`` construit en ``simulation=True`` — on ne veut
            que son détecteur et sa configuration, pas qu'il ouvre la caméra :
            elle est déjà prise par le jeu de données.
        grab_gris: fonction rendant l'image courante en niveaux de gris.
    """

    def __init__(self, dimmer, tracker, grab_gris, config_telecommande):
        self.dim = dimmer
        self.tracker = tracker
        self.gris = grab_gris
        self.ir = IRviaDimmer(dimmer, config_telecommande)

    # ── Lumière ───────────────────────────────────────────────────────────────

    def lumiere(self, pwm: int, settle: float = SETTLE_LAMPE) -> int:
        ack = self.dim.set_pwm(pwm)
        time.sleep(settle)
        return ack

    # ── Rotation ──────────────────────────────────────────────────────────────

    def tourne(self, pause: float = 2.5) -> tuple[bool, float]:
        """
        Le plateau tourne-t-il ? Tranché sur deux mesures d'angle ArUco.

        On ne mesure plus le mouvement par différence d'images : le bruit du
        capteur dépend de l'éclairage, et sous la lumière basse que réclame la
        détection des marqueurs il dépasse le signal de rotation — un plateau
        lancé y passait pour immobile. L'angle ArUco, lui, ne dépend pas de la
        luminosité une fois le marqueur décodé.

        Returns:
            ``(en_rotation, degrés parcourus)``. Faute d'angle exploitable on
            répond « immobile » : sans mesure, mieux vaut ne pas envoyer une
            bascule au hasard — elle **relancerait** un plateau à l'arrêt.
        """
        a, _na = self.mesure_angle(25)
        if a is None:
            return False, 0.0
        time.sleep(pause)
        b, _nb = self.mesure_angle(25)
        if b is None:
            return False, 0.0
        d = abs((b - a + 180.0) % 360.0 - 180.0)
        return d > SEUIL_ROTATION_DEG, d

    def arreter(self, essais: int = 5) -> bool:
        """
        Immobilise le plateau, en vérifiant à chaque coup.

        ``START_PAUSE`` étant une bascule, on la renvoie **uniquement** tant
        que l'image montre encore du mouvement. Envoyer deux bascules de suite
        « pour être sûr » relance le plateau — c'est exactement ce que fait le
        contrôleur d'origine, et ce qui a fait tourner le banc sans qu'on le
        sache pendant plusieurs mesures.
        """
        for essai in range(essais):
            en_mvt, d = self.tourne()
            if not en_mvt:
                log.info("Plateau immobile (%.1f° entre deux mesures)", d)
                return True
            log.info("Plateau en rotation (%.1f°) — bascule %d", d, essai + 1)
            self.ir.start_pause()
            time.sleep(3.0)
        log.error("Plateau toujours en rotation après %d bascules", essais)
        return False

    # ── Angle ─────────────────────────────────────────────────────────────────

    def mesure_angle(self, n: int = 120) -> tuple[float | None, int]:
        """
        Angle du plateau, par médiane circulaire sur ``n`` images.

        Une image isolée ne suffit pas : la moitié seulement en rend un.
        La médiane circulaire — et non la moyenne — parce que les angles
        bouclent à 360° et qu'une détection erronée ne doit pas tirer le
        résultat.

        Returns:
            ``(angle_deg | None, nombre d'images exploitables)``. ``None`` est
            un résultat légitime : à certaines positions aucun marqueur ne se
            décode, et un angle inventé serait pire qu'un angle absent.
        """
        import cv2  # noqa: PLC0415

        valeurs = []
        for _ in range(n):
            couleur = self.gris(couleur=True)
            coins, ids = self.tracker.detect_markers(couleur)
            if ids is None:
                continue
            angle, _info = self.tracker.estimate_turntable_angle(couleur, coins, ids)
            if angle is not None:
                valeurs.append(angle)
        if not valeurs:
            return None, 0
        rad = np.radians(valeurs)
        med = float(np.degrees(np.arctan2(np.median(np.sin(rad)),
                                          np.median(np.cos(rad)))) % 360.0)
        return med, len(valeurs)

    def dispersion(self, angle: float, valeurs) -> float:
        ecarts = [(v - angle + 180.0) % 360.0 - 180.0 for v in valeurs]
        return float(np.std(ecarts))


#: Fenêtre de lissage de la vitesse angulaire, en secondes. La vitesse est la
#: dérivée d'un angle vu par un ou deux marqueurs : calculée entre deux images
#: consécutives elle annonce couramment 112 à 167 °/s sur un plateau qui
#: plafonne à 46. Une régression sur une demi-seconde divise ce bruit par
#: environ quatre — c'est le remède que recommande ``FONCTIONNEMENT.md``.
FENETRE_VITESSE = 0.5

#: Vitesse au-delà de laquelle une mesure est refusée comme aberrante. Le
#: plateau ne dépasse pas ~46 °/s ; au-delà c'est du bruit, et le laisser
#: passer ferait freiner des dizaines de degrés trop tôt.
VITESSE_MAX_PLAUSIBLE = 60.0

#: Roue libre après l'ordre d'arrêt, mesurée à la caméra : ~15° parcourus à
#: 13 °/s. Ce n'est pas la latence d'émission infrarouge mais l'inertie du
#: plateau.
LATENCE_FREINAGE = 0.45


def _ecart(a: float, b: float) -> float:
    """Écart signé de ``b`` vers ``a``, ramené dans [-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


class Asservissement:
    """
    Amène le plateau à un angle, en respectant la sémantique de la télécommande.

    **L'ordre des commandes n'est pas indifférent.** ``ROTATION_DROITE`` et
    ``ROTATION_GAUCHE`` ne font pas que choisir un sens : elles **démarrent**
    le plateau (``running = True`` dans le modèle d'état). ``VITESSE_MOINS`` le
    démarre aussi, mais dans le **dernier sens utilisé**. Réduire la vitesse
    avant d'avoir fixé le sens fait donc toujours partir le plateau dans la
    direction de la consigne précédente — une fois sur deux la mauvaise. On
    émet donc le sens **d'abord**, la vitesse ensuite.

    Le freinage est anticipé de ``vitesse x latence``, la vitesse étant lissée
    par régression : calculée entre deux images consécutives, elle annonce
    couramment plus du triple du maximum physique, et l'anticipation devient
    alors plus grande que le déplacement à faire — d'où un déficit
    systématique, toujours court et jamais long.

    Aucune micro-impulsion : elle déplace le plateau d'au moins 15°, très
    au-dessus de la tolérance visée. On reprend une consigne entière.
    """

    def __init__(self, plateau: "Plateau", tolerance_deg: float = 5.0,
                 roue_libre_deg: float = 15.0):
        self.pl = plateau
        self.tolerance = tolerance_deg
        #: Degrés parcourus après l'ordre d'arrêt. C'est le **plancher** de
        #: l'anticipation de freinage : freiner moins tôt que la roue libre
        #: garantit le dépassement, quelle que soit la vitesse mesurée.
        #: ``mesurer_roue_libre`` le remplace par la valeur du jour.
        self.roue_libre = roue_libre_deg
        self._sens = None          # dernier sens émis, pour ne pas le répéter

    def mesurer_roue_libre(self, essais: int = 2) -> float:
        """
        Combien de degrés le plateau parcourt-il après l'ordre d'arrêt ?

        Cette distance est irréductible — c'est l'inertie du plateau, pas une
        latence de transmission — et elle fixe la précision atteignable. La
        mesurer en début de session vaut mieux que de la supposer : elle dépend
        de la vitesse, donc du réglage de la télécommande, qui n'est pas
        observable autrement.
        """
        mesures = []
        for _ in range(essais):
            self._lancer(horaire=True)
            time.sleep(3.0)                      # laisser la vitesse s'établir
            avant = self._angle(12)
            if avant is None:
                self.pl.arreter()
                continue
            self.pl.ir.start_pause()
            time.sleep(3.0)
            apres = self.pl.mesure_angle(40)[0]
            self.pl.arreter()
            if apres is not None:
                mesures.append(abs(_ecart(apres, avant)))
        if mesures:
            self.roue_libre = float(np.median(mesures))
            log.info("Roue libre mesurée : %.1f° (%s)", self.roue_libre,
                     ", ".join(f"{m:.1f}" for m in mesures))
        return self.roue_libre

    def _vitesse(self, historique) -> float:
        if len(historique) < 3:
            return 0.0
        t = np.array([x for x, _ in historique])
        if t[-1] - t[0] < 0.1:
            return 0.0
        a = np.unwrap(np.radians([y for _, y in historique]))
        v = abs(float(np.degrees(np.polyfit(t, a, 1)[0])))
        return 0.0 if v > VITESSE_MAX_PLAUSIBLE else v

    def _lancer(self, horaire: bool) -> None:
        """Sens puis vitesse — jamais l'inverse."""
        (self.pl.ir.rotation_droite if horaire else self.pl.ir.rotation_gauche)()
        self._sens = horaire
        time.sleep(0.3)
        for _ in range(3):
            self.pl.ir.vitesse_moins()
            time.sleep(0.12)

    def _angle(self, n: int = 8) -> float | None:
        vals = []
        for _ in range(n):
            couleur = self.pl.gris(couleur=True)
            coins, ids = self.pl.tracker.detect_markers(couleur)
            if ids is None:
                continue
            a, _i = self.pl.tracker.estimate_turntable_angle(couleur, coins, ids)
            if a is not None:
                vals.append(a)
        if not vals:
            return None
        r = np.radians(vals)
        return float(np.degrees(np.arctan2(np.median(np.sin(r)),
                                           np.median(np.cos(r)))) % 360.0)

    def une_passe(self, cible: float, timeout: float) -> dict:
        """
        Un aller vers ``cible``, freinage anticipé compris.

        **Le plateau démarre même sans angle connu.** À l'arrêt, une position
        sur deux ne décode aucun marqueur : exiger l'angle avant de bouger
        bloque définitivement. Or la rotation fait défiler les marqueurs et
        ramène la détection à une image sur deux. On part donc dans un sens
        arbitraire, on lit l'angle dès qu'il apparaît, et on inverse si l'on
        allait du mauvais côté — ce qui coûte quelques degrés, pas une passe.
        """
        depart = self.pl.mesure_angle(30)[0]
        horaire = True if depart is None else (_ecart(cible, depart) < 0)
        self._lancer(horaire=horaire)

        historique: list[tuple[float, float]] = []
        t0 = time.time()
        freine = False
        inverse = False
        vu = depart is not None
        while time.time() - t0 < timeout:
            a = self._angle(4)
            if a is None:
                continue
            maintenant = time.time()
            historique.append((maintenant, a))
            while historique and historique[0][0] < maintenant - FENETRE_VITESSE:
                historique.pop(0)
            erreur = _ecart(cible, a)

            # Premier angle d'une passe partie à l'aveugle : c'est seulement
            # maintenant qu'on sait si le sens choisi était le bon.
            if not vu:
                vu = True
                if depart is None:
                    depart = a
                voulu = erreur < 0
                if voulu != horaire and not inverse:
                    inverse = True
                    horaire = voulu
                    self._lancer(horaire=horaire)
                    historique.clear()
                    continue

            v = self._vitesse(historique)
            # Le plancher est la roue libre : freiner plus tard qu'elle
            # garantit le dépassement, quelle que soit la vitesse mesurée.
            avance = max(self.roue_libre, v * LATENCE_FREINAGE)
            if abs(erreur) <= avance:
                self.pl.ir.start_pause()
                freine = True
                break
        time.sleep(1.5)
        self.pl.arreter()
        arrivee = self.pl.mesure_angle(60)[0]
        return {"issue": "ok" if freine else "timeout",
                "depart": None if depart is None else round(depart, 2),
                "sens_inverse": inverse,
                "arrivee": None if arrivee is None else round(arrivee, 2),
                "ecart": None if arrivee is None else round(_ecart(cible, arrivee), 2)}

    def aller_a(self, cible: float, timeout: float = 45.0,
                essais: int = 3) -> dict:
        """
        Consigne complète, reprise tant que l'écart dépasse la tolérance.

        Une reprise est une **nouvelle consigne**, pas une impulsion : le
        plateau repart, ralentit et refreine normalement. C'est le seul moyen
        de corriger un écart de plus de quelques degrés sur ce matériel.
        """
        passes = []
        for essai in range(essais):
            r = self.une_passe(cible, timeout)
            passes.append(r)
            log.info("consigne %.1f° — passe %d : %s -> %s (écart %s)",
                     cible, essai + 1, r["depart"], r["arrivee"], r["ecart"])
            if r["ecart"] is not None and abs(r["ecart"]) <= self.tolerance:
                break
        derniere = passes[-1]
        return {"commande_deg": round(cible % 360.0, 2),
                "mesure_deg": derniere["arrivee"],
                "ecart_deg": derniere["ecart"],
                "passes": len(passes),
                "detail": passes}


#: Durée d'observation pour la calibration du centre, en secondes. À ~13 °/s,
#: 40 s couvrent largement un tour : l'ajustement d'ellipse a besoin d'un arc
#: long, faute de quoi l'ellipse glisse le long de la tangente sans que le
#: résidu n'augmente.
DUREE_CALIBRATION = 40.0


def calibrer_centre(plateau, duree: float = DUREE_CALIBRATION,
                    ecrire: bool = True) -> dict:
    """
    Réestime le centre de rotation du plateau, et l'écrit dans la config.

    **À refaire au début de chaque session.** Le centre est stocké en pixels :
    il ne vaut que pour la résolution *et* la pose caméra où il a été estimé.
    Une caméra déplacée de quelques centimètres le décale de dizaines de
    pixels, et tous les angles deviennent faux — sans que rien ne le signale.

    La méthode est celle de ``calibrate_center.py`` : pendant que le plateau
    tourne, chaque marqueur décrit un cercle autour de l'axe, qui se projette
    en **ellipse** puisque la caméra regarde de biais. On accumule les
    trajectoires et on y ajuste une ellipse par marqueur. Contrairement à une
    estimation sur une seule image, celle-ci ne dépend ni du nombre de
    marqueurs vus simultanément ni de leur disposition supposée — un seul
    marqueur suffit, ce qui est décisif ici où l'on en voit rarement deux.

    Returns:
        le résultat, sérialisable, pour le manifeste de session.
    """
    import sys  # noqa: PLC0415
    from collections import defaultdict  # noqa: PLC0415

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Control_Turtable_IR"))
    from calibrate_center import fit_trajectoire  # noqa: PLC0415

    tracker = plateau.tracker
    ancien = tracker.turntable_center
    log.info("Calibration du centre — rotation de %.0f s", duree)

    plateau.ir.rotation_droite()
    time.sleep(0.3)
    for _ in range(3):
        plateau.ir.vitesse_moins()
        time.sleep(0.12)

    trajectoires: dict[int, list] = defaultdict(list)
    t0 = time.time()
    while time.time() - t0 < duree:
        image = tracker.get_frame()
        coins, ids = tracker.detect_markers(image)
        if ids is None:
            continue
        for k, m in enumerate(ids.ravel()):
            pts = coins[k][0]
            trajectoires[int(m)].append([float(pts[:, 0].mean()),
                                         float(pts[:, 1].mean())])
    plateau.ir.start_pause()
    time.sleep(1.5)

    estimations, detail = [], []
    for m_id, pts in sorted(trajectoires.items()):
        arr = np.asarray(pts, dtype=np.float64)
        etendue = float(np.hypot(*(arr.max(axis=0) - arr.min(axis=0))))
        res = fit_trajectoire(arr)
        if res is None:
            detail.append({"marqueur": m_id, "points": len(arr), "retenu": False,
                           "motif": "trop peu de points"})
            continue
        cx, cy, a, b, residu = res
        # Un arc trop court rend l'ajustement instable : l'ellipse peut glisser
        # le long de la tangente sans que le résidu n'augmente.
        fiable = etendue > 0.6 * a and residu < 0.06 * a
        elevation = float(np.degrees(np.arcsin(min(b / a, 1.0))))
        detail.append({"marqueur": m_id, "points": len(arr),
                       "arc_px": round(etendue, 1), "residu_px": round(residu, 2),
                       "elevation_deg": round(elevation, 1),
                       "centre": [round(cx, 1), round(cy, 1)], "retenu": fiable})
        log.info("  marqueur %d : %d points, arc %.0f px, résidu %.2f px -> "
                 "(%.1f, %.1f) %s", m_id, len(arr), etendue, residu, cx, cy,
                 "retenu" if fiable else "écarté")
        if fiable:
            estimations.append((cx, cy, len(arr)))

    if not estimations:
        log.error("Aucune trajectoire exploitable : centre inchangé")
        return {"reussi": False, "centre": list(ancien) if ancien else None,
                "detail": detail}

    poids = np.array([e[2] for e in estimations], dtype=np.float64)
    cx = float(np.average([e[0] for e in estimations], weights=poids))
    cy = float(np.average([e[1] for e in estimations], weights=poids))
    dispersion = [round(float(np.std([e[0] for e in estimations])), 2),
                  round(float(np.std([e[1] for e in estimations])), 2)]
    ecart = (None if not ancien
             else round(float(np.hypot(cx - ancien[0], cy - ancien[1])), 1))
    log.info("Centre : (%.1f, %.1f), dispersion %s px, écart à l'ancien %s px",
             cx, cy, dispersion, ecart)

    if ecrire:
        tracker.turntable_center = (cx, cy)
        tracker.save_config()
    return {"reussi": True, "centre": [round(cx, 2), round(cy, 2)],
            "ancien_centre": list(ancien) if ancien else None,
            "ecart_px": ecart, "dispersion_px": dispersion,
            "marqueurs_retenus": len(estimations), "detail": detail}
