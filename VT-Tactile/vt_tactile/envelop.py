#!/usr/bin/env python3
"""
envelop.py — refermer la main sur un objet, en s'arrêtant au contact.

Le principe : on avance chaque doigt par petits pas et on l'immobilise dès
qu'il touche. Un doigt arrêté ne pousse plus, les autres continuent — la main
épouse l'objet au lieu de le serrer.

Trois détecteurs de contact, dans cet ordre de confiance :

1. **tactile** — la zone du doigt dépasse le seuil de pression. C'est le signal
   direct, disponible depuis que le décodage est correct ;
2. **courant** — le moteur tire plus que son seuil relevé. Filet pour un contact
   qui se produirait hors zone instrumentée (flanc de phalange, articulation) ;
3. **calage** — la position ne progresse plus malgré la consigne. Dernier filet,
   pour un doigt qui bute sans tirer de courant.

Sans ce troisième filet, un doigt bloqué qui ne déclenche ni le tactile ni le
courant pousserait jusqu'au timeout.

Trois garde-fous physiques, indépendants de la logique :

* le courant maximum est plafonné bien en dessous des seuils de contact, donc
  un doigt cale contre l'objet au lieu de l'écraser ;
* la position de consigne est bornée avant la butée mécanique ;
* l'ouverture est rejouée dans tous les cas de sortie, y compris sur erreur.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

from . import hardware as hw
from .tpdo import TactileReader

log = logging.getLogger(__name__)


@dataclass
class EnvelopConfig:
    """Paramètres de l'enveloppement."""

    #: Avance maximale de la consigne sur la position réelle, en counts. Borne
    #: la course résiduelle si la boucle meurt : le doigt s'arrête là, pas en
    #: fermeture complète.
    lead: int = 400
    #: Progression minimale de la consigne à chaque itération. Le variateur
    #: **ignore une consigne identique à la précédente** (spec constructeur :
    #: « the new control command needs to be different from the old »), donc
    #: une consigne asservie à une position qui ne bouge pas encore ne démarre
    #: jamais le mouvement. Choisi sous la limite de vitesse pour que la
    #: consigne ne prenne pas d'avance sur le doigt.
    creep: int = 25
    #: Plafond de fermeture. Sous la butée mécanique, exprès.
    max_position: int = hw.POSITION_MAX
    velocity: int = hw.VELOCITY_CLOSE
    #: Plafond de couple pendant l'approche. C'est le garde-fou principal.
    max_current: int = hw.GRASP_CURRENT
    #: Surcharge par moteur, pour en pousser un davantage sans relever le
    #: couple des autres — donc sans serrer l'objet plus fort partout.
    current_by_motor: dict[int, int] = field(default_factory=dict)
    #: Courant réduit une fois la saisie établie, pour ne pas chauffer.
    hold_current: int = hw.HOLD_CURRENT
    loop_period: float = 0.06
    timeout: float = 25.0

    #: Pression au-delà de laquelle une zone est déclarée en contact. Le bruit
    #: de fond mesuré est sous 1 LSB, soit 0,004 : 0,03 est franc sans être tardif.
    pressure_threshold: float = 0.03
    #: Nombre d'échantillons de mouvement libre servant à établir le courant
    #: de référence de chaque moteur.
    current_baseline_samples: int = 15
    #: Marge au-dessus du courant libre au-delà de laquelle on déclare un appui.
    #: Des seuils absolus figés ne marchent pas : le courant libre varie de 150
    #: à 330 ‰ selon le doigt et le jour, et un seuil sous cette valeur déclenche
    #: un faux contact dès le premier pas.
    #: Marge au-dessus du courant libre au-delà de laquelle un doigt est dit
    #: en appui. À 120 il s'arrêtait dès qu'il commençait à pousser, donc avant
    #: d'exciter son capteur : le tactile ne pouvait jamais être le critère
    #: d'arrêt. À 450, l'auriculaire et le majeur s'arrêtent bien sur le tactile.
    current_margin: int = 450
    #: Counts parcourus après le premier contact tactile, pour asseoir la prise.
    #: 0 fige le doigt au premier frôlement. Le plafond de courant reste actif.
    seat_counts: int = 0

    #: Délai laissé au doigt pour **démarrer**, en secondes, avant que le
    #: calage puisse être invoqué. ``move_motors`` n'est pas toujours pris :
    #: la consigne est acceptée, relue correctement, et le moteur ne bouge pas.
    #: Sans ce délai, les cinq itérations de calage tombent en 0,3 s et les
    #: quatre doigts sont déclarés calés à la position de départ — constaté sur
    #: une acquisition complète, six angles sur six.
    demarrage_s: float = 2.5
    #: Réémissions de l'ordre pendant ce délai, si rien n'a bougé.
    relances_demarrage: int = 2

    #: Itérations sans progression avant de déclarer un doigt calé.
    stall_iterations: int = 5
    #: Progression minimale, en counts, pour qu'une itération compte.
    stall_epsilon: int = 12

    #: Consigne du pivot du pouce, ``None`` pour ne pas y toucher. Par défaut on
    #: n'y touche pas : l'opérateur le règle avant la saisie, et le bouger en
    #: cours de fermeture risque surtout de déplacer l'objet.
    thumb_pivot: int | None = None
    #: Durée de maintien de la saisie avant relâchement, pour observer la dérive.
    hold_seconds: float = 2.0

    def current_for(self, motor: int) -> int:
        return self.current_by_motor.get(motor, self.max_current)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Contact:
    """Instant où un doigt a touché."""

    motor: int
    name: str
    reason: str
    position: int
    current: int
    elapsed: float
    zones: dict[str, float] = field(default_factory=dict)
    #: Seuil de courant retenu pour ce doigt, mesuré en début de fermeture.
    current_limit: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EnvelopResult:
    config: dict
    samples: list[dict] = field(default_factory=list)
    contacts: dict[str, dict] = field(default_factory=dict)
    baseline_raw: dict = field(default_factory=dict)
    outcome: str = ""
    duration: float = 0.0
    #: Itérations où la trame tactile n'avait pas changé depuis la précédente.
    #: Répond à « commander gêne-t-il la lecture ? » — un taux élevé pendant le
    #: mouvement, nul au repos, signerait une contention.
    stale_reads: int = 0
    loop_iterations: int = 0
    released: bool = False
    hold: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "config": self.config,
            "outcome": self.outcome,
            "duration_s": round(self.duration, 2),
            "released": self.released,
            "stale_reads": self.stale_reads,
            "loop_iterations": self.loop_iterations,
            "contacts": self.contacts,
            "hold": self.hold,
            "baseline_raw": self.baseline_raw,
            "samples": self.samples,
        }


def _zone_pressures(state, motor: int) -> dict[str, float]:
    return {z: round(state[z].pressure_max, 4)
            for z in hw.zones_of(motor) if z in state.zones}


def envelop(hand, reader: TactileReader, cfg: EnvelopConfig | None = None,
            on_step=None) -> EnvelopResult:
    """
    Referme la main sur l'objet posé, doigt par doigt, et enregistre tout.

    Args:
        hand: :class:`vt_tactile.bus.Hand` connecté et réveillé.
        reader: décodeur tactile, **déjà remis à zéro**.
        cfg: paramètres, valeurs par défaut prudentes.
        on_step: rappel optionnel appelé avec chaque échantillon, pour un
            affichage en direct.

    Returns:
        la trajectoire complète et les instants de contact.
    """
    cfg = cfg or EnvelopConfig()
    if not reader.zeroed:
        raise ValueError("le décodeur tactile n'a pas été remis à zéro")

    result = EnvelopResult(config=cfg.to_dict())
    motors = list(hw.WORKING_FLEXORS)

    if cfg.thumb_pivot is not None:
        log.info("Pré-positionnement du pivot du pouce à %d", cfg.thumb_pivot)
        hand.command({hw.THUMB_PIVOT: cfg.thumb_pivot},
                     hw.VELOCITY_CLOSE, cfg.max_current)
        time.sleep(1.5)

    start_pos = hand.positions(motors)
    targets = dict(start_pos)
    active = set(motors)
    stalled = {m: 0 for m in motors}
    demarre = {m: False for m in motors}
    relances = 0
    free_current: dict[int, list[int]] = {m: [] for m in motors}
    last_frame: bytes | None = None
    last_pos = dict(start_pos)
    t0 = time.time()

    log.info("Fermeture de %s depuis %s", [hw.MOTOR_NAMES[m] for m in motors],
             start_pos)

    try:
        while active and (time.time() - t0) < cfg.timeout:
            # La consigne reste à portée de la position réelle — sans quoi
            # « avancer par petits pas » n'est qu'une fermeture complète
            # déguisée — tout en croissant strictement, sans quoi le variateur
            # l'ignore et le doigt ne démarre jamais. Il faut les deux.
            for m in active:
                targets[m] = min(max(last_pos[m] + cfg.lead,
                                     targets[m] + cfg.creep),
                                 cfg.max_position)
            for m in active:
                hand.command({m: targets[m]}, cfg.velocity, cfg.current_for(m))
            # Réémettre tant qu'aucun doigt n'a démarré. Le variateur perd des
            # ordres : la consigne est acceptée et relue, mais le moteur ne
            # bouge pas. C'est le remède éprouvé, et il n'était pas ici.
            if (relances < cfg.relances_demarrage
                    and not any(demarre.values())
                    and (time.time() - t0) > 1.0 * (relances + 1)):
                hand.relancer()
                relances += 1
                log.info("Aucun doigt n'a démarré — ordre réémis (%d/%d)",
                         relances, cfg.relances_demarrage)
            time.sleep(cfg.loop_period)

            raw = hand.latest_tactile()
            if raw is None:
                continue
            result.loop_iterations += 1
            if raw == last_frame:
                result.stale_reads += 1
            last_frame = raw
            state = reader.decode(raw)
            pos = hand.positions(motors)
            cur = hand.currents(motors)
            elapsed = time.time() - t0

            sample = {
                "t": round(elapsed, 3),
                "targets": {str(m): targets[m] for m in motors},
                "positions": {str(m): pos[m] for m in motors},
                "currents": {str(m): cur[m] for m in motors},
                "active": sorted(active),
                "zones": state.to_dict(),
            }
            result.samples.append(sample)
            if on_step is not None:
                on_step(sample, state, dict(result.contacts))

            for m in sorted(active):
                # Courant de référence en mouvement libre, mesuré sur ce doigt
                # et cette session plutôt que repris d'une campagne antérieure.
                # Seul le critère « courant » attend sa référence. Le tactile
                # et le calage doivent agir dès la première itération : un doigt
                # déjà en appui au départ ne doit pas pousser une seconde de plus.
                if len(free_current[m]) < cfg.current_baseline_samples:
                    free_current[m].append(cur[m])
                    current_limit = 0
                else:
                    # Le seuil suit la session, mais ne descend jamais sous le
                    # courant d'appui mesuré pour ce moteur. Sans ce plancher il
                    # tombait à 256-332 selon les doigts, sous les valeurs
                    # relevées (270-500) : le doigt s'arrêtait alors sur son
                    # propre courant de marche à vide, bien avant l'objet.
                    current_limit = max(max(free_current[m]) + cfg.current_margin,
                                        hw.CURRENT_CONTACT.get(m, 0))

                reason = None
                zones = _zone_pressures(state, m)
                if any(v >= cfg.pressure_threshold for v in zones.values()):
                    reason = "tactile"
                elif current_limit and cur[m] >= current_limit:
                    reason = "courant"
                else:
                    moved = abs(pos[m] - last_pos[m]) >= cfg.stall_epsilon
                    if abs(pos[m] - start_pos[m]) >= cfg.stall_epsilon:
                        demarre[m] = True
                    stalled[m] = 0 if moved else stalled[m] + 1
                    # Un doigt qui n'a jamais quitté sa position de départ n'est
                    # pas calé : il n'a pas encore démarré. Les deux se
                    # ressemblent — immobile, courant faible — et les confondre
                    # arrête la fermeture avant qu'elle ait commencé.
                    if (stalled[m] >= cfg.stall_iterations
                            and (demarre[m] or elapsed > cfg.demarrage_s)):
                        reason = "calage"
                if pos[m] >= cfg.max_position - cfg.stall_epsilon and reason is None:
                    reason = "butée"

                if reason:
                    active.discard(m)
                    # On peut asseoir la prise de quelques counts au-delà du
                    # premier frôlement ; le plafond de courant reste le garde-fou.
                    seat = cfg.seat_counts if reason == "tactile" else 0
                    targets[m] = min(pos[m] + seat, cfg.max_position)
                    hand.command({m: targets[m]}, cfg.velocity, cfg.current_for(m))
                    c = Contact(m, hw.MOTOR_NAMES[m], reason, pos[m], cur[m],
                                round(elapsed, 2), zones,
                                current_limit=current_limit)
                    result.contacts[hw.MOTOR_NAMES[m]] = c.to_dict()
                    log.info("%s : %s à %d counts, %d ‰, %s",
                             hw.MOTOR_NAMES[m], reason, pos[m], cur[m], zones)
            last_pos = pos

        result.duration = time.time() - t0
        result.outcome = ("tous en contact" if not active
                          else f"timeout, restants : "
                               f"{[hw.MOTOR_NAMES[m] for m in sorted(active)]}")

        # Maintien à courant réduit : on regarde si la saisie tient ou glisse.
        if cfg.hold_seconds > 0:
            hand.command({m: targets[m] for m in motors},
                         cfg.velocity, cfg.hold_current)
            time.sleep(cfg.hold_seconds)
            raw = hand.latest_tactile()
            if raw is not None:
                held = reader.decode(raw)
                result.hold = {
                    "positions": {str(m): v
                                  for m, v in hand.positions(motors).items()},
                    "currents": {str(m): v
                                 for m, v in hand.currents(motors).items()},
                    "zones": held.to_dict(),
                }
    finally:
        # L'ouverture est rejouée quoi qu'il arrive : la main n'est pas
        # rétro-entraînable, une sortie par exception la laisserait fermée.
        result.released = hand.open_hand()
        if not result.released:
            log.error("Ouverture incomplète — la main tient peut-être encore "
                      "l'objet. Le couple est laissé actif pour pouvoir "
                      "réessayer : relancer hand.open_hand().")

    return result


def summarise(result: EnvelopResult) -> str:
    """Résumé lisible : ce qui a touché, quand, et avec quelle pression."""
    stale = (f", {result.stale_reads}/{result.loop_iterations} lectures "
             f"répétées" if result.loop_iterations else "")
    L = [f"Issue : {result.outcome}   ({result.duration:.1f} s, "
         f"{len(result.samples)} échantillons{stale})",
         f"Ouverture vérifiée : {'oui' if result.released else '**NON**'}", ""]
    if not result.contacts:
        L.append("Aucun contact détecté. L'objet est peut-être hors de portée "
                 "des doigts, ou le seuil est trop haut.")
        return "\n".join(L)

    L += [f"{'doigt':<14}{'cause':>9}{'position':>10}{'courant':>9}"
          f"{'pression max':>14}  zones",
          "─" * 74]
    for name, c in result.contacts.items():
        zones = c["zones"]
        best = max(zones, key=zones.get) if zones else "—"
        pmax = max(zones.values(), default=0.0)
        L.append(f"{name:<14}{c['reason']:>9}{c['position']:>10}"
                 f"{c['current']:>9}{pmax:>14.3f}  {best}")

    if result.hold:
        L += ["", "Après maintien :"]
        for zone, d in result.hold["zones"].items():
            if d["pressure_max"] >= 0.02:
                L.append(f"   {zone:<14} {d['pressure_max']:.3f}")
    return "\n".join(L)
