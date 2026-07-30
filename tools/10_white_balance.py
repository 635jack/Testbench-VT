#!/usr/bin/env python3
"""
Étape 2 — régler la balance des blancs sur le PLA blanc des marqueurs.

Pourquoi c'est nécessaire : le code existant coupe l'auto-balance sans écrire de
valeur, ce qui laisse la caméra sur la dernière valeur trouvée par l'automatisme
— différente à chaque démarrage. Les images d'un même jeu de données ne sont donc
pas colorimétriquement comparables.

Méthode : le PLA blanc des marqueurs est mat et de réflectance neutre. Sous la
bonne balance des blancs, ses trois canaux doivent être égaux. On balaie
2800-6500 K et on retient la valeur qui minimise le pire écart entre R/G et B/G.

Le balayage est fait à **deux niveaux de lumière** : si les deux optima
concordent, le réglage est bien une propriété de la lampe et non un artefact du
niveau choisi.
"""
import argparse
import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import Dimmer, D405, CameraSettings, RESULTS_DIR, write_csv, save_json  # noqa: E402
from vt_light import masks as masks_mod, metrics  # noqa: E402


def sweep(cam, dim, pwm, mask, kelvins, n_avg=3):
    dim.set_pwm(pwm)
    cam.flush(12)
    rows = []
    for kelvin in kelvins:
        cam.set_white_balance(kelvin, flush=8)
        colors, _ = cam.grab_stack(n_avg)
        mean = colors.astype(np.float32).mean(axis=0)
        stats = metrics.patch_stats(mean.astype(np.uint8), mask)
        rows.append({"pwm": pwm, "white_balance_k": kelvin, **stats})
    return rows


def best_of(rows):
    """
    Optimum au sens du moindre écart de neutralité, affiné par une parabole sur
    les trois points les plus bas — le pas de 100 K est plus grossier que la
    répétabilité de la mesure, autant l'interpoler.
    """
    usable = [r for r in rows if r.get("n_px") and np.isfinite(r["neutrality_err"])]
    if not usable:
        return None, None
    order = sorted(usable, key=lambda r: r["neutrality_err"])
    coarse = order[0]["white_balance_k"]

    ks = np.array([r["white_balance_k"] for r in usable], float)
    errs = np.array([r["neutrality_err"] for r in usable], float)
    idx = int(np.argmin(errs))
    if 0 < idx < len(ks) - 1:
        a, b, c = errs[idx - 1], errs[idx], errs[idx + 1]
        denom = a - 2 * b + c
        if abs(denom) > 1e-9:
            shift = 0.5 * (a - c) / denom          # en pas de grille
            fine = ks[idx] + shift * (ks[idx + 1] - ks[idx])
            fine = float(np.clip(fine, ks[0], ks[-1]))
            return coarse, int(round(fine / 10.0) * 10)
    return coarse, coarse


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("-e", "--exposure", type=int, default=4000)
    ap.add_argument("-g", "--gain", type=int, default=16)
    ap.add_argument("--pwm", type=int, nargs="+", default=[255, 128])
    ap.add_argument("--step", type=int, default=100, help="pas du balayage, en K")
    ap.add_argument("--masks", default=os.path.join(RESULTS_DIR, "masks.npz"))
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "10_white_balance"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.outdir, exist_ok=True)
    white = masks_mod.load(args.masks)["white"]

    kelvins = list(range(2800, 6501, args.step))
    all_rows, optima = [], {}
    with Dimmer(args.port) as dim, D405(CameraSettings(
            exposure_us=args.exposure, gain=args.gain)) as cam:
        for pwm in args.pwm:
            print(f"\n--- PWM {pwm} : {len(kelvins)} valeurs de 2800 à 6500 K ---")
            rows = sweep(cam, dim, pwm, white, kelvins)
            all_rows += rows
            coarse, fine = best_of(rows)
            optima[pwm] = {"coarse_k": coarse, "fine_k": fine}
            for r in rows[::4]:
                print(f"    {r['white_balance_k']:5d} K  R/G {r['r_over_g']:.3f}  "
                      f"B/G {r['b_over_g']:.3f}  err {r['neutrality_err']:.3f}  "
                      f"lum {r['mean_lum']:6.1f}")
            print(f"  optimum : {coarse} K sur la grille, {fine} K après interpolation")
        dim.set_pwm(0)

    write_csv(os.path.join(args.outdir, "wb_sweep.csv"), all_rows)

    fines = [o["fine_k"] for o in optima.values() if o["fine_k"]]
    chosen = int(round(float(np.mean(fines)) / 10.0) * 10) if fines else None
    spread = (max(fines) - min(fines)) if len(fines) > 1 else 0
    print(f"\nBalance des blancs retenue : {chosen} K "
          f"(écart entre niveaux : {spread} K)")
    if spread > 400:
        print("  /!\\ les deux niveaux ne concordent pas : mesure peu fiable "
              "(masque blanc trop petit, ou écrêtage).")

    save_json(os.path.join(args.outdir, "white_balance.json"),
              {"chosen_k": chosen, "per_pwm": optima, "spread_k": spread,
               "exposure_us": args.exposure, "gain": args.gain,
               "n_white_px": int(white.sum())})


if __name__ == "__main__":
    main()
