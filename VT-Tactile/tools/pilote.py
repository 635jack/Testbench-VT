#!/usr/bin/env python3
"""
pilote.py — piloter la main à la main, en ligne de commande.

    sudo python3 -m tools.pilote

Commandes, une par ligne :

    6 4000        le moteur 6 va à 4000
    6 4000 300    idem, à la vitesse 300
    index 4000    on peut nommer le doigt au lieu du numéro
    ouvrir        tous les moteurs valides à 0
    fermer        tous les moteurs valides à 4000
    pince         pouce en opposition + index
    etat          positions, courants, alarmes, pressions
    zero          refait le zéro tactile (main au repos)
    reconnexion   referme et rouvre le bus, comme un processus neuf
    ?             rappel des commandes
    q             quitter (la main est rouverte avant de rendre le bus)

**Reconnexion automatique.** Mesuré le 2026-08-19 : le variateur n'accepte que
**quatre mouvements** par session, puis les refuse en silence — consigne
acceptée, cible relue, aucune alarme, moteur immobile avec un courant de repos.
L'outil compte donc les mouvements, et rouvre le bus de lui-même avant d'y
arriver. Si un mouvement échoue quand même, il reconnecte et réessaie une fois.
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
from vt_tactile.tpdo import ZONE_NAMES, TactileReader  # noqa: E402

#: Repère hérité d'un diagnostic dépassé : on croyait le variateur limité à
#: quatre mouvements par session. C'était en fait notre livraison de commande
#: qui échouait — ``move_motors`` n'était pas toujours pris. Depuis qu'on
#: réémet l'ordre quand le mouvement ne démarre pas, douze mouvements
#: s'enchaînent sans une seule réouverture du bus. Conservé pour l'affichage.
MOUVEMENTS_PAR_SESSION = 4
TOLERANCE = 60
SEUIL_IMMOBILE = 100
#: Au-delà, un moteur immobile ne l'est pas faute d'ordre : il pousse contre
#: quelque chose. Relancer la commande dans ce cas, c'est forcer sur un doigt
#: coincé — l'annulaire a tiré 1059 ‰ le 2026-08-19 avant de passer en alarme.
SEUIL_BLOQUE = 500

NOMS = {"pouce": hw.THUMB_PIVOT, "pivot": hw.THUMB_PIVOT,
        "auriculaire": 3, "petit": 3,
        "annulaire": 4, "majeur": 5, "index": 6}

PRESETS = {
    "ouvrir": {2: 0, 3: 0, 4: 0, 5: 0, 6: 0},
    "fermer": {3: 4000, 4: 4000, 5: 4000, 6: 4000},
    "pince": {2: hw.THUMB_OPPOSITION, 6: 4000},
    "saisie": {2: hw.THUMB_OPPOSITION, 3: 0, 4: 0, 5: 0, 6: 0},
}


class Pilote:
    def __init__(self, hand: Hand, reader: TactileReader, vitesse: int,
                 courant: int):
        self.hand = hand
        self.reader = reader
        self.vitesse = vitesse
        self.courant = courant
        self.mouvements = 0
        self.tactile_ok = True
        self.auto_reconnexion = True

    # ── Bus ──────────────────────────────────────────────────────────────────

    def reveiller(self) -> None:
        for essai in range(1, 4):
            try:
                self.tactile_ok = self.hand.wake()
                return
            except BusError as e:
                if essai == 3:
                    # Dernier recours : réveiller sans exiger le tactile. Les
                    # moteurs obéissent même quand la main refuse de basculer
                    # en mode capteur.
                    self.tactile_ok = self.hand.wake(require_tactile=False)
                    return
                print(f"  réveil manqué ({e}) — nouvelle tentative", flush=True)
                time.sleep(2.0)

    def reconnecter(self, motif: str = "") -> None:
        print(f"  ↻ reconnexion du bus{(' — ' + motif) if motif else ''}…",
              flush=True)
        try:
            self.hand.close()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
        self.hand.connect()
        self.reveiller()
        frames = self.hand.collect(1.5)
        if frames:
            self.reader.zero(frames)
        self.mouvements = 0
        print("  ↻ bus rouvert.", flush=True)

    # ── Mouvement ────────────────────────────────────────────────────────────

    def _aller(self, cibles: dict[int, int], vitesse: int) -> tuple[dict[int, bool], dict, dict]:
        depart = self.hand.positions()
        course = max(abs(c - depart.get(m, 0)) for m, c in cibles.items())
        duree = course / max(vitesse, 1) + 4.0
        self.hand.enable()
        self.hand.command(cibles, vitesse, self.courant)
        self.mouvements += 1

        pics = {m: 0 for m in cibles}
        t0 = time.time()
        relances = 0
        while time.time() - t0 < duree:
            time.sleep(0.4)
            pos, cur = self.hand.positions(), self.hand.currents()
            # Le mouvement n'a pas démarré : on réémet l'ordre plutôt que de
            # conclure à un refus et de rouvrir tout le bus.
            if (relances < 2 and time.time() - t0 > 1.0
                    and all(abs(pos.get(m, 0) - depart.get(m, 0)) < SEUIL_IMMOBILE
                            for m in cibles)):
                self.hand.relancer()
                relances += 1
            for m in cibles:
                pics[m] = max(pics[m], cur.get(m, 0))
            if all(abs(pos.get(m, 0) - c) <= TOLERANCE for m, c in cibles.items()):
                break
        pos, cur = self.hand.positions(), self.hand.currents()
        ok = {m: abs(pos.get(m, 0) - depart.get(m, 0)) >= SEUIL_IMMOBILE
                 or abs(pos.get(m, 0) - c) <= TOLERANCE
              for m, c in cibles.items()}
        return ok, pos, cur, pics

    def bouger(self, cibles: dict[int, int], vitesse: int | None = None) -> None:
        vitesse = vitesse or self.vitesse
        refuses = [hw.MOTOR_NAMES[m] for m in cibles if m in hw.BROKEN_MOTORS]
        cibles = {m: max(0, min(c, hw.POSITION_MAX))
                  for m, c in cibles.items() if m not in hw.BROKEN_MOTORS}
        for nom in refuses:
            print(f"  — {nom} ignoré : panne d'actionneur connue.")
        if not cibles:
            return

        # On rouvre avant d'atteindre la limite plutôt que de subir un échec.
        ok, pos, cur, pics = self._aller(cibles, vitesse)

        # Un moteur immobile qui a tiré fort est BLOQUÉ : on ne relance pas,
        # ce serait forcer sur un doigt coincé.
        bloques = [m for m in cibles
                   if not ok[m] and pics[m] >= SEUIL_BLOQUE]
        if bloques:
            for m in bloques:
                print(f"  ⚠ {hw.MOTOR_NAMES[m]} BLOQUÉ : {pics[m]} ‰ sans bouger."
                      f" Commande NON relancée — dégagez le doigt.")
        elif not all(ok.values()) and self.auto_reconnexion:
            self.reconnecter("mouvement ignoré par le variateur")
            ok, pos, cur, pics = self._aller(cibles, vitesse)

        for m, c in cibles.items():
            if not ok[m]:
                etat = "BLOQUÉ" if pics[m] >= SEUIL_BLOQUE else "IMMOBILE"
            else:
                etat = "ok"
            print(f"  {hw.MOTOR_NAMES[m]:<16} {pos.get(m,0):>6} / {c:<6}"
                  f" courant {cur.get(m,0):>4}‰  pic {pics[m]:>4}‰   {etat}")

        al = self.hand.alarms()
        if al:
            print(f"  ⚠ ALARMES : "
                  + ", ".join(f"{hw.MOTOR_NAMES[m]} code {a}" for m, a in al.items()))
            print("    Tant qu'elles ne sont pas levées, le variateur refuse de "
                  "bouger. Dégagez la main, puis « raz ».")

    # ── Lecture ──────────────────────────────────────────────────────────────

    def etat(self) -> None:
        pos, cur = self.hand.positions(), self.hand.currents()
        print(f"  {'moteur':<16}{'position':>10}{'courant':>10}")
        for m in hw.MOTOR_IDS:
            marque = "  (en panne)" if m in hw.BROKEN_MOTORS else ""
            print(f"  {hw.MOTOR_NAMES[m]:<16}{pos.get(m,0):>10}"
                  f"{cur.get(m,0):>10}{marque}")
        al = self.hand.alarms()
        print(f"  alarmes : {al or 'aucune'}")
        raw = self.hand.latest_tactile()
        if raw is not None and self.reader.zeroed:
            st = self.reader.decode(raw)
            actives = [(n, st.zones[n].pressure_max) for n in ZONE_NAMES
                       if n in st.zones and st.zones[n].pressure_max > 0.02]
            print("  tactile : " + (", ".join(f"{n} {v:.2f}" for n, v in actives)
                                    if actives else "aucun contact"))
        print(f"  mouvements depuis la dernière ouverture du bus : {self.mouvements}")


#: Les positions sont en counts codeur, pas en degrés. 0 = doigt tendu,
#: POSITION_MAX = course mécanique complète. Une saisie utile se joue entre
#: 3000 et 5000 selon la taille de l'objet ; au-delà de 6000 le doigt vient
#: au contact de la paume.
LEGENDE = f"""  moteur  nom            doigt                    position
  ------  -------------  -----------------------  ----------------------------
    1     pouce          flexion du pouce         0 = tendu, {hw.POSITION_MAX} = replié
    2     pouce, pivot   opposition du pouce      0 = à plat, {hw.THUMB_OPPOSITION} = en face
    3     auriculaire    petit doigt              0 = tendu, {hw.POSITION_MAX} = replié
    4     annulaire                               0 = tendu, {hw.POSITION_MAX} = replié
    5     majeur                                  0 = tendu, {hw.POSITION_MAX} = replié
    6     index                                   0 = tendu, {hw.POSITION_MAX} = replié

  Ordres de grandeur : ~4000 referme franchement un doigt, ~3000 à 5000 pour
  saisir selon la taille de l'objet, au-delà de ~6000 il touche la paume."""

AIDE = """  6 4000 [vitesse]   déplacer un moteur (numéro ou nom : index, majeur,
                     annulaire, auriculaire, pouce)
  ouvrir | fermer | pince | saisie
  etat | zero | legende | alarmes | raz | reconnexion | ? | q"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--velocity", type=int, default=hw.VELOCITY_CLOSE)
    ap.add_argument("--max-current", type=int, default=hw.GRASP_CURRENT)
    ap.add_argument("--iface", type=int, default=None)
    ap.add_argument("--sdk-dir", default=None)
    ap.add_argument("--sans-reconnexion", action="store_true",
                    help="ne pas rouvrir le bus automatiquement au bout de "
                         "quatre mouvements — pour observer la panne telle quelle")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    hand = Hand(sdk_dir=args.sdk_dir)
    reader = TactileReader()
    p = Pilote(hand, reader, args.velocity, args.max_current)
    p.auto_reconnexion = not args.sans_reconnexion
    if args.sans_reconnexion:
        print("Reconnexion automatique désactivée : au-delà de "
              f"{MOUVEMENTS_PAR_SESSION} mouvements, le variateur cessera "
              "d'obéir en silence.\n")

    try:
        hand.connect(iface_index=args.iface)
        print("Réveil de la main (~10 s)…", flush=True)
        p.reveiller()
        frames = hand.collect(1.5)
        if frames:
            reader.zero(frames)
        print(f"Prêt. Zéro tactile sur {len(frames)} trames.")
        if not p.tactile_ok:
            print("Attention : pas de trame capteur, le tactile sera indisponible.")
        print()
        print(LEGENDE + "\n")
        print(AIDE + "\n")

        while True:
            try:
                ligne = input("main> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not ligne:
                continue
            mots = ligne.split()
            tete = mots[0].lower()

            if tete in ("q", "quit", "exit"):
                break
            if tete in ("?", "aide", "help"):
                print(AIDE)
            elif tete in ("legende", "doigts"):
                print(LEGENDE)
            elif tete == "etat":
                p.etat()
            elif tete == "zero":
                f = hand.collect(2.0)
                if f:
                    reader.zero(f)
                    print(f"  zéro refait sur {len(f)} trames.")
                else:
                    print("  aucune trame tactile.")
            elif tete in ("alarmes", "alarme"):
                print(f"  {hand.alarms() or 'aucune'}")
            elif tete in ("raz", "clear"):
                hand.clear_alarms()
                print(f"  alarmes après remise à zéro : {hand.alarms() or 'aucune'}")
            elif tete == "reconnexion":
                p.reconnecter("demandée")
            elif tete in PRESETS:
                p.bouger(dict(PRESETS[tete]))
            else:
                cible_moteur = NOMS.get(tete)
                if cible_moteur is None:
                    try:
                        cible_moteur = int(tete)
                    except ValueError:
                        print(f"  commande inconnue : {tete!r} — « ? » pour l'aide")
                        continue
                if len(mots) < 2:
                    print("  il manque la position, par exemple : 6 4000")
                    continue
                try:
                    position = int(mots[1])
                    vitesse = int(mots[2]) if len(mots) > 2 else None
                except ValueError:
                    print("  position et vitesse doivent être des entiers.")
                    continue
                p.bouger({cible_moteur: position}, vitesse)

    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        return 2
    finally:
        print("\nOuverture de la main…")
        try:
            hand.release()
        finally:
            hand.close()
        print("Main relâchée, bus fermé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
