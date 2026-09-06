#!/usr/bin/env python3
"""
Étape 4 — choisir l'exposition et les trois niveaux de lumière.

Le raisonnement, dans l'ordre.

**L'exposition doit être la même aux trois niveaux.** Sinon elle compense la
variation de lumière et le jeu de données ne montre plus rien : c'est le piège
central de ce réglage. L'exposition est donc fixée une fois, par la contrainte la
plus dure — ne pas écrêter l'objet au niveau le plus clair. L'écrêtage est le seul
défaut irréversible : un pixel à 255 a perdu son information, aucun traitement en
aval ne la retrouve.

**Le gain reste au minimum (16).** Le bruit de lecture est ce qu'on peut le moins
corriger ; on préfère toujours allonger l'exposition, sans conséquence ici puisque
les prises de vue sont à l'arrêt.

**Une conséquence non intuitive : le niveau haut choisi n'influe pas sur le
rapport signal/bruit du niveau bas.** Le signal au niveau bas vaut
E_bas x t = (E_haut x t) / 2^(2 x pas), et le produit E_haut x t est déjà plafonné
par l'écrêtage. Seul le **pas entre niveaux** décide donc de la qualité du niveau
bas. On mesure plusieurs pas et on retient le plus grand qui reste exploitable :
plus le contraste entre conditions est fort, mieux l'effet de la lumière se lit.

**Les niveaux sont espacés en éclairement, pas en PWM ni en niveaux de pixels.**
La réponse du variateur est plate au-delà de PWM 200 et le pipeline couleur
applique un gamma de 0,65 : trois PWM régulièrement répartis donneraient des
éclairements sans régularité. On utilise la courbe mesurée à l'étape 3.
"""
import argparse
import logging
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import (Dimmer, D405, CameraSettings, RESULTS_DIR,  # noqa: E402
                      write_csv, save_json, load_json, snap_exposure_to_pwm_period)
from vt_light import masks as masks_mod, metrics  # noqa: E402
from vt_light.measure import condition  # noqa: E402
from vt_light.photometry import OECF, ev, pick_levels  # noqa: E402

LEVEL_NAMES = ["haut", "moyen", "bas"]


def find_max_exposure(cam, dim, masks, detector, pwm_high, clip_frac_max,
                      p99_max, lo=300, hi=12000, count=20):
    """
    Plus longue exposition qui n'écrête pas l'objet au niveau de lumière haut.

    Balayage descendant sur une grille géométrique : la frontière d'écrêtage est
    franche (de 0 % à 25 % de pixels brûlés entre deux pas voisins), inutile de
    raffiner davantage que la période PWM.
    """
    dim.set_pwm(pwm_high)
    cam.flush(12)
    grid = sorted({snap_exposure_to_pwm_period(e)
                   for e in np.geomspace(lo, hi, count)}, reverse=True)
    rows, best = [], None
    for exposure in grid:
        cam.set_exposure(exposure, flush=8)
        row, _, _ = condition(cam, masks, detector, n_avg=3)
        row["exposure_us"] = exposure
        row["pwm"] = pwm_high
        rows.append(row)
        ok = (row["object_clipped_frac"] <= clip_frac_max
              and row["object_p99"] <= p99_max)
        print(f"    t = {exposure:6d} us | objet moy {row['object_mean_lum']:6.1f} "
              f"p99 {row['object_p99']:5.1f} écrêté {row['object_clipped_frac']*100:5.2f} % "
              f"| blanc {row['white_mean_lum']:6.1f} | {'OK' if ok else 'écrête'}")
        if ok and best is None:
            best = exposure
    return best, rows


def illuminance_at(cam, dim, masks, detector, oecf, pwm, base_exposure,
                   factors=(1, 2, 4, 8, 16, 0.5)):
    """
    Éclairement relatif d'un niveau, mesuré **dans la plage inversible** de la
    courbe de réponse.

    Nécessaire pour les niveaux sombres : au niveau bas, le blanc de référence
    tombe sous le plancher de la courbe et l'inversion rend NaN. L'éclairement
    étant indépendant de l'exposition, il suffit d'allonger celle-ci le temps de
    la mesure pour remonter le niveau dans la plage — la conversion redonne alors
    le même éclairement.

    Returns:
        (éclairement relatif, exposition employée, niveau du blanc)
    """
    dim.set_pwm(pwm)
    cam.flush(10)
    for factor in factors:
        exposure = snap_exposure_to_pwm_period(base_exposure * factor)
        if not 200 <= exposure <= 33000:
            continue
        cam.set_exposure(exposure, flush=8)
        colors, _ = cam.grab_stack(5)
        mean = colors.astype(np.float32).mean(axis=0).astype(np.uint8)
        value = metrics.patch_stats(mean, masks["white"])["mean_lum"]
        if bool(oecf.in_range(value)):
            return (float(oecf.relative_illuminance(value, exposure)),
                    exposure, value)
    return float("nan"), None, None


def find_pose_frame(cam, dim, masks, detector, exposure, pwms, min_rate=0.5,
                    n_frames=25):
    """
    Cherche le niveau de lumière qui décode le mieux les marqueurs, à exposition
    fixée.

    La détection n'est pas monotone en luminance : elle tient sur un plateau
    étroit puis tombe à zéro des deux côtés (voir ``25_markers.py``). On retient le
    **milieu du plateau** et non son meilleur point, pour ne pas s'installer au
    bord d'une falaise où une poussière ou un léger déplacement suffirait à perdre
    la vérité terrain.
    """
    cam.set_exposure(exposure, flush=8)
    rows = []
    for pwm in pwms:
        dim.set_pwm(pwm)
        cam.flush(10)
        row, _, _ = condition(cam, masks, detector, n_avg=3,
                              n_marker_frames=n_frames)
        row.update({"pwm": pwm, "exposure_us": exposure})
        rows.append(row)
        print(f"    PWM {pwm:3d} | blanc {row['white_mean_lum']:6.1f} "
              f"| ArUco moy {row['markers_mean']:4.2f} max {row['markers_max']} "
              f"| vus : {row['markers_ever'] or '-'}")

    plateau = [r for r in rows if r["markers_mean"] >= min_rate]
    if not plateau:
        return None, rows
    middle = plateau[len(plateau) // 2]
    return middle, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("-w", "--white-balance", type=int, required=True)
    ap.add_argument("-g", "--gain", type=int, default=16)
    ap.add_argument("--pwm-high", type=int, default=200,
                    help="niveau clair de référence ; au-delà la lampe ne monte "
                         "quasiment plus (mesuré étape 3)")
    ap.add_argument("--ev-steps", type=float, nargs="+", default=[1.0, 1.5, 2.0],
                    help="écarts entre niveaux à comparer, en diaphragmes")
    ap.add_argument("--headroom-ev", type=float, default=0.5,
                    help="marge d'exposition gardée pour les matériaux plus "
                         "spéculaires que celui monté (PLA argenté)")
    ap.add_argument("--clip-frac-max", type=float, default=0.001)
    ap.add_argument("--p99-max", type=float, default=240.0)
    ap.add_argument("--min-snr-white", type=float, default=20.0,
                    help="rapport signal/bruit minimal exigé au niveau bas")
    ap.add_argument("--pose-pwms", type=int, nargs="+",
                    default=[18, 21, 25, 30, 35, 40, 45, 50, 60],
                    help="niveaux candidats pour la prise de vue de pose ; le "
                         "plateau de détection ArUco se situe là (étape 3 bis)")
    ap.add_argument("--response", default=os.path.join(RESULTS_DIR, "20_response",
                                                       "response.json"))
    ap.add_argument("--masks", default=os.path.join(RESULTS_DIR, "masks.npz"))
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "30_choose"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.outdir, exist_ok=True)
    masks = masks_mod.load(args.masks)
    detector = metrics.make_detector()
    response = load_json(args.response)
    oecf = OECF.from_dict(response["oecf"])
    curve = response["illuminance_curve"]
    pwms = [c["pwm"] for c in curve]
    illums = [c["illuminance_rel"] for c in curve]

    settings = CameraSettings(exposure_us=4000, gain=args.gain,
                              white_balance_k=args.white_balance)
    candidates, level_rows = {}, []

    with Dimmer(args.port) as dim, D405(settings) as cam:
        print(f"\n--- A. Exposition maximale sans écrêtage, à PWM {args.pwm_high} ---")
        limit, scan = find_max_exposure(cam, dim, masks, detector, args.pwm_high,
                                        args.clip_frac_max, args.p99_max)
        write_csv(os.path.join(args.outdir, "exposure_scan.csv"), scan)
        if limit is None:
            print("  /!\\ aucune exposition testée n'évite l'écrêtage : "
                  "baisser --pwm-high.")
            return 1
        exposure = snap_exposure_to_pwm_period(limit / (2.0 ** args.headroom_ev))
        print(f"  limite mesurée : {limit} us")
        print(f"  exposition retenue : {exposure} us "
              f"({args.headroom_ev:.1f} diaphragme de marge, multiple de 200 us "
              f"pour rester en phase avec le PWM 5 kHz)")

        print(f"\n--- B. Trois niveaux, pour chaque pas testé, à t = {exposure} us ---")
        cam.set_exposure(exposure, flush=8)
        for ev_step in args.ev_steps:
            levels = pick_levels(pwms, illums, args.pwm_high, ev_step, n_levels=3)
            print(f"\n  pas de {ev_step:.1f} diaphragme -> PWM "
                  f"{[l['pwm'] for l in levels]}")
            measured = []
            for name, level in zip(LEVEL_NAMES, levels):
                dim.set_pwm(level["pwm"])
                cam.flush(10)
                row, color, _ = condition(cam, masks, detector, n_avg=5,
                                          n_marker_frames=15)
                row.update({"ev_step": ev_step, "level": name, "pwm": level["pwm"],
                            "exposure_us": exposure,
                            "illuminance_predicted": level["illuminance"]})
                # Vérification radiométrique : l'éclairement réellement reçu,
                # relu sur le blanc de référence via la courbe de réponse.
                row["illuminance_measured"] = float(
                    oecf.relative_illuminance(row["white_mean_lum"], exposure))
                measured.append(row)
                level_rows.append(row)
                cv2.imwrite(os.path.join(
                    args.outdir, f"ev{ev_step:.1f}_{name}_pwm{level['pwm']:03d}.png"),
                    color)
                print(f"    {name:6s} PWM {level['pwm']:3d} | blanc "
                      f"{row['white_mean_lum']:6.1f} (S/B {row['snr_white']:5.1f}) "
                      f"| objet {row['object_mean_lum']:6.1f} p99 {row['object_p99']:5.1f} "
                      f"écrêté {row['object_clipped_frac']*100:5.2f} % "
                      f"| ArUco {row['markers_mean']:4.1f} "
                      f"| profondeur {row['depth_fill_object']*100:5.1f} % "
                      f"rms {row['depth_rms_object_mm']:5.2f} mm")

            # Espacement réellement obtenu, mesuré et non supposé. Les niveaux dont
            # le blanc sort de la plage inversible sont re-mesurés à une exposition
            # allongée : sans cela, l'écart du niveau bas ne serait pas vérifié.
            for r in measured:
                if not np.isfinite(r["illuminance_measured"]):
                    illum, used_exposure, value = illuminance_at(
                        cam, dim, masks, detector, oecf, r["pwm"], exposure)
                    r["illuminance_measured"] = illum
                    r["illuminance_probe_exposure_us"] = used_exposure
                    if used_exposure:
                        print(f"    {r['level']:6s} : blanc hors plage à "
                              f"{exposure} us, re-mesuré à {used_exposure} us "
                              f"(blanc {value:.1f})")
            cam.set_exposure(exposure, flush=8)

            gaps = []
            for a, b in zip(measured, measured[1:]):
                if np.isfinite(a["illuminance_measured"]) and np.isfinite(
                        b["illuminance_measured"]):
                    gaps.append(ev(a["illuminance_measured"] / b["illuminance_measured"]))
                else:
                    gaps.append(float("nan"))
            top, low = measured[0], measured[-1]
            candidates[ev_step] = {
                "pwm": [r["pwm"] for r in measured],
                "measured_ev_gaps": gaps,
                "snr_white_low": low.get("snr_white"),
                "white_lum_low": low.get("white_mean_lum"),
                "clip_frac_high": top.get("object_clipped_frac"),
                "p99_high": top.get("object_p99"),
                "depth_fill_high": top.get("depth_fill_object"),
                "depth_fill_low": low.get("depth_fill_object"),
                "markers_mean_low": low.get("markers_mean"),
            }
            if gaps:
                print(f"    espacement mesuré : "
                      f"{' / '.join(f'{g:.2f}' for g in gaps)} diaphragme "
                      f"(consigne {ev_step:.1f})")
            else:
                print("    espacement non vérifiable : niveaux hors plage "
                      "inversible de la courbe de réponse")

        print(f"\n--- D. Prise de vue dédiée à la pose, à t = {exposure} us ---")
        pose, pose_rows = find_pose_frame(cam, dim, masks, detector, exposure,
                                          args.pose_pwms)
        write_csv(os.path.join(args.outdir, "pose_scan.csv"), pose_rows)
        dim.set_pwm(0)

    write_csv(os.path.join(args.outdir, "levels.csv"), level_rows)

    # ------------------------------------------------------------------ décision
    print("\n--- C. Décision ---")
    viable = []
    for ev_step, info in sorted(candidates.items()):
        reasons = []
        if info["clip_frac_high"] > args.clip_frac_max:
            reasons.append(f"écrête au niveau haut ({info['clip_frac_high']*100:.2f} %)")
        if info["snr_white_low"] is not None and info["snr_white_low"] < args.min_snr_white:
            reasons.append(f"S/B trop faible au niveau bas ({info['snr_white_low']:.1f} "
                           f"< {args.min_snr_white:.0f})")
        status = "exploitable" if not reasons else " ; ".join(reasons)
        print(f"    pas {ev_step:.1f} diaphragme -> PWM {info['pwm']} : {status}")
        if not reasons:
            viable.append(ev_step)

    if not viable:
        print("  /!\\ aucun pas ne satisfait les deux contraintes. "
              "Assouplir --min-snr-white ou réduire --headroom-ev.")
        return 1

    # Le plus grand pas exploitable : contraste maximal entre les trois conditions.
    chosen_step = max(viable)
    chosen = candidates[chosen_step]
    print(f"\n  Retenu : pas de {chosen_step:.1f} diaphragme, PWM {chosen['pwm']}, "
          f"exposition {exposure} us, balance des blancs {args.white_balance} K")

    if pose is None:
        print("  /!\\ aucun niveau ne décode les marqueurs : pas de vérité terrain "
              "angulaire possible dans cette pose caméra.")
    else:
        print(f"  Prise de vue de pose : PWM {pose['pwm']} "
              f"({pose['markers_mean']:.2f} marqueur en moyenne, "
              f"identifiants {pose['markers_ever'] or '-'}). "
              f"Aucun des trois niveaux du jeu de données n'est dans la fenêtre "
              f"de détection : la pose se mesure sur cette image-là.")

    profile = {
        "camera": CameraSettings(exposure_us=exposure, gain=args.gain,
                                 white_balance_k=args.white_balance).as_dict(),
        "resolution": [640, 480], "fps": 30,
        "levels": [
            {"name": name, "pwm": pwm}
            for name, pwm in zip(LEVEL_NAMES, chosen["pwm"])
        ],
        # Quatrième prise, hors jeu de données : elle ne sert qu'à mesurer l'angle
        # du plateau. Les marqueurs ne se décodent que sur un plateau étroit de
        # luminance, incompatible avec l'étalement voulu entre les trois niveaux.
        "pose_frame": None if pose is None else {
            "pwm": pose["pwm"],
            "exposure_us": exposure,
            "markers_mean": pose["markers_mean"],
            "markers_max": pose["markers_max"],
            "markers_ever": pose["markers_ever"],
            "white_mean_lum": pose["white_mean_lum"],
        },
        "ev_step": chosen_step,
        "measured_ev_gaps": chosen["measured_ev_gaps"],
        "exposure_clip_limit_us": limit,
        "headroom_ev": args.headroom_ev,
        "criteria": {"clip_frac_max": args.clip_frac_max, "p99_max": args.p99_max,
                     "min_snr_white": args.min_snr_white},
        "candidates": {str(k): v for k, v in candidates.items()},
        "provenance": {"response": args.response, "masks": args.masks},
    }
    save_json(os.path.join(RESULTS_DIR, "light_profile.json"), profile)
    print(f"  Profil écrit dans {os.path.join(RESULTS_DIR, 'light_profile.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
