#!/usr/bin/env python3
"""
Ce que la machine à états garantit — sans matériel.

Ces tests verrouillent le protocole lui-même : si quelqu'un ajoute un raccourci
dans la table, l'un d'eux casse. Le plus important est celui du contournement
sans motif : c'est la seule chose que ce protocole existe pour empêcher.
"""
from __future__ import annotations

import pytest

from vtctl.protocol import states as S
from vtctl.protocol.machine import Machine, TransitionRefusee, rejouer


# ── La table ──────────────────────────────────────────────────────────────────

def test_table_coherente():
    """Aucun état inatteignable, aucun état sans sortie."""
    S.valider_table()          # lève si la table est incohérente


def test_tous_les_etats_ont_une_description():
    assert set(S.DESCRIPTION) == set(S.ETATS)


def test_deroule_visuel_complet():
    m = Machine()
    assert m.etat == S.REPOS
    m.declencher("ouvrir")
    for _ in range(3):                       # trois angles d'affilée
        m.declencher("positionner")
        m.declencher("angle_arrete")
        m.declencher("capturer_visuel")
        m.declencher("angle_termine")
        assert m.etat == S.PRETE
    m.declencher("cloturer")
    assert m.etat == S.CLOTURE and m.termine


def test_deroule_tactile_complet():
    m = Machine()
    m.declencher("ouvrir")
    m.declencher("positionner")
    m.declencher("angle_arrete")
    m.declencher("preparer_main")
    m.declencher("main_prete")
    m.declencher("armer")
    m.declencher("fermer")
    m.declencher("saisie_etablie")
    m.declencher("capturer_tactile")
    m.declencher("relacher")
    m.declencher("a_valider")
    m.declencher("angle_termine")
    assert m.etat == S.PRETE


# ── Les refus ─────────────────────────────────────────────────────────────────

def test_transition_illegale_leve():
    """Une transition absente de la table n'est ni ignorée ni corrigée."""
    m = Machine()
    with pytest.raises(TransitionRefusee) as e:
        m.declencher("fermer")
    assert m.etat == S.REPOS
    assert "REPOS" in str(e.value)


def test_le_refus_nomme_ce_qui_est_permis():
    """« transition interdite » sans plus n'aide personne à 2 h du matin."""
    m = Machine()
    with pytest.raises(TransitionRefusee) as e:
        m.declencher("capturer_tactile")
    assert "ouvrir" in str(e.value)


def test_on_ne_ferme_pas_sans_passer_par_le_pouce():
    """Le chemin MAIN_AU_DEPART → FERMETURE n'existe pas."""
    m = Machine()
    m.declencher("ouvrir")
    m.declencher("positionner")
    m.declencher("angle_arrete")
    m.declencher("preparer_main")
    with pytest.raises(TransitionRefusee):
        m.declencher("fermer")


def test_desarmer_ramene_en_attente():
    """Tout déplacement du pouce doit pouvoir annuler une fermeture imminente."""
    m = Machine()
    for e in ("ouvrir", "positionner", "angle_arrete", "preparer_main",
              "main_prete", "armer"):
        m.declencher(e)
    assert m.etat == S.ARME
    m.declencher("desarmer")
    assert m.etat == S.POUCE_EN_ATTENTE


# ── Les trois issues du critère du pouce ──────────────────────────────────────

@pytest.mark.parametrize("issue", S.ISSUES_ARMANTES)
def test_les_trois_issues_menent_a_arme(issue):
    """Satisfait, contourné et non requis sont **tous trois** légitimes."""
    decision = {"issue": issue, "motif": "un motif" if issue == S.POUCE_CONTOURNE else ""}
    m = _machine_avec_garde_pouce(decision)
    for e in ("ouvrir", "positionner", "angle_arrete", "preparer_main", "main_prete"):
        m.declencher(e)
    m.declencher("armer")
    assert m.etat == S.ARME


def test_contournement_sans_motif_refuse():
    """
    Le cœur du protocole : un contournement silencieux est refusé.

    Le pivot du pouce n'amène pas le pouce en opposition sur tous les objets —
    contourner est donc courant, et légitime. Ce qui ne l'est pas, c'est de ne
    pas dire pourquoi.
    """
    m = _machine_avec_garde_pouce({"issue": S.POUCE_CONTOURNE, "motif": ""})
    for e in ("ouvrir", "positionner", "angle_arrete", "preparer_main", "main_prete"):
        m.declencher(e)
    with pytest.raises(TransitionRefusee) as e:
        m.declencher("armer")
    assert "motif" in str(e.value)
    assert m.etat == S.POUCE_EN_ATTENTE


def test_echec_du_critere_narme_pas():
    """``echec`` — exigé, non atteint, non contourné — ne laisse pas fermer."""
    m = _machine_avec_garde_pouce({"issue": S.POUCE_ECHEC})
    for e in ("ouvrir", "positionner", "angle_arrete", "preparer_main", "main_prete"):
        m.declencher(e)
    with pytest.raises(TransitionRefusee):
        m.declencher("armer")


def _machine_avec_garde_pouce(decision: dict) -> Machine:
    m = Machine()

    def garde():
        issue = decision.get("issue")
        if issue not in S.ISSUES_ARMANTES:
            return False, f"critère non tranché ({issue})"
        if issue == S.POUCE_CONTOURNE and not decision.get("motif"):
            return False, "un contournement doit porter un motif"
        return True, ""

    m.garde(S.POUCE_EN_ATTENTE, "armer", garde)
    return m


# ── Erreur et récupération ────────────────────────────────────────────────────

def test_echec_depuis_nimporte_quel_etat():
    """La sortie vers ERREUR est universelle, sans être déclarée quinze fois."""
    for depart in S.ETATS:
        if depart in S.TERMINAUX or depart in (S.ERREUR, S.RECUPERATION):
            continue
        m = Machine(etat=depart)
        assert m.echouer("panne") == S.ERREUR
        assert m.derniere_erreur["cause"] == "panne"


def test_echouer_ne_leve_jamais():
    """C'est la voie de la gestion d'erreur : elle ne doit pas échouer à son tour."""
    m = Machine(etat=S.CLOTURE)
    assert m.echouer("après clôture") == S.CLOTURE


def test_un_angle_perdu_nen_perd_pas_dautres():
    """Après récupération, la session repart sur PRETE : les autres angles suivent."""
    m = Machine()
    m.declencher("ouvrir")
    m.declencher("positionner")
    m.echouer("doigt bloqué")
    assert m.etat == S.ERREUR
    m.declencher("recuperer")
    m.declencher("reprendre")
    assert m.etat == S.PRETE
    m.declencher("positionner")             # l'angle suivant part normalement
    assert m.etat == S.POSITIONNEMENT


# ── Gardes ────────────────────────────────────────────────────────────────────

def test_garde_qui_leve_interdit_sans_casser():
    m = Machine()

    def garde():
        raise RuntimeError("capteur muet")

    m.garde(S.REPOS, "ouvrir", garde)
    ok, raison = m.verifier("ouvrir")
    assert not ok and "capteur muet" in raison
    with pytest.raises(TransitionRefusee):
        m.declencher("ouvrir")


def test_garde_sur_transition_inexistante_refusee():
    """Une garde mal placée est une faute de frappe : elle doit se voir tôt."""
    m = Machine()
    with pytest.raises(ValueError):
        m.garde(S.REPOS, "fermer", lambda: (True, ""))


def test_evenements_possibles_tient_compte_des_gardes():
    """L'interface n'affiche que les boutons qui marcheront."""
    m = Machine()
    assert "ouvrir" in m.evenements_possibles()
    m.garde(S.REPOS, "ouvrir", lambda: (False, "banc froid"))
    assert m.evenements_possibles() == []


# ── Journal et reprise ────────────────────────────────────────────────────────

def test_tout_est_journalise_y_compris_les_refus(tmp_path):
    from vtctl.store.journal import Journal

    j = Journal(tmp_path / "j.jsonl", t0=0.0)
    m = Machine(journal=j)
    m.declencher("ouvrir")
    with pytest.raises(TransitionRefusee):
        m.declencher("fermer")
    m.echouer("panne simulée")
    j.close()

    kinds = [e["kind"] for e in Journal.lire(tmp_path / "j.jsonl")]
    assert kinds.count("transition") == 2          # ouvrir, puis echec
    assert "transition_refusee" in kinds


def test_rejouer_retrouve_l_etat():
    """
    Savoir où une session tuée s'est arrêtée.

    Décisif : si elle est morte en FERMETURE, la main est restée fermée sur
    l'objet — elle n'est pas rétro-entraînable — et il faut intervenir avant
    de relancer quoi que ce soit.
    """
    evts = [
        {"kind": "transition", "depuis": S.REPOS, "evenement": "ouvrir", "vers": S.PRETE},
        {"kind": "transition", "depuis": S.PRETE, "evenement": "positionner",
         "vers": S.POSITIONNEMENT},
        {"kind": "image", "etape": "x"},
        {"kind": "transition", "depuis": S.POSITIONNEMENT, "evenement": "angle_arrete",
         "vers": S.STABILISATION},
    ]
    assert rejouer(evts).etat == S.STABILISATION


def test_forcer_est_journalise(tmp_path):
    """Le seul chemin hors table doit laisser une trace explicite."""
    from vtctl.store.journal import Journal

    j = Journal(tmp_path / "j.jsonl", t0=0.0)
    m = Machine(journal=j)
    m.forcer(S.CLOTURE, "reprise après plantage")
    j.close()
    evts = Journal.lire(tmp_path / "j.jsonl")
    force = [e for e in evts if e["kind"] == "etat_force"]
    assert len(force) == 1 and force[0]["raison"] == "reprise après plantage"
