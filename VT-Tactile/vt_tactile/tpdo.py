#!/usr/bin/env python3
"""
tpdo.py — décodage des trames tactiles de la DH116.

Ne dépend d'aucune bibliothèque constructeur : prend les 192 octets bruts que
SOEM remonte et les interprète.

**La table de découpage vient de la mesure**, pas de la lecture du binaire du
SDK. Elle a été obtenue le 2026-07-31 en sollicitant séparément le bout et la
pulpe de chaque doigt et en regardant quels octets bougeaient (`VT-Bench`, run
`complet2`). Les deux se contredisent : le SDK lit ses champs un octet trop tôt,
ce qui lui fait renvoyer une proximité constante à 1,00 et une force normale
valant 256 fois l'octet de poids faible de la vraie valeur. On suit la mesure.

Usage dans une boucle d'acquisition :

    from vt_tactile.tpdo import TactileReader

    reader = TactileReader()
    reader.zero(trames_au_repos)           # une fois, rien en contact
    state = reader.decode(raw_192_octets)  # à chaque trame
    state["index.tip"].pressure_max        # 0.0 à 1.0
    state.to_dict()                        # sérialisable pour un dataset
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ── Enveloppe ─────────────────────────────────────────────────────────────────

TPDO_SIZE = 192
FRAME_MOTOR = 0x00
FRAME_TACTILE = 0x40

PAYLOAD_OFFSET = 2
SLOT_SIZE = 30
SLOT_COUNT = 6

#: Trame d'état moteur : 27 créneaux de 6 octets à partir de l'offset 2, soit
#: ``position, vitesse, courant`` en u16 petit-boutiste. Structure lue dans
#: ``LHandProLibPrivate::decode_receive_tpdo_bytes`` et recoupée avec la spec
#: constructeur (« 7 bytes per axis… position/velocity/current feedback [2] »).
#: Les octets d'état commencent après le dernier créneau.
MOTOR_SLOT_SIZE = 6
MOTOR_SLOT_COUNT = 27
MOTOR_STATUS_OFFSET = PAYLOAD_OFFSET + MOTOR_SLOT_SIZE * MOTOR_SLOT_COUNT  # 164

FINGERS = ("thumb", "index", "middle", "ring", "little")
PALM = "palm"

#: Deux variantes de capteur, **mesurées** en sollicitant bout puis pulpe :
#: sur l'index, le majeur et l'annulaire, les deux excitent des canaux
#: distincts (1-4 contre 5-8) — deux capteurs. Sur le pouce et l'auriculaire,
#: les deux excitent **les mêmes** canaux 0-5 : il n'y a qu'un capteur, et les
#: canaux 6-8 n'existent pas. Cela correspond aux deux configurations du SDK,
#: ``{touch: 9, sprox: 2}`` contre ``{touch: 5, sprox: 1}``.
DUAL_SENSOR = ("index", "middle", "ring")
SINGLE_SENSOR = ("thumb", "little")

#: Ordre des créneaux dans la trame, mesuré : chaque zone n'allume que le sien.
SLOT_ORDER = (*FINGERS, PALM)

# ── Découpage d'un créneau de doigt (offsets relatifs au créneau) ─────────────

TIP_TOUCH = range(1, 5)      # pression du bout, doigts à deux capteurs
PAD_TOUCH = range(5, 9)      # pression de la pulpe, doigts à deux capteurs
#: Doigts à capteur unique : 5 canaux de pression, puis une voie de proximité.
#: Confondre celle-ci avec de la pression fait « voir » un appui du pouce alors
#: que sa flexion est en panne — c'est simplement l'objet qui s'approche.
SINGLE_TOUCH = range(0, 5)
SINGLE_PROX = 5
# offset 9 : inutilisé, constamment nul sur toutes les mesures
NF = {"tip": 10, "pad": 12}      # uint16 LE, ÷100
TF = {"tip": 14, "pad": 16}
DIR = {"tip": 18, "pad": 20}     # 0xFFFF = pas de direction
PROX = {"tip": 22, "pad": 23}    # 1 octet, ÷255

#: La paume n'a pas la structure d'un doigt : 26 points de pression, rien d'autre.
PALM_TOUCH = range(0, 26)

TOUCH_FULL_SCALE = 255.0
FORCE_SCALE = 100.0
NO_DIRECTION = 0xFFFF

#: Les neuf zones réellement instrumentées, dans l'ordre où on aime les lire.
#: Le SDK en déclare onze : il compte une pulpe au pouce et à l'auriculaire,
#: qui n'existent pas sur ce matériel.
ZONE_NAMES = ("thumb",
              "index.tip", "index.pad",
              "middle.tip", "middle.pad",
              "ring.tip", "ring.pad",
              "little",
              PALM)

#: Points de la paume restés muets au balayage du 2026-07-31 (quadrant thénar
#: compris). Exposé pour pouvoir les écarter d'un dataset en connaissance de cause.
PALM_SILENT = (0, 1, 2, 3, 4, 5, 6, 7, 11, 12, 13, 19)


class TpdoError(ValueError):
    """Trame inexploitable."""


# ── Sortie ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Zone:
    """
    Une zone tactile : un bout de doigt, une pulpe, ou la paume.

    ``pressure`` est normalisé sur [0, 1] après retrait de la ligne de base.
    Les lignes de base diffèrent fortement d'une zone à l'autre : sans remise à
    zéro, les valeurs ne sont pas comparables d'un doigt au suivant.
    """

    name: str
    pressure: tuple[float, ...]
    raw: tuple[int, ...]
    normal_force: float | None = None
    tangential_force: float | None = None
    direction: float | None = None
    proximity: float | None = None
    #: Voie de proximité interne au bloc de pression, présente uniquement sur
    #: les doigts à capteur unique. Elle monte quand l'objet approche, sans contact.
    self_proximity: float | None = None

    @property
    def pressure_max(self) -> float:
        return max(self.pressure, default=0.0)

    @property
    def pressure_mean(self) -> float:
        return sum(self.pressure) / len(self.pressure) if self.pressure else 0.0

    @property
    def contact(self) -> bool:
        """Le bruit de fond mesuré est sous 1 LSB : 2 % est déjà un franc contact."""
        return self.pressure_max > 0.02

    def to_dict(self) -> dict:
        return {
            "pressure": [round(v, 4) for v in self.pressure],
            "pressure_max": round(self.pressure_max, 4),
            # Le brut est conservé : ``pressure`` est bornée à zéro par le bas,
            # donc une pression qui *diminue* — objet soulevé de la paume, par
            # exemple — n'y laisse aucune trace. Sans le brut, l'information
            # est perdue pour de bon.
            "raw": list(self.raw),
            "normal_force": self.normal_force,
            "tangential_force": self.tangential_force,
            "direction": self.direction,
            "proximity": self.proximity,
            "self_proximity": self.self_proximity,
        }


@dataclass(frozen=True)
class TactileState:
    """L'état des onze zones à un instant."""

    zones: dict[str, Zone] = field(default_factory=dict)
    slot_count: int = 0

    def __getitem__(self, name: str) -> Zone:
        return self.zones[name]

    @property
    def in_contact(self) -> list[str]:
        return [n for n, z in self.zones.items() if z.contact]

    def to_dict(self) -> dict:
        return {n: z.to_dict() for n, z in self.zones.items()}


# ── Décodage ──────────────────────────────────────────────────────────────────


def is_tactile(buf: bytes) -> bool:
    return len(buf) >= TPDO_SIZE and buf[0] == FRAME_TACTILE


def slot_offset(index: int) -> int:
    return PAYLOAD_OFFSET + SLOT_SIZE * index


def _u16(raw: bytes, off: int) -> int:
    return struct.unpack_from("<H", raw, off)[0]


def _direction(value: int) -> float | None:
    return None if value == NO_DIRECTION else float(value)


def is_motor(buf: bytes) -> bool:
    return len(buf) >= TPDO_SIZE and buf[0] == FRAME_MOTOR


def decode_motor_frame(buf: bytes) -> dict:
    """
    Décode une trame d'état moteur, telle qu'elle arrive sur le fil.

    Sert à vérifier ce que l'API du SDK raconte : la position rendue par
    ``get_now_position`` passe par la bibliothèque, celle-ci vient du variateur.
    Quand les deux divergent, c'est la trame qui a raison.

    Le créneau d'un moteur donné n'est pas connu d'avance — l'ordre des axes sur
    le fil ne suit pas forcément la numérotation du SDK. On rend donc les 27
    créneaux et c'est à la corrélation avec un mouvement connu de trancher.
    """
    if len(buf) < TPDO_SIZE:
        raise TpdoError(f"trame de {len(buf)} octets, {TPDO_SIZE} attendus")
    if buf[0] != FRAME_MOTOR:
        raise TpdoError(f"type 0x{buf[0]:02x}, 0x{FRAME_MOTOR:02x} attendu")

    slots = []
    for i in range(MOTOR_SLOT_COUNT):
        off = PAYLOAD_OFFSET + MOTOR_SLOT_SIZE * i
        slots.append({"slot": i,
                      "position": _u16(buf, off),
                      "velocity": _u16(buf, off + 2),
                      "current": _u16(buf, off + 4)})
    status = list(buf[MOTOR_STATUS_OFFSET:MOTOR_STATUS_OFFSET + MOTOR_SLOT_COUNT])
    return {"declared_count": buf[1], "slots": slots, "status": status}


def raw_channels(buf: bytes) -> dict[str, list[int]]:
    """Canaux de pression bruts par zone. Base de la remise à zéro."""
    out: dict[str, list[int]] = {}
    for k in range(min(buf[1], SLOT_COUNT)):
        base = slot_offset(k)
        slot = SLOT_ORDER[k]
        if slot == PALM:
            out[PALM] = [buf[base + i] for i in PALM_TOUCH]
        elif slot in SINGLE_SENSOR:
            out[slot] = [buf[base + i] for i in SINGLE_TOUCH]
        else:
            out[f"{slot}.tip"] = [buf[base + i] for i in TIP_TOUCH]
            out[f"{slot}.pad"] = [buf[base + i] for i in PAD_TOUCH]
    return out


class TactileReader:
    """
    Décodeur avec remise à zéro par canal.

    Sans ligne de base, ``pressure`` rend la valeur absolue du canal, qui n'a
    pas de sens physique. Appeler :meth:`zero` une fois, main au repos et rien
    en contact.
    """

    def __init__(self) -> None:
        self._base: dict[str, list[float]] = {}

    @property
    def zeroed(self) -> bool:
        return bool(self._base)

    @property
    def baseline(self) -> dict[str, list[float]]:
        """
        Ligne de base par canal, telle qu'elle a été mesurée.

        À enregistrer avec les données : c'est la seule façon de savoir après
        coup si le zéro d'une session vaut pour une autre, et de refaire le
        calcul autrement si besoin.
        """
        return {n: [round(v, 2) for v in vals] for n, vals in self._base.items()}

    def zero(self, frames) -> int:
        """
        Fixe la ligne de base à partir de trames au repos.

        Args:
            frames: une trame de 192 octets, ou un itérable de trames.

        Returns:
            le nombre de trames retenues.
        """
        if isinstance(frames, (bytes, bytearray)):
            frames = [frames]
        kept = [bytes(f) for f in frames if is_tactile(f)]
        if not kept:
            raise TpdoError("aucune trame tactile pour la remise à zéro")

        acc: dict[str, list[float]] = {}
        for f in kept:
            for name, chan in raw_channels(f).items():
                acc.setdefault(name, [0.0] * len(chan))
                for i, v in enumerate(chan):
                    acc[name][i] += v
        self._base = {n: [v / len(kept) for v in vals] for n, vals in acc.items()}
        return len(kept)

    def reset_zero(self) -> None:
        self._base = {}

    def decode(self, buf: bytes) -> TactileState:
        if len(buf) < TPDO_SIZE:
            raise TpdoError(f"trame de {len(buf)} octets, {TPDO_SIZE} attendus")
        if buf[0] != FRAME_TACTILE:
            raise TpdoError(f"type 0x{buf[0]:02x}, 0x{FRAME_TACTILE:02x} attendu")

        count = min(buf[1], SLOT_COUNT)
        zones: dict[str, Zone] = {}

        for k in range(count):
            base = slot_offset(k)
            raw = bytes(buf[base:base + SLOT_SIZE])
            slot = SLOT_ORDER[k]

            if slot == PALM:
                zones[PALM] = self._zone(PALM, [raw[i] for i in PALM_TOUCH])
                continue

            if slot in SINGLE_SENSOR:
                # Un seul capteur : cinq canaux de pression, et le sixième est
                # une proximité qu'il ne faut surtout pas compter comme un appui.
                zones[slot] = self._zone(
                    slot,
                    [raw[i] for i in SINGLE_TOUCH],
                    normal_force=_u16(raw, NF["tip"]) / FORCE_SCALE,
                    tangential_force=_u16(raw, TF["tip"]) / FORCE_SCALE,
                    direction=_direction(_u16(raw, DIR["tip"])),
                    proximity=raw[PROX["tip"]] / TOUCH_FULL_SCALE,
                    self_proximity=raw[SINGLE_PROX] / TOUCH_FULL_SCALE,
                )
                continue

            for part, channels in (("tip", TIP_TOUCH), ("pad", PAD_TOUCH)):
                name = f"{slot}.{part}"
                zones[name] = self._zone(
                    name,
                    [raw[i] for i in channels],
                    normal_force=_u16(raw, NF[part]) / FORCE_SCALE,
                    tangential_force=_u16(raw, TF[part]) / FORCE_SCALE,
                    direction=_direction(_u16(raw, DIR[part])),
                    proximity=raw[PROX[part]] / TOUCH_FULL_SCALE,
                )
        return TactileState(zones=zones, slot_count=count)

    def _zone(self, name: str, raw: list[int], **extra) -> Zone:
        base = self._base.get(name)
        if base is None:
            pressure = tuple(v / TOUCH_FULL_SCALE for v in raw)
        else:
            pressure = tuple(
                max(0.0, (v - b) / max(TOUCH_FULL_SCALE - b, 1.0))
                for v, b in zip(raw, base)
            )
        return Zone(name=name, pressure=pressure, raw=tuple(raw), **extra)


# ── Fabrication de trames, pour les tests hors banc ───────────────────────────


def build_frame(zones: dict[str, dict] | None = None, count: int = SLOT_COUNT) -> bytes:
    """
    Fabrique une trame tactile synthétique.

    ``zones`` associe un nom de zone à un dict de champs, par exemple
    ``{"index.pad": {"touch": [200]*4, "nf": 1370}}``.
    """
    buf = bytearray(TPDO_SIZE)
    buf[0], buf[1] = FRAME_TACTILE, count
    for name, spec in (zones or {}).items():
        if name == PALM:
            base = slot_offset(SLOT_ORDER.index(PALM))
            for i, v in enumerate(spec.get("touch", [])):
                buf[base + i] = v
            continue
        finger, _, part = name.partition(".")
        base = slot_offset(SLOT_ORDER.index(finger))
        if finger in SINGLE_SENSOR:
            channels, part = SINGLE_TOUCH, "tip"
            if "sprox" in spec:
                buf[base + SINGLE_PROX] = spec["sprox"]
        else:
            channels = TIP_TOUCH if part == "tip" else PAD_TOUCH
        for i, v in zip(channels, spec.get("touch", [])):
            buf[base + i] = v
        for key, table in (("nf", NF), ("tf", TF), ("dir", DIR)):
            if key in spec:
                struct.pack_into("<H", buf, base + table[part], spec[key])
        if "prox" in spec:
            buf[base + PROX[part]] = spec["prox"]
    return bytes(buf)


# ─────────────────────────── Trame moteur ─────────────────────────────────
#
# Le SDK ne documente pas cette trame et n'expose ses champs que par des appels
# vivants : sans elle, un flux enregistré ne rend que le tactile, et la position
# des doigts reste inaccessible une fois le banc éteint.
#
# La table ci-dessous a été **établie par la mesure**, en confrontant deux
# sessions de palpation à leur flux brut : horloges recalées sur le signal de
# position, puis corrélation champ par champ.
#
#   position : r = 1,0000 sur les quatre doigts, erreur médiane 0 à 10 counts
#   courant  : r = 0,985 à 0,998, et décorrélé de la position (0,01 à 0,44)
#
# Les moteurs occupent des créneaux de 6 octets, en ordre **décroissant**
# d'identifiant à partir de l'offset 14 :
#
#   +0..1  position, u16 little-endian, en counts
#   +2..3  non identifié — sans corrélation avec consigne, position ni courant.
#          Candidat vitesse signée ; laissé brut plutôt que nommé à tort.
#   +4..5  courant, u16 little-endian, en pour mille du couple nominal

#: Ordre des moteurs dans les créneaux de la trame, **mesuré sur la main**.
#:
#: Ce ne sont pas les identifiants de moteur qui ordonnent les créneaux, mais
#: les **articulations dans l'ordre de l'URDF** : ``finger11, finger12,
#: finger21, finger31, finger41, finger51``. Traduit en moteurs, cela donne
#: 2, 1, 6, 5, 4, 3 — le pouce d'abord, puis les longs doigts de l'index à
#: l'auriculaire.
#:
#: La formule précédente, ``14 + (6 - m) * 6``, tombait juste pour les moteurs
#: 3 à 6 et **plaçait les deux du pouce quinze octets trop loin**. Conséquence :
#: ``positions()`` et ``currents()`` rendaient zéro pour le pouce quoi qu'il
#: fasse, et l'on en concluait un moteur non alimenté.
#:
#: Mesuré le 2026-08-26 en confrontant la trame au SDK : commandé à 5000, le
#: pivot du pouce apparaît à l'offset 2 et sa flexion à l'offset 8, tandis que
#: ``get_now_position`` suivait la consigne de 0 à 5000. C'est ce désaccord
#: entre les deux lectures qui a révélé le décalage.
MOTOR_ORDRE = (2, 1, 6, 5, 4, 3)

#: Premier octet des créneaux moteur.
MOTOR_SLOT_BASE = PAYLOAD_OFFSET
#: Taille d'un créneau moteur, en octets.
MOTOR_SLOT_SIZE = 6
#: Type de trame portant l'état moteur.
FRAME_MOTOR = 0x00


def motor_slot(motor: int) -> int:
    """
    Offset du créneau d'un moteur dans la trame, en octets.

    L'ordre est celui de :data:`MOTOR_ORDRE`, mesuré sur la main : les créneaux
    suivent les articulations de l'URDF, pas les numéros de moteur.
    """
    if motor not in MOTOR_ORDRE:
        raise ValueError(f"moteur {motor} hors de {sorted(MOTOR_ORDRE)}")
    return MOTOR_SLOT_BASE + MOTOR_ORDRE.index(motor) * MOTOR_SLOT_SIZE


def decode_motor(buf: bytes) -> dict:
    """
    Décode une trame d'état moteur : position et courant des six moteurs.

    Args:
        buf: les 192 octets d'une trame de type ``0x00``.

    Returns:
        ``{"positions": {m: counts}, "currents": {m: pour-mille},
        "raw_mid": {m: u16}}`` — ``raw_mid`` étant le champ non identifié,
        conservé tel quel pour qu'une relecture ultérieure puisse l'exploiter
        sans avoir à relire les octets.

    Raises:
        ValueError: si la trame n'est pas du bon type ou de la bonne taille.
    """
    if len(buf) < MOTOR_SLOT_BASE + 6 * MOTOR_SLOT_SIZE:
        raise ValueError(f"trame trop courte : {len(buf)} octets")
    if buf[0] != FRAME_MOTOR:
        raise ValueError(f"type de trame 0x{buf[0]:02x}, attendu "
                         f"0x{FRAME_MOTOR:02x} (état moteur)")
    positions, courants, milieu, bruts = {}, {}, {}, {}
    for m in range(1, 7):
        o = motor_slot(m)
        brut = _u16(buf, o)
        bruts[m] = brut
        # La position est **signee**. Un doigt pousse au-dela de son zero
        # rend 63993 plutot que -1543, et le getter du SDK, lui, ecrete a 0 :
        # tout outil qui juge le mouvement sur `positions()` conclut alors
        # « parti de 0, arrive a 0, immobile » pendant que le doigt remonte
        # bel et bien vers zero. C'est ce qui a fait passer quatre moteurs
        # sains pour muets pendant des heures.
        positions[m] = brut - 65536 if brut >= 32768 else brut
        milieu[m] = _u16(buf, o + 2)
        courants[m] = _u16(buf, o + 4)
    return {"positions": positions, "currents": courants,
            "raw_mid": milieu, "positions_u16": bruts}


def decode_stream(data, types=None):
    """
    Décode un flux brut enregistré, trame par trame.

    Args:
        data: tableau ``(N, 192)`` d'octets, tel que le rend
            :func:`vt_tactile.dataset.write_raw_stream`.
        types: types de trames à décoder ; par défaut les deux.

    Yields:
        ``(indice, type, dict)`` — le dict venant de :func:`decode_motor` pour
        les trames moteur, et le décodeur tactile restant à la charge de
        l'appelant, qui seul sait quelle remise à zéro appliquer.
    """
    for i, ligne in enumerate(data):
        brut = bytes(ligne)
        if types is not None and brut[0] not in types:
            continue
        if brut[0] == FRAME_MOTOR:
            yield i, brut[0], decode_motor(brut)
        else:
            yield i, brut[0], None
