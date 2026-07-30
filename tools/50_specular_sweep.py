#!/usr/bin/env python3
"""
Étape 6 — chercher un reflet spéculaire sur une révolution complète.

L'exposition a été fixée sur l'objet vu sous **une** orientation. Le jeu de données
en compte six, et une face latérale peut renvoyer la lampe vers la caméra à un
angle qui n'a pas été testé. Un tel éclat écrête localement, et l'écrêtage est
irréversible : la validation sous une seule pose ne suffit donc pas à garantir
l'exposition.

Le plateau tourne en continu pendant la mesure. À 2200 us d'exposition et 46 deg/s
au plus, le flou de filé vaut 0,1 deg : négligeable. On n'a donc pas besoin de
positionner le plateau, ce qui évite complètement la roue libre de ~15 deg et le
piège de la bascule ``START_PAUSE``.

La métrique est délibérément prise sur **toute l'image** et pas sur le masque objet :
l'éclat peut venir d'une face latérale, hors du masque défini sur la face supérieure.

Le test vérifie aussi que le plateau **a réellement tourné**. Sans cela, un plateau
resté immobile produirait un « aucun reflet trouvé » faussement rassurant.
"""
import argparse
import json
import logging
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import Dimmer, D405, RESULTS_DIR, write_csv, save_json  # noqa: E402
from vt_light import metrics  # noqa: E402
from vt_light.profile import LightProfile, DEFAULT_PATH  # noqa: E402

DEFAULT_IR_CONFIG = os.path.expanduser("~/Control_Turtable_IR/config_telecommande.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("--object", required=True)
    ap.add_argument("--profile", default=DEFAULT_PATH)
    ap.add_argument("--ir-config", default=DEFAULT_IR_CONFIG)
    ap.add_argument("--duration", type=float, default=35.0,
                    help="durée de rotation ; un cycle complet dure 30 s à la "
                         "vitesse la plus lente")
    ap.add_argument("--level", default=None,
                    help="niveau à tester (défaut : le plus clair, le seul qui "
                         "puisse écrêter)")
    ap.add_argument("--clip-frac-max", type=float, default=0.001)
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "50_specular"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    outdir = os.path.join(args.outdir, args.object)
    os.makedirs(outdir, exist_ok=True)

    with open(args.ir_config, encoding="utf-8") as fh:
        ir = json.load(fh)
    profile = LightProfile.load(args.profile)
    level = args.level or profile.level_names[0]
    detector = metrics.make_detector()

    rows, frames = [], []
    with Dimmer(args.port) as dim, D405(profile.camera_settings()) as cam:
        pwm = profile.apply(cam, dim, level, flush=14)
        print(f"    niveau {level} (PWM {pwm}), exposition "
              f"{cam.settings.exposure_us} us")

        reference, _ = cam.grab()
        print(f"    démarrage de la rotation pour {args.duration:.0f} s")
        dim.send_ir(ir["COMMANDE_ROTATION_DROITE"])

        t0 = time.time()
        previous = reference
        while time.time() - t0 < args.duration:
            color, _ = cam.grab()
            channel_max = color.max(axis=2)
            row = {
                "t_sec": time.time() - t0,
                "sat_frac": float((channel_max >= metrics.SAT_LEVEL).mean()),
                "p99_9": float(np.percentile(channel_max, 99.9)),
                "p99": float(np.percentile(channel_max, 99.0)),
                "frame_mean_lum": float(metrics.luminance(color).mean()),
                # Différence à l'image précédente : preuve que le plateau bouge.
                "motion": float(np.abs(color.astype(np.int16)
                                       - previous.astype(np.int16)).mean()),
                "markers": metrics.count_markers(color, detector),
            }
            rows.append(row)
            frames.append(color)
            previous = color

        # Un seul START_PAUSE : un ordre d'arrêt de trop **relance** le plateau.
        dim.send_ir(ir["COMMANDE_START_PAUSE"])
        dim.set_pwm(0)
        print(f"    arrêt demandé ({len(rows)} images capturées)")

    write_csv(os.path.join(outdir, "specular_sweep.csv"), rows)

    motion = np.array([r["motion"] for r in rows])
    moved = float(np.median(motion))
    print("\n--- Le plateau a-t-il tourné ? ---")
    print(f"    différence médiane entre images consécutives : {moved:.3f} niveau")
    if moved < 0.5:
        print("    /!\\ le plateau semble IMMOBILE. Le résultat ci-dessous ne "
              "couvre qu'une seule orientation et ne prouve rien. Vérifier la "
              "portée de l'émetteur infrarouge et l'alimentation du plateau.")

    worst = max(rows, key=lambda r: r["sat_frac"])
    worst_p99 = max(rows, key=lambda r: r["p99_9"])
    print("\n--- Pire orientation rencontrée ---")
    print(f"    écrêtage maximal : {worst['sat_frac']*100:.3f} % des pixels "
          f"à t = {worst['t_sec']:.1f} s")
    print(f"    p99,9 maximal    : {worst_p99['p99_9']:.0f} / 255 "
          f"à t = {worst_p99['t_sec']:.1f} s")

    verdict = {"object": args.object, "level": level, "pwm": pwm,
               "exposure_us": profile.raw["camera"]["exposure_us"],
               "n_frames": len(rows), "turntable_moved": bool(moved >= 0.5),
               "motion_median": moved,
               "max_sat_frac": worst["sat_frac"], "max_p99_9": worst_p99["p99_9"]}

    if worst["sat_frac"] > args.clip_frac_max:
        print(f"\n    /!\\ ÉCRÊTAGE sur au moins une orientation. L'exposition de "
              f"{profile.raw['camera']['exposure_us']} us ne convient pas aux six "
              f"angles : baisser les trois niveaux (voir 40_validate.py).")
        verdict["ok"] = False
    else:
        margin = float(np.log2(255.0 / max(1.0, worst_p99["p99_9"])))
        print(f"\n    OK : aucune orientation n'écrête. Marge au pire angle : "
              f"{margin:.2f} diaphragme.")
        verdict["ok"] = True
        verdict["worst_headroom_ev"] = margin

    # Garder l'image la plus brillante de la révolution : c'est celle qu'il faudra
    # regarder si un doute subsiste sur l'origine du pic.
    index = int(np.argmax([r["p99_9"] for r in rows]))
    cv2.imwrite(os.path.join(outdir, "brightest_orientation.png"), frames[index])
    save_json(os.path.join(outdir, "specular.json"), verdict)
    print(f"    Image la plus brillante : {outdir}/brightest_orientation.png")


if __name__ == "__main__":
    main()
