#!/usr/bin/env python3
"""
Étape 3 bis — arbitrer le conflit entre l'objet et les marqueurs.

Deux sujets se disputent la même exposition, et ils ne veulent pas la même chose :

- l'**objet** ne doit pas écrêter, ce qui plafonne l'exposition ;
- les **marqueurs ArUco** doivent se décoder, et ils fournissent la vérité
  terrain angulaire de tout le jeu de données.

Le balayage de l'étape 4 a montré, incidemment, que la détection était *meilleure*
en basse lumière. Contre-intuitif, et contraire au réglage retenu jusqu'ici
(``DEFAULT_EXPOSURE = 4000``). Comme un comptage sur une image isolée ne prouve
rien à cette taille de marqueur, on mesure ici un **taux sur 25 images** à chaque
condition.

Le balayage est croisé — exposition à PWM fixe, puis PWM à exposition fixe — pour
distinguer deux causes possibles : soit seule la *luminance du marqueur* compte
(les deux courbes se superposent alors quand on les trace contre elle), soit l'un
des deux réglages a un effet propre.
"""
import argparse
import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import (Dimmer, D405, CameraSettings, RESULTS_DIR,  # noqa: E402
                      write_csv, save_json, snap_exposure_to_pwm_period)
from vt_light import masks as masks_mod, metrics  # noqa: E402
from vt_light.measure import condition  # noqa: E402

N_FRAMES = 25


def run(cam, dim, masks, detector, pwm, exposure, tag):
    dim.set_pwm(pwm)
    cam.set_exposure(exposure, flush=10)
    row, _, _ = condition(cam, masks, detector, n_avg=3, n_marker_frames=N_FRAMES)
    row.update({"sweep": tag, "pwm": pwm, "exposure_us": exposure})
    print(f"    PWM {pwm:3d} t {exposure:6d} us | blanc {row['white_mean_lum']:6.1f} "
          f"| objet p99 {row['object_p99']:5.1f} écrêté "
          f"{row['object_clipped_frac']*100:5.2f} % "
          f"| ArUco moy {row['markers_mean']:4.2f} max {row['markers_max']} "
          f"| vus : {row['markers_ever'] or '-'}")
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("-w", "--white-balance", type=int, required=True)
    ap.add_argument("-g", "--gain", type=int, default=16)
    ap.add_argument("--exposures", type=int, nargs="+",
                    default=[200, 400, 600, 1000, 1400, 2200, 3000, 4000, 6000, 9000])
    ap.add_argument("--pwm-fixed", type=int, default=200,
                    help="PWM du balayage d'exposition")
    ap.add_argument("--exposure-fixed", type=int, default=2200,
                    help="exposition du balayage PWM")
    ap.add_argument("--pwms", type=int, nargs="+",
                    default=[21, 35, 60, 80, 108, 140, 200, 255])
    ap.add_argument("--masks", default=os.path.join(RESULTS_DIR, "masks.npz"))
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "25_markers"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.outdir, exist_ok=True)
    masks = masks_mod.load(args.masks)
    detector = metrics.make_detector()

    rows = []
    with Dimmer(args.port) as dim, D405(CameraSettings(
            exposure_us=2200, gain=args.gain,
            white_balance_k=args.white_balance)) as cam:
        print(f"\n--- A. Exposition variable, PWM fixé à {args.pwm_fixed} ---")
        for exposure in args.exposures:
            rows.append(run(cam, dim, masks, detector, args.pwm_fixed,
                            snap_exposure_to_pwm_period(exposure), "exposure"))

        print(f"\n--- B. PWM variable, exposition fixée à {args.exposure_fixed} us ---")
        for pwm in args.pwms:
            rows.append(run(cam, dim, masks, detector, pwm,
                            snap_exposure_to_pwm_period(args.exposure_fixed), "pwm"))
        dim.set_pwm(0)

    write_csv(os.path.join(args.outdir, "markers.csv"), rows)

    print("\n--- Lecture : taux de détection contre luminance du marqueur ---")
    # On trace les deux balayages ensemble contre la luminance du marqueur. S'ils
    # se superposent, c'est bien elle seule qui gouverne la détection, et non le
    # réglage employé pour l'obtenir.
    ordered = sorted(rows, key=lambda r: r["white_mean_lum"])
    for r in ordered:
        bar = "#" * int(round(r["markers_mean"] * 10))
        print(f"    blanc {r['white_mean_lum']:6.1f} ({r['sweep']:8s} "
              f"PWM {r['pwm']:3d} t {r['exposure_us']:5d}) "
              f"{r['markers_mean']:4.2f} {bar}")

    # La relation est non monotone — en U sur une plage large. Résumer par une
    # corrélation donnerait zéro et ne dirait rien. On délimite donc les plages
    # utilisables, définies comme les suites contiguës où le taux tient.
    windows, current = [], []
    for r in ordered:
        if r["markers_mean"] >= 0.5:
            current.append(r)
        elif current:
            windows.append(current)
            current = []
    if current:
        windows.append(current)
    windows = [w for w in windows if len(w) >= 2]

    print()
    for w in windows:
        lo, hi = w[0]["white_mean_lum"], w[-1]["white_mean_lum"]
        print(f"    fenêtre utilisable : blanc de {lo:.0f} à {hi:.0f} "
              f"({np.log2(hi / lo):.2f} diaphragme de large), "
              f"taux {min(r['markers_mean'] for r in w):.2f} à "
              f"{max(r['markers_mean'] for r in w):.2f}")
    if not windows:
        print("    aucune fenêtre utilisable dans les conditions testées.")

    best = max(rows, key=lambda r: r["markers_mean"])
    print(f"    meilleure détection : {best['markers_mean']:.2f} marqueur en moyenne "
          f"à PWM {best['pwm']} / {best['exposure_us']} us "
          f"(blanc à {best['white_mean_lum']:.1f})")

    all_ids = sorted({int(i) for r in rows if r["markers_ever"]
                      for i in r["markers_ever"].split("|")})
    print(f"    identifiants vus au moins une fois, toutes conditions : {all_ids}")
    if len(all_ids) < 3:
        print("    /!\\ moins de trois marqueurs accessibles quel que soit le "
              "réglage : la limite est géométrique (taille 10 mm, incidence "
              "rasante), pas photométrique. Aucun réglage d'exposition ne la "
              "lèvera.")

    save_json(os.path.join(args.outdir, "markers.json"), {
        "best": {k: best[k] for k in
                 ("sweep", "pwm", "exposure_us", "white_mean_lum", "markers_mean",
                  "markers_max", "markers_ever")},
        "usable_windows_white_lum": [[w[0]["white_mean_lum"], w[-1]["white_mean_lum"]]
                                     for w in windows],
        "ids_ever_seen": all_ids,
        "curve": [{k: r[k] for k in ("sweep", "pwm", "exposure_us",
                                     "white_mean_lum", "markers_mean",
                                     "markers_max", "markers_ever")}
                  for r in ordered],
    })


if __name__ == "__main__":
    main()
