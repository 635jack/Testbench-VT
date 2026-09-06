#!/usr/bin/env python3
"""
check_moteurs.py — le pilotage des moteurs répond-il ? Réponse en un tableau.

    sudo python3 -m tools.check_moteurs                 # les cinq moteurs valides
    sudo python3 -m tools.check_moteurs --motor 6       # l'index seul
    sudo python3 -m tools.check_moteurs --repeat 3      # trois allers-retours chacun

Sert à trancher sans ambiguïté quand un doute s'installe : la main ne bouge pas,
et rien ne dit si c'est la commande, le variateur, l'interface web ou la
mécanique. Cet outil ne touche ni à ROS, ni au serveur web, ni au tactile — il
va au bus, commande, et regarde ce qui se passe.

Ce qu'il vérifie vraiment, et qui compte : la position **et** le courant.
Une position qui progresse sans courant serait un codeur qui avance à vide ;
un courant qui monte sans progression serait un doigt bloqué. Les deux se
lisent dans le tableau.

Les moteurs inscrits dans `hw.BROKEN_MOTORS` sont écartés — la liste est vide
par défaut et sert à sortir un moteur qui lâche en campagne.

Sortie 0 si tout répond, 1 sinon — utilisable dans un script.
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

#: Tolérance sur la position atteinte. Le variateur s'arrête à quelques counts
#: de la consigne sans que ce soit un défaut.
TOLERANCE = 60
#: En dessous, on considère que le moteur n'a pas bougé du tout.
SEUIL_IMMOBILE = 100


def parcours(hand: Hand, motor: int, cible: int, vitesse: int, courant: int,
             verbeux: bool) -> dict:
    """Un aller simple, échantillonné pendant le mouvement."""
    depart = hand.positions().get(motor, 0)
    duree = abs(cible - depart) / max(vitesse, 1) + 4.0
    hand.enable()
    hand.command({motor: cible}, vitesse, courant)

    pic_courant, atteint, t0 = 0, depart, time.time()
    relances = 0
    while time.time() - t0 < duree:
        time.sleep(0.5)
        # Le mouvement n'a pas démarré : on réémet l'ordre plutôt que d'attendre
        # en vain. Deux relances suffisent en pratique.
        if (relances < 2 and time.time() - t0 > 1.0
                and abs(hand.positions().get(motor, 0) - depart) < SEUIL_IMMOBILE):
            hand.relancer()
            relances += 1
        pos = hand.positions().get(motor, 0)
        cur = hand.currents().get(motor, 0)
        pic_courant = max(pic_courant, cur)
        atteint = pos
        if verbeux:
            print(f"      t+{time.time()-t0:4.1f}s  pos={pos:<6} courant={cur}",
                  flush=True)
        if abs(pos - cible) <= TOLERANCE:
            break

    ecart = abs(atteint - cible)
    return {
        "depart": depart, "cible": cible, "atteint": atteint,
        "course": abs(atteint - depart), "pic_courant": pic_courant,
        "ok": ecart <= TOLERANCE,
        "immobile": abs(atteint - depart) < SEUIL_IMMOBILE,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motor", type=int, action="append", default=[],
                    help="moteur à tester ; répétable. Par défaut, tous les valides")
    ap.add_argument("--target", type=int, default=4000,
                    help="position de fermeture visée (défaut 4000 sur %d)" % hw.POSITION_MAX)
    ap.add_argument("--repeat", type=int, default=1,
                    help="nombre d'allers-retours par moteur")
    ap.add_argument("--velocity", type=int, default=hw.VELOCITY_CLOSE)
    ap.add_argument("--max-current", type=int, default=hw.GRASP_CURRENT)
    ap.add_argument("--iface", type=int, default=None)
    ap.add_argument("--sdk-dir", default=None)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="afficher la trajectoire seconde par seconde")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    moteurs = args.motor or [hw.THUMB_PIVOT, *hw.WORKING_FLEXORS]
    refuses = [m for m in moteurs if m in hw.BROKEN_MOTORS]
    moteurs = [m for m in moteurs if m not in hw.BROKEN_MOTORS]
    for m in refuses:
        print(f"— {hw.MOTOR_NAMES[m]} écarté : panne d'actionneur connue.")
    if not moteurs:
        print("Aucun moteur à tester.")
        return 1

    hand = Hand(sdk_dir=args.sdk_dir)
    resultats: list[tuple] = []
    try:
        hand.connect(iface_index=args.iface)
        # Le réveil échoue par intermittence : la main reste en trames moteur.
        # Une nouvelle tentative suffit.
        for essai in range(1, 4):
            print(f"Réveil de la main (tentative {essai}/3, ~10 s)…", flush=True)
            try:
                hand.wake()
                break
            except BusError as e:
                if essai == 3:
                    print("  dernier essai sans exiger le tactile "
                          "(les moteurs restent pilotables)…", flush=True)
                    hand.wake(require_tactile=False)
                    break
                print(f"  échec : {e} — nouvelle tentative", flush=True)
                time.sleep(2.0)

        al = hand.alarms()
        print(f"Alarmes au départ : {al or 'aucune'}\n")

        for m in moteurs:
            nom = hw.MOTOR_NAMES[m]
            for tour in range(1, args.repeat + 1):
                suffixe = f" (tour {tour}/{args.repeat})" if args.repeat > 1 else ""
                print(f"  {nom}{suffixe} : fermeture vers {args.target}…", flush=True)
                fermeture = parcours(hand, m, args.target, args.velocity,
                                     args.max_current, args.verbose)
                print(f"  {nom}{suffixe} : retour à 0…", flush=True)
                ouverture = parcours(hand, m, 0, args.velocity,
                                     args.max_current, args.verbose)
                resultats.append((m, nom, tour, fermeture, ouverture))

        print()
        print(f"{'moteur':<16}{'sens':<11}{'atteint':>9}{'/ cible':>9}"
              f"{'course':>9}{'courant max':>13}   verdict")
        print("-" * 76)
        echecs = 0
        for m, nom, tour, fer, ouv in resultats:
            for sens, r in (("fermeture", fer), ("ouverture", ouv)):
                if r["immobile"]:
                    verdict, ko = "IMMOBILE", True
                elif not r["ok"]:
                    verdict, ko = "incomplet", True
                else:
                    verdict, ko = "ok", False
                echecs += ko
                print(f"{nom:<16}{sens:<11}{r['atteint']:>9}{r['cible']:>9}"
                      f"{r['course']:>9}{r['pic_courant']:>13}   {verdict}")
        print()
        al = hand.alarms()
        if al:
            print(f"Alarmes à l'arrivée : {al}")
        if echecs:
            print(f"{echecs} mouvement(s) en échec.")
            print("Un moteur IMMOBILE avec un courant faible n'a pas reçu l'ordre ;")
            print("avec un courant élevé, il est bloqué mécaniquement.")
            return 1
        print("Tous les moteurs répondent.")
        return 0

    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        return 2
    finally:
        hand.release()
        hand.close()
        print("\nMain relâchée, bus fermé.")


if __name__ == "__main__":
    sys.exit(main())
