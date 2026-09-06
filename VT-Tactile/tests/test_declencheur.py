#!/usr/bin/env python3
"""Le déclencheur de saisie, vérifié sans matériel."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from vt_tactile.declencheur import ContactStable  # noqa: E402


def nourrir(det, valeurs, pas=0.05, t0=0.0):
    """Injecte une suite de pressions à cadence fixe, rend le dernier état."""
    etat = None
    for i, v in enumerate(valeurs):
        etat = det.ajouter(t0 + i * pas, v)
    return etat


def test_appui_franc_et_immobile_declenche():
    det = ContactStable(seuil=0.06, epsilon=0.04, duree=1.0)
    etat = nourrir(det, [0.20] * 40)          # 2 s d'appui parfaitement stable
    assert etat.pret
    assert etat.raison == "contact stable"


def test_fenetre_incomplete_ne_declenche_pas():
    """Même un appui parfait doit durer : sinon un frôlement suffirait."""
    det = ContactStable(duree=1.0)
    etat = nourrir(det, [0.20] * 10)          # 0,45 s seulement
    assert not etat.pret
    assert "fenêtre" in etat.raison


def test_appui_trop_faible_ne_declenche_pas():
    det = ContactStable(seuil=0.06, duree=1.0)
    etat = nourrir(det, [0.02] * 40)
    assert not etat.pret
    assert "trop faible" in etat.raison


def test_appui_qui_retombe_ne_declenche_pas():
    """C'est le minimum qui compte, pas la moyenne : un contact qui se rompt
    au milieu de la fenêtre n'est pas un contact stable."""
    det = ContactStable(seuil=0.06, epsilon=0.40, duree=1.0)
    # 25 points à 0,05 s : la fenêtre d'une seconde couvre les 20 derniers,
    # donc elle contient encore la coupure.
    etat = nourrir(det, [0.30] * 10 + [0.00] * 5 + [0.30] * 10)
    assert not etat.pret
    assert "trop faible" in etat.raison


def test_contact_repris_et_tenu_finit_par_declencher():
    """Une coupure ne condamne pas la prise : dès qu'elle sort de la fenêtre
    et que l'appui a tenu une seconde pleine, le déclenchement est légitime."""
    det = ContactStable(seuil=0.06, epsilon=0.40, duree=1.0)
    etat = nourrir(det, [0.30] * 10 + [0.00] * 5 + [0.30] * 30)
    assert etat.pret


def test_pouce_en_mouvement_ne_declenche_pas():
    """Pendant l'approche la pression monte : l'amplitude doit le refuser."""
    det = ContactStable(seuil=0.06, epsilon=0.04, duree=1.0)
    etat = nourrir(det, [0.10 + 0.02 * i for i in range(40)])
    assert not etat.pret
    assert "mouvement" in etat.raison


def test_declenche_une_fois_le_mouvement_arrete():
    """Approche puis immobilisation : c'est le cas nominal de la manipulation."""
    det = ContactStable(seuil=0.06, epsilon=0.04, duree=1.0)
    montee = [0.02 * i for i in range(20)]     # approche, 0 -> 0,38
    etat = nourrir(det, montee)
    assert not etat.pret
    # le pouce se pose et ne bouge plus
    etat = nourrir(det, [0.38] * 30, t0=20 * 0.05)
    assert etat.pret


def test_reinitialiser_vide_la_fenetre():
    det = ContactStable(duree=1.0)
    assert nourrir(det, [0.20] * 40).pret
    det.reinitialiser()
    etat = nourrir(det, [0.20] * 5, t0=10.0)
    assert not etat.pret


def test_duree_nulle_refusee():
    with pytest.raises(ValueError):
        ContactStable(duree=0.0)


def test_amplitude_et_minimum_rapportes():
    det = ContactStable(seuil=0.06, epsilon=0.50, duree=1.0)
    etat = nourrir(det, [0.10, 0.30] * 20)
    assert etat.minimum == pytest.approx(0.10)
    assert etat.amplitude == pytest.approx(0.20)
