#!/usr/bin/env python3
"""
selftest.py — vérifie la chaîne de mesure sur des trames fabriquées.

Aucun matériel requis. On génère un flux dont on connaît la vérité (octets
morts, figés, bruyants, un créneau qui répond au stimulus) et on contrôle que
les métriques la retrouvent. À lancer avant d'aller au banc : une erreur
découverte ici coûte une minute, découverte là-bas elle coûte une séance.

    python3 selftest.py
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench.metrics import (byte_stats, infer_stride, response, responders,
                           stream_health, summarise_bytes)
from bench.sdk import Frame

random.seed(7)
STRIDE, SLOTS, ACTIVE = 30, 6, 23   # vérité terrain fabriquée


def make_stream(n: int, t0: float, dt: float, press_slot: int | None = None,
                stalls: int = 0) -> list[Frame]:
    """Flux alterné 0x00/0x40 ; un créneau peut être « appuyé »."""
    out, t = [], t0
    for k in range(n):
        ftype = 0x40 if k % 2 else 0x00
        buf = bytearray(192)
        buf[0], buf[1] = ftype, SLOTS
        if ftype == 0x40:
            for s in range(SLOTS):
                base = 2 + STRIDE * s
                for j in range(ACTIVE):          # octets actifs du créneau
                    level = 120 + 3 * j
                    if press_slot == s and j < 9:
                        level = 240
                    buf[base + j] = min(255, level + random.randint(-1, 1))
                buf[base + 4] = 0                # un canal mort, exprès
                buf[base + 5] = 77               # un canal figé, exprès
        out.append(Frame(t, bytes(buf)))
        t += dt
        if stalls and k == n // 2:
            t += 0.25                            # une coupure franche
    return out


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok ' if cond else 'ÉCHEC'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        raise SystemExit(1)


print("santé du flux")
frames = make_stream(600, 0.0, 0.005, stalls=1)
h = stream_health(frames)
check("types détectés", set(h.types) == {"0x00", "0x40"}, str(h.types))
check("cadence plausible", 150 < h.poll_rate_hz < 260, f"{h.poll_rate_hz} Hz")
check("coupure repérée", h.longest_stall_ms > 200, f"{h.longest_stall_ms} ms")
check("longueur d'en-tête stable", h.header_len_values["0x40"] == [SLOTS])

print("\nstatistiques par octet")
stats = byte_stats(frames, 0x40)
summary = summarise_bytes(stats)
# par créneau : 1 canal mis à zéro + les 7 octets de queue jamais remplis,
# plus les 10 octets de fin de trame au-delà du dernier créneau
DEAD = SLOTS * (1 + (STRIDE - ACTIVE)) + (192 - 2 - SLOTS * STRIDE)
check("octets morts comptés exactement", summary["mort"] == DEAD,
      f"{summary['mort']} (attendu {DEAD})")
check("un octet figé par créneau", summary["figé"] == SLOTS, str(summary["figé"]))
actifs = summary["stable"] + summary["bruyant"] + summary["très bruyant"]
check("le reste est actif", actifs == 190 - DEAD - SLOTS, str(actifs))

print("\ndéduction de la structure")
st = infer_stride(stats)
check(f"pas retrouvé sans le supposer (attendu {STRIDE})",
      st["pas_probable"] == STRIDE, f"pas={st['pas_probable']} score={st['score']}")

print("\nréponse à un stimulus")
rest = make_stream(300, 0.0, 0.005)
stim = make_stream(300, 2.0, 0.005, press_slot=2)
z = response(rest, stim, 0x40)
hits = responders(z)
expected = {2 + STRIDE * 2 + j for j in range(9)} - {2 + STRIDE * 2 + 4,
                                                     2 + STRIDE * 2 + 5}
check("seul le créneau appuyé réagit", set(hits) == expected,
      f"{len(hits)} octets, créneaux {sorted({(i - 2) // STRIDE for i in hits})}")

print("\nrapport et comparaison")
from bench.run import build_report  # noqa: E402
from bench import compare  # noqa: E402

result = {
    "label": "synthetique", "started": "2026-01-01T00:00:00+00:00",
    "host": {"machine": "aarch64", "virtualised": "qemu", "node": "test",
             "kernel": "6.8"},
    "link": {"interface": "enx0", "driver": "r8152", "mac": "00:11", "speed_mbps": 100,
             "input_size": 192, "output_size": 64, "slaves": [], "host": "test",
             "kernel": "6.8", "machine": "aarch64", "virtualised": "qemu"},
    "capabilities": {"lhandprolib_set_tpdo_frame_type": True},
    "before_enable": {"stream": stream_health(make_stream(50, 0, 0.005)).to_dict()},
    "rest": {"frames": len(frames), "stream": h.to_dict(),
             "tactile_frame_type": "0x40", "sdk": {},
             "bytes": [s.to_dict() for s in stats],
             "byte_summary": summary, "structure": st},
    "pressure_reset_rc": 0,
}
report = build_report(result)
check("rapport non vide et chiffré", "Octets" in report or "octets" in report,
      f"{len(report)} caractères")

with tempfile.TemporaryDirectory() as d:
    a, b = Path(d) / "a.json", Path(d) / "b.json"
    a.write_text(json.dumps(result))
    other = json.loads(json.dumps(result))
    other["label"] = "autre"
    other["rest"]["byte_summary"]["mort"] = 12       # un défaut qui n'existe qu'ici
    b.write_text(json.dumps(other))
    sys.argv = ["compare", str(a), str(b), "--bytes"]
    compare.main()

print("\nTout passe. La chaîne de mesure est cohérente.")
