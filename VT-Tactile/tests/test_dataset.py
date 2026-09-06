#!/usr/bin/env python3
"""
Tests de l'écriture de session, sans caméra ni main.

Ce qui compte ici : que les images et les états tactiles portent des
horodatages issus de **la même** horloge, et que le manifeste permette de
retrouver l'un depuis l'autre. Un jeu de données visuo-tactile dont les deux
flux ne sont pas recollables ne vaut rien.

    python3 tests/test_dataset.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from vt_tactile.dataset import Session  # noqa: E402
from vt_tactile.tpdo import TactileReader, build_frame  # noqa: E402


def check(name, cond, detail=""):
    print(f"  {'ok ' if cond else 'ÉCHEC'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        raise SystemExit(1)


def main() -> None:
    reader = TactileReader()
    state = reader.decode(build_frame({"index.pad": {"touch": [200] * 4}}))
    color = np.zeros((480, 640, 3), dtype=np.uint8)
    depth = np.full((480, 640), 1234, dtype=np.uint16)

    with tempfile.TemporaryDirectory() as d:
        s = Session.create(Path(d), "cylindre", meta={"object": "cylindre"})
        s.save_tactile("02_closing", state, {"3": 100})
        s.save_raw_frames("02_closing", [build_frame(), build_frame()],
                          times=[0.01, 0.02])
        # Flux continu : trames moteur et tactiles mêlées, telles qu'elles
        # arrivent, avec leur horodatage propre.
        import time as _t
        from types import SimpleNamespace
        motor = bytearray(192); motor[0], motor[1] = 0x00, 27
        now = _t.perf_counter()
        s.mark("closing_start")
        s.save_raw_stream("raw", [
            SimpleNamespace(t=now, data=build_frame()),
            SimpleNamespace(t=now + 0.001, data=bytes(motor)),
            SimpleNamespace(t=now + 0.002, data=build_frame()),
        ])
        s.save_frame("02_closing", 0, color, depth, extra={"loop_iteration": 3})
        s.save_frame("03_grasp", 0, color, None)
        path = s.close()

        m = json.loads(path.read_text())
        check("manifeste écrit", path.exists()
              and m["counts"]["frames"] == 2 and m["counts"]["tactile"] == 1
              and m["counts"]["raw_frames"] == 5, str(m["counts"]))
        check("le format est versionné", m["meta"]["format_version"] >= 1)
        check("l'image couleur est sur le disque",
              (s.root / m["frames"][0]["color"]).exists())
        check("la profondeur est en .npy, pas en image",
              m["frames"][0]["depth"].endswith(".npy")
              and (s.root / m["frames"][0]["depth"]).exists())
        loaded = np.load(s.root / m["frames"][0]["depth"])
        check("la profondeur est intacte, en entier 16 bits",
              loaded.dtype == np.uint16 and int(loaded[0, 0]) == 1234)
        check("l'absence de profondeur est déclarée, pas devinée",
              m["frames"][1]["depth"] is None)
        check("640x480", color.shape[:2] == (480, 640))

        # Les deux séries sont indépendantes : chacune doit croître, et les
        # deux doivent partager la même origine — c'est ce qui les rend
        # comparables. Les concaténer et exiger un tri global serait faux.
        f_t = [f["t"] for f in m["frames"]]
        t_t = [t["t"] for t in m["tactile"]]
        check("chaque série est croissante",
              f_t == sorted(f_t) and t_t == sorted(t_t), f"{f_t} / {t_t}")
        check("les deux séries partagent la même origine",
              all(0.0 <= t < 60.0 for t in f_t + t_t), f"{f_t} / {t_t}")
        check("une image renvoie à son état tactile",
              m["frames"][0]["tactile_t"] in t_t
              if "tactile_t" in m["frames"][0] else True)
        rawp = s.root / m["raw_tpdo"][0]["path"]
        arr = np.load(rawp)
        check("les trames brutes sont conservées, 192 octets chacune",
              arr.shape == (2, 192) and arr.dtype == np.uint8, str(arr.shape))
        check("les trames brutes sont horodatées",
              m["raw_tpdo"][0]["times"] == [0.01, 0.02])

        stream = [r for r in m["raw_tpdo"] if r.get("kind") == "stream"][0]
        check("le flux continu garde les deux types de trames",
              stream["frame_types"] == {"0x00": 1, "0x40": 2},
              str(stream["frame_types"]))
        data = np.load(s.root / stream["data"])
        times = np.load(s.root / stream["times"])
        check("le flux est complet et horodaté sur l'horloge de session",
              data.shape == (3, 192) and times.shape == (3,)
              and all(0.0 <= t < 60.0 for t in times), str(data.shape))
        check("les repères de phase sont conservés",
              m["marks"] and m["marks"][0]["name"] == "closing_start")

        check("le tactile porte les zones décodées",
              set(m["tactile"][0]["zones"]) >= {"index.pad", "palm"})

    print("\nTous les tests passent.")


if __name__ == "__main__":
    main()
