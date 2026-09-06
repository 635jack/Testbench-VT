#!/usr/bin/env python3
"""
Ce que le gestionnaire de ressources garantit.

Trois ressources du banc sont physiquement exclusives et chacune a déjà coûté
une session. Ces tests verrouillent la règle : **un seul propriétaire**, et un
second prétendant est *refusé*, jamais mis en file — une attente silencieuse
sur un port série ressemble exactement à un banc qui rame.
"""
from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from vtctl.hw.resources import (
    CAMERA, ETHERCAT, RESSOURCES, SERIE, ResourceBusy, ResourceManager,
    demons_concurrents,
)


def test_une_ressource_ne_se_prend_quune_fois():
    mgr = ResourceManager()
    mgr.acquire(CAMERA, "premier")
    with pytest.raises(ResourceBusy):
        mgr.acquire(CAMERA, "second")


def test_le_refus_nomme_le_detenteur():
    """« ressource occupée » sans plus n'aide personne à 2 h du matin."""
    mgr = ResourceManager()
    mgr.acquire(SERIE, "BenchOwner")
    with pytest.raises(ResourceBusy) as e:
        mgr.acquire(SERIE, "autre")
    assert "BenchOwner" in str(e.value)


def test_liberer_rend_la_ressource():
    mgr = ResourceManager()
    t = mgr.acquire(CAMERA, "premier")
    t.release()
    assert mgr.libre(CAMERA)
    mgr.acquire(CAMERA, "second")          # ne lève pas


def test_liberer_deux_fois_est_sans_effet():
    """Une double libération ne doit pas voler la ressource au suivant."""
    mgr = ResourceManager()
    t = mgr.acquire(CAMERA, "premier")
    t.release()
    mgr.acquire(CAMERA, "second")
    t.release()                            # tardive : ne doit rien casser
    assert mgr.tenues() == {CAMERA: "second"}


def test_contexte_libere_meme_sur_exception():
    mgr = ResourceManager()
    with pytest.raises(RuntimeError):
        with mgr.acquire(ETHERCAT, "essai"):
            raise RuntimeError("panne au milieu")
    assert mgr.libre(ETHERCAT)


def test_les_trois_ressources_sont_independantes():
    mgr = ResourceManager()
    for nom in RESSOURCES:
        mgr.acquire(nom, f"proprietaire-{nom}")
    assert set(mgr.tenues()) == set(RESSOURCES)


def test_ressource_inconnue_refusee():
    """Une faute de frappe sur un nom de ressource doit se voir tout de suite."""
    with pytest.raises(ValueError):
        ResourceManager().acquire("gyroscope", "x")


def test_release_all():
    mgr = ResourceManager()
    for nom in RESSOURCES:
        mgr.acquire(nom, "x")
    mgr.release_all()
    assert mgr.tenues() == {}


# ── Verrou inter-processus ────────────────────────────────────────────────────

def test_verrou_systeme_bloque_un_autre_processus(tmp_path):
    """
    Un second ``vtctl`` ne doit pas pouvoir monter le même banc.

    Le verrou est un ``flock`` : il meurt avec le processus qui le tient, donc
    un plantage ne laisse pas de verrou fantôme — contrairement à un fichier
    témoin, qu'il faudrait nettoyer à la main après chaque crash.
    """
    mgr = ResourceManager(lock_dir=tmp_path)
    mgr.acquire(ETHERCAT, "premier")

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_essayer_dans_un_autre_processus, args=(str(tmp_path), q))
    p.start()
    p.join(30)
    assert q.get(timeout=5) == "refuse"


def test_verrou_meurt_avec_le_processus(tmp_path):
    """Un processus tué ne laisse pas la ressource verrouillée pour toujours."""
    ctx = multiprocessing.get_context("spawn")
    pret = ctx.Queue()
    p = ctx.Process(target=_tenir_puis_mourir, args=(str(tmp_path), pret))
    p.start()
    assert pret.get(timeout=20) == "tenu"
    p.kill()
    p.join(10)

    mgr = ResourceManager(lock_dir=tmp_path)
    mgr.acquire(ETHERCAT, "apres-le-crash")    # ne lève pas
    assert mgr.tenues() == {ETHERCAT: "apres-le-crash"}


def _essayer_dans_un_autre_processus(lock_dir, q):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from vtctl.hw.resources import ETHERCAT, ResourceBusy, ResourceManager

    try:
        ResourceManager(lock_dir=lock_dir).acquire(ETHERCAT, "second")
        q.put("accepte")
    except ResourceBusy:
        q.put("refuse")


def _tenir_puis_mourir(lock_dir, pret):
    import sys
    import time
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from vtctl.hw.resources import ETHERCAT, ResourceManager

    ResourceManager(lock_dir=lock_dir).acquire(ETHERCAT, "condamne")
    pret.put("tenu")
    time.sleep(60)


# ── Démons concurrents ────────────────────────────────────────────────────────

def test_pgrep_ne_se_trouve_pas_lui_meme():
    """
    Piège classique : ``pgrep -af "py3nocap"`` trouve son propre ``pgrep``.

    Les crochets de ``py3noca[p]`` l'empêchent, et le filtre écarte aussi notre
    propre PID. Sans ça, le contrôle annonce un démon en permanence et le banc
    devient impossible à démarrer.
    """
    trouves = demons_concurrents()
    assert all(d["pid"] != os.getpid() for d in trouves)
    assert all("pgrep" not in d["cmd"] for d in trouves)
