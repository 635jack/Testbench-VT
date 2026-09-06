#!/usr/bin/env python3
"""
dataset.py — écriture d'une session d'acquisition visuo-tactile.

Une session est un dossier, une étape est un sous-dossier, et **tout est
horodaté sur la même horloge monotone**. C'est ce qui permet, après coup, de
mettre une image en face d'un état tactile : sans base de temps commune, les
deux flux ne sont plus recollables.

Rien n'est écrasé ni recalculé à la lecture : les images sont brutes, la
profondeur est en unités capteur avec son échelle dans les métadonnées, et le
tactile garde ses valeurs brutes à côté des valeurs normalisées.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: Version du format. Un lecteur écrit aujourd'hui doit pouvoir refuser une
#: session écrite par une version qu'il ne connaît pas, plutôt que la lire de
#: travers en silence.
FORMAT_VERSION = 1

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


def write_raw_stream(directory, stem: str, frames, rel=None) -> dict:
    """
    Écrit un flux de trames brutes en ``.npy``, et décrit ce qui a été écrit.

    Unique implémentation de la persistance du flux : :meth:`Session.save_raw_stream`
    l'utilise, et les outils qui n'ouvrent pas de session — la palpation, par
    exemple — l'appellent directement. Deux écritures divergentes du même flux
    seraient le meilleur moyen de rendre les sessions incomparables.

    Args:
        directory: dossier de destination, créé au besoin.
        stem: préfixe des deux fichiers (``<stem>_data.npy``, ``<stem>_t.npy``).
        frames: objets portant ``.t`` (``time.perf_counter``) et ``.data``.
        rel: fonction ramenant un ``t`` sur une autre horloge. Par défaut les
            temps sont comptés depuis la **première trame**, ce qui suffit quand
            il n'y a pas de session pour imposer son zéro.

    Returns:
        le descriptif du flux, ou ``{}`` si ``frames`` est vide.
    """
    if not frames:
        return {}
    data = np.asarray([bytearray(f.data) for f in frames], dtype=np.uint8)
    if rel is None:
        t0 = frames[0].t
        def rel(t, _t0=t0):  # noqa: E306
            return round(t - _t0, 4)
    times = np.asarray([rel(f.t) for f in frames], dtype=np.float64)

    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / f"{stem}_data.npy", data)
    np.save(d / f"{stem}_t.npy", times)

    types = {int(k): int(v) for k, v in
             zip(*np.unique(data[:, 0], return_counts=True))}
    span = float(times[-1] - times[0])
    return {"data": str(d / f"{stem}_data.npy"),
            "times": str(d / f"{stem}_t.npy"),
            "count": int(data.shape[0]),
            "bytes_per_frame": int(data.shape[1]),
            "frame_types": {f"0x{k:02x}": v for k, v in types.items()},
            "span_s": round(span, 3),
            "rate_hz": round(len(frames) / span, 1) if span > 0 else None}


@dataclass
class Session:
    """
    Dossier de session, et l'index de tout ce qu'on y écrit.

    L'horloge de référence est ``time.perf_counter`` prise au démarrage : tous
    les ``t`` du manifeste sont des secondes depuis ce zéro.
    """

    root: Path
    meta: dict = field(default_factory=dict)
    frames: list[dict] = field(default_factory=list)
    tactile: list[dict] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    @classmethod
    def create(cls, base: Path, label: str, meta: dict | None = None) -> "Session":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = Path(base) / f"{stamp}_{label}"
        root.mkdir(parents=True, exist_ok=True)
        s = cls(root=root, meta=dict(meta or {}))
        s.meta.update({"label": label, "created": stamp,
                       "format_version": FORMAT_VERSION})
        return s

    @property
    def t(self) -> float:
        return round(time.perf_counter() - self._t0, 4)

    # ── Écriture ──────────────────────────────────────────────────────────────

    def save_frame(self, step: str, index: int, color, depth,
                   extra: dict | None = None) -> dict:
        """
        Enregistre une image couleur et sa profondeur.

        La profondeur part en ``.npy`` et non en image : c'est de l'entier 16
        bits en unités capteur, la convertir pour l'affichage détruirait la
        mesure.
        """
        if cv2 is None:
            raise RuntimeError("cv2 absent : ce module doit tourner dans la VM.")
        d = self.root / step
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{index:04d}"
        cv2.imwrite(str(d / f"{stem}_color.png"), color)
        if depth is not None:
            np.save(d / f"{stem}_depth.npy", depth)

        entry = {"t": self.t, "step": step, "index": index,
                 "color": f"{step}/{stem}_color.png",
                 "depth": f"{step}/{stem}_depth.npy" if depth is not None else None}
        entry.update(extra or {})
        self.frames.append(entry)
        return entry

    def save_tactile(self, step: str, state, motors: dict | None = None,
                     extra: dict | None = None) -> dict:
        """Ajoute un état tactile à la série, horodaté sur la même horloge."""
        entry = {"t": self.t, "step": step, "zones": state.to_dict()}
        if motors:
            entry["motors"] = motors
        entry.update(extra or {})
        self.tactile.append(entry)
        return entry

    def save_raw_frames(self, step: str, frames, times=None) -> dict:
        """
        Enregistre les trames TPDO **brutes**, 192 octets chacune.

        Les valeurs décodées dépendent de ma table de découpage ; les octets
        bruts, non. Les garder, c'est pouvoir tout recalculer si la table
        évolue — et elle a déjà changé une fois.

        Stockées en ``.npy`` uint8 (N, 192) plutôt qu'en JSON : mille trames
        font 192 ko en binaire contre plusieurs mégaoctets en texte.
        """
        arr = np.asarray([bytearray(f) for f in frames], dtype=np.uint8)
        d = self.root / step
        d.mkdir(parents=True, exist_ok=True)
        name = f"raw_tpdo_{len(self.raw):02d}.npy"
        np.save(d / name, arr)
        entry = {"t": self.t, "step": step, "path": f"{step}/{name}",
                 "count": int(arr.shape[0]),
                 "times": [round(x, 4) for x in times] if times else None}
        self.raw.append(entry)
        return entry

    def rel(self, t_perf: float) -> float:
        """Ramène un ``time.perf_counter`` sur l'horloge de la session."""
        return round(t_perf - self._t0, 4)

    def save_raw_stream(self, step: str, frames) -> dict:
        """
        Enregistre **toutes** les trames reçues, sans filtrage ni sous-échantillonnage.

        Prend des objets portant ``.t`` (perf_counter) et ``.data`` (192 octets),
        c'est-à-dire ce que la pompe du bus produit telle quelle. Le type de
        chaque trame est conservé à part : moteur et tactile alternent, et le
        tri se fait à la lecture.

        C'est ce qui rend une session réinterprétable : toute conclusion tirée
        d'un décodage peut être refaite sur les octets, y compris avec une table
        de découpage qui n'existe pas encore.
        """
        idx = len(self.raw)
        entry = write_raw_stream(self.root / step, f"stream_{idx:02d}",
                                 frames, rel=self.rel)
        if not entry:
            return {}
        entry["data"] = f"{step}/{Path(entry['data']).name}"
        entry["times"] = f"{step}/{Path(entry['times']).name}"
        entry.update({"t": self.t, "step": step, "kind": "stream"})
        self.raw.append(entry)
        return entry

    def mark(self, name: str, **extra) -> dict:
        """
        Pose un repère horodaté.

        Le flux brut est continu et ignore les étapes ; ce sont ces repères qui
        permettent de retrouver après coup où commence la fermeture, où le pouce
        s'est mis en opposition, où la main s'est rouverte.
        """
        entry = {"t": self.t, "name": name, **extra}
        self.marks.append(entry)
        return entry

    def note(self, key: str, value) -> None:
        self.meta[key] = value

    def close(self) -> Path:
        """Écrit le manifeste. À appeler quoi qu'il arrive."""
        manifest = {
            "meta": self.meta,
            "frames": self.frames,
            "tactile": self.tactile,
            "raw_tpdo": self.raw,
            "marks": self.marks,
            "counts": {"frames": len(self.frames), "tactile": len(self.tactile),
                       "raw_blocks": len(self.raw),
                       "raw_frames": sum(r["count"] for r in self.raw)},
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        return path
