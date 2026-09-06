#!/usr/bin/env python3
"""
run.py — batterie de caractérisation de la main, reproductible à l'identique.

Une exécution = une configuration matérielle. On lance la même batterie pour
chaque combinaison (alimentation, interface réseau), puis ``bench.compare``
met les résultats côte à côte. Toute différence entre deux exécutions vient du
matériel, pas du protocole.

    sudo python3 -m bench.run --label alimA-nic1
    sudo python3 -m bench.run --label alimB-nic1 --skip-touch
    python3 -m bench.compare runs/*.json

Phases :

  0. identification — hôte, lien, esclaves, symboles réellement exportés
  1. avant réveil   — ce que la main émet avant homing : rien d'exploitable
  2. repos          — bruit, dérive, octets morts, santé du flux
  3. proximité      — main approchée sans contact, doigt par doigt
  4. toucher        — appui franc, zone par zone (``--skip-touch`` pour sauter)

Les phases 3 et 4 sont guidées et demandent une présence. Les phases 0 à 2 sont
automatiques : c'est le socle qu'on compare entre configurations.
"""
from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.metrics import (  # noqa: E402
    amplitude,
    byte_stats,
    infer_stride,
    response,
    responders,
    sdk_series_stats,
    stream_health,
    summarise_bytes,
)
from bench.sdk import BenchError, Hand  # noqa: E402

#: Zones sollicitées en phase 4. Les pulpes y figurent délibérément : on veut
#: constater qu'elles ne remontent rien plutôt que le supposer.
TOUCH_ZONES = [
    ("thumb.tip", "le BOUT du pouce"),
    ("thumb.pad", "la PULPE du pouce"),
    ("index.tip", "le BOUT de l'index"),
    ("index.pad", "la PULPE de l'index"),
    ("middle.tip", "le BOUT du majeur"),
    ("middle.pad", "la PULPE du majeur"),
    ("ring.tip", "le BOUT de l'annulaire"),
    ("ring.pad", "la PULPE de l'annulaire"),
    ("little.tip", "le BOUT de l'auriculaire"),
    ("little.pad", "la PULPE de l'auriculaire"),
    # La paume est une matrice de 26 points : un seul appui central n'en
    # sollicite qu'une partie. On la balaie en quatre quadrants.
    ("palm.center", "le CENTRE de la paume"),
    ("palm.thumbside", "le côté POUCE de la paume (éminence thénar)"),
    ("palm.pinkyside", "le côté AURICULAIRE de la paume (éminence hypothénar)"),
    ("palm.top", "le HAUT de la paume, à la base des doigts"),
]

#: Zones approchées en phase 3, sans contact.
PROXIMITY_ZONES = [
    ("prox.thumb", "le bout du pouce"),
    ("prox.index", "le bout de l'index"),
    ("prox.palm", "la paume"),
]


def select(zones, pattern: str | None):
    """Filtre les zones par motif. Sert à rejouer une seule partie du protocole."""
    return zones if not pattern else [z for z in zones if pattern in z[0]]


def sample_sdk(hand: Hand, seconds: float, period: float = 0.1) -> list[dict]:
    """Interroge l'API tactile du SDK à cadence fixe pendant la durée demandée."""
    out, deadline = [], time.time() + seconds
    while time.time() < deadline:
        out.append(hand.read_sdk_sensors())
        time.sleep(period)
    return out


def dominant_type(frames) -> int | None:
    """Type de trame capteur majoritaire, déterminé sans le présupposer."""
    counts: dict[int, int] = {}
    for f in frames:
        if f.data[0] != 0x00:
            counts[f.data[0]] = counts.get(f.data[0], 0) + 1
    return max(counts, key=counts.get) if counts else None


def phase_rest(hand: Hand, seconds: float) -> dict:
    print(f"\n── Phase 2 : repos ({seconds:.0f} s). Ne touchez pas la main.")
    frames = hand.record(seconds)
    sdk = sample_sdk(hand, min(5.0, seconds))
    health = stream_health(frames)
    ftype = dominant_type(frames)

    out: dict = {
        "frames": len(frames),
        "stream": health.to_dict(),
        "tactile_frame_type": f"0x{ftype:02x}" if ftype is not None else None,
        "sdk": sdk_series_stats(sdk),
    }
    if ftype is not None:
        stats = byte_stats(frames, ftype)
        out["bytes"] = [s.to_dict() for s in stats]
        out["byte_summary"] = summarise_bytes(stats)
        out["structure"] = infer_stride(stats)
    return out


def phase_guided(hand: Hand, zones, ftype: int, rest_s: float, stim_s: float,
                 verb: str) -> dict:
    out: dict = {}
    for key, human in zones:
        input(f"  [{key}] Écartez tout, puis Entrée — {rest_s:.0f} s de repos… ")
        rest = hand.record(rest_s)
        input(f"  [{key}] {verb} {human}, maintenez, puis Entrée — {stim_s:.0f} s… ")
        # Brut et API sur la MÊME fenêtre : sinon on compare deux instants
        # différents et l'écart n'est plus imputable au stimulus.
        hand.start_recording()
        sdk_during = sample_sdk(hand, stim_s, period=0.05)
        stim = hand.stop_recording()
        z = response(rest, stim, ftype)
        amp = amplitude(rest, stim, ftype)
        hits = responders(z)
        out[key] = {
            "rest_frames": len(rest),
            "stim_frames": len(stim),
            "responders": [{"byte": i, "z": round(z[i], 1), "delta_lsb": amp.get(i)}
                           for i in hits[:32]],
            "n_responders": len(hits),
            "zmax": round(max(z.values()), 1) if z else 0.0,
            "delta_max_lsb": max((abs(v) for v in amp.values()), default=0.0),
            "sdk_during": sdk_series_stats(sdk_during),
        }
        verdict = "répond" if hits else "AUCUNE RÉPONSE"
        print(f"  [{key}] {verdict} — {len(hits)} octets, amplitude max "
              f"{out[key]['delta_max_lsb']:.0f} LSB\n")
    return out


def build_report(result: dict) -> str:
    """
    Résumé lisible, pour décider sans ouvrir le JSON.

    Doit rester valable même sur une exécution avortée : un rapport tronqué
    documente la panne, un rapport qui plante ne documente rien.
    """
    host = result.get("host", {})
    L = [f"# Caractérisation DH116 — {result.get('label', '?')}", "",
         f"`{result.get('started', '?')}` · {host.get('machine', '?')} · "
         f"virtualisation : **{host.get('virtualised', '?')}**", ""]

    if result.get("error"):
        L += [f"> **Exécution interrompue : {result['error']}**", ""]

    link = result.get("link")
    if link:
        L += ["## Lien et esclaves", "",
              f"- interface `{link['interface']}` ({link['driver']}), "
              f"MAC `{link['mac']}`, lien **{link['speed_mbps']} Mb/s**",
              f"- PDO : {link['input_size']} octets en entrée, "
              f"{link['output_size']} en sortie",
              f"- esclaves : {link['slaves']}", ""]

    if "capabilities" in result:
        missing = [k for k, v in result["capabilities"].items() if not v]
        L += ["## Symboles du SDK", "",
              "Tous présents." if not missing
              else "**Absents : " + ", ".join(missing) + "**"]
        extra = result.get("missing_symbols") or []
        if extra:
            L += ["", "Symboles déclarés par le chargeur Leadshine mais absents "
                  "de la `.so` : `" + "`, `".join(extra) + "`."]
        L.append("")

    for phase, title in (("before_enable", "Avant `set_tpdo_frame_type`"),
                         ("rest", "Au repos")):
        p = result.get(phase)
        if not p:
            continue
        s = p["stream"]
        L += [f"## {title}", "",
              f"- trames vues : {s['types']}",
              f"- lectures {s['poll_rate_hz']} Hz, dont "
              f"{s['fresh_rate_hz']} Hz de trames neuves "
              f"({s['fresh_ratio'] * 100:.0f} %)",
              f"- intervalle entre trames neuves : {s['interval_ms']} ms",
              f"- plus longue interruption : {s['longest_stall_ms']} ms", ""]
        if "byte_summary" in p:
            b = p["byte_summary"]
            L += [f"- octets : {b['stable']} stables, {b['bruyant']} bruyants, "
                  f"{b['très bruyant']} très bruyants, {b['figé']} figés, "
                  f"**{b['mort']} morts** sur {b['total']}",
                  f"- écart-type médian des actifs : {b['std_median_actifs']} LSB",
                  f"- dérive max sur la fenêtre : {b['drift_max_abs']} LSB",
                  f"- octets saturés à 255 : {b['saturés'] or 'aucun'}", ""]
        if p.get("structure"):
            st = p["structure"]
            L += [f"- périodicité déduite du motif d'octets actifs : "
                  f"**pas {st['pas_probable']}** (score {st['score']}), "
                  f"{st['octets_actifs']} octets actifs", ""]

    rest = result.get("rest", {})
    if rest.get("sdk"):
        L += ["## API tactile du SDK, au repos", "",
              "| id | capteur | pression | force N | proximité | positions |",
              "|---:|:--|:--|:--|:--|:--|"]
        for sid, e in sorted(rest["sdk"].items(), key=lambda kv: int(kv[0])):
            def cell(name):
                d = e[name]
                if d["n"] == 0:
                    return f"err {list(d.get('errors', {}))}"
                return "figé" if d["constant"] else f"σ={d['std']}"
            L.append(f"| {sid} | {e['label']} | {cell('pressure')} | "
                     f"{cell('normal_force')} | {cell('proximity')} | "
                     f"{e['sensor_pos_count']} |")
        L.append("")

    for phase, title in (("proximity", "Approche sans contact"),
                         ("touch", "Appui")):
        p = result.get(phase)
        if not p:
            continue
        L += [f"## {title}", "", "| zone | octets réactifs | z max | verdict |",
              "|:--|---:|---:|:--|"]
        for zone, d in p.items():
            L.append(f"| {zone} | {d['n_responders']} | {d['zmax']} | "
                     f"{'répond' if d['n_responders'] else '**muet**'} |")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True,
                    help="identifiant de la configuration, ex. alimA-nic1")
    ap.add_argument("--rest", type=float, default=60.0, help="fenêtre de repos (s)")
    ap.add_argument("--stim", type=float, default=4.0, help="fenêtre de stimulus (s)")
    ap.add_argument("--zone-rest", type=float, default=3.0)
    ap.add_argument("--iface", type=int, default=None)
    ap.add_argument("--sdk-dir", default=None)
    ap.add_argument("--out", type=Path, default=Path("runs"))
    # Mesuré le 2026-07-31 : sans alimentation des moteurs ET homing, la main
    # n'émet aucune télémétrie — ni position, ni courant, ni tactile. Les trames
    # circulent, mais leur charge utile est nulle. Le réveil est donc la valeur
    # par défaut, et le sauter est le cas particulier.
    ap.add_argument("--no-motors", dest="motors", action="store_false",
                    help="ne pas réveiller la main (elle restera muette)")
    ap.add_argument("--no-home", dest="home", action="store_false",
                    help="alimenter sans lancer le homing")
    ap.add_argument("--skip-touch", action="store_true")
    ap.add_argument("--skip-proximity", action="store_true")
    ap.add_argument("--only", default=None,
                    help="ne garder que les zones dont le nom contient ce motif, "
                         "ex. --only palm")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args.out.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "label": args.label,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"rest": args.rest, "stim": args.stim},
    }

    hand = Hand(sdk_dir=args.sdk_dir)
    try:
        print("── Phase 0 : identification")
        hand.connect(iface_index=args.iface)
        result["link"] = hand.link.to_dict()
        result["host"] = {"machine": platform.machine(),
                          "kernel": hand.link.kernel,
                          "node": hand.link.host,
                          "virtualised": hand.link.virtualised}
        result["capabilities"] = hand.capabilities
        result["missing_symbols"] = hand.missing_symbols
        result["dof"] = hand.dof

        print("\n── Phase 1 : avant bascule en mode capteur (5 s)")
        before = hand.record(5.0)
        result["before_enable"] = {"stream": stream_health(before).to_dict()}

        hand.enable_tactile()
        time.sleep(0.5)

        # Avant toute remise à zéro : les valeurs absolues de l'API. Après un
        # reset, la pression vaut 0 par construction et n'apprend plus rien.
        result["sdk_before_reset"] = sdk_series_stats([hand.read_sdk_sensors()])

        if args.motors:
            print("\n── Réveil : alimentation des moteurs"
                  + (" + homing" if args.home else " (sans homing)"))
            result["motors_enabled"] = hand.enable_motors(home=args.home)

        result["pressure_reset_rc"] = hand.reset_pressure_reference()
        time.sleep(1.0)

        result["rest"] = phase_rest(hand, args.rest)
        ftype_str = result["rest"]["tactile_frame_type"]
        if ftype_str is None:
            print("\nAucune trame capteur reçue. Les phases guidées n'auraient "
                  "aucun sens : on s'arrête ici.")
        else:
            ftype = int(ftype_str, 16)
            if not args.skip_proximity:
                print(f"\n── Phase 3 : approche SANS CONTACT (type {ftype_str})")
                result["proximity"] = phase_guided(
                    hand, select(PROXIMITY_ZONES, args.only), ftype,
                    args.zone_rest, args.stim, "Approchez la main à 1-2 cm de")
            if not args.skip_touch:
                print(f"\n── Phase 4 : appui (type {ftype_str})")
                result["touch"] = phase_guided(
                    hand, select(TOUCH_ZONES, args.only), ftype,
                    args.zone_rest, args.stim, "Appuyez fermement sur")

    except BenchError as e:
        print(f"\nBanc : {e}", file=sys.stderr)
        result["error"] = str(e)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompu — les phases déjà faites sont conservées.")
        result["interrupted"] = True
    finally:
        if args.motors:
            hand.disable_motors()
        hand.close()
        stem = args.out / args.label
        stem.with_suffix(".json").write_text(json.dumps(result, indent=2,
                                                        ensure_ascii=False))
        report = build_report(result)
        stem.with_suffix(".md").write_text(report)
        print("\n" + report)
        print(f"\nÉcrit : {stem}.json et {stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
