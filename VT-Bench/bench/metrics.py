#!/usr/bin/env python3
"""
metrics.py — statistiques sur les trames, sans hypothèse sur leur contenu.

Ce module ne sait pas ce qu'est un doigt. Il voit un flux de trames de 192
octets et il en mesure : la santé du flux, le comportement de chaque octet au
repos, et la réaction de chaque octet à un stimulus. La structure — s'il y en a
une — est **déduite** des données par autocorrélation, jamais postulée.

C'est délibéré : une caractérisation matérielle qui part d'un mapping supposé
ne peut que confirmer ce mapping.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

TPDO_SIZE = 192
HEADER_BYTES = 2

#: Au-delà de cet écart-type au repos, un octet est dit bruyant. 1 LSB sur
#: 8 bits, c'est le tremblement normal d'un canal capacitif ; au-delà de 3, le
#: canal mange une partie utile de sa dynamique.
NOISE_LSB = 1.0
NOISY_LSB = 3.0
#: En deçà de ce z-score, on considère qu'un octet n'a pas réagi au stimulus.
RESPONSE_Z = 6.0
#: Plancher sur l'écart-type de référence. Sans lui, un octet parfaitement
#: constant au repos donne un z infini dès qu'il bouge d'un cran, et tout se
#: met à « répondre » — ce qui ne distingue plus rien.
SIGMA_FLOOR = 0.5


# ── Santé du flux ──────────────────────────────────────────────────────────────


@dataclass
class StreamHealth:
    """
    Qualité du flux TPDO, indépendamment de son contenu.

    C'est ici que se voit un problème d'hôte (VM, ordonnancement, lien) : si la
    main émet régulièrement mais que l'hôte relit la même trame dix fois de
    suite, ``fresh_ratio`` s'effondre alors que ``poll_rate_hz`` reste haut.
    """

    duration_s: float = 0.0
    polls: int = 0
    fresh: int = 0
    poll_rate_hz: float = 0.0
    fresh_rate_hz: float = 0.0
    fresh_ratio: float = 0.0
    types: dict[str, int] = field(default_factory=dict)
    fresh_by_type: dict[str, int] = field(default_factory=dict)
    interval_ms: dict[str, float] = field(default_factory=dict)
    longest_stall_ms: float = 0.0
    header_len_values: dict[str, list[int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def stream_health(frames) -> StreamHealth:
    h = StreamHealth()
    if not frames:
        return h

    h.duration_s = round(frames[-1].t - frames[0].t, 3)
    h.polls = len(frames)

    types = Counter()
    fresh_by_type = Counter()
    gaps: list[float] = []
    len_values: dict[int, set] = defaultdict(set)

    prev_data = None
    prev_fresh_t = frames[0].t
    for fr in frames:
        t = fr.data[0]
        types[t] += 1
        len_values[t].add(fr.data[1])
        if fr.data != prev_data:
            fresh_by_type[t] += 1
            h.fresh += 1
            gaps.append((fr.t - prev_fresh_t) * 1000.0)
            prev_fresh_t = fr.t
            prev_data = fr.data

    h.types = {f"0x{k:02x}": v for k, v in sorted(types.items())}
    h.fresh_by_type = {f"0x{k:02x}": v for k, v in sorted(fresh_by_type.items())}
    h.header_len_values = {f"0x{k:02x}": sorted(v) for k, v in sorted(len_values.items())}

    if h.duration_s > 0:
        h.poll_rate_hz = round(h.polls / h.duration_s, 1)
        h.fresh_rate_hz = round(h.fresh / h.duration_s, 1)
    h.fresh_ratio = round(h.fresh / h.polls, 3) if h.polls else 0.0

    if len(gaps) > 2:
        ordered = sorted(gaps[1:])
        h.interval_ms = {
            "median": round(statistics.median(ordered), 2),
            "p95": round(ordered[int(0.95 * (len(ordered) - 1))], 2),
            "max": round(ordered[-1], 2),
        }
        h.longest_stall_ms = round(ordered[-1], 2)
    return h


# ── Comportement des octets au repos ───────────────────────────────────────────


@dataclass
class ByteStat:
    index: int
    mean: float
    std: float
    lo: int
    hi: int
    distinct: int
    drift: float
    verdict: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def byte_stats(frames, frame_type: int) -> list[ByteStat]:
    """
    Statistiques par octet sur les trames d'un type donné.

    ``drift`` compare la moyenne du premier dixième de la fenêtre au dernier :
    c'est ce qui distingue un capteur qui dérive d'un capteur qui bruite.

    Verdicts : ``mort`` (toujours nul), ``figé`` (constant non nul),
    ``stable`` (σ ≤ 1 LSB), ``bruyant`` (σ ≤ 3 LSB), ``très bruyant`` au-delà.
    """
    sel = [f.data for f in frames if f.data[0] == frame_type]
    out: list[ByteStat] = []
    if len(sel) < 4:
        return out

    tenth = max(2, len(sel) // 10)
    for i in range(HEADER_BYTES, TPDO_SIZE):
        col = [d[i] for d in sel]
        lo, hi = min(col), max(col)
        mean = statistics.fmean(col)
        std = statistics.pstdev(col)
        drift = statistics.fmean(col[-tenth:]) - statistics.fmean(col[:tenth])
        if hi == 0:
            verdict = "mort"
        elif lo == hi:
            verdict = "figé"
        elif std > NOISY_LSB:
            verdict = "très bruyant"
        elif std > NOISE_LSB:
            verdict = "bruyant"
        else:
            verdict = "stable"
        out.append(ByteStat(i, round(mean, 2), round(std, 3), lo, hi,
                            len(set(col)), round(drift, 2), verdict))
    return out


def summarise_bytes(stats: list[ByteStat]) -> dict:
    counts = Counter(s.verdict for s in stats)
    active = [s for s in stats if s.verdict != "mort"]
    return {
        "total": len(stats),
        "mort": counts["mort"],
        "figé": counts["figé"],
        "stable": counts["stable"],
        "bruyant": counts["bruyant"],
        "très bruyant": counts["très bruyant"],
        "std_median_actifs": round(statistics.median([s.std for s in active]), 3)
        if active else 0.0,
        "drift_max_abs": round(max((abs(s.drift) for s in stats), default=0.0), 2),
        "saturés": [s.index for s in stats if s.hi == 255],
    }


def infer_stride(stats: list[ByteStat], max_stride: int = 64) -> dict:
    """
    Déduit la périodicité du motif d'octets actifs, sans la supposer.

    On construit le masque « cet octet est-il autre chose que mort ? » et on
    cherche le décalage qui le fait le mieux coïncider avec lui-même. Si la
    charge utile est une suite de créneaux identiques, le pas ressort tout seul.
    Sinon, le score reste bas et c'est une information en soi.
    """
    if not stats:
        return {}
    mask = [0 if s.verdict == "mort" else 1 for s in stats]
    n = len(mask)
    scores = {}
    for stride in range(4, min(max_stride, n // 2) + 1):
        pairs = n - stride
        agree = sum(1 for i in range(pairs) if mask[i] == mask[i + stride])
        scores[stride] = agree / pairs
    if not scores:
        return {}
    best = max(scores, key=lambda s: (scores[s], -s))
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:5]
    return {
        "pas_probable": best,
        "score": round(scores[best], 3),
        "top5": [{"pas": s, "score": round(v, 3)} for s, v in ranked],
        "octets_actifs": sum(mask),
    }


# ── Réaction à un stimulus ─────────────────────────────────────────────────────


def response(rest_frames, stim_frames, frame_type: int) -> dict[int, float]:
    """
    z-score de chaque octet entre une fenêtre de repos et une de stimulus.

    Un octet ne « répond » que s'il bouge nettement plus que son propre bruit
    au repos — ce qui évite de baptiser réactif un octet simplement bruyant.
    """
    rest = [f.data for f in rest_frames if f.data[0] == frame_type]
    stim = [f.data for f in stim_frames if f.data[0] == frame_type]
    if len(rest) < 4 or len(stim) < 4:
        return {}
    out = {}
    for i in range(HEADER_BYTES, TPDO_SIZE):
        r = [d[i] for d in rest]
        s = [d[i] for d in stim]
        sigma = max(statistics.pstdev(r), SIGMA_FLOOR)
        out[i] = abs(statistics.fmean(s) - statistics.fmean(r)) / sigma
    return out


def amplitude(rest_frames, stim_frames, frame_type: int) -> dict[int, float]:
    """Écart de moyenne en LSB entre repos et stimulus. Le z dit « est-ce que
    ça bouge », l'amplitude dit « de combien » — c'est elle qui parle de la
    qualité du capteur."""
    rest = [f.data for f in rest_frames if f.data[0] == frame_type]
    stim = [f.data for f in stim_frames if f.data[0] == frame_type]
    if len(rest) < 4 or len(stim) < 4:
        return {}
    return {i: round(statistics.fmean([d[i] for d in stim])
                     - statistics.fmean([d[i] for d in rest]), 1)
            for i in range(HEADER_BYTES, TPDO_SIZE)}


def responders(z: dict[int, float], threshold: float = RESPONSE_Z) -> list[int]:
    return sorted((i for i, v in z.items() if v >= threshold), key=lambda i: -z[i])


# ── Séries de l'API du SDK ─────────────────────────────────────────────────────


def sdk_series_stats(samples: list[dict]) -> dict:
    """
    Résume une suite d'appels à l'API tactile du SDK.

    Pour chaque capteur et chaque grandeur : la valeur bouge-t-elle, reste-t-elle
    figée, ou l'appel échoue-t-il systématiquement ? Un id qui répond toujours
    la même chose et un id qui renvoie toujours une erreur sont deux pannes
    différentes, et il faut pouvoir les distinguer.
    """
    if not samples:
        return {}
    out: dict[str, dict] = {}
    for sid, first in samples[0].items():
        entry: dict = {"label": first["label"]}
        for field_name in ("pressure", "normal_force", "tangential_force",
                           "force_direction", "proximity"):
            values, errors = [], Counter()
            for snap in samples:
                cell = snap.get(sid, {}).get(field_name, {})
                if "value" in cell:
                    v = cell["value"]
                    values += list(v) if isinstance(v, (list, tuple)) else [v]
                else:
                    errors[str(cell.get("error_code", cell.get("error", "?")))] += 1
            if values:
                entry[field_name] = {
                    "n": len(values),
                    "min": round(min(values), 4),
                    "max": round(max(values), 4),
                    "std": round(statistics.pstdev(values), 5) if len(values) > 1 else 0.0,
                    "constant": min(values) == max(values),
                }
            else:
                entry[field_name] = {"n": 0, "errors": dict(errors)}
        counts = {s[sid].get("sensor_pos_count") for s in samples}
        entry["sensor_pos_count"] = sorted(c for c in counts if c is not None) or None
        out[str(sid)] = entry
    return out
