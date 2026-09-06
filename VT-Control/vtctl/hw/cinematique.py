#!/usr/bin/env python3
"""
cinematique.py — des counts moteur aux bouts de doigts, en trois dimensions.

C'est le chaînon qui manquait. On enregistrait des positions moteur en counts
et des images ; rien ne permettait de dire **où** un contact avait eu lieu dans
la scène. Un jeu de données de fusion visuo-tactile a besoin exactement de ça.

La chaîne complète, et d'où vient chaque morceau :

1. **counts → angle articulaire.** Le variateur publie des counts ; le SDK sait
   les convertir en degrés (``get_now_angle``). Le facteur n'est écrit nulle
   part, donc :func:`etalonner` le mesure en lisant les deux à la fois.
2. **angle → toutes les articulations.** Six moteurs pour onze articulations :
   les cinq distales suivent leur proximale au rapport 1. Correspondance et
   rapports repris du publieur d'état du constructeur.
3. **articulations → poses.** Cinématique directe sur l'URDF
   ``DH116-L000-A1``, qui donne origines et axes de chaque liaison.
4. **repère main → repère caméra.** Par le carreau ArUco du pouce, dont la pose
   6D est déjà relevée à chaque capture — à condition de connaître son montage
   sur le doigt.

Ce que ce module **ne sait pas**, et qu'il dit plutôt que de le supposer en
silence : le facteur counts/radian tant qu'on ne l'a pas étalonné, et le
montage du carreau sur le pouce. Les deux se mesurent au banc ; en attendant,
les poses sont rendues dans le repère de la main, ce qui est déjà exploitable.
"""
from __future__ import annotations

import logging
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from .. import config

log = logging.getLogger("vtctl.cinematique")

#: URDF du constructeur, main gauche, **embarqué**.
#:
#: Une copie plutôt qu'un chemin vers ``RealDH116`` : VT-Control est déployé
#: seul dans la machine virtuelle, où ce dépôt n'est pas. Dépendre de lui ferait
#: manquer le modèle au pire moment — en pleine session, sous forme de poses
#: simplement absentes. La copie fige de surcroît le modèle avec le code qui
#: l'utilise : une géométrie qui change ne réécrit pas les sessions passées.
URDF_EMBARQUE = Path(__file__).resolve().parent.parent / "data" / "DH116-L000-A1.urdf"

#: Celui du dépôt voisin, s'il est là : il fait foi quand les deux existent.
URDF_DEPOT = (config.REPO / "RealDH116" / "src" / "lhandpro_description" / "urdf"
              / "DH116-L000-A1.urdf")


def urdf_par_defaut() -> Path:
    """L'URDF du dépôt s'il existe, sinon celui embarqué."""
    return URDF_DEPOT if URDF_DEPOT.exists() else URDF_EMBARQUE

#: Moteur → articulation motrice. Repris de ``lhandpro_state_publisher.py`` du
#: constructeur : c'est lui qui fait autorité, et le déduire des noms serait
#: une conjecture.
MOTEUR_VERS_ARTICULATION = {
    1: "finger12",   # pouce, flexion
    2: "finger11",   # pouce, pivot
    3: "finger51",   # auriculaire
    4: "finger41",   # annulaire
    5: "finger31",   # majeur
    6: "finger21",   # index
}

#: Articulation menée → (articulation motrice, rapport). Onze articulations
#: pour six moteurs : les cinq distales suivent leur proximale au rapport 1.
COUPLAGES = {
    "finger13": ("finger12", 1.0),
    "finger22": ("finger21", 1.0),
    "finger32": ("finger31", 1.0),
    "finger42": ("finger41", 1.0),
    "finger52": ("finger51", 1.0),
}

#: Bout de chaque doigt, pour situer les contacts. Le maillon distal porte la
#: zone tactile du bout ; sa pulpe est sur le maillon précédent.
BOUTS = {
    "thumb": "finger13_Link",
    "index": "finger22_Link",
    "middle": "finger32_Link",
    "ring": "finger42_Link",
    "little": "finger52_Link",
}

#: Pleine course du moteur, en counts. **10000, et non 8500.**
#:
#: L'en-tête du SDK est explicite : « position, plage [0 = position de départ,
#: 10000 = pleine course] ». Les 8500 de ``vt_tactile.hardware.POSITION_MAX``
#: sont un **garde-fou** délibérément placé sous la butée, pas la course elle-
#: même — confondre les deux surestimait l'angle de 18 %.
#:
#: Conséquence à garder en tête pour relire les sessions déjà faites : les
#: doigts qui « atteignaient la butée à 8501 counts » touchaient le garde-fou
#: logiciel, pas une limite mécanique.
COUNTS_PLEINE_COURSE = 10000

#: Débattement d'une articulation à pleine course, en radians. C'est la limite
#: déclarée dans l'URDF pour toutes les flexions : 1,57 rad, soit 90°.
#:
#: Le SDK, lui, ne donne pas de valeur : il dit « plage [0, MAX = angle maximal
#: atteignable par le moteur] ». L'URDF est donc la seule source chiffrée, et
#: c'est elle qu'on prend — jusqu'à ce que :func:`etalonner` mesure le rapport
#: réel, ce qui reste la seule façon d'en être sûr.
RADIANS_PLEINE_COURSE = math.pi / 2


# ── L'URDF ────────────────────────────────────────────────────────────────────

class Chaine:
    """
    L'arbre cinématique lu dans l'URDF : liaisons, origines, axes.

    Ne dépend d'aucune bibliothèque de robotique : l'URDF est du XML, et la
    cinématique directe d'un arbre de liaisons rotoïdes tient en vingt lignes.
    Ajouter une dépendance pour ça compliquerait l'installation dans la machine
    virtuelle sans rien apporter.
    """

    def __init__(self, chemin=None):
        self.chemin = Path(chemin or urdf_par_defaut())
        if not self.chemin.exists():
            raise FileNotFoundError(f"URDF introuvable : {self.chemin}")
        racine = ET.parse(self.chemin).getroot()
        self.robot = racine.get("name")
        self.liaisons: dict = {}
        for j in racine.findall("joint"):
            org = j.find("origin")
            axe = j.find("axis")
            lim = j.find("limit")
            self.liaisons[j.get("name")] = {
                "type": j.get("type"),
                "parent": j.find("parent").get("link"),
                "enfant": j.find("child").get("link"),
                "xyz": _triplet(org.get("xyz") if org is not None else None),
                "rpy": _triplet(org.get("rpy") if org is not None else None),
                "axe": _triplet(axe.get("xyz") if axe is not None else None,
                                defaut=(0.0, 0.0, 1.0)),
                "min": float(lim.get("lower")) if lim is not None else None,
                "max": float(lim.get("upper")) if lim is not None else None,
            }
        # Chaque maillon connaît la liaison qui le porte : c'est ce qui permet
        # de remonter jusqu'à la base sans reconstruire l'arbre à chaque appel.
        self.liaison_du_maillon = {d["enfant"]: n for n, d in self.liaisons.items()}
        self.mobiles = [n for n, d in self.liaisons.items() if d["type"] != "fixed"]

        # Longueur des maillons distaux, estimée depuis leur centre d'inertie.
        self._distales: dict = {}
        for lien in racine.findall("link"):
            o = lien.find("inertial/origin")
            if o is None:
                continue
            z = _triplet(o.get("xyz"))[2]
            if z > 0:
                self._distales[lien.get("name")] = round(2.0 * float(z), 5)

    def chemin_vers(self, maillon: str) -> list:
        """Les liaisons de la base jusqu'à ce maillon, dans l'ordre."""
        suite = []
        courant = maillon
        while courant in self.liaison_du_maillon:
            nom = self.liaison_du_maillon[courant]
            suite.append(nom)
            courant = self.liaisons[nom]["parent"]
        return list(reversed(suite))

    def pose(self, maillon: str, angles: dict) -> np.ndarray:
        """
        Pose 4×4 d'un maillon dans le repère de la base, pour des angles donnés.

        Args:
            angles: ``{nom_de_liaison: radians}``. Une liaison absente vaut zéro.
        """
        T = np.eye(4)
        for nom in self.chemin_vers(maillon):
            d = self.liaisons[nom]
            T = T @ _transformation(d["xyz"], d["rpy"])
            if d["type"] != "fixed":
                T = T @ _rotation_autour(d["axe"], float(angles.get(nom, 0.0)))
        return T

    def squelette(self, angles: dict) -> dict:
        """
        Les segments de chaque doigt, en repère main, pour un affichage.

        Un point par articulation, plus le bout. C'est ce qu'il faut pour
        dessiner la main et **juger à l'œil** si la cinématique correspond à ce
        que fait la vraie main — un tableau de coordonnées ne se compare pas à
        une main qui bouge, un dessin si.

        La dernière liaison de chaque doigt est fixe et sans décalage : le
        maillon distal n'a donc pas de longueur dans l'arbre. On l'estime à
        **deux fois la cote z de son centre de masse**, ce qui suppose une
        phalange à peu près homogène. C'est une estimation, pas une mesure, et
        elle n'affecte que le dessin du dernier segment — les articulations,
        elles, viennent de l'URDF.
        """
        doigts = {}
        for doigt, maillon in BOUTS.items():
            if maillon not in self.liaison_du_maillon:
                continue
            points = [[0.0, 0.0, 0.0]]
            for nom in self.chemin_vers(maillon):
                T = self.pose(self.liaisons[nom]["enfant"], angles)
                points.append([round(float(x), 5) for x in T[:3, 3]])
            # Le bout, prolongé le long de l'axe du dernier maillon.
            T = self.pose(maillon, angles)
            bout = T @ np.array([0.0, 0.0, self.longueur_distale(maillon), 1.0])
            points.append([round(float(x), 5) for x in bout[:3]])
            doigts[doigt] = points
        return doigts

    def longueur_distale(self, maillon: str) -> float:
        """
        Longueur estimée du maillon distal, depuis son centre de masse.

        Deux fois la cote z du centre d'inertie : c'est ce que donne une
        phalange homogène. Faute de mieux — l'arbre cinématique s'arrête à
        l'articulation, et les maillages ne voyagent pas avec l'URDF.
        """
        return self._distales.get(maillon, 0.030)

    def poses_des_bouts(self, angles: dict) -> dict:
        """Position et orientation de chaque bout de doigt, en repère main."""
        out = {}
        for doigt, maillon in BOUTS.items():
            if maillon not in self.liaison_du_maillon:
                continue
            T = self.pose(maillon, angles)
            out[doigt] = {
                "maillon": maillon,
                "position_m": [round(float(x), 5) for x in T[:3, 3]],
                "rotation": [[round(float(v), 5) for v in ligne] for ligne in T[:3, :3]],
            }
        return out


def _triplet(texte, defaut=(0.0, 0.0, 0.0)):
    if not texte:
        return np.array(defaut, dtype=float)
    return np.array([float(x) for x in texte.split()], dtype=float)


def _transformation(xyz, rpy) -> np.ndarray:
    """Transformation homogène d'une origine URDF : translation puis RPY fixes."""
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    R = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp,     cp * sr,                cp * cr],
    ])
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = xyz
    return T


def _rotation_autour(axe, angle: float) -> np.ndarray:
    """Rotation d'un angle autour d'un axe quelconque — formule de Rodrigues."""
    a = np.asarray(axe, dtype=float)
    n = np.linalg.norm(a)
    if n < 1e-12:
        return np.eye(4)
    a = a / n
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    R = np.eye(3) + math.sin(angle) * K + (1 - math.cos(angle)) * (K @ K)
    T = np.eye(4)
    T[:3, :3] = R
    return T


# ── counts → articulations ────────────────────────────────────────────────────

class Conversion:
    """
    Le passage des counts aux radians, et son incertitude assumée.

    Args:
        counts_par_radian: le facteur mesuré. ``None`` prend l'hypothèse par
            défaut, et ``etalonne`` reste faux — de sorte qu'une pose calculée
            sans étalonnage se sache telle et le dise dans le jeu de données.
    """

    def __init__(self, counts_par_radian: "float | None" = None):
        self.etalonne = counts_par_radian is not None
        self.counts_par_radian = float(
            counts_par_radian if counts_par_radian is not None
            else COUNTS_PLEINE_COURSE / RADIANS_PLEINE_COURSE)

    def radians(self, counts: float) -> float:
        return float(counts) / self.counts_par_radian

    def articulations(self, positions: dict) -> dict:
        """
        Tous les angles articulaires, à partir des positions moteur.

        Args:
            positions: ``{moteur: counts}``, tels que la trame brute les donne
                — **signés**. Un doigt repoussé sous son zéro a un angle négatif,
                et l'écrêter à zéro placerait le bout de doigt au mauvais endroit.

        Returns:
            ``{nom_de_liaison: radians}``, articulations menées comprises.
        """
        angles = {}
        for moteur, counts in positions.items():
            nom = MOTEUR_VERS_ARTICULATION.get(int(moteur))
            if nom:
                angles[nom] = self.radians(counts)
        for menee, (motrice, rapport) in COUPLAGES.items():
            if motrice in angles:
                angles[menee] = angles[motrice] * rapport
        return angles

    def to_dict(self) -> dict:
        return {"counts_par_radian": round(self.counts_par_radian, 3),
                "etalonne": self.etalonne,
                "hypothese": None if self.etalonne else
                f"{COUNTS_PLEINE_COURSE} counts pour {RADIANS_PLEINE_COURSE:.4f} rad"}


def etalonner(main, moteurs=(6, 5, 4, 3), pas=(1000, 2500, 4000, 5500)) -> dict:
    """
    Mesure le facteur counts/radian en confrontant la trame brute au SDK.

    Le variateur publie des counts ; le SDK sait les rendre en degrés, sans que
    le facteur soit écrit nulle part. On déplace donc quelques doigts à des
    positions connues et on lit les deux à la fois.

    Args:
        main: un :class:`~vtctl.hw.hand.HandOwner` ouvert.

    Returns:
        le facteur ajusté et les points qui l'ont produit, pour qu'on puisse
        juger de la qualité de l'ajustement au lieu de la croire.
    """
    import time  # noqa: PLC0415

    lire_angle = getattr(main.hand, "_lhp", None)
    if lire_angle is None or not hasattr(lire_angle, "get_now_angle"):
        raise RuntimeError("le SDK n'expose pas get_now_angle : étalonnage "
                           "impossible sur cette main")

    points = []
    for moteur in moteurs:
        for cible in pas:
            main.aller_a({moteur: int(cible)}, velocity=1200, max_current=400)
            time.sleep(0.4)
            counts = main.positions((moteur,))[moteur]
            degres = float(lire_angle.get_now_angle(moteur))
            if abs(degres) > 1e-6:
                points.append({"moteur": moteur, "counts": counts,
                               "degres": degres,
                               "counts_par_radian": counts / math.radians(degres)})
    main.ouvrir()

    if not points:
        raise RuntimeError("aucun point exploitable : le SDK a rendu 0° partout")
    facteurs = [p["counts_par_radian"] for p in points]
    # Médiane : un point aberrant — doigt bloqué, consigne non exécutée — ne
    # doit pas tirer le facteur.
    facteur = float(np.median(facteurs))
    return {
        "counts_par_radian": round(facteur, 3),
        "dispersion": round(float(np.std(facteurs)), 3),
        "points": points,
        "hypothese_par_defaut": round(COUNTS_PLEINE_COURSE / RADIANS_PLEINE_COURSE, 3),
    }


# ── Assemblage ────────────────────────────────────────────────────────────────

class Cinematique:
    """
    L'ensemble : URDF, conversion, et les poses des bouts de doigts.

        cin = Cinematique()
        cin.bouts({6: 4200, 5: 3800})     # counts → poses en repère main
    """

    def __init__(self, urdf=None, counts_par_radian: "float | None" = None):
        self.chaine = Chaine(urdf)
        self.conversion = Conversion(counts_par_radian)

    def bouts(self, positions: dict) -> dict:
        """
        Où sont les bouts de doigts, pour des positions moteur données.

        Le résultat porte son propre degré de confiance : sans étalonnage, les
        angles sont issus d'une hypothèse, et une pose calculée dessus ne doit
        pas être prise pour une mesure.
        """
        angles = self.conversion.articulations(positions)
        return {
            "repere": "main (base_link)",
            "conversion": self.conversion.to_dict(),
            "articulations_rad": {k: round(v, 5) for k, v in angles.items()},
            "bouts": self.chaine.poses_des_bouts(angles),
        }

    def squelette(self, positions: dict) -> dict:
        """Les segments à dessiner, pour des positions moteur données."""
        angles = self.conversion.articulations(positions)
        return {"doigts": self.chaine.squelette(angles),
                "conversion": self.conversion.to_dict()}

    def infos(self) -> dict:
        return {
            "urdf": str(self.chaine.chemin),
            "robot": self.chaine.robot,
            "articulations_mobiles": len(self.chaine.mobiles),
            "moteurs": len(MOTEUR_VERS_ARTICULATION),
            "couplages": {k: v[0] for k, v in COUPLAGES.items()},
            "conversion": self.conversion.to_dict(),
        }
