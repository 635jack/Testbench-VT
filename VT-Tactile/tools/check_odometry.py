#!/usr/bin/env python3
"""
check_odometry.py — la position rendue par le SDK est-elle la vraie ?

    sudo python3 -m tools.check_odometry --motor 6

On déplace un seul doigt sur une trajectoire connue, et on enregistre à chaque
pas **deux** mesures indépendantes : ce que ``get_now_position`` répond, et ce
que la trame d'état moteur porte sur le fil. On cherche ensuite lequel des 27
créneaux de la trame suit le doigt — s'il en existe un.

Trois issues possibles, et elles n'appellent pas les mêmes suites :

* un créneau colle au SDK à quelques counts près → l'odométrie est bonne, et
  c'est ailleurs qu'il faut chercher ;
* un créneau suit le doigt mais avec un décalage constant → le zéro du homing
  a bougé, et toutes les positions rapportées sont fausses d'autant ;
* aucun créneau ne suit → le SDK invente, ou la trame se lit autrement.

Aucune détection de contact n'est active : le doigt va jusqu'à la consigne ou
jusqu'à ce qu'il cale, protégé seulement par le plafond de couple.
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile.bus import BusError, Hand  # noqa: E402
from vt_tactile.tpdo import MOTOR_SLOT_COUNT, decode_motor_frame  # noqa: E402

BOLD, RESET = "\033[1m", "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motor", type=int, default=6)
    ap.add_argument("--to", type=int, default=4000, help="position visée")
    ap.add_argument("--max-current", type=int, default=hw.APPROACH_CURRENT)
    ap.add_argument("--seconds", type=float, default=14.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.motor in hw.BROKEN_MOTORS:
        print(f"{hw.MOTOR_NAMES[args.motor]} est en panne.", file=sys.stderr)
        return 2

    hand = Hand()
    samples: list[tuple[float, int, list[int]]] = []
    try:
        hand.connect()
        hand.wake()
        hand.open_hand()
        m = args.motor
        print(f"\n{BOLD}{hw.MOTOR_NAMES[m]}{RESET} : 0 → {args.to}, "
              f"plafond {args.max_current} ‰\n")
        print(f"  {'t':>5} {'consigne':>9} {'SDK':>7} {'courant':>8}")

        t0 = time.time()
        target = 0
        while time.time() - t0 < args.seconds:
            pos = hand.positions((m,))[m]
            target = min(max(pos + 400, target + 25), args.to)
            hand.command({m: target}, hw.VELOCITY_CLOSE, args.max_current)
            time.sleep(0.20)

            pos = hand.positions((m,))[m]
            cur = hand.currents((m,))[m]
            frame = hand.latest_motor()
            if frame is None:
                continue
            slots = [s["position"] for s in decode_motor_frame(frame)["slots"]]
            samples.append((round(time.time() - t0, 2), pos, slots))
            if len(samples) % 5 == 1:
                print(f"  {samples[-1][0]:>5.1f} {target:>9} {pos:>7} {cur:>7}‰")
            if pos >= args.to - 30:
                break

        print(f"\n  {len(samples)} échantillons, course SDK "
              f"{samples[0][1]} → {samples[-1][1]}" if samples else "  aucun échantillon")
    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        return 2
    finally:
        try:
            hand.release()
        except Exception:  # noqa: BLE001
            pass
        hand.close()

    if len(samples) < 6:
        print("Trop peu d'échantillons pour conclure.", file=sys.stderr)
        return 1

    sdk = [s[1] for s in samples]
    span = max(sdk) - min(sdk)
    print(f"\n{BOLD}Créneaux de la trame qui suivent le doigt{RESET}  "
          f"(course SDK {span} counts)\n")
    print(f"  {'créneau':>8} {'course':>8} {'écart moyen au SDK':>20} {'verdict'}")
    found = False
    for i in range(MOTOR_SLOT_COUNT):
        col = [s[2][i] for s in samples]
        if max(col) - min(col) < max(50, span * 0.5):
            continue                      # ce créneau ne bouge pas comme le doigt
        diff = [c - p for c, p in zip(col, sdk)]
        spread = max(diff) - min(diff)
        offset = statistics.median(diff)
        verdict = ("identique au SDK" if abs(offset) < 30 and spread < 60
                   else f"décalé de {offset:+.0f}" if spread < 60
                   else "suit de loin")
        print(f"  {i:>8} {max(col) - min(col):>8} {offset:>19.0f} {verdict}")
        found = True
    if not found:
        print("  Aucun créneau ne suit le doigt. Soit la trame se lit autrement,")
        print("  soit la position du SDK ne vient pas de là.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
