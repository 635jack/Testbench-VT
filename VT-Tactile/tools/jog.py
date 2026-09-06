#!/usr/bin/env python3
"""
jog.py — déplacer un moteur à la main, pour trouver une pose à l'œil.

    sudo python3 -m tools.jog                # pivot du pouce par défaut
    sudo python3 -m tools.jog --motor 6      # index

Sert à ce qu'aucune mesure ne peut donner : la valeur de consigne qui *a l'air*
juste. On tape une position, on regarde la main, on ajuste. À la sortie, l'outil
rappelle la position atteinte pour qu'elle soit réutilisable telle quelle.

Commandes, une par ligne :

    4000     aller à la position absolue 4000
    +500     avancer de 500 counts
    -500     reculer de 500
    0        revenir à l'ouverture
    p        relire position et courant
    q        quitter (le moteur reste où il est, le couple est coupé)

Le courant est plafonné bas et la position bornée : le moteur cale plutôt que
de forcer. Un moteur inscrit dans `hw.BROKEN_MOTORS` est refusé.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile.bus import BusError, Hand  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def show(hand: Hand, motor: int) -> int:
    pos = hand.positions((motor,))[motor]
    cur = hand.currents((motor,))[motor]
    print(f"   {hw.MOTOR_NAMES[motor]} : position {BOLD}{pos}{RESET}, "
          f"courant {cur} ‰")
    return pos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motor", type=int, default=hw.THUMB_PIVOT,
                    help=f"moteur à déplacer (défaut {hw.THUMB_PIVOT}, "
                         f"pivot du pouce)")
    ap.add_argument("--max-current", type=int, default=hw.APPROACH_CURRENT)
    ap.add_argument("--max-position", type=int, default=hw.POSITION_MAX)
    ap.add_argument("--velocity", type=int, default=hw.VELOCITY_CLOSE)
    ap.add_argument("--iface", type=int, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    if args.motor not in hw.MOTOR_IDS:
        print(f"Moteur {args.motor} inconnu.", file=sys.stderr)
        return 2
    if args.motor in hw.BROKEN_MOTORS:
        print(f"{hw.MOTOR_NAMES[args.motor]} est en panne : il n'exécute aucune "
              f"consigne de position. Refus.", file=sys.stderr)
        return 2

    hand = Hand()
    motor = args.motor
    try:
        hand.connect(iface_index=args.iface)
        print("Réveil de la main (~10 s)…")
        hand.wake()

        print(f"\n{BOLD}Déplacement de {hw.MOTOR_NAMES[motor]}{RESET} "
              f"(moteur {motor}), bornes 0 à {args.max_position}, "
              f"courant plafonné à {args.max_current} ‰")
        print(f"{DIM}   nombre = position absolue · +N / -N = relatif · "
              f"0 = ouverture · p = relire · q = quitter{RESET}\n")
        pos = show(hand, motor)

        while True:
            try:
                line = input("jog> ").strip().lower()
            except EOFError:
                break
            if not line:
                continue
            if line == "q":
                break
            if line == "p":
                pos = show(hand, motor)
                continue

            try:
                target = (pos + int(line) if line[0] in "+-" else int(line))
            except ValueError:
                print("   Entrée non comprise. Exemples : 4000, +500, -500, 0, p, q")
                continue

            target = max(0, min(target, args.max_position))
            hand.command({motor: target}, args.velocity, args.max_current)
            time.sleep(max(1.0, abs(target - pos) / max(args.velocity, 1) + 0.4))
            pos = show(hand, motor)

        print(f"\n{BOLD}Position retenue : {pos}{RESET}")
        print(f"Pour l'utiliser à la fermeture :\n"
              f"   sudo python3 -m tools.envelop --thumb-pivot {pos} --seat 150")

    except KeyboardInterrupt:
        print()
    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        return 2
    finally:
        hand.release()
        hand.close()
        print("Couple coupé, bus fermé. Le moteur reste où il est.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
