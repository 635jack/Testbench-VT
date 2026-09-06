#!/usr/bin/env python3
"""
compare.py — met plusieurs caractérisations côte à côte.

L'intérêt de lancer la même batterie sous plusieurs configurations matérielles
n'apparaît qu'à la comparaison : si le nombre d'octets morts change avec
l'alimentation, c'est l'alimentation ; s'il ne change pas, c'est la main.

    python3 -m bench.compare runs/*.json
    python3 -m bench.compare runs/*.json --bytes    # détail octet par octet

Sur chaque ligne, une valeur qui diffère d'une configuration à l'autre est
signalée par « ≠ ». Le reste est identique et n'appelle pas de discussion.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def get(d: dict, path: str, default="—"):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


ROWS = [
    ("Hôte", "host.node"),
    ("Virtualisation", "host.virtualised"),
    ("Interface", "link.interface"),
    ("Pilote", "link.driver"),
    ("Débit du lien (Mb/s)", "link.speed_mbps"),
    ("MAC", "link.mac"),
    ("Octets PDO entrée", "link.input_size"),
    ("Esclaves", "link.slaves"),
    ("Types avant bascule", "before_enable.stream.types"),
    ("Type de trame capteur", "rest.tactile_frame_type"),
    ("Types au repos", "rest.stream.types"),
    ("Lectures (Hz)", "rest.stream.poll_rate_hz"),
    ("Trames neuves (Hz)", "rest.stream.fresh_rate_hz"),
    ("Taux de fraîcheur", "rest.stream.fresh_ratio"),
    ("Intervalle médian (ms)", "rest.stream.interval_ms.median"),
    ("Intervalle p95 (ms)", "rest.stream.interval_ms.p95"),
    ("Plus longue coupure (ms)", "rest.stream.longest_stall_ms"),
    ("Octets stables", "rest.byte_summary.stable"),
    ("Octets bruyants", "rest.byte_summary.bruyant"),
    ("Octets très bruyants", "rest.byte_summary.très bruyant"),
    ("Octets figés", "rest.byte_summary.figé"),
    ("Octets morts", "rest.byte_summary.mort"),
    ("Bruit médian (LSB)", "rest.byte_summary.std_median_actifs"),
    ("Dérive max (LSB)", "rest.byte_summary.drift_max_abs"),
    ("Octets saturés", "rest.byte_summary.saturés"),
    ("Pas déduit", "rest.structure.pas_probable"),
    ("Score du pas", "rest.structure.score"),
    ("Retour reset pression", "pressure_reset_rc"),
]


def fmt(v) -> str:
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 21 else s[:20] + "…"
    return str(v)


def compare_zone_phase(runs, labels, phase: str) -> list[str]:
    zones = []
    for r in runs:
        for z in r.get(phase, {}):
            if z not in zones:
                zones.append(z)
    if not zones:
        return []
    out = [f"\n## {phase}  (nombre d'octets réactifs / z max)", ""]
    w = max(len(z) for z in zones) + 2
    out.append(" " * w + "".join(f"{lab:>22}" for lab in labels))
    for z in zones:
        cells = []
        for r in runs:
            d = r.get(phase, {}).get(z)
            cells.append("—" if d is None
                         else f"{d['n_responders']} / {d['zmax']}")
        mark = " ≠" if len(set(cells)) > 1 else ""
        out.append(f"{z:<{w}}" + "".join(f"{c:>22}" for c in cells) + mark)
    return out


def compare_bytes(runs, labels) -> list[str]:
    """Verdict de chaque octet dans chaque configuration, désaccords d'abord."""
    tables = []
    for r in runs:
        tables.append({b["index"]: b for b in r.get("rest", {}).get("bytes", [])})
    indices = sorted({i for t in tables for i in t})
    if not indices:
        return []
    out = ["\n## Verdict par octet (seuls les désaccords)", "",
           f"{'octet':>6}" + "".join(f"{lab:>22}" for lab in labels)]
    disagreements = 0
    for i in indices:
        cells = []
        for t in tables:
            b = t.get(i)
            cells.append("—" if b is None
                         else f"{b['verdict']} {b['lo']}-{b['hi']} σ{b['std']}")
        if len(set(cells)) > 1:
            disagreements += 1
            out.append(f"{i:>6}" + "".join(f"{c:>22}" for c in cells))
    out.append("")
    out.append(f"{disagreements} octets sur {len(indices)} se comportent "
               f"différemment selon la configuration.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", type=Path, nargs="+")
    ap.add_argument("--bytes", action="store_true",
                    help="détailler le verdict octet par octet")
    args = ap.parse_args()

    runs = [json.loads(p.read_text()) for p in args.json]
    labels = [r.get("label", p.stem) for r, p in zip(runs, args.json)]

    width = max(len(lbl) for lbl, _ in ROWS) + 2
    print("# Comparaison de configurations\n")
    print(" " * width + "".join(f"{lab:>22}" for lab in labels))
    print("-" * (width + 22 * len(labels)))
    for label, path in ROWS:
        cells = [fmt(get(r, path)) for r in runs]
        mark = " ≠" if len(set(cells)) > 1 else ""
        print(f"{label:<{width}}" + "".join(f"{c:>22}" for c in cells) + mark)

    for phase in ("proximity", "touch"):
        for line in compare_zone_phase(runs, labels, phase):
            print(line)

    if args.bytes:
        for line in compare_bytes(runs, labels):
            print(line)

    print("\nLes lignes marquées ≠ sont celles qui dépendent de la configuration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
