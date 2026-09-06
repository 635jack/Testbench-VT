#!/usr/bin/env python3
"""
Étape 0 — vérifier le banc avant toute mesure.

Répond à cinq questions, dans cet ordre, parce que chacune invalide les suivantes
si elle échoue :

1. Le dimmer acquitte-t-il ?
2. Les réglages écrits dans la D405 sont-ils bien ceux qu'elle applique
   (relecture depuis le capteur) ?
3. **Le PWM agit-il, et dans quel sens ?** Rien ne garantit que 255 = lampe
   allumée : selon le circuit du variateur, la consigne peut être inversée.
4. Quelle est la lumière parasite, PWM à 0 ? C'est le plancher qu'aucun niveau
   bas ne pourra descendre en dessous.
5. Le PLA blanc des marqueurs est-il exploitable comme référence de blanc ?

Écrit des aperçus PNG dans ``results/00_check/``.
"""
import argparse
import logging
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import Dimmer, D405, CameraSettings, RESULTS_DIR, save_json  # noqa: E402
from vt_light import metrics  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("-e", "--exposure", type=int, default=4000)
    ap.add_argument("-g", "--gain", type=int, default=16)
    ap.add_argument("-w", "--white-balance", type=int, default=4600)
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "00_check"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.outdir, exist_ok=True)
    detector = metrics.make_detector()
    report = {}

    with Dimmer(args.port) as dim, D405(CameraSettings(
            exposure_us=args.exposure, gain=args.gain,
            white_balance_k=args.white_balance)) as cam:

        print("\n--- 2. Réglages relus depuis le capteur ---")
        readback = cam.read_back()
        for key, value in readback.items():
            print(f"    {key:28s} {value}")
        report["camera_readback"] = readback
        report["intrinsics"] = cam.intrinsics
        report["depth_scale"] = cam.depth_scale

        expected = {"exposure": args.exposure, "gain": args.gain,
                    "white_balance": args.white_balance,
                    "enable_auto_exposure": 0, "enable_auto_white_balance": 0}
        drift = {k: (v, readback.get(k)) for k, v in expected.items()
                 if readback.get(k) is not None and abs(readback[k] - v) > 0.5}
        if drift:
            print(f"  /!\\ réglages non appliqués : {drift}")
        else:
            print("  OK : la caméra applique exactement les réglages demandés.")
        report["settings_drift"] = drift

        print("\n--- 3/4. Effet du PWM et lumière parasite ---")
        levels = {}
        for pwm in (0, 64, 128, 192, 255):
            dim.set_pwm(pwm)
            cam.flush(10)
            colors, depths = cam.grab_stack(5)
            color = colors[-1]
            lum = float(metrics.luminance(color).mean())
            row = {
                "pwm": pwm,
                "mean_lum": lum,
                "sat_frac": metrics.saturation_fraction(color),
                "markers": metrics.count_markers(color, detector),
                "noise": metrics.temporal_noise(colors),
            }
            if depths is not None:
                row["depth_fill"] = metrics.depth_stats(
                    depths[-1], cam.depth_scale, cam.intrinsics)["fill_ratio"]
            levels[pwm] = row
            cv2.imwrite(os.path.join(args.outdir, f"color_pwm{pwm:03d}.png"), color)
            print(f"    PWM {pwm:3d} | lum {lum:6.2f} | sat {row['sat_frac']*100:5.2f} % "
                  f"| ArUco {row['markers']} | bruit {row['noise']:4.2f} "
                  f"| depth {row.get('depth_fill', float('nan'))*100:5.1f} %")
        report["pwm_probe"] = levels

        lum0, lum255 = levels[0]["mean_lum"], levels[255]["mean_lum"]
        if abs(lum255 - lum0) < 1.0:
            print("  /!\\ le PWM ne change pas l'image : lampe non alimentée, "
                  "ou sortie A2 non reliée au variateur.")
            report["pwm_effective"] = False
        else:
            sense = "255 = clair" if lum255 > lum0 else "255 = sombre (consigne INVERSÉE)"
            print(f"  OK : le PWM agit — {sense}. "
                  f"Plancher parasite (PWM 0) : luminance {lum0:.2f}/255.")
            report["pwm_effective"] = True
            report["pwm_inverted"] = bool(lum255 < lum0)

        print("\n--- 5. Référence de blanc (PLA des marqueurs) ---")
        bright_pwm = 255 if lum255 > lum0 else 0
        dim.set_pwm(bright_pwm)
        cam.flush(10)
        color, _ = cam.grab()
        mask, n_markers = metrics.white_mask_from_markers(color, detector)
        source = "marqueurs ArUco"
        if mask is None or mask.sum() < 200:
            mask = metrics.brightest_mask(color, top_percentile=99.0)
            source = "repli : 1 % des pixels les plus clairs"
        stats = metrics.patch_stats(color, mask)
        print(f"    source : {source} ({n_markers} marqueurs, {stats['n_px']} pixels)")
        print(f"    R/G = {stats['r_over_g']:.3f}   B/G = {stats['b_over_g']:.3f}   "
              f"écart de neutralité = {stats['neutrality_err']:.3f}")
        print(f"    luminance du blanc = {stats['mean_lum']:.1f}/255, "
              f"écrêté sur {stats['clipped_frac']*100:.1f} % du masque")
        report["white_reference"] = {"source": source, "n_markers": n_markers, **stats}

        overlay = color.copy()
        overlay[mask] = (0, 0, 255)
        cv2.imwrite(os.path.join(args.outdir, "white_mask_overlay.png"), overlay)
        np.save(os.path.join(args.outdir, "white_mask.npy"), mask)

        dim.set_pwm(0)

    save_json(os.path.join(args.outdir, "check.json"), report)
    print(f"\nAperçus et rapport dans {args.outdir}")


if __name__ == "__main__":
    main()
