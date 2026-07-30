#!/usr/bin/env python3
"""
Ré-analyse hors banc des profondeurs déjà enregistrées.

`40_validate.py` enregistre la carte de profondeur et le masque de chaque niveau.
On peut donc corriger une métrique **après coup**, sur les trois matériaux, sans
remonter les objets — ce qui serait impossible autrement puisqu'un seul tient sur le
plateau à la fois.

Motif de cette ré-analyse : l'ajustement de plan global mesurait l'angle entre deux
faces dès que le masque en couvrait plusieurs, ce qui est le cas du cube translucide
posé sur un sommet. La version robuste (RANSAC) isole le plan dominant et rend la
fraction d'inliers, qui signale elle-même le cas multi-faces.
"""
import argparse
import glob
import logging
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import RESULTS_DIR, write_csv, load_json  # noqa: E402
from vt_light import masks as masks_mod, metrics  # noqa: E402


def planar_subset(depth_path, mask, depth_scale, intrinsics, min_px=2000):
    """
    Réduit un masque à la face plane dominante de l'objet.

    Rend tous les matériaux comparables à surface équivalente : les cubes posés à
    plat n'exposent qu'une face à la lampe, celui posé sur un sommet en expose
    deux, et un taux de remplissage moyenné sur deux faces inégalement éclairées
    ne se compare pas à celui d'une seule.

    Returns:
        masque booléen restreint, ou None si l'opération n'a pas de sens ici.
    """
    if not os.path.exists(depth_path):
        return None
    depth = cv2.imread(depth_path, cv2.IMREAD_UNCHANGED)
    inside = mask & (depth > 0)
    if inside.sum() < min_px:
        return None
    vv, uu = np.nonzero(inside)
    z = depth[inside].astype(np.float64) * depth_scale
    X = (uu - intrinsics["cx"]) * z / intrinsics["fx"]
    Y = (vv - intrinsics["cy"]) * z / intrinsics["fy"]
    inliers, _ = metrics._ransac_plane(X, Y, z)
    if inliers is None or inliers.sum() < min_px:
        return None
    out = np.zeros_like(mask)
    out[vv[inliers], uu[inliers]] = True
    # Refermer les trous laissés par les pixels sans profondeur : le masque doit
    # rester une surface, sinon le taux de remplissage vaudrait 100 % par
    # construction.
    out = cv2.morphologyEx(out.astype(np.uint8), cv2.MORPH_CLOSE,
                           np.ones((9, 9), np.uint8)).astype(bool)
    return out & mask


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate-dir", default=os.path.join(RESULTS_DIR, "40_validate"))
    ap.add_argument("--check", default=os.path.join(RESULTS_DIR, "00_check", "check.json"))
    ap.add_argument("-o", "--out", default=os.path.join(RESULTS_DIR,
                                                        "depth_comparison.csv"))
    ap.add_argument("--planar", action="store_true", default=True,
                    help="restreindre chaque masque à sa face plane dominante, "
                         "pour comparer les matériaux à surface équivalente")
    ap.add_argument("--no-planar", dest="planar", action="store_false")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    check = load_json(args.check)
    intrinsics, depth_scale = check["intrinsics"], check["depth_scale"]

    rows = []
    for objdir in sorted(glob.glob(os.path.join(args.validate_dir, "*"))):
        if not os.path.isdir(objdir):
            continue
        obj = os.path.basename(objdir)
        mask_path = os.path.join(objdir, "masks.npz")
        if not os.path.exists(mask_path):
            continue
        mask = masks_mod.load(mask_path).get("object")
        if mask is None:
            continue
        note = ""
        if args.planar:
            # Restreindre le masque à sa **face dominante**, déterminée sur le
            # niveau le plus clair puis appliquée telle quelle aux autres. Sans
            # cela le taux de remplissage du translucide porte sur deux faces
            # inégalement éclairées et ne se compare pas à celui des cubes posés
            # à plat, dont le masque ne couvre qu'une face.
            restricted = planar_subset(os.path.join(objdir, "haut_depth.png"),
                                       mask, depth_scale, intrinsics)
            if restricted is not None:
                note = f", face dominante {int(restricted.sum())} px"
                mask = restricted

        print(f"\n--- {obj} ({int(mask.sum())} px de masque{note}) ---")
        for level in ("haut", "moyen", "bas", "pose"):
            path = os.path.join(objdir, f"{level}_depth.png")
            if not os.path.exists(path):
                continue
            depth = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            stats = metrics.depth_stats(depth, depth_scale, intrinsics,
                                        mask=mask, fit_plane=True)
            rows.append({"object": obj, "level": level, **stats})
            print(f"    {level:6s} | rempli {stats['fill_ratio']*100:5.1f} % "
                  f"| plan dominant {stats['plane_inlier_frac']*100:5.1f} % du masque "
                  f"| bruit {stats['plane_rms_mm']:6.2f} mm "
                  f"| ajustement global {stats['plane_rms_all_mm']:7.2f} mm")

    write_csv(args.out, rows)

    print("\n--- Lecture ---")
    print("    Un écart important entre « bruit » et « ajustement global », ou une "
          "fraction d'inliers nettement sous 100 %, indique un masque couvrant "
          "plusieurs faces : seule la colonne « bruit » est alors comparable "
          "d'un matériau à l'autre.")


if __name__ == "__main__":
    main()
