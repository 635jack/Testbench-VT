#!/usr/bin/env python3
"""
L'orchestrateur, de bout en bout — sur le banc simulé, sans matériel.

Ce sont les tests les plus lents de la suite (une session complète prend une
poignée de secondes en simulation rapide), mais ce sont les seuls qui vérifient
que les couches tiennent **ensemble** : machine à états, propriétaires de
ressources, écriture, et les points où l'opérateur décide.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from vtctl import config
from vtctl.hw.banc import Banc
from vtctl.protocol import states as S
from vtctl.protocol.runner import PHASE_TACTILE, PHASE_VISUELLE, Runner

#: Chaque test de ce fichier fait tourner une session complète : positionnement
#: du plateau, réveil de la main, fermeture, écriture. Quelques minutes en tout.
pytestmark = pytest.mark.lent


@pytest.fixture
def banc():
    b = Banc(simulation=True, avec_main=True, settle=0.0, verrous=False, seed=3)
    b.open()
    # Le plateau est déjà en place : on ne teste pas ici l'asservissement, qui a
    # ses propres tests, mais l'enchaînement du protocole.
    b.asservissement.tolerance = 180.0
    yield b
    b.close()


def _reglages(**kw):
    d = dict(objet="essai", angles=[0.0], velocity=6000, timeout_fermeture=12.0,
             zero_secondes=0.3, timeout_angle=25.0)
    d.update(kw)
    return config.Reglages(**d)


def test_phase_visuelle_ecrit_tout_ce_quil_faut(banc, tmp_path):
    r = Runner(banc, _reglages(), tmp_path, phases=(PHASE_VISUELLE,))
    session = r.demarrer()
    assert r.attendre(timeout=180), "la phase visuelle n'a pas fini"

    racine = session.root
    assert (racine / "manifest.json").exists()
    assert (racine / "session.json").exists()
    assert (racine / "journal.jsonl").exists()
    angle = json.loads((racine / "visuel/angle_00/angle.json").read_text())
    assert "mesure_deg" in angle and "images_exploitables" in angle
    capture = json.loads((racine / "visuel/angle_00/capture.json").read_text())
    # La main n'intervient pas : le critère n'est pas contourné, il n'est pas requis.
    assert capture["pouce"]["statut"] == S.POUCE_NON_REQUIS
    assert capture["pouce"]["requis"] is False
    assert len(list((racine / "visuel/angle_00").glob("*_color.png"))) >= 1


def test_deroule_tactile_avec_contournement(banc, tmp_path):
    """Le cas courant du banc : le pouce n'atteint pas l'objet, on contourne."""
    r = Runner(banc, _reglages(), tmp_path, phases=(PHASE_TACTILE,))
    session = r.demarrer()
    _repondre(r, issue=S.POUCE_CONTOURNE, motif="le pouce n'atteint pas la face avant")
    assert r.attendre(timeout=240), f"bloqué en {r.machine.etat}"

    pouce = json.loads((session.root / "tactile/angle_00/essai_00/pouce.json").read_text())
    assert pouce["statut"] == S.POUCE_CONTOURNE
    assert pouce["motif"] == "le pouce n'atteint pas la face avant"
    assert pouce["requis"] is True
    assert pouce["decide_par"] == "operateur"

    capture = json.loads((session.root / "tactile/angle_00/essai_00/capture.json").read_text())
    assert capture["validation"] == "valide"
    # Les trames brutes des deux types, sans filtrage, écrites **par angle**.
    # Le bloc unique de session a disparu avec le vidage par angle : il butait
    # sur le plafond de 400 000 trames au bout de onze minutes.
    flux = sorted(session.root.glob("tactile/angle_*/essai_*/brut/*_data.npy"))
    assert flux, "aucun flux brut écrit à côté de la capture"
    assert flux[0].stat().st_size > 10_000, "le flux de l'angle est vide"


def test_contournement_sans_motif_refuse_par_le_runner(banc, tmp_path):
    """La garde de la machine et l'API disent la même chose."""
    r = Runner(banc, _reglages(), tmp_path, phases=(PHASE_TACTILE,))
    r.demarrer()
    try:
        with pytest.raises(ValueError, match="motif"):
            r.armer(S.POUCE_CONTOURNE, "")
        with pytest.raises(ValueError, match="motif"):
            r.armer(S.POUCE_CONTOURNE, "   ")
        with pytest.raises(ValueError):
            r.armer("peut_etre")
    finally:
        r.arreter("fin du test")
        r.attendre(timeout=120)


def test_sauter_un_angle_ne_tue_pas_la_session(banc, tmp_path):
    """Un angle perdu n'en perd pas d'autres."""
    r = Runner(banc, _reglages(angles=[0.0, 180.0]), tmp_path,
               phases=(PHASE_TACTILE,))
    session = r.demarrer()
    _repondre(r, issue="saut", n_angles=2)
    assert r.attendre(timeout=360), f"bloqué en {r.machine.etat}"

    prog = r.snapshot()["progression"]
    assert len(prog) == 2, f"les deux angles doivent être traités : {prog}"
    assert all(x["validation"] == "invalide" for x in prog)
    # Les deux dossiers existent, et disent pourquoi.
    for i in (0, 1):
        c = json.loads((session.root
                        / f"tactile/angle_{i:02d}/essai_00/capture.json").read_text())
        assert "sauté" in c["commentaire"]


def test_pouce_non_requis_est_un_protocole_pas_un_contournement(banc, tmp_path):
    """
    Quand le critère n'est pas exigé, le statut est ``non_requis``.

    Ce n'est pas la même chose que ``contourne`` : l'un dit « on ne demande pas
    cette condition dans cette campagne », l'autre « on la demandait et on est
    passé outre, voici pourquoi ». Les confondre rendrait le jeu de données
    impossible à trier.
    """
    r = Runner(banc, _reglages(pouce_requis=False), tmp_path,
               phases=(PHASE_TACTILE,))
    session = r.demarrer()
    _repondre(r, valider_seulement=True)
    assert r.attendre(timeout=240), f"bloqué en {r.machine.etat}"

    pouce = json.loads((session.root / "tactile/angle_00/essai_00/pouce.json").read_text())
    assert pouce["statut"] == S.POUCE_NON_REQUIS
    assert pouce["requis"] is False
    assert pouce["motif"]                      # dit toujours pourquoi


def test_arret_demande_cloture_proprement(banc, tmp_path):
    """Un arrêt n'abandonne pas le flux brut : c'est ce qui ne se rejoue pas."""
    r = Runner(banc, _reglages(angles=[0.0, 90.0, 180.0]), tmp_path,
               phases=(PHASE_TACTILE,))
    session = r.demarrer()
    time.sleep(3.0)
    r.arreter("arrêt de test")
    assert r.attendre(timeout=180)

    assert (session.root / "manifest.json").exists()
    m = json.loads((session.root / "manifest.json").read_text())
    assert m["complet"] is False               # interrompue, et elle le dit
    assert r.machine.etat in S.TERMINAUX


def _repondre(runner, issue=S.POUCE_SATISFAIT, motif="", n_angles=1,
              valider_seulement=False):
    """
    Joue l'opérateur dans un fil : arme, puis valide, autant de fois qu'il faut.

    Les décisions viennent de l'extérieur du fil de protocole — exactement comme
    l'interface web et la console, qui appellent les mêmes méthodes.
    """
    def boucle():
        vus = 0
        fin = time.time() + 400
        while time.time() < fin and runner.en_cours:
            s = runner.snapshot()
            if s["attend_pouce"] and not valider_seulement:
                if issue == "saut":
                    runner.sauter_angle("sauté par le test")
                else:
                    runner.armer(issue, motif)
                vus += 1
            elif s["attend_validation"]:
                runner.valider("valide", "test")
            time.sleep(0.15)
            if vus >= n_angles and not runner.en_cours:
                return

    threading.Thread(target=boucle, daemon=True).start()


def test_invalider_permet_de_refaire_le_meme_angle(banc, tmp_path):
    """
    Valider ou invalider **ne fait pas avancer**.

    Une prise ratée — la main a glissé, l'objet a bougé — se refait sur le même
    angle. On ne passe au suivant que si l'opérateur le dit. Et chaque reprise
    écrit son propre essai : écraser le précédent priverait de la comparaison
    qui dit *pourquoi* le premier était raté.
    """
    r = Runner(banc, _reglages(pouce_requis=False), tmp_path, phases=(PHASE_TACTILE,))
    session = r.demarrer()

    decisions = [("invalide", "reprendre"), ("valide", "suivant")]
    vues = []

    def operateur():
        fin = time.time() + 400
        while time.time() < fin and r.en_cours:
            if r.snapshot()["attend_validation"] and decisions:
                v, suite = decisions.pop(0)
                vues.append(r.snapshot()["essai"])
                r.valider(v, "essai du test", suite)
            time.sleep(0.15)

    threading.Thread(target=operateur, daemon=True).start()
    assert r.attendre(timeout=400), f"bloqué en {r.machine.etat}"

    assert vues == [0, 1], f"le second passage doit être l'essai 1 : {vues}"
    # Les deux essais coexistent sur le disque.
    assert (session.root / "tactile/angle_00/essai_00/capture.json").exists()
    assert (session.root / "tactile/angle_00/essai_01/capture.json").exists()
    par_essai = {json.loads((session.root / f"tactile/angle_00/essai_{i:02d}/capture.json")
                            .read_text())["validation"] for i in (0, 1)}
    assert par_essai == {"invalide", "valide"}


def test_lordre_des_phases_est_celui_quon_demande(banc, tmp_path):
    """
    Le tactile passe d'abord par défaut, et le manifeste porte l'ordre suivi.

    L'inverse paraît naturel, mais il fait dépendre la partie longue et fragile
    — la saisie, avec l'opérateur dans la boucle — d'un balayage déjà consommé.
    """
    assert S.ORDRE_PHASES == (PHASE_TACTILE, PHASE_VISUELLE)

    r = Runner(banc, _reglages(pouce_requis=False), tmp_path)
    assert r.phases[0] == PHASE_TACTILE
    r_inverse = Runner(banc, _reglages(), tmp_path,
                       phases=(PHASE_VISUELLE, PHASE_TACTILE))
    assert r_inverse.phases[0] == PHASE_VISUELLE


def test_suite_inconnue_refusee(banc, tmp_path):
    r = Runner(banc, _reglages(), tmp_path, phases=(PHASE_TACTILE,))
    r.demarrer()
    try:
        with pytest.raises(ValueError, match="suite"):
            r.valider("valide", "", "peut-etre")
    finally:
        r.arreter("fin du test")
        r.attendre(timeout=120)


def test_le_flux_brut_est_vide_a_chaque_angle(banc, tmp_path):
    """
    Le tampon plafonne à 400 000 trames et **cesse d'accumuler** au-delà.

    À ~600 trames par seconde cela fait onze minutes, alors qu'une session de
    six angles en demande quarante. Deux sessions déjà enregistrées ont été
    tronquées exactement à ce plafond, et la fin de leur acquisition n'existe
    nulle part. Vider par angle supprime le plafond comme problème.
    """
    r = Runner(banc, _reglages(angles=[0.0, 120.0], pouce_requis=False),
               tmp_path, phases=(PHASE_TACTILE,))
    session = r.demarrer()

    def operateur():
        fin = time.time() + 500
        while time.time() < fin and r.en_cours:
            if r.snapshot()["attend_validation"]:
                r.valider("valide", "test", "suivant")
            time.sleep(0.15)

    threading.Thread(target=operateur, daemon=True).start()
    assert r.attendre(timeout=500), f"bloqué en {r.machine.etat}"

    # Un flux par angle, écrit à côté de sa capture — pas un bloc unique.
    par_angle = sorted(session.root.glob("tactile/angle_*/essai_*/brut/*_data.npy"))
    assert len(par_angle) == 2, f"un flux par angle attendu : {par_angle}"
    for f in par_angle:
        import numpy as _np

        d = _np.load(f, mmap_mode="r")
        assert d.shape[1] == 192
        assert d.shape[0] > 100, "un flux d'angle ne doit pas être vide"
        # Les deux types sont conservés : le tri se fait à la lecture.
        types = set(_np.unique(_np.asarray(d[:, 0])).tolist())
        assert types == {0x00, 0x40}, f"types conservés attendus, vu {types}"
