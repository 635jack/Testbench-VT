#!/usr/bin/env python3
"""
config.py — où vivent les dépôts voisins, et les constantes du banc.

VT-Control n'embarque aucun pilote : il importe ceux qui existent. Ce module
est le seul endroit qui sait où ils sont, pour qu'un déplacement de dépôt se
corrige ici et nulle part ailleurs.

**Rien n'est recopié depuis VT-Light.** Les réglages photométriques sont lus
dans ``VT-Light/results/light_profile.json`` par ``LightProfile`` : le README
de VT-Light prévient qu'une copie divergerait, et elle divergerait.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Racine du méta-dépôt : le dossier qui contient VT-Control, VT-Tactile, …
REPO = Path(__file__).resolve().parents[2]

VT_TACTILE = REPO / "VT-Tactile"
VT_LIGHT = REPO / "VT-Light"
TURNTABLE = REPO / "Control_Turtable_IR"

CONFIG_TELECOMMANDE = TURNTABLE / "config_telecommande.json"
CONFIG_ARUCO = TURNTABLE / "aruco_config.json"
LIGHT_PROFILE = VT_LIGHT / "results" / "light_profile.json"


def install_paths() -> None:
    """
    Rend les dépôts voisins importables.

    Appelé une fois au démarrage. ``VT-Tactile`` est inséré en tête pour que
    ``tools.web`` — d'où vient le ``Backend``, seul chemin de commande moteur
    reproductible — se résolve depuis ce dépôt et non depuis un homonyme.
    """
    for p in (VT_TACTILE, VT_LIGHT, TURNTABLE):
        s = str(p)
        if p.is_dir() and s not in sys.path:
            sys.path.insert(0, s)


# ── Constantes du banc ────────────────────────────────────────────────────────
#
# Celles qui ne vivent nulle part ailleurs. Tout ce qui est déjà mesuré et
# publié dans un autre dépôt est lu là-bas : niveaux de lumière et réglages
# caméra dans le profil VT-Light, angles des marqueurs et centre du plateau
# dans aruco_config.json, bornes moteur dans vt_tactile.hardware.

#: Établissement de la lampe après un changement de consigne, en secondes.
#: Mesuré : un saut vers PWM 255 met plus de 2 s à se stabiliser, et les 0,9 s
#: employées au début ont produit des tableaux entiers de valeurs fausses.
SETTLE_LAMPE = 3.5

#: Réglages sous lesquels les marqueurs se décodent le mieux dans la pose
#: caméra courante. À luminance de marqueur égale l'exposition la plus courte
#: gagne nettement : 96 % d'images utiles et 4,45° d'écart-type à 600 µs,
#: contre 25 % et 11,8° à 4000 µs. Le gain vient de la netteté du motif.
PWM_ARUCO = 120
EXPO_ARUCO = 600

#: Gain du capteur pour la détection. ``None`` = celui du profil (16, le
#: minimum, donc le bruit de lecture le plus bas). À relever seulement si
#: l'éclairage du banc est défaillant : le gain amplifie le bruit autant que le
#: signal, et un marqueur bruité se décode plus mal, pas mieux.
GAIN_ARUCO = None

#: Lumière sous laquelle on juge l'immobilité du plateau et on prend les
#: images du jeu de données. Le variateur sature au-delà de 200 : de 200 à 255
#: l'éclairement ne gagne que 0,06 diaphragme.
NIVEAU_CAPTURE = "haut"

#: Écart d'angle, en degrés, au-delà duquel deux mesures successives signent
#: une rotation réelle.
SEUIL_ROTATION_DEG = 3.0

#: Tolérance de positionnement par défaut. L'asservissement tient 2 à 5° en
#: régime établi ; viser moins, c'est enchaîner les reprises pour rien.
TOLERANCE_DEG = 5.0

#: Pose de départ du pivot du pouce : à mi-course entre le pouce replié (0) et
#: son ouverture maximale atteignable (~6080 mesurés). L'opérateur ajuste
#: ensuite ; cette valeur ne sert qu'à ne pas partir d'une pose absurde.
PIVOT_DEPART = 3000

#: Au-delà, un moteur immobile ne l'est pas faute d'ordre : il pousse contre
#: quelque chose. Relancer la commande dans ce cas, c'est forcer sur un doigt
#: coincé — l'annulaire a tiré 1059 ‰ le 2026-08-19 avant de passer en alarme.
SEUIL_BLOQUE = 500

#: Port par défaut de l'interface web. Volontairement différent du 8080 de
#: ``dh116-web`` : les deux doivent pouvoir coexister sur la même machine,
#: même si un seul des deux peut tenir le bus.
PORT_WEB = 8090


@dataclass
class Reglages:
    """
    Ce que l'opérateur choisit pour une session.

    Les valeurs par défaut viennent du banc et des mesures ; celles qui sont à
    ``None`` sont résolues au démarrage depuis le profil VT-Light.
    """

    objet: str = "objet"

    #: Ce qui identifie l'objet **au-delà de son nom**. Un jeu de données de
    #: prédiction de forme se relit contre une géométrie de référence : sans
    #: savoir quel modèle imprimé et à quelle échelle, une session ne se compare
    #: ni à une autre ni à un maillage. Le nom seul ne suffit pas — « cube_gris »
    #: désigne trois objets différents dans le dépôt d'impression.
    objet_reference: str = ""        # ex. « cube_100 » du dépôt visuotactile
    objet_echelle: str = ""          # 100 %, 75 %, 60 %
    objet_materiau: str = ""         # pla_gris, pla_marbre, pla_translucide…

    #: Où l'objet est posé sur le plateau. Un objet décalé de l'axe de rotation
    #: ne présente pas la même face au même angle d'une session à l'autre, et
    #: rien dans les images ne permet de le rattraper après coup.
    objet_pose: str = ""             # « centré », « décalé 20 mm vers la caméra »…

    angles: list = field(default_factory=lambda: [0.0, 60.0, 120.0, 180.0, 240.0, 300.0])

    #: Le critère « pouce stable » est-il exigé pour cette session ? Quand il
    #: ne l'est pas, les captures portent ``statut: non_requis`` — ce n'est pas
    #: un contournement, c'est un protocole différent, et il est écrit comme tel.
    pouce_requis: bool = True
    seuil_pouce: float = 0.06
    epsilon_pouce: float = 0.04
    duree_pouce: float = 1.0

    tolerance_deg: float = TOLERANCE_DEG
    timeout_angle: float = 60.0
    niveau: str = NIVEAU_CAPTURE

    #: ``None`` = celle du profil VT-Light (2200 µs aujourd'hui). La mesure du
    #: 2026-08-20 conclut à 5000 µs, mais elle n'a jamais été portée dans le
    #: profil : on ne la force pas ici, sans quoi les deux divergeraient.
    exposition_us: "int | None" = None

    pivot_depart: int = PIVOT_DEPART
    #: Plafond de couple à la fermeture. 400 ‰ ne permet jamais de serrer :
    #: les doigts s'arrêtent sur leur propre plafond entre 3200 et 4200 counts
    #: sur 8500, sans jamais toucher l'objet.
    max_current: int = 800
    velocity: int = 500
    timeout_fermeture: float = 80.0

    #: Plafond **absolu** de durée pendant laquelle la main reste serrée, en
    #: secondes, du début de la fermeture au relâchement — photos de la saisie
    #: comprises. Ce n'est pas une cible mais une sécurité : au-delà, la main
    #: s'ouvre quoi que fasse le protocole.
    #:
    #: Une main qui n'est pas rétro-entraînable et qui pousse contre un objet
    #: pousse jusqu'à ce qu'on lui dise d'arrêter. ``timeout_fermeture`` ne
    #: suffit pas : il borne la recherche du contact, pas le temps passé serré
    #: ensuite, et une exception entre la fermeture et l'ouverture laisserait la
    #: main fermée indéfiniment.
    #:
    #: Le vrai levier pour serrer moins longtemps reste ``timeout_fermeture`` :
    #: si la main met 80 s à trouver l'objet, c'est le réglage qu'il faut
    #: revoir, pas ce plafond.
    duree_serrage_max: float = 30.0

    #: Une image toutes les N itérations de la boucle de fermeture. C'est le
    #: seul moment où l'on voit les doigts **en train** de rencontrer l'objet ;
    #: le reste du temps on n'a que l'avant et l'après. 0 les désactive.
    images_fermeture: int = 3

    #: Facteur counts → radians de la main. ``None`` reprend le dernier
    #: étalonnage du journal (``vtctl.store.etalonnage``) et, à défaut,
    #: l'hypothèse par défaut — 10000 counts pour π/2 — auquel cas chaque pose
    #: calculée porte la mention « non étalonné ».
    #: ``vtctl cinematique --etalonner`` le mesure en confrontant la trame
    #: brute aux degrés du SDK, et l'inscrit au journal.
    #:
    #: Mesuré le 2026-08-26 sur la main montée la veille : **7161.972**,
    #: dispersion nulle sur 16 points — soit 80° de course réelle pour les
    #: 10000 counts, et non les 90° de l'URDF.
    counts_par_radian: "float | None" = None
    zero_secondes: float = 2.0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["angles"] = [round(float(a), 2) for a in self.angles]
        return d


def lock_dir() -> Path:
    """
    Où poser les verrous inter-processus.

    ``/var/lock`` sur le banc, un dossier temporaire ailleurs : les tests
    tournent sur un Mac où ce chemin n'existe pas, et un verrou qu'on ne peut
    pas poser ferait échouer la suite pour une mauvaise raison.
    """
    for candidat in (Path("/run/lock"), Path("/var/lock")):
        if candidat.is_dir() and os.access(candidat, os.W_OK):
            return candidat
    import tempfile  # noqa: PLC0415

    d = Path(tempfile.gettempdir()) / "vtctl-locks"
    d.mkdir(parents=True, exist_ok=True)
    return d
