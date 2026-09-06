#!/usr/bin/env python3
"""
analyse.py — ce que contiennent vraiment les trames EtherCAT enregistrées.

Le flux brut est la seule pièce du jeu de données qui ne se rejoue pas, et
c'est aussi la seule qu'on ne regarde jamais : elle est faite d'octets. Ce
module la relit et répond aux questions qui décident si une session vaut
quelque chose.

**Le bus a-t-il tenu ?** Cadence, trous, doublons. Une trame perdue ne se voit
pas dans le fichier — il est simplement plus court — et un décrochage au milieu
d'une saisie enlève précisément le moment intéressant.

**Les doigts ont-ils bougé ?** Le variateur accepte des consignes qu'il
n'exécute jamais. La position lue dans la trame est la seule preuve, et elle
est **signée** : un doigt repoussé sous son zéro rend 63993 pour −1543, que le
getter du SDK écrête à 0.

**Les capteurs ont-ils répondu ?** Une zone à zéro pendant toute une saisie ne
prouve pas une panne : il faut d'abord établir qu'elle a été sollicitée. C'est
l'erreur qui a fait conclure à tort à une carte capteurs morte.

Rien n'est recalculé ni corrigé ici : on lit les octets et on dit ce qu'ils
contiennent, y compris quand ils ne contiennent rien.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

#: Plafond du tampon d'enregistrement, en trames. Un flux qui l'atteint
#: **exactement** a été tronqué : la suite n'a jamais été écrite.
RECORD_MAX_FRAMES = 400_000

#: Intervalle nominal entre deux trames, en secondes. Mesuré sur le bus :
#: médiane 1,11 ms, p95 1,16 ms.
INTERVALLE_NOMINAL = 0.00111

#: Au-delà de ce multiple de l'intervalle nominal, on parle de trou. Cinq
#: trames manquantes d'affilée sont un décrochage, pas une gigue.
SEUIL_TROU = 5.0

#: Part minimale de trames capteur dans un flux sain. La main alterne les deux
#: types à parts égales : tout ce qui descend nettement sous la moitié signale
#: des trames capteur **perdues**, pas une variation de cadence.
SEUIL_PART_TACTILE = 0.35


def flux_dune_session(racine) -> list:
    """Tous les flux bruts d'une session, chemins triés."""
    r = Path(racine)
    return sorted(r.rglob("*_data.npy"))


def analyser_flux(chemin) -> dict:
    """
    Caractérise un flux brut : bus, moteurs, tactile.

    Args:
        chemin: le ``*_data.npy``. Le fichier de temps est déduit du nom.

    Returns:
        un dictionnaire sérialisable, prêt pour un rapport ou un manifeste.
    """
    import sys  # noqa: PLC0415

    chemin = Path(chemin)
    # ``mmap_mode`` : ces fichiers font des dizaines de mégaoctets et l'on n'en
    # lit qu'une fraction pour la plupart des mesures.
    data = np.load(chemin, mmap_mode="r")
    temps = _charger_temps(chemin)

    res = {"fichier": str(chemin), "trames": int(data.shape[0]),
           "octets_par_trame": int(data.shape[1])}
    res.update(_bus(data, temps))
    res.update(_moteurs(data))
    res.update(_tactile(data))
    return res


def _charger_temps(chemin: Path):
    for nom in (chemin.name.replace("_data.npy", "_t.npy"),
                chemin.name.replace("_data.npy", "_times.npy")):
        p = chemin.with_name(nom)
        if p.exists():
            return np.load(p)
    return None


# ── Le bus ────────────────────────────────────────────────────────────────────

def _bus(data, temps) -> dict:
    """Cadence, trous, doublons — le flux a-t-il tenu ?"""
    types, comptes = np.unique(np.asarray(data[:, 0]), return_counts=True)
    compte = {int(t): int(c) for t, c in zip(types, comptes)}
    moteur, tactile = compte.get(0x00, 0), compte.get(0x40, 0)
    total = moteur + tactile
    part = (tactile / total) if total else 0.0
    r = {
        "types": {f"0x{t:02x}": c for t, c in compte.items()},
        "tronque": bool(data.shape[0] >= RECORD_MAX_FRAMES),
        "part_tactile": round(part, 3),
        # La main alterne trames moteur et trames capteur à parts égales. Un
        # flux qui tombe très en dessous n'est pas bruité : il a **perdu** ses
        # trames capteur, et le tactile de cette période n'existe pas.
        #
        # Le mécanisme est connu : ``home_motors`` remet la configuration des
        # trames à sa valeur par défaut, et ``set_tpdo_frame_type`` doit être
        # réémis **après** le homing. Un réveil en cours de session sans cette
        # réémission donne exactement ce profil.
        "tactile_appauvri": bool(total > 1000 and part < SEUIL_PART_TACTILE),
        "tactile_manquant_estime": int(max(0, total / 2 - tactile)) if total else 0,
    }

    if temps is None or len(temps) < 2:
        r["duree_s"] = None
        return r

    dt = np.diff(np.asarray(temps, dtype=float))
    span = float(temps[-1] - temps[0])
    trous = dt > INTERVALLE_NOMINAL * SEUIL_TROU
    r.update({
        "duree_s": round(span, 2),
        "cadence_hz": round(len(temps) / span, 1) if span > 0 else None,
        "intervalle_median_ms": round(float(np.median(dt)) * 1000, 3),
        "intervalle_p95_ms": round(float(np.percentile(dt, 95)) * 1000, 3),
        "temps_croissants": bool(np.all(dt >= 0)),
        "trous": int(trous.sum()),
        "trou_max_s": round(float(dt.max()), 3) if len(dt) else None,
        # Le temps réellement perdu compte plus que le nombre de trous : cent
        # micro-trous ne valent pas un décrochage de trois secondes au milieu
        # d'une fermeture.
        "temps_perdu_s": round(float(dt[trous].sum()), 2) if trous.any() else 0.0,
    })

    # Doublons : deux trames identiques à la suite. Au repos c'est **normal** —
    # la charge utile ne bouge pas — et c'est précisément ce qu'il ne faut pas
    # dédupliquer. On le mesure pour savoir quelle part du flux est immobile.
    ech = _echantillon(data, 40_000)
    if len(ech) > 1:
        identiques = np.all(ech[1:] == ech[:-1], axis=1)
        r["part_immobile"] = round(float(identiques.mean()), 3)
    return r


def _echantillon(data, n: int):
    """Un sous-échantillon régulier, pour les mesures qui n'ont pas besoin de tout."""
    total = data.shape[0]
    if total <= n:
        return np.asarray(data)
    pas = max(1, total // n)
    return np.asarray(data[::pas])


# ── Les moteurs ───────────────────────────────────────────────────────────────

def _moteurs(data) -> dict:
    """
    Trajectoire et courant de chaque moteur, lus dans les trames d'état.

    La position est **signée** : un doigt repoussé sous son zéro rend 63993
    pour −1543. Le getter du SDK écrête ces valeurs à zéro, ce qui a fait passer
    quatre moteurs sains pour muets pendant une nuit entière.
    """
    from vt_tactile import hardware as hw  # noqa: PLC0415
    from vt_tactile.tpdo import FRAME_MOTOR, motor_slot  # noqa: PLC0415

    lignes = np.asarray(data[np.asarray(data[:, 0]) == FRAME_MOTOR])
    if not len(lignes):
        return {"moteurs": {}, "trames_moteur": 0}

    out = {}
    for m in hw.MOTOR_IDS:
        o = motor_slot(m)
        brut = lignes[:, o].astype(np.int32) | (lignes[:, o + 1].astype(np.int32) << 8)
        pos = np.where(brut >= 32768, brut - 65536, brut)
        cur = lignes[:, o + 4].astype(np.int32) | (lignes[:, o + 5].astype(np.int32) << 8)

        course = int(pos.max() - pos.min())
        out[hw.MOTOR_NAMES[m]] = {
            "moteur": m,
            "position_min": int(pos.min()),
            "position_max": int(pos.max()),
            "course_counts": course,
            "a_bouge": bool(course > 100),
            "passe_sous_zero": bool(pos.min() < -20),
            "courant_median": int(np.median(cur)),
            "courant_max": int(cur.max()),
            # Immobile **à courant élevé** = obstacle mécanique, pas ordre perdu.
            # C'est la distinction qui dit s'il faut réémettre ou s'arrêter.
            "bloque": bool(course <= 100 and cur.max() >= 500),
        }
    return {"moteurs": out, "trames_moteur": int(len(lignes))}


# ── Le tactile ────────────────────────────────────────────────────────────────

def _tactile(data) -> dict:
    """
    Amplitude de chaque zone, en counts bruts.

    En counts et non en pression normalisée : la normalisation dépend d'une
    ligne de base qu'on n'a pas forcément ici, alors que l'amplitude brute dit
    directement si la zone a été sollicitée. Une zone à zéro ne prouve rien tant
    qu'on n'a pas établi qu'on avait appuyé dessus.
    """
    from vt_tactile.tpdo import (  # noqa: PLC0415
        FRAME_TACTILE, PAD_TOUCH, PALM, PALM_TOUCH, SINGLE_SENSOR, SINGLE_TOUCH,
        SLOT_ORDER, TIP_TOUCH, slot_offset,
    )

    lignes = data[np.asarray(data[:, 0]) == FRAME_TACTILE]
    if not len(lignes):
        return {"zones": {}, "trames_tactile": 0}
    ech = _echantillon(lignes, 20_000)

    zones = {}
    for k, slot in enumerate(SLOT_ORDER):
        base = slot_offset(k)
        if slot == PALM:
            canaux = {PALM: [base + i for i in PALM_TOUCH]}
        elif slot in SINGLE_SENSOR:
            canaux = {slot: [base + i for i in SINGLE_TOUCH]}
        else:
            canaux = {f"{slot}.tip": [base + i for i in TIP_TOUCH],
                      f"{slot}.pad": [base + i for i in PAD_TOUCH]}
        for nom, idx in canaux.items():
            v = ech[:, idx].astype(np.int32)
            amplitude = int(v.max() - v.min())
            zones[nom] = {
                "amplitude_counts": amplitude,
                "repos_median": int(np.median(v)),
                "pic": int(v.max()),
                # Le seuil est bas exprès : on cherche « a-t-elle bougé ? »,
                # pas « a-t-elle beaucoup bougé ». Le bruit de fond mesuré est
                # sous 1 LSB.
                "a_repondu": bool(amplitude >= 5),
                "sature": bool(v.max() >= 255),
            }
    return {"zones": zones, "trames_tactile": int(len(lignes))}


# ── Rapport ───────────────────────────────────────────────────────────────────

def analyser_session(racine) -> dict:
    """Tous les flux d'une session, plus un résumé."""
    r = Path(racine)
    flux = []
    for f in flux_dune_session(r):
        d = analyser_flux(f)
        # L'étiquette doit **identifier** le flux. Le seul nom du dossier parent
        # ne suffit pas : une session en a six qui s'appellent tous
        # « raw_precedent », et un rapport où trois lignes portent le même nom
        # ne se lit pas.
        d["etiquette"] = str(f.relative_to(r).parent)
        flux.append(d)
    total = sum(f["trames"] for f in flux)
    duree = sum(f.get("duree_s") or 0 for f in flux)
    bouges = set()
    repondu = set()
    for f in flux:
        bouges |= {n for n, m in f.get("moteurs", {}).items() if m["a_bouge"]}
        repondu |= {n for n, z in f.get("zones", {}).items() if z["a_repondu"]}
    return {
        "session": r.name, "racine": str(r), "flux": flux,
        "resume": {
            "flux": len(flux), "trames": total, "duree_s": round(duree, 1),
            "moteurs_ayant_bouge": sorted(bouges),
            "zones_ayant_repondu": sorted(repondu),
            "flux_tronques": sum(1 for f in flux if f.get("tronque")),
            "flux_sans_tactile": sum(1 for f in flux if f.get("tactile_appauvri")),
            "temps_perdu_s": round(sum(f.get("temps_perdu_s") or 0 for f in flux), 1),
        },
    }


def rendre(rapport: dict, verbeux: bool = True) -> str:
    """Le rapport en texte, pour la console."""
    L = []
    res = rapport["resume"]
    L.append(f"\n{rapport['session']}")
    L.append(f"  {res['flux']} flux · {res['trames']:,} trames · "
             f"{res['duree_s']} s".replace(",", " "))
    if res["flux_tronques"]:
        L.append(f"  ⚠ {res['flux_tronques']} flux tronqué(s) au plafond de "
                 f"{RECORD_MAX_FRAMES:,} trames".replace(",", " "))
    if res["temps_perdu_s"]:
        L.append(f"  ⚠ {res['temps_perdu_s']} s perdues en décrochages")
    if res.get("flux_sans_tactile"):
        L.append(f"  ⚠ {res['flux_sans_tactile']} flux ont perdu leurs trames "
                 f"capteur — le tactile de ces périodes n'existe pas")
    L.append(f"  moteurs ayant bougé : {', '.join(res['moteurs_ayant_bouge']) or 'aucun'}")
    L.append(f"  zones ayant répondu : {', '.join(res['zones_ayant_repondu']) or 'aucune'}")

    if not verbeux:
        return "\n".join(L)

    for f in rapport["flux"]:
        nom = f.get("etiquette") or Path(f["fichier"]).parent.name
        L.append(f"\n  ── {nom} ── {f['trames']:,} trames".replace(",", " "))
        if f.get("duree_s"):
            L.append(f"     bus : {f['cadence_hz']} Hz · médiane "
                     f"{f['intervalle_median_ms']} ms · p95 {f['intervalle_p95_ms']} ms"
                     f" · {f['trous']} trou(s)"
                     + (f", max {f['trou_max_s']} s" if f.get("trou_max_s") else ""))
            L.append(f"     types : {f['types']} · immobile "
                     f"{100 * f.get('part_immobile', 0):.0f} %")
        if f.get("tactile_appauvri"):
            L.append(f"     ⚠ TACTILE PERDU : {100 * f['part_tactile']:.0f} % de "
                     f"trames capteur au lieu de 50 % — environ "
                     f"{f['tactile_manquant_estime']:,} manquantes. "
                     f"set_tpdo_frame_type non réémis après un homing ?"
                     .replace(",", " "))
        actifs = {n: m for n, m in f.get("moteurs", {}).items()
                  if m["a_bouge"] or m["bloque"] or m["passe_sous_zero"]}
        for n, m in actifs.items():
            marques = []
            if m["bloque"]:
                marques.append("BLOQUÉ")
            if m["passe_sous_zero"]:
                marques.append("sous zéro")
            L.append(f"     {n:<16} {m['position_min']:>6} → {m['position_max']:>6} "
                     f"({m['course_counts']:>5} counts) · courant max "
                     f"{m['courant_max']:>4} ‰ {' '.join(marques)}")
        vivantes = {n: z for n, z in f.get("zones", {}).items() if z["a_repondu"]}
        if vivantes:
            L.append("     tactile : " + ", ".join(
                f"{n} {z['amplitude_counts']}" + ("*" if z["sature"] else "")
                for n, z in vivantes.items()))
        elif f.get("trames_tactile"):
            L.append("     tactile : aucune zone n'a bougé — a-t-on sollicité ?")
    return "\n".join(L)
