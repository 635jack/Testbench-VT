#!/usr/bin/env python3
"""
Étape 1 — figer les zones de mesure.

Construit le masque du PLA blanc des marqueurs et celui de l'objet, sur une image
prise dans une condition de référence claire, puis les enregistre. Tous les
balayages suivants réutilisent **ces** pixels : c'est ce qui rend leurs résultats
comparables entre eux.

À relancer à chaque changement d'objet ou de pose caméra.
"""
import argparse
import logging
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import Dimmer, D405, CameraSettings, RESULTS_DIR, save_json  # noqa: E402
from vt_light import masks as masks_mod, metrics  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("--pwm", type=int, default=255, help="niveau de référence")
    ap.add_argument("-e", "--exposure", type=int, default=4000)
    ap.add_argument("-g", "--gain", type=int, default=16)
    ap.add_argument("-w", "--white-balance", type=int, default=4600)
    ap.add_argument("--marker-conditions", type=lambda s: tuple(int(v) for v in s.split(",")),
                    nargs="+", default=[(255, 4000), (35, 2200), (200, 400), (60, 1000)],
                    metavar="PWM,EXPO_US",
                    help="conditions essayées pour relever le blanc des marqueurs, "
                         "dans l'ordre ; la première qui décode est retenue")
    ap.add_argument("-o", "--outdir", default=RESULTS_DIR)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.outdir, exist_ok=True)

    detector = metrics.make_detector()
    with Dimmer(args.port) as dim, D405(CameraSettings(
            exposure_us=args.exposure, gain=args.gain,
            white_balance_k=args.white_balance)) as cam:
        # Image du niveau clair : elle sert à délimiter l'objet.
        dim.set_pwm(args.pwm)
        cam.flush(12)
        color, _ = cam.grab()

        # Le blanc de référence demande une condition où les marqueurs se
        # décodent, et elle ne coïncide pas avec la précédente. On ne la connaît
        # pas encore à ce stade du protocole : on essaie donc plusieurs
        # candidates et on garde la première qui décode.
        marker_reference, used = None, None
        for pwm, exposure in args.marker_conditions:
            dim.set_pwm(pwm)
            cam.set_exposure(exposure, flush=10)
            candidate, _ = cam.grab()
            mask, n_markers = metrics.white_mask_from_markers(candidate, detector)
            print(f"    condition marqueurs PWM {pwm:3d} / {exposure:5d} us "
                  f"-> {n_markers} marqueur(s)")
            if mask is not None and mask.sum() >= 150:
                marker_reference, used = candidate, {"pwm": pwm, "exposure_us": exposure}
                break
        dim.set_pwm(0)

    if marker_reference is None:
        print("  /!\\ aucune condition testée ne décode de marqueur : le blanc de "
              "référence retombera sur les pixels les plus clairs, qui désignent "
              "l'objet. Retirer l'objet du plateau, ou élargir "
              "--marker-conditions.")

    masks, info = masks_mod.build(color, detector, bgr_markers=marker_reference)
    info["reference"] = {"pwm": args.pwm, "exposure_us": args.exposure,
                         "gain": args.gain, "white_balance_k": args.white_balance}
    info["marker_reference"] = used

    for name in ("white", "object"):
        mask = masks.get(name)
        if mask is None:
            print(f"  /!\\ masque '{name}' introuvable")
            continue
        stats = metrics.patch_stats(color, mask)
        info[f"{name}_stats"] = stats
        print(f"  {name:7s} : {stats['n_px']:6d} px | luminance {stats['mean_lum']:6.1f} "
              f"| écrêté {stats['clipped_frac']*100:5.1f} % "
              f"| R/G {stats['r_over_g']:.3f} B/G {stats['b_over_g']:.3f}")

    masks_mod.save(os.path.join(args.outdir, "masks.npz"), masks, info)
    cv2.imwrite(os.path.join(args.outdir, "masks_overlay.png"),
                masks_mod.overlay(color, masks))
    cv2.imwrite(os.path.join(args.outdir, "masks_reference.png"), color)
    save_json(os.path.join(args.outdir, "masks_info.json"), info)
    print(f"\nVérifier {os.path.join(args.outdir, 'masks_overlay.png')} "
          "(rouge = blanc de référence, vert = objet).")


if __name__ == "__main__":
    main()
