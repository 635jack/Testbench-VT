#!/usr/bin/env python3
"""
envelop.py — referme la main sur un objet et enregistre le tactile.

    sudo python3 -m tools.envelop --object "cylindre, paume vers le haut"

Déroulé : réveil de la main, ouverture, remise à zéro du tactile **objet déjà
posé**, puis fermeture progressive doigt par doigt avec arrêt au contact, un
temps de maintien, et l'ouverture. La trajectoire complète part dans un JSON.

Les cinq fléchisseurs referment sur l'objet, pouce compris. Le pivot du pouce
n'en est pas un : il l'amène en opposition, et sans lui la flexion du pouce se
referme à côté de l'objet.

Répétition sans fermeture — le réveil et l'ouverture bougent quand même les
doigts, mais aucune consigne de fermeture n'est envoyée :

    sudo python3 -m tools.envelop --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile.bus import BusError, Hand  # noqa: E402
from vt_tactile.dataset import write_raw_stream  # noqa: E402
from vt_tactile.envelop import EnvelopConfig, envelop, summarise  # noqa: E402
from vt_tactile.tpdo import TactileReader  # noqa: E402

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def live_line(sample, state, contacts) -> None:
    """Une ligne par itération : où en est chaque doigt."""
    cells = []
    for m in hw.WORKING_FLEXORS:
        name = hw.MOTOR_NAMES[m][:5]
        zones = [z for z in hw.zones_of(m) if z in state.zones]
        pmax = max((state[z].pressure_max for z in zones), default=0.0)
        done = hw.MOTOR_NAMES[m] in contacts
        mark = "✓" if done else " "
        cells.append(f"{name}{mark}{sample['positions'][str(m)]:>5} "
                     f"{sample['currents'][str(m)]:>4}‰ p{pmax:4.2f}")
    print(f"\r  t={sample['t']:5.1f}s  " + " │ ".join(cells), end="", flush=True)


def preflight(hand: Hand) -> dict:
    """Vérifie l'état avant de bouger quoi que ce soit."""
    pos = hand.positions()
    cur = hand.currents()
    print("\nÉtat des moteurs avant fermeture :")
    for m in hw.MOTOR_IDS:
        flag = "  (exclu : panne)" if m in hw.BROKEN_MOTORS else ""
        print(f"   {m} {hw.MOTOR_NAMES[m]:<16} position {pos[m]:>6}  "
              f"courant {cur[m]:>4} ‰{flag}")
    return {"positions": {str(m): pos[m] for m in pos},
            "currents": {str(m): cur[m] for m in cur}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--object", default="inconnu",
                    help="description de l'objet, conservée dans le JSON")
    ap.add_argument("--out", type=Path, default=None,
                    help="fichier de sortie (défaut : grasps/<horodatage>.json)")
    ap.add_argument("--lead", type=int, default=400,
                    help="avance max de la consigne sur la position réelle")
    ap.add_argument("--max-position", type=int, default=hw.POSITION_MAX)
    ap.add_argument("--max-current", type=int, default=hw.GRASP_CURRENT,
                    help="plafond de couple pendant l'approche, en pour-mille")
    ap.add_argument("--pressure", type=float, default=0.03,
                    help="seuil tactile de détection de contact")
    ap.add_argument("--motor-current", action="append", default=[],
                    metavar="MOTEUR:VALEUR",
                    help="plafond de couple d'un seul moteur, ex. 4:600 pour "
                         "l'annulaire ; répétable")
    ap.add_argument("--current-margin", type=int, default=450,
                    help="marge au-dessus du courant libre au-delà de laquelle "
                         "un doigt est dit en appui. Trop basse, elle arrête le "
                         "doigt dès qu'il commence à pousser, donc avant qu'il "
                         "n'excite son capteur tactile")
    ap.add_argument("--seat", type=int, default=0,
                    help="counts parcourus après le premier contact tactile")
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--thumb-pivot", type=int, default=hw.THUMB_OPPOSITION,
                    help=f"pivot du pouce avant fermeture (défaut "
                         f"{hw.THUMB_OPPOSITION} = opposition ; 0 = pouce relevé)")
    ap.add_argument("--zero-seconds", type=float, default=2.0)
    ap.add_argument("--iface", type=int, default=None)
    ap.add_argument("--sdk-dir", default=None)
    ap.add_argument("--no-raw", action="store_true",
                    help="ne pas enregistrer le flux EtherCAT brut (déconseillé : "
                         "c'est lui qui rend la palpation réinterprétable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="s'arrêter après le zéro tactile, sans lancer la fermeture")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.max_current > hw.FULL_CURRENT:
        print(f"Courant demandé ({args.max_current}) au-dessus du plafond "
              f"variateur ({hw.FULL_CURRENT}).", file=sys.stderr)
        return 2

    per_motor = {}
    for spec in args.motor_current:
        try:
            m, v = spec.split(":")
            per_motor[int(m)] = int(v)
        except ValueError:
            print(f"--motor-current attend MOTEUR:VALEUR, reçu {spec!r}",
                  file=sys.stderr)
            return 2
    if any(v > hw.FULL_CURRENT for v in per_motor.values()):
        print(f"Courant au-dessus du plafond variateur ({hw.FULL_CURRENT}).",
              file=sys.stderr)
        return 2

    cfg = EnvelopConfig(
        current_by_motor=per_motor,
        lead=args.lead, max_position=args.max_position,
        max_current=args.max_current, pressure_threshold=args.pressure,
        seat_counts=args.seat, hold_seconds=args.hold, timeout=args.timeout,
        thumb_pivot=args.thumb_pivot, current_margin=args.current_margin,
    )

    out = args.out or Path("grasps") / (
        datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    hand = Hand(sdk_dir=args.sdk_dir)
    reader = TactileReader()
    record: dict = {
        "object": args.object,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "motors": {str(m): hw.MOTOR_NAMES[m] for m in hw.MOTOR_IDS},
        "excluded_motors": [hw.MOTOR_NAMES[m] for m in hw.BROKEN_MOTORS],
    }

    try:
        hand.connect(iface_index=args.iface)
        print("Réveil de la main (alimentation + homing, ~10 s)…")
        hand.wake()

        # Enregistrer **avant** l'ouverture et le zéro : la ligne de base au
        # repos et la pose de départ font partie de ce qu'il faudra pouvoir
        # réinterpréter, pas seulement la fermeture.
        if not args.no_raw:
            hand.start_recording()

        print("Ouverture avant mise à zéro…")
        if not hand.open_hand():
            print("La main ne s'ouvre pas complètement : on s'arrête là plutôt "
                  "que de refermer depuis une pose inconnue.", file=sys.stderr)
            return 3

        record["preflight"] = preflight(hand)

        print(f"\nZéro tactile ({args.zero_seconds:.0f} s) — **l'objet est déjà "
              f"posé**, on mesure donc les variations dues à l'enveloppement, "
              f"pas le poids au repos.")
        frames = hand.collect(args.zero_seconds)
        if not frames:
            raise BusError("aucune trame tactile")
        reader.zero(frames)
        record["baseline_frames"] = len(frames)
        record["baseline"] = reader.baseline
        print(f"Zéro fait sur {len(frames)} trames. Lignes de base "
              f"(max par zone, en counts bruts) :")
        for zone, vals in reader.baseline.items():
            if max(vals, default=0) > 0:
                print(f"   {zone:<12} max {max(vals):5.1f}   {vals}")
        print("   (les zones absentes sont à zéro sur tous leurs canaux)")

        if args.dry_run:
            print("\n--dry-run : la fermeture n'a pas été lancée. Réveil, "
                  "ouverture, état moteur et zéro tactile sont vérifiés.")
            return 0

        print(f"\n{BOLD}Fermeture{RESET} — Ctrl-C interrompt et rouvre.\n")
        result = envelop(hand, reader, cfg, on_step=live_line)
        print("\n")
        print(summarise(result))
        record["result"] = result.to_dict()

    except KeyboardInterrupt:
        print("\nInterrompu — ouverture en cours…")
        try:
            hand.open_hand()
        except Exception:  # noqa: BLE001
            pass
        record["interrupted"] = True
    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        record["error"] = str(e)
        return 2
    finally:
        # Le flux brut est vidé en premier : c'est la pièce qui ne se rejoue
        # pas, elle ne doit pas se perdre sur une sortie par erreur.
        if not args.no_raw:
            try:
                frames = hand.stop_recording()
                if frames:
                    info = write_raw_stream(out.parent, out.stem + "_raw",
                                            frames)
                    record["raw_stream"] = info
                    print(f"\nFlux brut : {info['count']} trames "
                          f"{info['frame_types']} sur {info['span_s']} s "
                          f"({info['rate_hz']}/s)")
            except Exception as e:  # noqa: BLE001
                print(f"Flux brut non enregistré : {e}", file=sys.stderr)
        try:
            hand.release()
        except Exception:  # noqa: BLE001
            pass
        hand.close()
        if "result" in record or "interrupted" in record or "error" in record:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(record, indent=2, ensure_ascii=False))
            print(f"\nEnregistré : {out}")
        print("Main relâchée, bus fermé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
