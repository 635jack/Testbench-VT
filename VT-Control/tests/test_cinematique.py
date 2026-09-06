#!/usr/bin/env python3
"""
La cinématique de la main : des counts aux bouts de doigts.

C'est le chaînon qui manquait pour la fusion. Ces tests vérifient qu'il repose
sur les bonnes sources — l'URDF du constructeur et la correspondance du service
— et non sur des conjectures tirées des noms d'articulations.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from vtctl.hw.cinematique import (
    BOUTS, COUPLAGES, MOTEUR_VERS_ARTICULATION, Chaine, Cinematique, Conversion,
)


def test_larbre_correspond_a_la_main_reelle():
    """
    Onze articulations mobiles pour six moteurs : 6 motrices, 5 menées.

    C'est la définition de cette main — 11 degrés de liberté dont 6 actifs — et
    si l'URDF cessait d'y correspondre, tout ce qui en découle serait faux.
    """
    c = Chaine()
    assert c.robot == "DH116-L000-A1"
    assert len(c.mobiles) == 11
    assert len(MOTEUR_VERS_ARTICULATION) == 6
    assert len(COUPLAGES) == 5
    assert len(MOTEUR_VERS_ARTICULATION) + len(COUPLAGES) == len(c.mobiles)

    # Toutes les articulations nommées existent bien dans l'URDF.
    for nom in MOTEUR_VERS_ARTICULATION.values():
        assert nom in c.liaisons, f"{nom} absente de l'URDF"
    for menee, (motrice, _r) in COUPLAGES.items():
        assert menee in c.liaisons and motrice in c.liaisons


def test_les_bouts_de_doigts_existent():
    c = Chaine()
    for doigt, maillon in BOUTS.items():
        assert maillon in c.liaison_du_maillon, f"{doigt} : {maillon} introuvable"


def test_la_main_ouverte_a_des_doigts_devant():
    """
    Repère de vraisemblance : une main ouverte tend ses doigts loin de la base.

    Sans ce genre de contrôle, une erreur de convention sur les origines URDF
    passerait inaperçue — les nombres resteraient plausibles, mais la main
    serait repliée à l'envers.
    """
    cin = Cinematique()
    r = cin.bouts({m: 0 for m in MOTEUR_VERS_ARTICULATION})
    for doigt in ("index", "middle", "ring", "little"):
        p = r["bouts"][doigt]["position_m"]
        assert 0.10 < p[2] < 0.20, f"{doigt} à z={p[2]:.3f} m, hors du plausible"
    # Les quatre longs doigts sont côte à côte, étalés sur la largeur de la main.
    y = [r["bouts"][d]["position_m"][1] for d in ("index", "middle", "ring", "little")]
    assert y == sorted(y), "les doigts doivent se suivre dans l'ordre"
    assert 0.05 < (max(y) - min(y)) < 0.10, "largeur de main hors du plausible"


def test_fermer_rapproche_les_bouts_de_la_paume():
    cin = Cinematique()
    ouvert = cin.bouts({m: 0 for m in MOTEUR_VERS_ARTICULATION})["bouts"]
    ferme = cin.bouts({3: 5000, 4: 5000, 5: 5000, 6: 5000})["bouts"]
    for doigt in ("index", "middle", "ring", "little"):
        a = np.array(ouvert[doigt]["position_m"])
        b = np.array(ferme[doigt]["position_m"])
        assert b[2] < a[2], f"{doigt} devrait se rapprocher de la paume"
        deplacement = float(np.linalg.norm(b - a))
        assert 0.01 < deplacement < 0.08, \
            f"{doigt} se déplace de {1000*deplacement:.0f} mm, hors du plausible"


def test_le_couplage_suit_la_proximale():
    """Cinq articulations menées au rapport 1 : c'est ce que dit le service."""
    conv = Conversion()
    angles = conv.articulations({6: 4000})
    assert angles["finger21"] == pytest.approx(angles["finger22"])
    assert "finger32" not in angles, "seul l'index était commandé"


def test_une_position_negative_donne_un_angle_negatif():
    """
    Un doigt repoussé sous son zéro a un angle négatif.

    L'écrêter à zéro placerait le bout de doigt au mauvais endroit — et les
    positions sous zéro sont fréquentes : les quatre fléchisseurs y descendent
    à −1900 counts quand l'objet les repousse.
    """
    conv = Conversion()
    a = conv.articulations({6: -1900})
    assert a["finger21"] < 0
    assert a["finger22"] < 0, "l'articulation menée doit suivre le signe"


def test_une_pose_non_etalonnee_le_dit():
    """
    Le facteur counts/radian est une **hypothèse** tant qu'il n'est pas mesuré.

    Une pose calculée dessus ne doit pas être prise pour une mesure : elle est
    fausse d'un facteur constant si l'hypothèse l'est, donc rattrapable — mais
    seulement si le jeu de données dit qu'elle n'était pas étalonnée.
    """
    suppose = Cinematique().bouts({6: 4000})
    assert suppose["conversion"]["etalonne"] is False
    assert suppose["conversion"]["hypothese"]

    mesure = Cinematique(counts_par_radian=5000.0).bouts({6: 4000})
    assert mesure["conversion"]["etalonne"] is True
    assert mesure["conversion"]["hypothese"] is None


def test_le_facteur_change_les_angles_pas_la_geometrie():
    """
    Un facteur faux décale les angles, pas les longueurs de segments.

    C'est ce qui rend l'erreur rattrapable : l'URDF reste juste, seule la
    conversion est à refaire.
    """
    a = Cinematique(counts_par_radian=5411.0).bouts({6: 4000})
    b = Cinematique(counts_par_radian=2705.5).bouts({6: 4000})
    # Tolérance absolue : les angles sont arrondis à 1e-5 avant d'être écrits,
    # et doubler un arrondi ne redonne pas l'arrondi du double.
    assert b["articulations_rad"]["finger21"] == pytest.approx(
        2 * a["articulations_rad"]["finger21"], abs=2e-5)
    assert a["bouts"]["index"]["position_m"] != b["bouts"]["index"]["position_m"]


def test_la_pleine_course_est_celle_du_sdk_pas_le_garde_fou():
    """
    10000 counts = pleine course, et non 8500.

    L'en-tête du SDK le dit : « plage [0 = position de départ, 10000 = pleine
    course] ». Les 8500 de ``POSITION_MAX`` sont un garde-fou placé sous la
    butée. Les confondre surestimait l'angle de 18 % — et donc plaçait tous les
    bouts de doigts au mauvais endroit.
    """
    from vtctl.hw.cinematique import COUNTS_PLEINE_COURSE
    from vt_tactile import hardware as hw

    assert COUNTS_PLEINE_COURSE == 10000
    assert hw.POSITION_MAX < COUNTS_PLEINE_COURSE, \
        "le garde-fou doit rester sous la pleine course"

    conv = Conversion()
    assert conv.radians(10000) == pytest.approx(math.pi / 2, rel=1e-6)
    # Le garde-fou logiciel ne laisse donc pas atteindre les 90°.
    assert math.degrees(conv.radians(hw.POSITION_MAX)) == pytest.approx(76.5, abs=0.5)

    c = Chaine()
    assert c.liaisons["finger21"]["max"] == pytest.approx(1.57, abs=0.01)


def test_le_squelette_suit_la_fermeture():
    """
    Le dessin doit bouger comme la main, sinon il ne sert à rien à comparer.

    C'est tout l'objet du visualiseur : mettre côte à côte ce que le modèle dit
    et ce que la main fait. Un dessin qui ne suivrait pas donnerait une fausse
    confiance plutôt qu'une vérification.
    """
    cin = Cinematique()
    ouvert = cin.squelette({m: 0 for m in MOTEUR_VERS_ARTICULATION})["doigts"]
    ferme = cin.squelette({6: 6000})["doigts"]

    for doigt, chaine in ouvert.items():
        assert len(chaine) >= 3, f"{doigt} : trop peu de points pour un dessin"
        assert chaine[0] == [0.0, 0.0, 0.0], "le premier point est la base"

    a = np.array(ouvert["index"][-1])
    b = np.array(ferme["index"][-1])
    assert b[2] < a[2], "l'index doit s'enrouler vers la paume"
    assert b[0] > a[0], "et se rabattre vers l'avant"
    # Les doigts non commandés ne bougent pas : un couplage mal branché les
    # ferait tous suivre le premier.
    assert ferme["little"][-1] == ouvert["little"][-1]


def test_les_segments_ont_des_longueurs_constantes():
    """
    Une articulation ne change pas la longueur d'un os.

    Si une longueur variait avec l'angle, c'est que la chaîne est mal composée
    — et rien dans le dessin ne le montrerait, la main paraissant seulement
    un peu difforme.
    """
    cin = Cinematique()
    longueurs = []
    for counts in (0, 2000, 5000, 8000):
        sq = cin.squelette({6: counts})["doigts"]["index"]
        longueurs.append([float(np.linalg.norm(np.array(sq[i + 1]) - np.array(sq[i])))
                          for i in range(len(sq) - 1)])
    reference = longueurs[0]
    for autre in longueurs[1:]:
        for a, b in zip(reference, autre):
            # Les points sont écrits à 1e-5 près : la tolérance suit l'arrondi,
            # pas la précision du calcul.
            assert a == pytest.approx(b, abs=3e-5)


def test_les_longueurs_distales_viennent_de_lurdf():
    """
    Estimées depuis le centre d'inertie, pas inventées.

    L'arbre s'arrête à l'articulation : le maillon distal n'a pas de longueur.
    Deux fois la cote de son centre de masse est ce que donne une phalange
    homogène — une estimation, mais tirée du fichier plutôt que du pouce.
    """
    c = Chaine()
    for maillon in BOUTS.values():
        L = c.longueur_distale(maillon)
        assert 0.01 < L < 0.08, f"{maillon} : longueur distale {L} m hors du plausible"
