#!/usr/bin/env python3
"""
journal.py — le fil des événements, écrit avant d'être vrai.

Une ligne JSON par événement, ``flush`` puis ``fsync`` à chaque écriture. Un
processus tué perd au pire l'événement en cours.

Ce module existe à cause d'un chiffre : sur 39 sessions produites par
``tools/protocole.py``, **deux n'ont pas de manifeste** — 218 Mo d'images et de
trames sans index, parce que le manifeste n'était écrit qu'au ``close()``
final. Le journal renverse la charge : il est écrit en continu, et le manifeste
n'en est qu'un résumé reconstructible.

Une ligne tronquée en fin de fichier est **normale** — c'est la signature d'un
processus tué en pleine écriture — et :func:`lire` l'ignore au lieu de refuser
tout le fichier.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

#: Version du format de journal. Un lecteur doit pouvoir refuser une version
#: qu'il ne connaît pas plutôt que la lire de travers.
JOURNAL_VERSION = 1


class Journal:
    """
    Fichier d'événements append-only.

    Args:
        path: le ``.jsonl`` à écrire. Le dossier parent est créé au besoin.
        t0: l'origine monotone de la session. Chaque événement porte son
            ``t`` relatif à elle, plus l'heure murale, pour qu'un journal reste
            lisible sans son manifeste.
    """

    def __init__(self, path, t0: float):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._t0 = t0
        self._lock = threading.Lock()
        self._n = 0
        self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self.ecrire("journal_ouvert", version=JOURNAL_VERSION)

    # ── Écriture ──────────────────────────────────────────────────────────────

    def ecrire(self, kind: str, **champs) -> dict:
        """
        Ajoute un événement et le rend.

        L'écriture est **synchrone jusqu'au disque**. C'est le seul endroit du
        programme où on paie un ``fsync`` par appel, et c'est délibéré : le
        journal ne vaut que s'il survit à ce qui l'a interrompu.
        """
        evt = {
            "n": self._n,
            "t": round(time.perf_counter() - self._t0, 4),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(time.time() % 1 * 1000):03d}",
            "kind": kind,
        }
        evt.update(champs)
        with self._lock:
            self._n += 1
            self._fh.write(json.dumps(evt, ensure_ascii=False, default=_json_safe) + "\n")
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except (OSError, ValueError):
                # Certains systèmes de fichiers refusent fsync (montages réseau,
                # conteneurs). Perdre la garantie vaut mieux que perdre la
                # session : on continue, l'événement est déjà dans le tampon OS.
                pass
        return evt

    def close(self) -> None:
        try:
            self.ecrire("journal_ferme", evenements=self._n)
        except (ValueError, OSError):
            pass
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Lecture ───────────────────────────────────────────────────────────────

    @staticmethod
    def lire(path) -> list:
        """
        Relit un journal, en tolérant une dernière ligne tronquée.

        Returns:
            la liste des événements complets, dans l'ordre d'écriture.

        Une ligne illisible **en fin de fichier** est ignorée : c'est la trace
        d'un processus tué en pleine écriture, le cas que ce module existe pour
        couvrir. Une ligne illisible **au milieu** est également ignorée, mais
        signalée par un événement de substitution, parce qu'elle signe autre
        chose qu'un arrêt brutal.
        """
        p = Path(path)
        if not p.exists():
            return []
        evts = []
        lignes = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, ligne in enumerate(lignes):
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                evts.append(json.loads(ligne))
            except json.JSONDecodeError:
                if i < len(lignes) - 1:
                    evts.append({"n": None, "kind": "ligne_illisible", "ligne": i})
        return evts


def _json_safe(o):
    """Dernier recours pour les types que ``json`` ne connaît pas."""
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "tolist"):          # ndarray, scalaires numpy
        return o.tolist()
    if hasattr(o, "to_dict"):
        return o.to_dict()
    return str(o)
