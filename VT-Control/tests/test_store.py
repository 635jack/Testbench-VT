#!/usr/bin/env python3
"""
Ce que le stockage garantit : le journal survit, les octets ne bougent pas.

Le chiffre qui motive ce module : sur 39 sessions produites par l'outil
précédent, **deux n'ont pas de manifeste** — 218 Mo d'images et de trames sans
index. Ces tests vérifient qu'une session tuée reste exploitable.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from vtctl.store import recover
from vtctl.store.journal import Journal
from vtctl.store.session import Session


class _Frame:
    __slots__ = ("t", "data")

    def __init__(self, t, data):
        self.t, self.data = t, data


# ── Journal ───────────────────────────────────────────────────────────────────

def test_journal_relit_ce_quil_ecrit(tmp_path):
    j = Journal(tmp_path / "j.jsonl", t0=time.perf_counter())
    j.ecrire("essai", valeur=42, texte="accentué é")
    j.close()
    evts = Journal.lire(tmp_path / "j.jsonl")
    milieu = [e for e in evts if e["kind"] == "essai"]
    assert milieu[0]["valeur"] == 42 and milieu[0]["texte"] == "accentué é"


def test_ligne_tronquee_en_fin_de_fichier_ignoree(tmp_path):
    """
    La signature d'un processus tué en pleine écriture.

    C'est le cas que ce module existe pour couvrir : il ne doit **pas** faire
    perdre les événements précédents.
    """
    p = tmp_path / "j.jsonl"
    j = Journal(p, t0=0.0)
    for i in range(5):
        j.ecrire("evt", i=i)
    j.close()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"n": 99, "kind": "evt", "i": 99')     # coupée net

    evts = Journal.lire(p)
    assert [e["i"] for e in evts if e["kind"] == "evt"] == [0, 1, 2, 3, 4]


def test_ligne_illisible_au_milieu_est_signalee(tmp_path):
    """Au milieu, ce n'est pas un arrêt brutal : ça doit se voir."""
    p = tmp_path / "j.jsonl"
    p.write_text('{"kind": "a"}\nPAS DU JSON\n{"kind": "b"}\n', encoding="utf-8")
    kinds = [e["kind"] for e in Journal.lire(p)]
    assert kinds == ["a", "ligne_illisible", "b"]


def test_journal_absent_rend_une_liste_vide(tmp_path):
    assert Journal.lire(tmp_path / "rien.jsonl") == []


# ── Horloge ───────────────────────────────────────────────────────────────────

def test_l_ancre_permet_de_dater(tmp_path):
    """
    Sans ancre, un ``perf_counter`` relatif n'a de sens que dans son processus.

    C'est ce qui manquait au format 1 : deux sessions n'étaient pas comparables
    et un redémarrage effaçait la référence.
    """
    s = Session.create(tmp_path, "objet")
    h = s.meta["horloge"]
    assert {"t0_monotonic", "t0_unix", "t0_iso"} <= set(h)
    assert h["source"] == "time.perf_counter"
    # Le décalage UTC est explicite : une date sans fuseau est ambiguë.
    assert h["t0_iso"][-6] in "+-"
    t = s.t
    absolu = h["t0_unix"] + t
    assert abs(absolu - time.time()) < 5.0
    s.close()


def test_t_dimage_est_celui_de_la_prise_de_vue(tmp_path):
    """
    L'horodatage vient de l'appelant, relevé au retour de ``grab()``.

    En version 1 il était pris *après* ``cv2.imwrite`` : le temps d'encodage
    PNG s'ajoutait au délai réel de la mesure.
    """
    s = Session.create(tmp_path, "objet")
    couleur = np.zeros((8, 8, 3), np.uint8)
    t_prise = s.t
    time.sleep(0.05)
    e = s.save_image("etape", 0, couleur, None, t=t_prise)
    assert e["t"] == pytest.approx(t_prise, abs=1e-4)
    assert e["t_ecriture"] > e["t"]
    s.close()


# ── Flux brut ─────────────────────────────────────────────────────────────────

def test_les_octets_relus_sont_les_octets_ecrits(tmp_path):
    s = Session.create(tmp_path, "objet")
    trames = [_Frame(time.perf_counter() + i * 0.001,
                     bytes([i % 256] * 192)) for i in range(500)]
    e = s.save_raw_stream("brut", trames)
    s.close()

    data = np.load(s.root / e["data"])
    assert data.shape == (500, 192)
    assert data.dtype == np.uint8
    for i in range(500):
        assert bytes(data[i]) == trames[i].data


def test_aucune_deduplication(tmp_path):
    """
    Deux trames identiques sont deux trames.

    Au repos la charge utile ne bouge pas d'un octet pendant des secondes :
    dédupliquer effacerait ces périodes, qui sont précisément la ligne de base
    dont dépend tout décodage ultérieur.
    """
    s = Session.create(tmp_path, "objet")
    identique = bytes(192)
    e = s.save_raw_stream("brut", [_Frame(i * 0.001, identique) for i in range(300)])
    s.close()
    assert e["count"] == 300
    assert np.load(s.root / e["data"]).shape[0] == 300


def test_les_temps_du_flux_sont_croissants(tmp_path):
    s = Session.create(tmp_path, "objet")
    t0 = time.perf_counter()
    e = s.save_raw_stream("brut", [_Frame(t0 + i * 0.0011, bytes(192))
                                   for i in range(200)])
    s.close()
    t = np.load(s.root / e["times"])
    assert np.all(np.diff(t) > 0)
    assert t[0] >= 0                       # relatif au t0 de la session


def test_profondeur_conservee_en_uint16(tmp_path):
    """
    La profondeur est une mesure, pas une image.

    En unités capteur — le dixième de millimètre sur la D405 — avec l'échelle
    dans les métadonnées. La convertir pour l'affichage détruirait la mesure.
    """
    s = Session.create(tmp_path, "objet")
    prof = (np.arange(64, dtype=np.uint16).reshape(8, 8) * 500).astype(np.uint16)
    e = s.save_image("etape", 0, np.zeros((8, 8, 3), np.uint8), prof, t=0.0)
    s.close()
    relue = np.load(s.root / e["depth"])
    assert relue.dtype == np.uint16
    assert np.array_equal(relue, prof)


# ── Captures et validation ────────────────────────────────────────────────────

def test_validation_revisable_et_tracee(tmp_path):
    s = Session.create(tmp_path, "objet")
    s.capture("tactile/angle_00", {"phase": "tactile", "validation": "en_attente"})
    s.valider("tactile/angle_00", "invalide", commentaire="rien en contact")
    s.valider("tactile/angle_00", "valide", commentaire="finalement bon")
    s.close()

    fichier = json.loads((s.root / "tactile/angle_00/capture.json").read_text())
    assert fichier["validation"] == "valide"
    # Le fichier ne montre que le dernier état ; le journal garde l'historique.
    hist = [e["validation"] for e in Journal.lire(s.root / "journal.jsonl")
            if e["kind"] == "validation"]
    assert hist == ["invalide", "valide"]


def test_validation_inconnue_refusee(tmp_path):
    s = Session.create(tmp_path, "objet")
    s.capture("e", {"validation": "en_attente"})
    with pytest.raises(ValueError):
        s.valider("e", "peut-etre")
    s.close()


# ── Récupération ──────────────────────────────────────────────────────────────

def test_manifeste_reconstruit_egale_celui_qu_on_aurait_ecrit(tmp_path):
    """Une session tuée avant ``close()`` reste indexable."""
    s = Session.create(tmp_path, "objet")
    couleur = np.zeros((8, 8, 3), np.uint8)
    for i in range(4):
        s.save_image(f"visuel/angle_{i:02d}", 0, couleur, None, t=float(i))
        s.capture(f"visuel/angle_{i:02d}", {"phase": "visuelle", "validation": "valide"})
    attendu = s.manifeste(complet=True)
    racine = s.root
    s.journal.close()                       # tué : pas de close(), pas de manifeste
    assert not (racine / "manifest.json").exists()

    m = recover.recuperer(racine)
    assert m["complet"] is False
    assert m["compteurs"]["images"] == attendu["compteurs"]["images"]
    assert m["compteurs"]["captures"] == attendu["compteurs"]["captures"]
    assert m["compteurs"]["validees"] == attendu["compteurs"]["validees"]
    assert m["recuperation"]["session_close_proprement"] is False


def test_recuperation_retire_les_fichiers_annonces_mais_absents(tmp_path):
    """Un manifeste qui ment est pire qu'un manifeste incomplet."""
    s = Session.create(tmp_path, "objet")
    e = s.save_image("visuel/angle_00", 0, np.zeros((8, 8, 3), np.uint8), None, t=0.0)
    (s.root / e["color"]).unlink()          # écriture interrompue
    s.journal.close()

    m = recover.recuperer(s.root)
    assert m["compteurs"]["images"] == 0
    assert m["recuperation"]["images_annoncees_absentes"] == [e["color"]]


def test_recuperation_refuse_decraser_sans_force(tmp_path):
    s = Session.create(tmp_path, "objet")
    s.close()
    with pytest.raises(FileExistsError):
        recover.recuperer(s.root)
    assert recover.recuperer(s.root, force=True)["complet"] is False


def test_inspecter_signale_labsence_de_manifeste(tmp_path):
    s = Session.create(tmp_path, "objet")
    s.save_image("e", 0, np.zeros((4, 4, 3), np.uint8), None, t=0.0)
    s.journal.close()
    i = recover.inspecter(s.root)
    assert i["complete"] is False and i["images_sur_disque"] == 1
    s2 = Session.create(tmp_path, "autre")
    s2.close()
    assert recover.inspecter(s2.root)["complete"] is True


def test_recuperation_sans_journal_balaye_le_disque(tmp_path):
    """
    Une session du **format 1** n'a pas de journal : le disque est tout.

    Sans ce balayage, une session de 83 Mo se relit « 0 image » — exact, et
    parfaitement inutilisable. Vérifié sur une vraie session orpheline du banc :
    11 images et 396 085 trames brutes retrouvées.
    """
    import cv2

    racine = tmp_path / "20260821-111357_essai"
    (racine / "B/angle_00/00_pouce_pose").mkdir(parents=True)
    (racine / "B/angle_00/raw_precedent").mkdir(parents=True)
    for i in range(3):
        d = racine / "B/angle_00/00_pouce_pose"
        cv2.imwrite(str(d / f"{i:04d}_color.png"), np.zeros((8, 8, 3), np.uint8))
        np.save(d / f"{i:04d}_depth.npy", np.zeros((8, 8), np.uint16))
    np.save(racine / "B/angle_00/raw_precedent/stream_00_data.npy",
            np.zeros((250, 192), np.uint8))
    np.save(racine / "B/angle_00/raw_precedent/stream_00_t.npy",
            np.arange(250, dtype=np.float64))

    m = recover.recuperer(racine)
    assert m["recuperation"]["source"] == "disque"
    assert m["compteurs"]["images"] == 3
    assert m["compteurs"]["trames_brutes"] == 250
    assert m["images"][0]["etape"] == "B/angle_00/00_pouce_pose"
    assert m["images"][0]["depth"] is not None
    # Aucun horodatage n'est inventé : sans journal, l'instant de la prise de
    # vue est perdu, et un ``t`` plausible serait pire qu'un ``t`` absent.
    assert all(e["t"] is None for e in m["images"])
    assert all(e["reconstruit_depuis_le_disque"] for e in m["images"])


def test_le_manifeste_reconstruit_ne_se_fait_pas_passer_pour_un_autre(tmp_path):
    """Confondre les deux, c'est croire terminée une session interrompue."""
    s = Session.create(tmp_path, "objet")
    s.save_image("e", 0, np.zeros((4, 4, 3), np.uint8), None, t=0.0)
    s.journal.close()
    m = recover.recuperer(s.root)
    assert m["complet"] is False
    assert "recuperation" in m
