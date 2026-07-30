#!/usr/bin/env python3
"""
Étape 3 — mesurer les deux courbes de réponse du banc.

**Partie A : réponse de la caméra.** À éclairement constant, on balaie
l'exposition. Comme H = E x t, cela donne la courbe v = f(H) à un facteur près,
sans photomètre. C'est elle qui permettra ensuite de raisonner en éclairement et
non en niveaux de pixels.

**Partie B : réponse du variateur.** À exposition constante, on balaie le PWM et
on convertit chaque niveau mesuré en éclairement relatif via f^-1. On obtient
l'éclairement réellement produit par chaque consigne PWM — ce que ni le rapport
cyclique ni la luminance de l'image ne donnent directement.

La partie B est répétée à **deux expositions**. Les deux courbes d'éclairement
doivent se superposer : c'est le contrôle qui valide la conversion.

Toutes les expositions sont des multiples de 200 us, la période de la porteuse
PWM à 5 kHz — sans quoi l'obturateur intègre un nombre non entier de créneaux et
l'éclairement reçu varie d'une image à l'autre.
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
from vt_light.photometry import OECF, ev  # noqa: E402


def measure(cam, masks, detector, n_avg=3):
    return condition(cam, masks, detector, n_avg=n_avg)[0]


def exposure_grid(lo, hi, count):
    """Expositions log-réparties, alignées sur la période PWM, sans doublon."""
    raw = np.geomspace(lo, hi, count)
    snapped = sorted({snap_exposure_to_pwm_period(e) for e in raw})
    return [int(e) for e in snapped]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("-w", "--white-balance", type=int, required=True,
                    help="valeur retenue à l'étape 2, en K")
    ap.add_argument("-g", "--gain", type=int, default=16)
    ap.add_argument("--oecf-pwm", type=int, default=128,
                    help="niveau de lumière du balayage d'exposition")
    ap.add_argument("--exposure-min", type=int, default=200)
    ap.add_argument("--exposure-max", type=int, default=33000)
    ap.add_argument("--exposure-count", type=int, default=26)
    ap.add_argument("--probe-exposures", type=int, nargs="+", default=[2000, 6000],
                    help="expositions du balayage PWM (contrôle croisé)")
    ap.add_argument("--pwm-step", type=int, default=5)
    ap.add_argument("--masks", default=os.path.join(RESULTS_DIR, "masks.npz"))
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "20_response"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.outdir, exist_ok=True)
    masks = masks_mod.load(args.masks)
    detector = metrics.make_detector()

    settings = CameraSettings(exposure_us=4000, gain=args.gain,
                              white_balance_k=args.white_balance)

    with Dimmer(args.port) as dim, D405(settings) as cam:
        # ---------------------------------------------------- A : réponse caméra
        exposures = exposure_grid(args.exposure_min, args.exposure_max,
                                  args.exposure_count)
        print(f"\n--- A. Réponse caméra : {len(exposures)} expositions à PWM "
              f"{args.oecf_pwm} ---")
        dim.set_pwm(args.oecf_pwm)
        cam.flush(12)
        oecf_rows = []
        for exposure in exposures:
            cam.set_exposure(exposure, flush=8)
            row = {"exposure_us": exposure, "pwm": args.oecf_pwm,
                   **measure(cam, masks, detector)}
            oecf_rows.append(row)
            print(f"    t = {exposure:6d} us | blanc {row['white_mean_lum']:6.2f} "
                  f"| objet {row['object_mean_lum']:6.2f} "
                  f"(écrêté {row['object_clipped_frac']*100:5.1f} %) "
                  f"| ArUco {row['markers_mean']:.1f}")
        write_csv(os.path.join(args.outdir, "camera_response.csv"), oecf_rows)

        oecf = OECF([r["exposure_us"] for r in oecf_rows],
                    [r["white_mean_lum"] for r in oecf_rows])
        print(f"  gamma effectif du pipeline couleur : {oecf.gamma_estimate():.3f}")
        print(f"  plage inversible : niveaux {oecf.value_range[0]:.1f} "
              f"à {oecf.value_range[1]:.1f}")

        # ------------------------------------------------- B : réponse variateur
        pwm_values = sorted(set(list(range(0, 256, args.pwm_step)) + [255]))
        dimmer_rows = []
        for exposure in args.probe_exposures:
            exposure = snap_exposure_to_pwm_period(exposure)
            print(f"\n--- B. Réponse variateur : {len(pwm_values)} PWM à "
                  f"t = {exposure} us ---")
            cam.set_exposure(exposure, flush=8)
            for pwm in pwm_values:
                dim.set_pwm(pwm)
                cam.flush(8)
                row = {"exposure_us": exposure, "pwm": pwm,
                       **measure(cam, masks, detector)}
                row["illuminance_rel"] = float(
                    oecf.relative_illuminance(row["white_mean_lum"], exposure))
                dimmer_rows.append(row)
            for r in [r for r in dimmer_rows if r["exposure_us"] == exposure][::4]:
                illum = r["illuminance_rel"]
                shown = f"{illum:8.4f}" if np.isfinite(illum) else "hors plage"
                print(f"    PWM {r['pwm']:3d} | blanc {r['white_mean_lum']:6.2f} "
                      f"| E_rel {shown}")
        dim.set_pwm(0)

    write_csv(os.path.join(args.outdir, "dimmer_response.csv"), dimmer_rows)

    # Contrôle croisé : les courbes d'éclairement des deux expositions doivent
    # coïncider. Un désaccord signifierait que la courbe de réponse est fausse.
    print("\n--- Contrôle croisé des deux expositions ---")
    by_exposure = {}
    for row in dimmer_rows:
        by_exposure.setdefault(row["exposure_us"], {})[row["pwm"]] = row["illuminance_rel"]
    keys = sorted(by_exposure)
    agreement = None
    if len(keys) >= 2:
        a, b = by_exposure[keys[0]], by_exposure[keys[1]]
        common = [p for p in sorted(set(a) & set(b))
                  if np.isfinite(a[p]) and np.isfinite(b[p]) and a[p] > 0 and b[p] > 0]
        if common:
            ratios = np.array([b[p] / a[p] for p in common])
            agreement = {"n_common_pwm": len(common),
                         "median_ratio": float(np.median(ratios)),
                         "max_ev_deviation": float(np.max(np.abs(
                             np.log2(ratios / np.median(ratios)))))}
            print(f"    {len(common)} PWM communs | rapport médian "
                  f"{agreement['median_ratio']:.3f} | écart max "
                  f"{agreement['max_ev_deviation']:.3f} diaphragme")
            if agreement["max_ev_deviation"] > 0.25:
                print("  /!\\ les deux expositions ne donnent pas le même "
                      "éclairement : conversion peu fiable.")
            else:
                print("  OK : conversion en éclairement cohérente.")

    # Courbe d'éclairement retenue : moyenne géométrique des expositions
    # exploitables pour chaque PWM, ramenée à 1 au maximum.
    curve = []
    for pwm in sorted({r["pwm"] for r in dimmer_rows}):
        vals = [by_exposure[e][pwm] for e in keys
                if np.isfinite(by_exposure[e].get(pwm, np.nan))
                and by_exposure[e][pwm] > 0]
        if not vals:
            continue
        # Normaliser chaque exposition sur elle-même avant de moyenner : les deux
        # séries sont proportionnelles, pas égales.
        curve.append({"pwm": pwm, "illuminance_rel": float(np.exp(np.mean(np.log(vals))))})
    if curve:
        top = max(c["illuminance_rel"] for c in curve)
        for c in curve:
            c["illuminance_norm"] = c["illuminance_rel"] / top
            c["ev_below_max"] = -ev(c["illuminance_norm"])

    save_json(os.path.join(args.outdir, "response.json"),
              {"oecf": oecf.as_dict(),
               "oecf_pwm": args.oecf_pwm,
               "white_balance_k": args.white_balance,
               "gain": args.gain,
               "probe_exposures_us": [snap_exposure_to_pwm_period(e)
                                      for e in args.probe_exposures],
               "cross_check": agreement,
               "illuminance_curve": curve})

    if curve:
        print("\n--- Éclairement relatif par consigne PWM ---")
        for c in curve[::4]:
            print(f"    PWM {c['pwm']:3d} | E {c['illuminance_norm']:7.4f} du max "
                  f"| -{c['ev_below_max']:5.2f} diaphragme")


if __name__ == "__main__":
    main()
