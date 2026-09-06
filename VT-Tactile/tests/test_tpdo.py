#!/usr/bin/env python3
"""
Tests du décodeur, sans matériel.

Les valeurs de référence sont celles **mesurées au banc** le 2026-07-31 :
appuyer sur le bout de l'index déplace les canaux 1-4 et les champs en 10-11,
14-15, 18-19, 22 ; appuyer sur la pulpe déplace 5-8 et 12-13, 16-17, 20-21, 23.
Si un jour ce test casse, c'est que quelqu'un a réintroduit le découpage du SDK.

    python3 tests/test_tpdo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vt_tactile.tpdo import (  # noqa: E402
    PALM, TactileReader, ZONE_NAMES, build_frame, slot_offset,
)


def check(name, cond, detail=""):
    print(f"  {'ok ' if cond else 'ÉCHEC'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        raise SystemExit(1)


def test_zones():
    r = TactileReader()
    s = r.decode(build_frame())
    check("neuf zones décodées", set(s.zones) == set(ZONE_NAMES), str(len(s.zones)))
    check("quatre canaux au bout comme à la pulpe",
          len(s["index.tip"].pressure) == 4 and len(s["index.pad"].pressure) == 4)
    check("vingt-six points de paume", len(s[PALM].pressure) == 26)


def test_single_sensor_fingers():
    """
    Le pouce et l'auriculaire n'ont qu'un capteur, et leur 6e canal est une
    proximité. Le compter comme de la pression ferait « voir » un appui là où
    le capteur ne mesure qu'une approche.
    """
    r = TactileReader()
    s = r.decode(build_frame())
    check("pas de pulpe au pouce ni à l'auriculaire",
          "thumb.pad" not in s.zones and "little.pad" not in s.zones)
    check("cinq canaux de pression sur ces doigts",
          len(s["thumb"].pressure) == 5 and len(s["little"].pressure) == 5)

    s = r.decode(build_frame({"thumb": {"touch": [0] * 5, "sprox": 255}}))
    check("la proximité est séparée de la pression",
          s["thumb"].pressure_max == 0.0 and s["thumb"].self_proximity == 1.0)
    check("les doigts à deux capteurs n'ont pas d'approche",
          s["index.tip"].self_proximity is None)


def test_tip_pad_separation():
    """Le point qui distingue notre table de celle du SDK."""
    r = TactileReader()
    tip = r.decode(build_frame({"index.tip": {"touch": [200] * 4, "nf": 1370,
                                              "tf": 210, "dir": 142, "prox": 255}}))
    check("appui bout : la pulpe reste à zéro",
          tip["index.pad"].pressure_max == 0.0)
    check("force normale du bout lue en 10-11",
          abs(tip["index.tip"].normal_force - 13.70) < 1e-6,
          str(tip["index.tip"].normal_force))
    check("direction et proximité du bout",
          tip["index.tip"].direction == 142.0 and tip["index.tip"].proximity == 1.0)

    pad = r.decode(build_frame({"index.pad": {"touch": [200] * 4, "nf": 999}}))
    check("appui pulpe : le bout reste à zéro",
          pad["index.tip"].pressure_max == 0.0)
    check("force normale de la pulpe lue en 12-13",
          abs(pad["index.pad"].normal_force - 9.99) < 1e-6)
    check("les champs du bout ne bougent pas",
          pad["index.tip"].normal_force == 0.0)


def test_no_direction_sentinel():
    r = TactileReader()
    s = r.decode(build_frame({"middle.tip": {"dir": 0xFFFF}}))
    check("0xFFFF vaut « pas de direction »", s["middle.tip"].direction is None)


def test_zeroing():
    """Sans zéro les valeurs absolues ne sont pas comparables entre doigts."""
    r = TactileReader()
    repos = build_frame({"index.tip": {"touch": [170] * 4},
                         "little": {"touch": [80] * 5}})
    check("zéro pris", r.zero(repos) == 1)
    s = r.decode(repos)
    check("au repos, tout retombe à zéro",
          s["index.tip"].pressure_max == 0.0 and s["little"].pressure_max == 0.0)

    appui = build_frame({"index.tip": {"touch": [255] * 4},
                         "little": {"touch": [255] * 5}})
    s = r.decode(appui)
    check("pleine échelle vaut 1.0 quelle que soit la ligne de base",
          s["index.tip"].pressure_max == 1.0 and s["little"].pressure_max == 1.0)
    check("le contact est détecté", set(s.in_contact) == {"index.tip", "little"},
          str(s.in_contact))


def test_slot_offsets():
    expected = [2, 32, 62, 92, 122, 152]
    check("créneaux aux bons offsets",
          [slot_offset(i) for i in range(6)] == expected)


if __name__ == "__main__":
    for fn in (test_zones, test_single_sensor_fingers, test_tip_pad_separation, test_no_direction_sentinel,
               test_zeroing, test_slot_offsets):
        print(f"\n{fn.__name__}")
        fn()
    print("\nTous les tests passent.")
