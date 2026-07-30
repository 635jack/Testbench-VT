#!/usr/bin/env python3
"""
Étape 5 — valider le profil sur l'objet monté, et le documenter.

**À relancer pour chaque matériau.** Le profil a été réglé sur un objet donné ; le
PLA argenté renvoie des reflets spéculaires bien plus forts et peut écrêter là où
le marbré ne le faisait pas, le translucide fait l'inverse.

Si l'objet écrête au niveau haut, la correction est de **baisser les trois niveaux
du même écart** — jamais de toucher à l'exposition. L'exposition et l'écart entre
niveaux sont ce qui rend les matériaux comparables entre eux ; le décalage
d'ensemble, lui, se rattrape au traitement. L'outil calcule cet écart et le PWM
correspondant.

Écrit un dossier par objet : les quatre images (trois niveaux + pose), la
profondeur, et un JSON de métriques.
"""
import argparse
import logging
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import Dimmer, D405, RESULTS_DIR, write_csv, save_json, load_json  # noqa: E402
from vt_light import masks as masks_mod, metrics  # noqa: E402
from vt_light.measure import condition  # noqa: E402
from vt_light.profile import LightProfile, DEFAULT_PATH  # noqa: E402
from vt_light.photometry import OECF, ev  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("--object", required=True,
                    help="nom du matériau monté, ex. pla_marbre / pla_argent / "
                         "pla_translucide")
    ap.add_argument("--profile", default=DEFAULT_PATH)
    ap.add_argument("--response", default=os.path.join(RESULTS_DIR, "20_response",
                                                       "response.json"))
    ap.add_argument("--clip-frac-max", type=float, default=0.001)
    ap.add_argument("--rebuild-masks", action="store_true", default=True,
                    help="reconstruire le masque objet (obligatoire après un "
                         "changement d'objet)")
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "40_validate"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    outdir = os.path.join(args.outdir, args.object)
    os.makedirs(outdir, exist_ok=True)

    profile = LightProfile.load(args.profile)
    detector = metrics.make_detector()
    print("\n--- Profil ---")
    print("    " + profile.summary().replace("\n", "\n    "))

    rows, report = [], {"object": args.object, "profile": profile.raw}
    with Dimmer(args.port) as dim, D405(profile.camera_settings()) as cam:
        # Le masque objet doit être refait : ce n'est plus le même objet. Et le
        # blanc de référence se relève sur la condition de pose, seule où les
        # marqueurs se décodent — sinon le repli désignerait l'objet lui-même.
        if profile.apply_pose_frame(cam, dim, flush=14) is not None:
            marker_reference, _ = cam.grab()
        else:
            marker_reference = None
        top_level = profile.level_names[0]
        profile.apply(cam, dim, top_level, flush=14)
        reference, _ = cam.grab()
        masks, info = masks_mod.build(reference, detector,
                                      bgr_markers=marker_reference)
        if masks.get("object") is None:
            print("  /!\\ objet non détecté dans l'image de référence : "
                  "vérifier qu'il est bien posé et éclairé.")
            return 1
        report["masks_info"] = info
        masks_mod.save(os.path.join(outdir, "masks.npz"), masks)
        cv2.imwrite(os.path.join(outdir, "masks_overlay.png"),
                    masks_mod.overlay(reference, masks))
        print(f"    masque objet : {info['object_n_px']} px | "
              f"masque blanc : {info['white_n_px']} px "
              f"({info['white_n_markers']} marqueur)")

        print("\n--- Les trois niveaux ---")
        for name in profile.level_names:
            pwm = profile.apply(cam, dim, name, flush=12)
            row, color, depth = condition(cam, masks, detector, n_avg=5,
                                          n_marker_frames=15)
            row.update({"level": name, "pwm": pwm, "object": args.object,
                        "exposure_us": cam.settings.exposure_us})
            rows.append(row)
            cv2.imwrite(os.path.join(outdir, f"{name}_color.png"), color)
            if depth is not None:
                cv2.imwrite(os.path.join(outdir, f"{name}_depth.png"), depth)
            print(f"    {name:6s} PWM {pwm:3d} | objet moy "
                  f"{row['object_mean_lum']:6.1f} p99 {row['object_p99']:5.1f} "
                  f"écrêté {row['object_clipped_frac']*100:5.2f} % "
                  f"| S/B {row['snr_object']:6.1f} "
                  f"| profondeur {row['depth_fill_object']*100:5.1f} % "
                  f"rms {row['depth_rms_object_mm']:5.2f} mm")

        pose_pwm = profile.apply_pose_frame(cam, dim, flush=12)
        if pose_pwm is not None:
            row, color, depth = condition(cam, masks, detector, n_avg=3,
                                          n_marker_frames=25)
            row.update({"level": "pose", "pwm": pose_pwm, "object": args.object,
                        "exposure_us": cam.settings.exposure_us})
            rows.append(row)
            cv2.imwrite(os.path.join(outdir, "pose_color.png"), color)
            print(f"\n--- Prise de vue de pose ---")
            print(f"    PWM {pose_pwm:3d} | ArUco moy {row['markers_mean']:4.2f} "
                  f"max {row['markers_max']} | identifiants "
                  f"{row['markers_ever'] or '-'}")
            if row["markers_mean"] < 1.0:
                print("    /!\\ moins d'un marqueur par image : l'angle du plateau "
                      "ne sera pas fiable pour cet objet (il masque peut-être des "
                      "marqueurs).")
        dim.set_pwm(0)

    write_csv(os.path.join(outdir, "metrics.csv"), rows)

    # -------------------------------------------------------------- verdict
    print("\n--- Verdict ---")
    top = rows[0]
    verdict = {"clips_at_top": bool(top["object_clipped_frac"] > args.clip_frac_max)}
    if not verdict["clips_at_top"]:
        margin = ev(255.0 / max(1.0, top["object_p99"]))
        verdict["headroom_ev"] = margin
        print(f"    pas d'écrêtage au niveau haut. Marge restante avant "
              f"saturation : {margin:.2f} diaphragme (p99 = {top['object_p99']:.0f}).")
    else:
        print(f"    /!\\ écrêtage au niveau haut : "
              f"{top['object_clipped_frac']*100:.2f} % des pixels de l'objet, "
              f"p99 = {top['object_p99']:.0f}.")
        # De combien faut-il baisser la lumière ? On raisonne en éclairement via la
        # courbe de réponse, la relation niveau/lumière n'étant pas linéaire.
        try:
            response = load_json(args.response)
            oecf = OECF.from_dict(response["oecf"])
            curve = response["illuminance_curve"]
            pwms = np.array([c["pwm"] for c in curve], float)
            illums = np.array([c["illuminance_rel"] for c in curve], float)
            # Viser p99 = 230 en supposant la réponse localement en loi de
            # puissance, de pente gamma.
            gamma = response["oecf"].get("gamma_estimate", 0.65)
            drop_ev = ev((top["object_p99"] / 230.0) ** (1.0 / gamma))
            print(f"    baisser les trois niveaux de {drop_ev:.2f} diaphragme :")
            for level in profile.levels:
                current = float(np.interp(level["pwm"], pwms, illums))
                target = current / (2.0 ** drop_ev)
                new_pwm = int(round(float(np.interp(target, illums, pwms))))
                print(f"        {level['name']:6s} PWM {level['pwm']:3d} -> {new_pwm:3d}")
            verdict["suggested_drop_ev"] = drop_ev
        except Exception as exc:
            print(f"    (correction non calculable : {exc})")

    report["metrics"] = rows
    report["verdict"] = verdict
    save_json(os.path.join(outdir, "validation.json"), report)
    print(f"\n    Images et métriques dans {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
