#!/usr/bin/env python3
"""
Tests de l'enveloppement sur une main simulée. Aucun matériel.

Une main factice fait avancer ses doigts vers la consigne et allume la zone
tactile correspondante au-delà d'une position de contact propre à chaque doigt.
On vérifie que chaque doigt s'arrête bien à son contact, que les autres
continuent, et que l'ouverture est rejouée même quand tout se passe mal.

C'est ce test qui autorise à lancer le vrai enveloppement : un doigt qui ne
s'arrête pas est ici une assertion qui tombe, là-bas un objet écrasé.

    python3 tests/test_envelop.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile.envelop import EnvelopConfig, envelop  # noqa: E402
from vt_tactile.tpdo import TactileReader, build_frame  # noqa: E402


class FakeHand:
    """Main simulée : les doigts suivent la consigne, sauf ceux qu'on bloque."""

    def __init__(self, contact_at: dict[int, int], frozen: set[int] = frozenset(),
                 current_at_contact: int = 600):
        self.contact_at = contact_at
        self.frozen = set(frozen)
        self.current_at_contact = current_at_contact
        self.pos = {m: 0 for m in hw.MOTOR_IDS}
        self.targets = {m: 0 for m in hw.MOTOR_IDS}
        self.opened = False
        self.commands: list[dict] = []

    # ── primitives attendues par envelop() ────────────────────────────────────

    def positions(self, motors=hw.MOTOR_IDS):
        return {m: self.pos[m] for m in motors}

    def currents(self, motors=hw.MOTOR_IDS):
        return {m: (self.current_at_contact
                    if self.pos[m] >= self.contact_at.get(m, 10**9) else 60)
                for m in motors}

    def command(self, targets, velocity, max_current):
        self.commands.append(dict(targets))
        for m, t in targets.items():
            self.targets[m] = t
            if m in self.frozen:
                continue
            # le doigt rejoint la consigne, mais bute à sa position de contact
            limit = self.contact_at.get(m, 10**9)
            self.pos[m] = min(t, max(self.pos[m], min(t, limit + 40)))

    def open_hand(self, **kw):
        self.opened = True
        for m in hw.FLEXORS:
            self.pos[m] = 0
        return True

    def latest_tactile(self):
        zones = {}
        for m, prefix in hw.MOTOR_TO_ZONE.items():
            touching = self.pos[m] >= self.contact_at.get(m, 10**9)
            level = 220 if touching else 40
            if prefix in ("thumb", "little"):
                zones[prefix] = {"touch": [level] * 5}
            else:
                zones[f"{prefix}.pad"] = {"touch": [level] * 4}
        return build_frame(zones)


def zeroed_reader(hand) -> TactileReader:
    r = TactileReader()
    r.zero([hand.latest_tactile()])
    return r


def check(name, cond, detail=""):
    print(f"  {'ok ' if cond else 'ÉCHEC'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        raise SystemExit(1)


def test_stops_at_tactile_contact():
    hand = FakeHand(contact_at={1: 2500, 3: 1000, 4: 2000, 5: 3000, 6: 1500})
    reader = zeroed_reader(hand)
    res = envelop(hand, reader, EnvelopConfig(lead=200, loop_period=0.0,
                                              hold_seconds=0.0, timeout=10.0))
    check("les cinq doigts pilotables ont touché",
          len(res.contacts) == len(hw.WORKING_FLEXORS), str(list(res.contacts)))
    check("le contact est attribué au tactile",
          all(c["reason"] == "tactile" for c in res.contacts.values()),
          str({n: c["reason"] for n, c in res.contacts.items()}))
    check("chaque doigt s'arrête près de sa position de contact",
          all(abs(res.contacts[hw.MOTOR_NAMES[m]]["position"] - p) <= 260
              for m, p in hand.contact_at.items()),
          str({n: c["position"] for n, c in res.contacts.items()}))
    check("l'issue est complète", res.outcome == "tous en contact", res.outcome)
    check("l'ouverture a été rejouée", hand.opened and res.released)


def test_early_finger_stops_while_others_continue():
    """Le point du protocole : un doigt arrêté ne pousse plus."""
    hand = FakeHand(contact_at={1: 3000, 3: 400, 4: 3000, 5: 3000, 6: 3000})
    reader = zeroed_reader(hand)
    res = envelop(hand, reader, EnvelopConfig(lead=200, loop_period=0.0,
                                              hold_seconds=0.0, timeout=10.0))
    little = res.contacts["auriculaire"]["position"]
    index = res.contacts["index"]["position"]
    check("l'auriculaire s'arrête tôt", little < 700, str(little))
    check("l'index continue bien au-delà", index > 2500, str(index))
    check("l'auriculaire n'a plus reçu de consigne croissante après contact",
          max(c.get(3, 0) for c in hand.commands) <= little + 260,
          str(max(c.get(3, 0) for c in hand.commands)))


def test_stall_backstop():
    """Un doigt bloqué qui n'allume rien doit quand même s'arrêter."""
    hand = FakeHand(contact_at={1: 2000, 4: 2000, 5: 2000, 6: 2000}, frozen={3})
    reader = zeroed_reader(hand)
    res = envelop(hand, reader, EnvelopConfig(lead=200, loop_period=0.0,
                                              hold_seconds=0.0, timeout=10.0,
                                              stall_iterations=3))
    check("le doigt bloqué est repéré par calage",
          res.contacts["auriculaire"]["reason"] == "calage",
          res.contacts["auriculaire"]["reason"])


def test_release_even_on_failure():
    """Une exception en cours de fermeture ne doit pas laisser la main serrée."""
    class Exploding(FakeHand):
        def currents(self, motors=hw.MOTOR_IDS):
            raise RuntimeError("panne de lecture")

    hand = Exploding(contact_at={1: 1000, 3: 1000, 4: 1000, 5: 1000, 6: 1000})
    reader = zeroed_reader(hand)
    try:
        envelop(hand, reader, EnvelopConfig(loop_period=0.0, hold_seconds=0.0))
    except RuntimeError:
        pass
    check("l'ouverture a bien eu lieu malgré l'exception", hand.opened)


def test_requires_zero():
    hand = FakeHand(contact_at={})
    try:
        envelop(hand, TactileReader(), EnvelopConfig())
    except ValueError:
        check("refuse de fermer sans remise à zéro tactile", True)
        return
    check("refuse de fermer sans remise à zéro tactile", False)


def test_thumb_flexes_with_the_others():
    """
    La flexion du pouce referme la main comme les quatre longs doigts.

    Elle a été exclue tant que la main précédente l'avait en panne. Sur la
    main montée le 2026-08-25 elle répond : voir ``hw.BROKEN_MOTORS``.
    """
    check("la flexion du pouce est un fléchisseur pilotable",
          1 in hw.WORKING_FLEXORS and 1 in hw.FLEXORS)
    check("les cinq fléchisseurs sont pilotables",
          set(hw.WORKING_FLEXORS) == set(hw.FLEXORS) == {1, 3, 4, 5, 6})
    check("le pivot du pouce n'est pas un fléchisseur",
          hw.THUMB_PIVOT not in hw.FLEXORS)


if __name__ == "__main__":
    for fn in (test_thumb_flexes_with_the_others, test_requires_zero,
               test_stops_at_tactile_contact,
               test_early_finger_stops_while_others_continue,
               test_stall_backstop, test_release_even_on_failure):
        print(f"\n{fn.__name__}")
        fn()
    print("\nTous les tests passent.")
