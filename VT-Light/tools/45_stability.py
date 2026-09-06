#!/usr/bin/env python3
"""
Étape 5 bis — vérifier que la lumière est stable juste après un changement de niveau.

Le déroulé prévu au banc met la lampe au maximum pour faire tourner le plateau et
cadrer, puis redescend au niveau de la prise de vue. Deux façons dont cela peut
fausser le jeu de données :

1. **Latence du pipeline** : les images déjà en file ont été prises sous l'ancien
   éclairage. C'est le rôle du ``flush`` — on mesure ici combien d'images il faut
   réellement jeter.
2. **Dérive thermique de la LED** : une diode chaude rend moins de lumière à
   rapport cyclique égal. Après un séjour prolongé à PWM 255, l'éclairement peut
   donc continuer de bouger pendant que le composant se refroidit.

Le test reproduit exactement la séquence réelle : maintien au maximum, bascule au
niveau visé, puis suivi du blanc de référence image par image.
"""
import argparse
import logging
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vt_light import Dimmer, D405, RESULTS_DIR, write_csv, save_json  # noqa: E402
from vt_light import masks as masks_mod, metrics  # noqa: E402
from vt_light.profile import LightProfile, DEFAULT_PATH  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="/dev/ttyACM0")
    ap.add_argument("--profile", default=DEFAULT_PATH)
    ap.add_argument("--masks", default=os.path.join(RESULTS_DIR, "masks.npz"))
    ap.add_argument("--soak-sec", type=float, default=60.0,
                    help="durée passée au maximum avant la bascule, comme pendant "
                         "une rotation du plateau")
    ap.add_argument("--follow-sec", type=float, default=30.0)
    ap.add_argument("-o", "--outdir", default=os.path.join(RESULTS_DIR, "45_stability"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.outdir, exist_ok=True)
    white = masks_mod.load(args.masks)["white"]
    profile = LightProfile.load(args.profile)

    rows, summary = [], {}
    with Dimmer(args.port) as dim, D405(profile.camera_settings()) as cam:
        for level in profile.levels:
            name, pwm = level["name"], int(level["pwm"])
            print(f"\n--- Niveau {name} (PWM {pwm}) ---")
            print(f"    maintien à PWM 255 pendant {args.soak_sec:.0f} s...")
            dim.set_pwm(255)
            t0 = time.time()
            while time.time() - t0 < args.soak_sec:
                cam.grab()          # on garde le flux vivant, comme en usage réel

            dim.set_pwm(pwm)
            switch = time.time()
            series = []
            index = 0
            while time.time() - switch < args.follow_sec:
                color, _ = cam.grab()
                value = float(metrics.luminance(color)[white].mean())
                elapsed = time.time() - switch
                series.append((index, elapsed, value))
                rows.append({"level": name, "pwm": pwm, "frame": index,
                             "t_sec": elapsed, "white_mean_lum": value})
                index += 1

            values = np.array([s[2] for s in series])
            # Combien d'images portent encore l'ancien éclairage ? Elles sont bien
            # plus claires que le régime établi : on compte celles qui dépassent
            # largement la médiane de la fin de série.
            settled = float(np.median(values[len(values) // 2:]))
            stale = int(np.argmax(values < settled * 1.10)) if values[0] > settled * 1.10 else 0
            after = values[stale:]
            drift = float(np.polyfit(np.arange(len(after)), after, 1)[0] * len(after))
            spread = float(after.std())
            print(f"    images encore sous l'ancien éclairage : {stale}")
            print(f"    régime établi : blanc {settled:.2f} "
                  f"| dispersion {spread:.3f} | dérive sur {args.follow_sec:.0f} s "
                  f"{drift:+.3f} niveau ({drift / max(settled, 1e-6) * 100:+.2f} %)")
            summary[name] = {"pwm": pwm, "stale_frames": stale,
                             "settled_white_lum": settled,
                             "std_after_settle": spread,
                             "drift_over_follow": drift,
                             "drift_pct": drift / max(settled, 1e-6) * 100.0,
                             "n_frames": len(values)}
        dim.set_pwm(0)

    write_csv(os.path.join(args.outdir, "stability.csv"), rows)
    save_json(os.path.join(args.outdir, "stability.json"),
              {"soak_sec": args.soak_sec, "follow_sec": args.follow_sec,
               "levels": summary})

    print("\n--- Conclusion ---")
    worst_stale = max(s["stale_frames"] for s in summary.values())
    worst_drift = max(abs(s["drift_pct"]) for s in summary.values())
    print(f"    jeter au moins {worst_stale + 2} images après un changement de "
          f"niveau (mesuré : {worst_stale}, plus une marge).")
    if worst_drift < 1.0:
        print(f"    dérive thermique négligeable ({worst_drift:.2f} % au pire) : "
              f"la lampe peut rester au maximum entre les prises de vue.")
    else:
        print(f"    /!\\ dérive de {worst_drift:.2f} % après la bascule : prévoir "
              f"un temps de stabilisation, ou éviter le maximum entre les prises.")


if __name__ == "__main__":
    main()
