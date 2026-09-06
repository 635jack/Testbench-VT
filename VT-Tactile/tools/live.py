#!/usr/bin/env python3
"""
live.py — pression des onze zones tactiles, en direct dans le terminal.

    sudo python3 -m tools.live

Chaque zone affiche une barre par canal, la pression maximale, et pour les
doigts la force normale, la force tangentielle, la direction et la proximité.
La main est réveillée automatiquement (alimentation + homing) : sans ça elle
n'émet rien.

Touches, pendant l'affichage :

    Entrée   refaire le zéro (main au repos, rien en contact)
    r        idem
    q        quitter

Le zéro est pris au démarrage. Les lignes de base diffèrent beaucoup d'un doigt
à l'autre, donc sans lui les zones ne sont pas comparables entre elles.
"""
from __future__ import annotations

import argparse
import logging
import select
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vt_tactile.bus import BusError, Hand  # noqa: E402
from vt_tactile.tpdo import (  # noqa: E402
    PALM,
    PALM_SILENT,
    ZONE_NAMES,
    TactileReader,
)

BLOCKS = " ▁▂▃▄▅▆▇█"
CLEAR = "\033[2J\033[H"
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED = "\033[32m", "\033[33m", "\033[31m"


def bar(v: float) -> str:
    return BLOCKS[min(len(BLOCKS) - 1, max(0, int(v * (len(BLOCKS) - 1) + 0.5)))]


def colour(v: float) -> str:
    return RED if v > 0.66 else YELLOW if v > 0.20 else GREEN if v > 0.02 else DIM


def num(v: float | None, fmt: str = "6.2f") -> str:
    return "     —" if v is None else format(v, fmt)


def render(state, rate: float, zeroed: bool, hidden_palm: bool) -> str:
    out = [f"{BOLD}Pression tactile DH116{RESET}   {rate:5.1f} trames/s   "
           + ("zéro fait" if zeroed else f"{YELLOW}zéro non fait{RESET}"),
           "",
           f"{'zone':<12} {'canaux':<7} {'max':>6}  {'force N':>8} {'force T':>8}"
           f" {'dir':>6} {'prox':>6} {'approche':>9}",
           "─" * 72]

    previous = None
    for name in ZONE_NAMES:
        if name == PALM:
            continue
        z = state.zones.get(name)
        if z is None:
            continue
        finger = name.split(".")[0]
        if previous is not None and finger != previous:
            out.append("")
        previous = finger
        bars = "".join(f"{colour(v)}{bar(v)}" for v in z.pressure) + RESET
        m = z.pressure_max
        out.append(f"{z.name:<12} {bars:<7}{'':6} {colour(m)}{m:6.2f}{RESET} "
                   f"{num(z.normal_force):>8} {num(z.tangential_force):>8}"
                   f" {num(z.direction, '6.0f'):>6} {num(z.proximity):>6}"
                   f" {num(z.self_proximity):>9}")
    out.append("")

    palm = state.zones.get(PALM)
    if palm is not None:
        cells = []
        for i, v in enumerate(palm.pressure):
            if hidden_palm and i in PALM_SILENT:
                cells.append(f"{DIM}·{RESET}")
            else:
                cells.append(f"{colour(v)}{bar(v)}{RESET}")
        m = palm.pressure_max
        out += [f"{'palm':<12} {''.join(cells)}  {colour(m)}{m:.2f}{RESET}",
                f"{DIM}             les points « · » n'ont jamais répondu au "
                f"balayage du 2026-07-31{RESET}"]

    touched = state.in_contact
    out += ["", f"{DIM}« approche » n'existe que sur le pouce et l'auriculaire : "
            f"un seul capteur, dont le 6e canal est une proximité.{RESET}",
            f"en contact : {', '.join(touched) if touched else '—'}",
            f"{DIM}Entrée/r = refaire le zéro    q = quitter{RESET}"]
    return "\n".join(out)


#: Passe à vrai quand l'entrée standard est épuisée : on cesse alors de
#: l'interroger, sinon ``select`` la signale lisible à chaque tour.
_stdin_epuise = False


def key_pressed() -> str | None:
    """
    Lit une touche si elle est disponible, sans bloquer l'affichage.

    ``readline()`` rend ``""`` en **fin de fichier** et ``"\n"`` pour un appui
    sur Entrée. Les confondre faisait refaire le zéro en boucle dès que l'entrée
    standard n'était pas un terminal — script, ``nohup``, ``ssh`` sans ``-t`` —
    et l'affichage restait vide en permanence.
    """
    global _stdin_epuise
    if _stdin_epuise:
        return None
    if select.select([sys.stdin], [], [], 0)[0]:
        ligne = sys.stdin.readline()
        if ligne == "":
            _stdin_epuise = True
            return None
        return ligne.strip().lower()
    return None


def take_zero(hand: Hand, reader: TactileReader, seconds: float) -> None:
    print(f"Zéro en cours ({seconds:.0f} s) — ne touchez pas la main…")
    frames = hand.collect(seconds)
    if not frames:
        raise BusError("aucune trame tactile : la main n'a pas été réveillée")
    reader.zero(frames)
    print(f"Zéro fait sur {len(frames)} trames.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zero-seconds", type=float, default=2.0)
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--iface", type=int, default=None)
    ap.add_argument("--sdk-dir", default=None)
    ap.add_argument("--no-wake", action="store_true",
                    help="ne pas réveiller la main (elle restera muette)")
    ap.add_argument("--all-palm", action="store_true",
                    help="afficher aussi les points de paume connus muets")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    hand = Hand(sdk_dir=args.sdk_dir)
    reader = TactileReader()

    try:
        hand.connect(iface_index=args.iface)
        if not args.no_wake:
            print("Réveil de la main (alimentation + homing, ~10 s)…")
            hand.wake()
        take_zero(hand, reader, args.zero_seconds)

        period = 1.0 / max(args.fps, 1.0)
        last, seen, t0 = None, 0, time.time()
        rate = 0.0

        while True:
            key = key_pressed()
            if key == "q":
                break
            if key in ("", "r"):
                take_zero(hand, reader, args.zero_seconds)
                t0, seen = time.time(), 0

            raw = hand.latest_tactile()
            if raw is not None:
                if raw != last:
                    last, seen = raw, seen + 1
                state = reader.decode(raw)
                elapsed = time.time() - t0
                if elapsed > 1.0:
                    rate = seen / elapsed
                print(CLEAR + render(state, rate, reader.zeroed,
                                     not args.all_palm), flush=True)
            time.sleep(period)

    except KeyboardInterrupt:
        pass
    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        return 2
    finally:
        if not args.no_wake:
            hand.release()
        hand.close()
        print("\nMain relâchée, bus fermé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
