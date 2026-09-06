#!/usr/bin/env python3
"""
recover.py — reconstruire un manifeste depuis le journal.

Une session tuée n'a pas de ``manifest.json``. Elle a en revanche son
``journal.jsonl``, écrit et ``fsync``é événement par événement, et ses fichiers
sur le disque. Le manifeste n'étant qu'un résumé, il se refait.

Le manifeste reconstruit porte ``complet: false`` et un bloc ``recuperation``
qui dit d'où il vient. Il ne se fait pas passer pour un manifeste normal :
confondre les deux, c'est croire une session terminée alors qu'elle a été
interrompue au milieu d'un angle.

Sait aussi traiter les sessions du **format 1** (celles de
``tools/protocole.py``), qui n'ont pas de journal : dans ce cas il n'y a que le
disque, et le manifeste est marqué ``source: "disque"``.
"""
from __future__ import annotations

import json
from pathlib import Path

from .journal import Journal


def est_complete(racine) -> bool:
    """Une session a-t-elle un manifeste ?"""
    return (Path(racine) / "manifest.json").exists()


def inspecter(racine) -> dict:
    """
    Que contient cette session, et lui manque-t-il son manifeste ?

    Utilisé par la ligne de commande et par l'API pour lister ce qui est
    récupérable, sans rien écrire.
    """
    r = Path(racine)
    evts = Journal.lire(r / "journal.jsonl")
    return {
        "racine": str(r),
        "nom": r.name,
        "complete": est_complete(r),
        "journal": len(evts),
        "images_sur_disque": len(list(r.rglob("*_color.png"))),
        "flux_sur_disque": len(list(r.rglob("stream_*_data.npy"))),
        "octets": sum(f.stat().st_size for f in r.rglob("*") if f.is_file()),
        "format": _format_de(r),
    }


def _format_de(r: Path) -> int:
    for nom in ("session.json", "manifest.json"):
        p = r / nom
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        meta = d.get("meta", d)
        v = meta.get("format_version")
        if v:
            return int(v)
    return 1 if (r / "journal.jsonl").exists() is False else 2


def recuperer(racine, force: bool = False) -> dict:
    """
    Écrit un ``manifest.json`` pour une session qui n'en a pas.

    Args:
        force: réécrire même si un manifeste existe.

    Returns:
        le manifeste reconstruit.

    Raises:
        FileExistsError: si un manifeste existe et ``force`` est faux. Écraser
            un manifeste complet par un manifeste reconstruit serait une perte
            nette : celui d'origine sait ce que le journal seul ignore.
    """
    r = Path(racine)
    if est_complete(r) and not force:
        raise FileExistsError(f"{r/'manifest.json'} existe déjà (force=True pour écraser)")

    evts = Journal.lire(r / "journal.jsonl")
    meta = _meta_de(r, evts)

    images, raw, marks, captures = [], [], [], []
    validations = {}
    illisibles = 0
    for e in evts:
        kind = e.get("kind")
        if kind == "image":
            images.append({k: v for k, v in e.items() if k not in ("kind", "n", "iso")})
        elif kind == "flux_brut":
            raw.append({k: v for k, v in e.items() if k not in ("kind", "n", "iso")})
        elif kind == "mark":
            marks.append({k: v for k, v in e.items() if k not in ("kind", "n", "iso")})
        elif kind == "json" and str(e.get("chemin", "")).endswith("capture.json"):
            captures.append(dict(e.get("contenu") or {}))
        elif kind == "validation":
            validations[e.get("etape")] = e
        elif kind == "ligne_illisible":
            illisibles += 1

    # La dernière validation d'une étape l'emporte : le journal garde
    # l'historique, le manifeste ne montre que l'état final.
    for c in captures:
        v = validations.get(c.get("etape"))
        if v:
            c.update({"validation": v.get("validation"), "valide_par": v.get("par"),
                      "commentaire": v.get("commentaire", "")})

    images = _dedoublonner(images, ("etape", "index"))
    raw = _dedoublonner(raw, ("etape", "data"))
    captures = _dedoublonner(captures, ("etape",))

    # Pas de journal — session du format 1, ou journal perdu. Le disque est
    # alors la seule source, et il vaut mieux qu'un index vide : sans lui, une
    # session de 83 Mo se relit « 0 image », ce qui est exact et inutilisable.
    depuis_disque = not evts
    if depuis_disque:
        images = _balayer_images(r)
        raw = _balayer_flux(r)

    # Ce que le journal annonce doit exister sur le disque. Un fichier annoncé
    # mais absent signe une écriture interrompue : on le retire de l'index
    # plutôt que de livrer un manifeste qui ment.
    images, manquantes = _filtrer_presents(r, images, "color")
    raw, manquants_flux = _filtrer_presents(r, raw, "data")

    ferme = any(e.get("kind") == "session_fermee" for e in evts)
    source = "disque" if depuis_disque else "journal"
    manifeste = {
        "meta": meta,
        "complet": False,
        "recuperation": {
            "source": source,
            "evenements": len(evts),
            "lignes_illisibles": illisibles,
            "session_close_proprement": ferme,
            "images_annoncees_absentes": manquantes,
            "flux_annonces_absents": manquants_flux,
            "images_sur_disque": len(list(r.rglob("*_color.png"))),
        },
        "images": images,
        "raw_tpdo": raw,
        "marks": marks,
        "captures": captures,
        "compteurs": {
            "images": len(images),
            "blocs_bruts": len(raw),
            "trames_brutes": sum(int(x.get("count", 0)) for x in raw),
            "captures": len(captures),
            "validees": sum(1 for c in captures if c.get("validation") == "valide"),
        },
    }
    (r / "manifest.json").write_text(
        json.dumps(manifeste, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifeste


def _balayer_images(r: Path) -> list:
    """
    Indexe les images trouvées sur le disque, faute de journal.

    L'étape est le dossier relatif, l'index vient du nom de fichier : c'est la
    convention d'écriture, la même en version 1 et en version 2. Aucun ``t``
    n'est inventé — sans journal l'instant de la prise de vue est perdu, et
    poser un horodatage plausible serait pire que d'avouer qu'il manque.
    """
    entrees = []
    for couleur in sorted(r.rglob("*_color.png")):
        rel = couleur.relative_to(r)
        stem = couleur.name[: -len("_color.png")]
        prof = couleur.with_name(f"{stem}_depth.npy")
        entrees.append({
            "t": None,
            "etape": str(rel.parent),
            "index": int(stem) if stem.isdigit() else stem,
            "color": str(rel),
            "depth": str(prof.relative_to(r)) if prof.exists() else None,
            "reconstruit_depuis_le_disque": True,
        })
    return entrees


def _balayer_flux(r: Path) -> list:
    """Indexe les flux bruts du disque, dans les deux conventions de nommage."""
    import numpy as np  # noqa: PLC0415

    entrees = []
    for motif in ("stream_*_data.npy", "raw_tpdo_*.npy"):
        for chemin in sorted(r.rglob(motif)):
            rel = chemin.relative_to(r)
            temps = chemin.with_name(chemin.name.replace("_data.npy", "_t.npy"))
            try:
                # ``mmap_mode`` : ces fichiers font des dizaines de mégaoctets,
                # et seule leur forme nous intéresse.
                forme = np.load(chemin, mmap_mode="r").shape
            except (OSError, ValueError):
                continue
            entrees.append({
                "t": None,
                "etape": str(rel.parent),
                "data": str(rel),
                "times": str(temps.relative_to(r)) if temps.exists() else None,
                "count": int(forme[0]),
                "bytes_per_frame": int(forme[1]) if len(forme) > 1 else None,
                "reconstruit_depuis_le_disque": True,
            })
    return entrees


def _meta_de(r: Path, evts: list) -> dict:
    """Métadonnées : ``session.json`` s'il existe, sinon ce que le journal en dit."""
    p = r / "session.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    meta = {"format_version": 1, "objet": {"nom": _objet_du_nom(r.name)},
            "reconstruit": True}
    for e in evts:
        if e.get("kind") == "note":
            meta[e.get("cle")] = e.get("valeur")
    return meta


def _objet_du_nom(nom: str) -> str:
    """``20260821-125907_cube_pla_gris`` → ``cube_pla_gris``."""
    return nom.split("_", 1)[1] if "_" in nom else nom


def _dedoublonner(entrees: list, cles: tuple) -> list:
    """Garde la dernière occurrence de chaque clé, dans l'ordre d'apparition."""
    vus = {}
    for e in entrees:
        vus[tuple(e.get(k) for k in cles)] = e
    return list(vus.values())


def _filtrer_presents(r: Path, entrees: list, champ: str):
    """Retire les entrées dont le fichier annoncé n'est pas sur le disque."""
    gardes, absents = [], []
    for e in entrees:
        rel = e.get(champ)
        if rel and not (r / rel).exists():
            absents.append(rel)
            continue
        gardes.append(e)
    return gardes, absents
