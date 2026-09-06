#!/usr/bin/env python3
"""
angle.py — l'angle du plateau, mesuré par les marqueurs et rien d'autre.

Deux principes tiennent tout ce module :

**On enregistre l'angle mesuré, jamais l'angle commandé.** La caméra donne la
vérité terrain ; la consigne n'est qu'une intention. En régime établi l'écart
est de 2 à 5°, et la première consigne d'une série est systématiquement fausse
de 20 à 35°.

**``None`` est un résultat légitime.** Les marqueurs font 10 mm et la caméra
est à 29,5° d'élévation : la détection est structurellement marginale, la
moitié des images seulement rend un marqueur, et jamais plus de deux à la fois.
Un angle inventé serait pire qu'un angle absent.

D'où la mesure par **médiane circulaire sur une centaine d'images** — la
médiane et non la moyenne, parce que les angles bouclent à 360° et qu'une
détection erronée ne doit pas tirer le résultat.

Le tracker est construit en ``simulation=True`` pour n'en garder que le
détecteur : on ne veut surtout pas qu'il ouvre la caméra, elle appartient au
``CameraOwner``. Deux réglages que ce mode écrase sont restaurés depuis la
configuration réelle, faute de quoi la détection tombe à zéro sans rien dire.
"""
from __future__ import annotations

import json
import logging
import math
import time

import numpy as np

from .. import config

log = logging.getLogger("vtctl.angle")


class Mesure:
    """Le résultat d'une mesure d'angle, tel qu'il part dans le manifeste."""

    __slots__ = ("angle", "images", "tentees", "dispersion", "marqueurs",
                 "t_debut", "t_fin", "hors_table")

    def __init__(self, angle, images, tentees, dispersion, marqueurs,
                 t_debut, t_fin, hors_table=None):
        self.angle = angle
        self.images = images
        self.tentees = tentees
        self.dispersion = dispersion
        self.marqueurs = marqueurs
        self.t_debut = t_debut
        self.t_fin = t_fin
        #: Identifiants décodés qui **n'appartiennent pas** à la table du
        #: plateau. Ce n'est pas du bruit anodin : quand la caméra est trop
        #: rasante, le détecteur trouve bien les quadrilatères mais lit le motif
        #: de travers, et rend des identifiants arbitraires. Mesuré le
        #: 2026-08-23 après un déplacement de la caméra : 12, 17, 26, 34 au lieu
        #: de 1 à 6. C'est la signature d'une pose caméra à corriger, et sans
        #: elle le symptôme se confond avec « pas de marqueur ».
        self.hors_table = set(hors_table or ())

    @property
    def connu(self) -> bool:
        return self.angle is not None

    @property
    def pose_suspecte(self) -> bool:
        """
        Des motifs sont lus, mais aucun n'est du plateau : la caméra a bougé.

        Distinguer ce cas de « aucun marqueur » est ce qui évite de chercher du
        côté de l'éclairage un problème qui est géométrique.
        """
        return bool(self.hors_table) and not self.marqueurs

    def diagnostic(self) -> str:
        """Pourquoi l'angle est introuvable, en une phrase utilisable."""
        if self.connu:
            return ""
        if self.pose_suspecte:
            return (f"des carreaux sont vus mais aucun n'est du plateau "
                    f"(identifiants décodés : {sorted(self.hors_table)}). "
                    f"Le motif se lit de travers — la caméra est trop rasante "
                    f"ou a été déplacée. Relever ou reculer la caméra, puis "
                    f"recalibrer le centre.")
        return (f"aucun motif décodé sur {self.tentees} images — éclairage, "
                f"exposition, ou champ de vue.")

    def to_dict(self) -> dict:
        return {
            "mesure_deg": None if self.angle is None else round(self.angle, 2),
            "images_exploitables": self.images,
            "images_tentees": self.tentees,
            "dispersion_deg": None if self.dispersion is None else round(self.dispersion, 2),
            "marqueurs_vus": sorted(self.marqueurs),
            "identifiants_hors_table": sorted(self.hors_table),
            "pose_suspecte": self.pose_suspecte,
            "t_debut": round(self.t_debut, 4),
            "t_fin": round(self.t_fin, 4),
        }

    def __repr__(self) -> str:
        a = "—" if self.angle is None else f"{self.angle:.1f}°"
        return f"<Mesure {a} sur {self.images}/{self.tentees} images>"


class AngleMeter:
    """
    Détecte les marqueurs et en déduit l'angle. **Sans caméra propre.**

    Args:
        camera: le ``CameraOwner``. C'est lui qui détient le pipeline ; ce
            module ne fait que lui demander des images en mode ArUco.
        config_path: ``aruco_config.json``. Y vivent les angles des carreaux et
            le centre du plateau.
    """

    def __init__(self, camera, config_path=None):
        self.cam = camera
        self.config_path = str(config_path or config.CONFIG_ARUCO)
        self.tracker = None
        self._cfg = {}

    # ── Construction ──────────────────────────────────────────────────────────

    def open(self) -> "AngleMeter":
        """
        Construit le détecteur, et répare ce que le mode simulation casse.

        ``ArUcoTracker(simulation=True)`` force ``invert_colors = False`` et ne
        lit aucune caméra. Le premier point est un piège : les carreaux du banc
        sont **imprimés en couleurs inversées**, et sans cette inversion la
        détection tombe à zéro sans le moindre message. On restaure donc la
        valeur du fichier de configuration, qui fait foi.
        """
        if self.tracker is not None:
            return self
        from aruco_tracker import ArUcoTracker  # noqa: PLC0415

        try:
            self._cfg = json.loads(open(self.config_path, encoding="utf-8").read())
        except (OSError, ValueError):
            self._cfg = {}

        tr = ArUcoTracker(config_path=self.config_path, simulation=True)
        # On ne veut que le détecteur : ni ses images de synthèse, ni sa caméra.
        tr.simulation = False
        tr.invert_colors = bool(self._cfg.get("invert_colors", True))
        self.tracker = tr

        # Le tracker tire ses intrinsèques de la caméra qu'il ouvre lui-même.
        # Ici il n'en ouvre aucune, donc il n'en a pas — et sans elles
        # ``estimate_marker_pose_3d`` rend un dictionnaire vide sans rien dire.
        # On les lui donne depuis le propriétaire de la caméra.
        if self.cam.ouverte:
            intr = self.cam.infos().get("intrinsics")
            if intr:
                tr._camera_intrinsics = dict(intr)   # noqa: SLF001

        centre = self._cfg.get("turntable_center")
        res = self._cfg.get("calibrated_resolution")
        if centre:
            tr.turntable_center = tuple(centre)
        if res and self.cam.ouverte:
            intr = self.cam.infos().get("intrinsics") or {}
            if [intr.get("width"), intr.get("height")] != list(res):
                # Le centre est en pixels : réutilisé dans un autre mode il
                # donne des angles faux sans que rien ne le signale, les modes
                # de la D405 n'ayant pas le même champ de vision.
                log.warning("centre calibré en %s, caméra en %sx%s — "
                            "les angles seront douteux", res,
                            intr.get("width"), intr.get("height"))
        log.info("détecteur ArUco prêt : centre %s, inversion %s, %d carreaux",
                 tr.turntable_center, tr.invert_colors, len(tr.marker_angles))
        return self

    def close(self) -> None:
        self.tracker = None

    def __enter__(self) -> "AngleMeter":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Mesure ────────────────────────────────────────────────────────────────

    def reinitialiser(self) -> None:
        """
        Vide le lissage interne du tracker.

        ``estimate_turntable_angle`` porte une moyenne exponentielle (α = 0,25),
        une médiane glissante de cinq valeurs et un compteur d'images sans
        détection. L'angle rendu dépend donc de l'historique — recréer le
        tracker le remettait à zéro par effet de bord, et c'était invisible.
        On le fait ici explicitement, avant chaque mesure indépendante.
        """
        if self.tracker is None:
            return
        self.tracker.smooth_angle = None
        self.tracker._angle_history.clear()      # noqa: SLF001
        self.tracker._frames_without_detection = 0  # noqa: SLF001

    def mesurer(self, n: int = 90, reinit: bool = True) -> Mesure:
        """
        L'angle du plateau, par médiane circulaire sur ``n`` images.

        Une image isolée ne suffit pas : la moitié seulement en rend un.

        Returns:
            une :class:`Mesure`. ``angle is None`` quand aucun marqueur ne s'est
            décodé — résultat légitime, pas une erreur.
        """
        self._exige_pret()
        if reinit:
            self.reinitialiser()
        t0 = time.perf_counter()
        connus = set(self.tracker.marker_angles)
        valeurs, ids_vus, hors_table = [], set(), set()
        for _ in range(max(1, n)):
            couleur, _prof, _t = self.cam.grab_aruco()
            coins, ids = self.tracker.detect_markers(couleur)
            if ids is None or len(ids) == 0:
                continue
            lus = {int(np.ravel(i)[0]) for i in ids}
            hors_table |= lus - connus
            angle, _info = self.tracker.estimate_turntable_angle(couleur, coins, ids)
            if angle is not None:
                valeurs.append(angle)
                ids_vus |= lus & connus
        t1 = time.perf_counter()

        if not valeurs:
            return Mesure(None, 0, n, None, ids_vus, t0, t1, hors_table)
        rad = np.radians(valeurs)
        med = float(np.degrees(np.arctan2(np.median(np.sin(rad)),
                                          np.median(np.cos(rad)))) % 360.0)
        ecarts = [(v - med + 180.0) % 360.0 - 180.0 for v in valeurs]
        return Mesure(med, len(valeurs), n, float(np.std(ecarts)), ids_vus,
                      t0, t1, hors_table)

    def tourne(self, pause: float = 2.5, n: int = 25) -> tuple:
        """
        Le plateau tourne-t-il ? Tranché sur deux mesures d'angle successives.

        **Pas par différence d'images** : le bruit du capteur dépend de
        l'éclairage, et sous la lumière basse que réclame la détection des
        marqueurs il dépasse le signal de rotation — un plateau lancé y passait
        pour immobile. L'angle ArUco, lui, ne dépend pas de la luminosité une
        fois le marqueur décodé.

        Returns:
            ``(en_rotation, degrés parcourus)``. Faute d'angle exploitable on
            répond « immobile » : sans mesure, mieux vaut ne pas envoyer une
            bascule au hasard — elle **relancerait** un plateau à l'arrêt.
        """
        a = self.mesurer(n)
        if not a.connu:
            return False, 0.0
        time.sleep(pause)
        b = self.mesurer(n)
        if not b.connu:
            return False, 0.0
        d = abs((b.angle - a.angle + 180.0) % 360.0 - 180.0)
        return d > config.SEUIL_ROTATION_DEG, d

    def _exige_pret(self) -> None:
        if self.tracker is None:
            raise RuntimeError("détecteur non construit : appeler open()")
        if not self.cam.ouverte:
            raise RuntimeError("caméra fermée : l'angle ne se mesure pas sans images")

    # ── Métadonnées ───────────────────────────────────────────────────────────

    def infos(self) -> dict:
        if self.tracker is None:
            return {"pret": False}
        return {
            "pret": True,
            "centre_px": list(self.tracker.turntable_center or ()),
            "inversion_couleurs": self.tracker.invert_colors,
            "angles_carreaux": {str(k): v for k, v in self.tracker.marker_angles.items()},
            "resolution_calibration": self._cfg.get("calibrated_resolution"),
            "dictionnaire": self._cfg.get("dictionary", "DICT_4X4_50"),
            "config": self.config_path,
        }


#: Côté physique du carreau du pouce, en mètres. **Mesuré au pied à coulisse le
#: 2026-08-26 : 10 mm pile.** La pose rendue par ``solvePnP`` est
#: proportionnelle à cette valeur — une taille fausse d'un facteur k donne une
#: distance fausse du même facteur, sans que rien ne le signale. Elle reste
#: écrite à côté de chaque pose : si le carreau est un jour remplacé, les
#: anciennes captures restent interprétables sans qu'on ait à deviner.
TAILLE_MARQUEUR_POUCE_M = 0.010

#: Identifiant du carreau collé sur le pouce. Il n'est **pas** dans la table du
#: plateau : il ne sert pas à mesurer l'angle, mais à savoir si le pouce est
#: dans le champ de la caméra. Sur ce banc c'est le seul qui se décode
#: systématiquement — 15 sur 15 le 2026-07-27, quand les six du plateau
#: n'en rendaient aucun.
MARQUEUR_POUCE = 7


def voir_le_pouce(camera, meter, n: int = 12) -> dict:
    """
    Le carreau du pouce est-il visible, et où ?

    Sert avant une saisie : si le pouce n'est pas dans le champ, l'image de la
    prise ne le montrera pas, et la capture perd une bonne part de son intérêt.
    C'est une **information**, pas une condition — le pouce peut parfaitement
    toucher l'objet hors du champ, et refuser la saisie pour ça serait absurde.

    La détection n'est pas tout ou rien : sur un carreau vu de biais, elle
    passe sur certaines images et pas sur d'autres. On rend donc **avec quelle
    fiabilité** il a été vu (``taux``) et **avec quelle reproductibilité** sa
    pose a été estimée (``dispersion``), pour qu'un traitement ultérieur puisse
    écarter les captures douteuses au lieu de les découvrir aberrantes.

    Returns:
        ``{"vu", "images", "tentees", "taux", "position_px", "taille_px",
        "pose_3d", "dispersion", "taille_marqueur_m"}``.
    """
    import cv2  # noqa: PLC0415

    vues, centres, tailles, poses = 0, [], [], []
    for _ in range(max(1, n)):
        couleur, _prof, _t = camera.grab_aruco()
        coins, ids = meter.tracker.detect_markers(couleur)
        if ids is None or not len(ids):
            continue
        for i, brut in enumerate(ids):
            if int(np.ravel(brut)[0]) != MARQUEUR_POUCE:
                continue
            pts = coins[i][0]
            vues += 1
            centres.append((float(np.mean(pts[:, 0])), float(np.mean(pts[:, 1]))))
            tailles.append(float(cv2.arcLength(pts, True)))
        # La pose 3D dans le repère caméra : c'est elle qui place la main dans
        # la scène. Sans elle on enregistre un toucher parfait et une image
        # parfaite qu'aucun traitement ultérieur ne saura mettre en regard.
        p3 = meter.tracker.estimate_marker_pose_3d(
            coins, ids, TAILLE_MARQUEUR_POUCE_M, target_ids={MARQUEUR_POUCE})
        if MARQUEUR_POUCE in p3:
            poses.append(p3[MARQUEUR_POUCE])

    return {
        "marqueur": MARQUEUR_POUCE,
        "vu": vues > 0,
        "images": vues,
        "tentees": n,
        "position_px": ([round(float(np.median([c[0] for c in centres])), 1),
                         round(float(np.median([c[1] for c in centres])), 1)]
                        if centres else None),
        "taille_px": round(float(np.median(tailles)), 1) if tailles else None,
        # Fraction des images où le carreau a été décodé. 1.0 = vu partout ;
        # une valeur intermédiaire dit que la détection est marginale, ce qui
        # est l'information utile pour trier le dataset après coup.
        "taux": round(vues / max(1, n), 3),
        "pose_3d": _pose_mediane(poses),
        "dispersion": _dispersion_pose(poses),
        # La taille supposée voyage avec la pose : sans elle, une distance en
        # mètres n'est pas interprétable, et une erreur de mesure du carreau
        # devient irrattrapable.
        "taille_marqueur_m": TAILLE_MARQUEUR_POUCE_M,
    }


def _pose_mediane(poses: list) -> "dict | None":
    """
    Médiane composante par composante des poses relevées.

    La médiane et non la moyenne : une seule estimation aberrante — et
    ``solvePnP`` en produit sur un carreau vu de biais — tirerait la moyenne
    sans que rien ne le montre.
    """
    if not poses:
        return None
    tvec = np.median([p["tvec"] for p in poses], axis=0)
    rvec = np.median([p["rvec"] for p in poses], axis=0)
    return {
        "tvec_m": [round(float(x), 5) for x in tvec],
        "rvec_rad": [round(float(x), 5) for x in rvec],
        "distance_m": round(float(np.linalg.norm(tvec)), 4),
        "estimations": len(poses),
    }


def _dispersion_pose(poses: list) -> "dict | None":
    """
    De combien les estimations successives se contredisent.

    C'est la mesure de précision qui manque à une pose médiane : deux carreaux
    peuvent rendre la même médiane, l'un parce que les douze estimations
    coïncident, l'autre parce qu'elles s'annulent. Seul le second est à jeter,
    et rien d'autre ne permet de les distinguer après coup.

    Écart absolu médian et non écart-type : ``solvePnP`` produit des poses
    franchement aberrantes sur un carreau vu de biais, et une seule suffirait à
    faire passer un relevé sain pour bruité.
    """
    if len(poses) < 2:
        return None
    t = np.asarray([p["tvec"] for p in poses], dtype=float).reshape(len(poses), 3)
    r = np.asarray([p["rvec"] for p in poses], dtype=float).reshape(len(poses), 3)
    eam = lambda a: np.median(np.abs(a - np.median(a, axis=0)), axis=0)  # noqa: E731
    return {
        "position_m": [round(float(x), 5) for x in eam(t)],
        "rotation_rad": [round(float(x), 5) for x in eam(r)],
        # Un scalaire pour trier sans avoir à lire les composantes.
        "position_m_max": round(float(np.max(eam(t))), 5),
        "rotation_rad_max": round(float(np.max(eam(r))), 5),
        "estimations": len(poses),
    }


def ecart(a: float, b: float) -> float:
    """Écart signé le plus court entre deux angles, dans ``]-180, 180]``."""
    return (a - b + 180.0) % 360.0 - 180.0


def median_circulaire(valeurs) -> "float | None":
    """Médiane circulaire d'une liste d'angles en degrés."""
    if not len(valeurs):
        return None
    r = np.radians(np.asarray(valeurs, dtype=float))
    return float(np.degrees(math.atan2(float(np.median(np.sin(r))),
                                       float(np.median(np.cos(r))))) % 360.0)
