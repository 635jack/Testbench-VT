"""
Le plafond de durée de serrage — la sécurité de la main.

Séparé de ``test_runner`` à dessein : ce module y serait marqué ``lent`` et ne
tournerait qu'en suite longue. Une sécurité se vérifie à chaque passage, et
ces quatre tests coûtent deux secondes.
"""

from __future__ import annotations

import time

import pytest

from vtctl import config

class _MainFactice:
    """Une main qui retient si on l'a ouverte."""

    def __init__(self):
        self.ouverte = True
        self.ouvertures = 0

    def ouvrir(self):
        self.ouvertures += 1
        return True


class _SessionFactice:
    def __init__(self):
        self.marques = []
        self.t = 0.0

    def mark(self, quoi, **kw):
        self.marques.append((quoi, kw))


def _runner_nu(plafond):
    """Un objet portant juste ce que ``_serrage_borne`` utilise."""
    from vtctl.protocol.runner import Runner

    class Nu:
        pass

    r = Nu()
    r.reglages = config.Reglages(duree_serrage_max=plafond)
    r.banc = Nu()
    r.banc.hand = _MainFactice()
    r.session = _SessionFactice()
    r.serrages_bornes = []
    r._serrage_borne = Runner._serrage_borne.__get__(r, Nu)
    return r


def test_le_serrage_est_borne_meme_si_le_protocole_ne_rend_pas_la_main():
    """
    La main s'ouvre d'office au-delà du plafond, sans rien attendre du protocole.

    C'est le cas qui compte : la DH116 n'est pas rétro-entraînable, une main
    fermée sur un objet pousse jusqu'à ce qu'on l'arrête. Ici on simule une
    étape qui ne rend jamais la main.
    """
    r = _runner_nu(0.3)
    with r._serrage_borne("tactile/angle_00") as garde:
        time.sleep(0.9)
        assert garde["declenche"], "la sécurité n'a pas agi"
    assert r.banc.hand.ouvertures == 1
    assert r.serrages_bornes and r.serrages_bornes[0]["plafond_s"] == 0.3
    assert any("serrage_borne" in m for m, _ in r.session.marques)


def test_une_saisie_normale_ne_declenche_rien():
    """Sous le plafond, la sécurité reste muette et n'ouvre pas la main."""
    r = _runner_nu(5.0)
    with r._serrage_borne("tactile/angle_00") as garde:
        time.sleep(0.2)
    assert not garde["declenche"]
    assert r.banc.hand.ouvertures == 0
    assert r.serrages_bornes == []


def test_le_minuteur_ne_survit_pas_a_la_sortie():
    """
    Le minuteur est annulé en sortant, y compris sur exception.

    Sans ça, une main rouverte par la récupération d'erreur se ferait rouvrir
    une seconde fois quelques secondes plus tard, en plein angle suivant.
    """
    r = _runner_nu(0.3)
    with pytest.raises(RuntimeError):
        with r._serrage_borne("tactile/angle_00"):
            raise RuntimeError("la fermeture a explosé")
    time.sleep(0.6)
    assert r.banc.hand.ouvertures == 0, "le minuteur a survécu à la sortie"


def test_plafond_nul_desactive_la_securite():
    """``0`` la désactive — utile pour une mise au point à la main."""
    r = _runner_nu(0)
    with r._serrage_borne("tactile/angle_00") as garde:
        time.sleep(0.2)
    assert not garde["declenche"] and r.banc.hand.ouvertures == 0
