#!/usr/bin/env python3
"""
cli.py — ``vtctl``, la ligne de commande qui regroupe tout le banc.

    sudo python3 -m vtctl selftest              # le banc répond-il ?
    sudo python3 -m vtctl serve                 # interface web + API
    sudo python3 -m vtctl session --objet cube  # protocole complet en console
    sudo python3 -m vtctl angle                 # mesurer l'angle du plateau
    sudo python3 -m vtctl main --ouvrir         # pilotage direct de la main
    python3 -m vtctl recover <session>          # reconstruire un manifeste

Ajouter ``--simulation`` à n'importe laquelle : le banc entier est simulé, avec
ses défauts mesurés, et **aucun matériel n'est touché**. C'est ce qui permet de
valider le protocole sans immobiliser le banc.

Toutes les sous-commandes montent le même :class:`~vtctl.hw.banc.Banc` et donc
la même exclusivité : impossible de lancer deux outils qui se marchent dessus,
le second est refusé avec un message qui nomme le premier.

Les droits root ne sont nécessaires que pour la main — le maître EtherCAT ouvre
des sockets raw. ``--sans-main`` s'en dispense.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

from . import config
from .hw.banc import Banc
from .hw.resources import ResourceBusy
from .protocol import states as S
from .protocol.runner import PHASE_TACTILE, PHASE_VISUELLE, Runner
from .store import recover

G, J, R, D, N = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _log(niveau: str) -> None:
    logging.basicConfig(level=getattr(logging, niveau.upper(), logging.INFO),
                        format="%(levelname).1s %(name)s │ %(message)s")
    # Le tracker parle beaucoup et sur le logger racine.
    for nom in ("aruco_tracker", "turntable_position_controller"):
        logging.getLogger(nom).setLevel(logging.WARNING)


def _banc(a) -> Banc:
    return Banc(simulation=a.simulation,
                avec_main=not a.sans_main,
                avec_camera=not a.sans_camera,
                avec_plateau=not a.sans_plateau,
                avec_profondeur=not a.sans_profondeur,
                settle=0.0 if (a.simulation and a.rapide) else config.SETTLE_LAMPE,
                exposition_us=a.exposition,
                pwm_aruco=a.pwm_aruco,
                expo_aruco=a.expo_aruco,
                gain_aruco=a.gain_aruco,
                iface_index=a.iface,
                verrous=not a.sans_verrous,
                ignorer_demons=a.ignorer_demons,
                moteurs_exclus=_exclusions(a))


def _exclusions(a) -> "tuple | None":
    """
    Les moteurs à exclure, ou ``None`` pour garder ceux de ``vt_tactile``.

    Une panne d'actionneur appartient à un exemplaire de main, pas au modèle :
    ``--aucun-exclu`` sert quand on change de main et que le doigt donné pour
    mort remarche.
    """
    if getattr(a, "aucun_exclu", False):
        return ()
    exclus = getattr(a, "exclure", None)
    return tuple(int(x) for x in exclus) if exclus else None


def _reglages(a) -> config.Reglages:
    r = config.Reglages()
    for cle in ("objet", "tolerance_deg", "pivot_depart", "max_current",
                "velocity", "timeout_fermeture"):
        v = getattr(a, cle, None)
        if v is not None:
            setattr(r, cle, v)
    if getattr(a, "angles", None):
        r.angles = [float(x) % 360.0 for x in a.angles]
    elif getattr(a, "nb_angles", None):
        pas = a.pas if a.pas else 360.0 / a.nb_angles
        r.angles = [(i * pas) % 360.0 for i in range(a.nb_angles)]
    if getattr(a, "sans_pouce", False):
        r.pouce_requis = False
    if getattr(a, "exposition", None):
        r.exposition_us = a.exposition
    return r


# ── selftest ──────────────────────────────────────────────────────────────────

def cmd_selftest(a) -> int:
    print(f"\n{'Banc simulé' if a.simulation else 'Banc réel'} — vérification\n")
    with _banc(a) as b:
        r = b.selftest(verbeux=True)
    print(f"\n  {G if r['ok'] else R}{'Tout répond.' if r['ok'] else 'Au moins un contrôle a échoué.'}{N}\n")
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
    return 0 if r["ok"] else 1


# ── angle ─────────────────────────────────────────────────────────────────────

def cmd_angle(a) -> int:
    with _banc(a) as b:
        if a.aller is not None:
            r = b.aller_a(a.aller, timeout=a.timeout)
            print(f"consigne {a.aller:.0f}° → mesuré {r['mesure_deg']}° "
                  f"(écart {r['ecart_deg']}°, {r['passes']} passe(s))")
            b.arreter_plateau()
        if a.arreter:
            print("plateau arrêté" if b.arreter_plateau() else f"{R}plateau NON arrêté{N}")
        if a.calibrer:
            print(json.dumps(b.calibrer_centre(duree=a.duree), indent=2,
                             ensure_ascii=False, default=str))
        for _ in range(max(1, a.repeter)):
            m = b.angle.mesurer(a.images)
            d = m.to_dict()
            print(f"angle {'—' if not m.connu else f'{m.angle:7.2f}°'}  "
                  f"± {d['dispersion_deg'] or '—'}°  "
                  f"{m.images}/{m.tentees} images  carreaux {sorted(m.marqueurs) or '—'}")
            if not m.connu:
                print(f"  {J}{m.diagnostic()}{N}")
            elif m.hors_table:
                print(f"  {J}identifiants hors table lus {sorted(m.hors_table)} — "
                      f"pose caméra à surveiller{N}")
    return 0


# ── pose ──────────────────────────────────────────────────────────────────────

def cmd_pose(a) -> int:
    """
    Aide au placement de la caméra, en direct.

    Le centre du plateau est stocké **en pixels** : une caméra déplacée rend
    tous les angles faux sans que rien ne le signale. Cette commande montre en
    continu ce que la caméra voit, pendant qu'on la bouge à la main.
    """
    from .hw import pose  # noqa: PLC0415

    with _banc(a) as b:
        if a.image:
            r = pose.annoter(b.camera, b.angle, a.image)
            print(f"image annotée : {r['chemin']}  marqueurs {r['marqueurs'] or '—'}")
        if a.suivre:
            print(f"\n{D}Bougez la caméra — la ligne se met à jour. "
                  f"Ctrl-C pour arrêter.{N}\n")
            etat = pose.suivre(b.camera, b.angle, a.suivre,
                               afficher=lambda l: print("  " + _colorer(l)))
        else:
            etat = pose.observer(b.camera, b.angle, n=a.images)
            print("  " + _colorer(etat.ligne()))
        print()
        if etat is not None and not etat.utilisable:
            print(f"  {J}{etat.conseil()}{N}")
            print(f"  {D}Une fois la pose bonne : "
                  f"python3 -m vtctl angle --calibrer{N}")
            return 1
        print(f"  {G}Pose exploitable.{N}  "
              f"{D}Recalibrer le centre avant d'acquérir : "
              f"python3 -m vtctl angle --calibrer{N}")
    return 0


def _colorer(ligne: str) -> str:
    return (G + ligne + N) if ligne.startswith("OK") else (J + ligne + N)


# ── main ──────────────────────────────────────────────────────────────────────

def cmd_main(a) -> int:
    if a.sans_main:
        print(f"{R}--sans-main et « main » sont contradictoires.{N}", file=sys.stderr)
        return 2
    with _banc(a) as b:
        h = b.hand
        if getattr(a, "tester", False):
            print(f"\n{D}essai de chaque moteur, un aller-retour court…{N}\n")
            r = h.tester_moteurs()
            for m, d in sorted(r["moteurs"].items()):
                if d.get("erreur"):
                    print(f"  {m} {d['nom']:<16} {R}refusé{N} — {d['erreur'][:60]}")
                    continue
                etat = (f"{G}répond{N}" if d["repond"]
                        else (f"{J}BLOQUÉ{N}" if d["bloque"] else f"{R}muet{N}"))
                print(f"  {m} {d['nom']:<16} {d['depart']:>6} → {d['arrivee']:>6} "
                      f"({d['course']:>5} counts)  courant max {d['courant_max']:>4} ‰  {etat}")
            if r["exclusions_suggerees"]:
                print(f"\n  {J}à exclure : {' '.join(f'--exclure {m}' for m in r['exclusions_suggerees'])}{N}")
            elif r["exclus"]:
                print(f"\n  {G}tous les moteurs répondent, y compris les exclus "
                      f"({', '.join(map(str, r['exclus']))}) — relancer avec "
                      f"--aucun-exclu{N}")
            else:
                print(f"\n  {G}tous les moteurs répondent{N}")
            if r["bloques"]:
                print(f"  {J}bloqué(s) à courant élevé : {', '.join(r['bloques'])} — "
                      f"obstacle, pas panne. Dégager avant de conclure.{N}")
            print()
        if a.ouvrir:
            print("main ouverte" if h.ouvrir() else f"{R}la main ne s'ouvre pas{N}")
        if a.zero:
            print(f"zéro tactile : ligne de base sur {h.zero(a.zero)} trames")
        if a.pivot is not None:
            r = h.pivot_pouce(a.pivot)
            print(f"pivot du pouce → {h.positions((2,))[2]} counts  {D}{r}{N}")
        if a.position:
            cibles = {}
            for item in a.position:
                m, _, v = item.partition(":")
                cibles[int(m)] = int(v)
            r = h.aller_a(cibles, a.velocity, a.max_current)
            print(f"consigne {cibles} → {r}")
        if a.suivre:
            _suivre_tactile(b, a.suivre)
        else:
            print(f"positions : {h.positions()}")
            print(f"courants  : {h.courants()}")
            etat = h.etat_tactile()
            if etat is not None and h.reader.zeroed:
                print("tactile   : " + "  ".join(
                    f"{z}={etat[z].pressure_max:.3f}" for z in etat.zones))
    return 0


def _suivre_tactile(b, secondes: float) -> None:
    """Affiche la pression en direct, comme ``tools/live.py``."""
    h = b.hand
    if not h.reader.zeroed:
        print(f"zéro tactile ({D}main au repos, ne touchez rien{N})…")
        h.zero(2.0)
    fin = time.time() + secondes
    while time.time() < fin:
        etat = h.etat_tactile()
        if etat is not None:
            ligne = "  ".join(f"{z.split('.')[0][:3]}{'.' + z.split('.')[1][:1] if '.' in z else ''}"
                              f" {etat[z].pressure_max:5.3f}" for z in etat.zones)
            print(f"\r{ligne}", end="", flush=True)
        time.sleep(0.1)
    print()


# ── session ───────────────────────────────────────────────────────────────────

def cmd_session(a) -> int:
    """
    Le protocole complet en console, avec les mêmes décisions que l'interface.

    S'arrête aux mêmes endroits : la pose du pouce et la validation. La réponse
    se tape, ce qui survit à une liaison SSH — c'est ce que font déjà
    ``tools/jog.py`` et ``tools/protocole.py``.
    """
    reglages = _reglages(a)
    phases = tuple(p.strip() for p in a.phases.split(",") if p.strip())
    if a.sans_main:
        phases = tuple(p for p in phases if p != PHASE_TACTILE)

    print(f"\nObjet   : {reglages.objet}")
    print(f"Angles  : {', '.join(f'{x:.0f}°' for x in reglages.angles)}")
    print(f"Phases  : {', '.join(phases)}")
    print(f"Pouce   : {'exigé' if reglages.pouce_requis else 'non requis'}\n")

    with _banc(a) as b:
        runner = Runner(b, reglages, a.out, phases=phases)
        _brancher_signaux(runner)
        session = runner.demarrer()
        print(f"Session : {session.root}\n")
        _console(runner, a)
        runner.attendre()
        print(f"\nManifeste : {session.root / 'manifest.json'}")
        _resume(runner)
    return 0


def _brancher_signaux(runner) -> None:
    """``Ctrl-C`` demande un arrêt propre, il ne tue pas le fil."""
    def handler(signum, frame):
        print(f"\n{J}Arrêt demandé — le protocole termine l'étape en cours.{N}")
        runner.arreter("SIGINT")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _console(runner, a) -> None:
    """Boucle d'affichage, et invites aux points de décision."""
    dernier = None
    while runner.en_cours:
        s = runner.snapshot()
        etat = s["machine"]["etat"]
        if etat != dernier:
            dernier = etat
            ac = s.get("angle_courant") or {}
            suffixe = ""
            if ac.get("mesure_deg") is not None:
                suffixe = (f"  {D}angle {ac['mesure_deg']}° "
                           f"(écart {ac['ecart_deg']}°){N}")
            elif ac.get("consigne_deg") is not None:
                suffixe = f"  {D}angle introuvable{N}"
            print(f"  {s['machine']['description']}{suffixe}")

        if s["attend_pouce"]:
            _invite_pouce(runner, a)
            dernier = None
        elif s["attend_validation"]:
            _invite_validation(runner, a)
            dernier = None
        time.sleep(0.2)


def _invite_pouce(runner, a) -> None:
    p = runner.snapshot().get("pouce") or {}
    print(f"\n  {J}Posez le pouce sur l'objet.{N}  "
          f"pression {p.get('pression', 0):.3f} — {p.get('raison', '')}")
    print(f"  {D}entrée = armer · c <motif> = contourner · s = sauter · "
          f"p <counts> = pivot du pouce · q = arrêter{N}")
    while True:
        try:
            ligne = input("  > ").strip()
        except EOFError:
            runner.arreter("entrée fermée")
            return
        if ligne == "":
            runner.armer(S.POUCE_SATISFAIT)
            return
        if ligne == "q":
            runner.arreter("demandé")
            return
        if ligne == "s":
            runner.sauter_angle("sauté depuis la console")
            return
        if ligne.startswith("c"):
            motif = ligne[1:].strip()
            if not motif:
                print(f"  {R}un contournement doit porter un motif{N}")
                continue
            runner.armer(S.POUCE_CONTOURNE, motif)
            return
        if ligne.startswith("p"):
            try:
                cible = int(ligne[1:].strip())
            except ValueError:
                print(f"  {R}exemple : p 3200{N}")
                continue
            runner.banc.hand.pivot_pouce(cible)
            pos = runner.banc.hand.positions((2,))[2]
            print(f"  pivot → {pos} counts, pression "
                  f"{runner.banc.hand.pression('thumb'):.3f}")
            continue
        print(f"  {R}entrée non comprise{N}")


def _invite_validation(runner, a) -> None:
    if a.auto_valider:
        runner.valider("valide", "validation automatique (--auto-valider)")
        return
    print(f"\n  {J}Valider cette capture ?{N}  "
          f"{D}entrée = valider · n <commentaire> = invalider{N}")
    try:
        ligne = input("  > ").strip()
    except EOFError:
        runner.valider("valide")
        return
    if ligne.startswith("n"):
        runner.valider("invalide", ligne[1:].strip())
    else:
        runner.valider("valide", ligne)


def _resume(runner) -> None:
    s = runner.snapshot()
    prog = s.get("progression") or []
    if not prog:
        print("aucune capture.")
        return
    print(f"\n{len(prog)} capture(s) :")
    for x in prog:
        mes = "—" if x["mesure_deg"] is None else f"{x['mesure_deg']:.1f}°"
        couleur = G if x["validation"] == "valide" else R
        print(f"  {x['phase']:9s} angle {x['angle_index']:2d}  "
              f"consigne {x['consigne_deg']:5.1f}°  mesuré {mes:>7s}  "
              f"{couleur}{x['validation']}{N}")


# ── serve ─────────────────────────────────────────────────────────────────────

def cmd_serve(a) -> int:
    from .api.server import Service, servir  # noqa: PLC0415

    with _banc(a) as b:
        service = Service(b, a.out, _reglages(a))
        service.noter(f"Banc {'simulé' if a.simulation else 'réel'} prêt.")
        srv = servir(service, host=a.host, port=a.port)
        print(f"\n  Interface : {G}http://{a.host}:{a.port}{N}")
        if a.host == "127.0.0.1":
            print(f"  {D}Depuis le Mac : ssh -N -L {a.port}:127.0.0.1:{a.port} openclaw-vm{N}")
        print(f"  {D}Ctrl-C pour arrêter.{N}\n")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nArrêt…")
        finally:
            if service.runner and service.runner.en_cours:
                service.runner.arreter("serveur arrêté")
                service.runner.attendre(timeout=30)
            service.arreter_apercu()
            srv.shutdown()
    return 0


# ── recover ───────────────────────────────────────────────────────────────────

def cmd_recover(a) -> int:
    base = Path(a.out)
    if a.lister:
        cibles = sorted(d for d in base.iterdir() if d.is_dir()) if base.exists() else []
        if not cibles:
            print("aucune session.")
            return 0
        for d in cibles:
            i = recover.inspecter(d)
            etat = f"{G}manifeste{N}" if i["complete"] else f"{J}SANS MANIFESTE{N}"
            print(f"  {i['nom']:44s} {i['images_sur_disque']:5d} img  "
                  f"{i['octets']/1048576:7.1f} Mo  {etat}")
        return 0

    chemins = [Path(x) if Path(x).is_absolute() or Path(x).exists() else base / x
               for x in a.sessions]
    for chemin in chemins:
        if not chemin.is_dir():
            print(f"{R}{chemin} : introuvable{N}", file=sys.stderr)
            continue
        try:
            m = recover.recuperer(chemin, force=a.force)
        except FileExistsError as e:
            print(f"{J}{e}{N}")
            continue
        c, rec = m["compteurs"], m["recuperation"]
        print(f"{G}{chemin.name}{N} : {c['images']} images, "
              f"{c['blocs_bruts']} bloc(s) brut(s) ({c['trames_brutes']} trames), "
              f"{c['captures']} capture(s)")
        print(f"  source {rec['source']}, {rec['evenements']} évènements, "
              f"session close proprement : {rec['session_close_proprement']}")
        if rec["images_annoncees_absentes"]:
            print(f"  {J}{len(rec['images_annoncees_absentes'])} image(s) annoncée(s) "
                  f"mais absente(s) du disque — écriture interrompue{N}")
    return 0


# ── cinematique ───────────────────────────────────────────────────────────────

def cmd_cinematique(a) -> int:
    """
    Le modèle cinématique, et son étalonnage.

    Sans matériel : décrit le modèle et calcule des poses pour des counts
    donnés. Avec ``--etalonner`` : mesure le facteur counts/radian sur la main,
    en confrontant la trame brute aux degrés que rend le SDK.
    """
    from .hw.cinematique import Cinematique, etalonner  # noqa: PLC0415

    if a.etalonner:
        if a.sans_main:
            print(f"{R}--etalonner a besoin de la main{N}", file=sys.stderr)
            return 2
        with _banc(a) as b:
            print(f"\n{D}déplacement de quelques doigts, une minute environ…{N}")
            r = etalonner(b.hand)
        print(f"\n  facteur mesuré  : {G}{r['counts_par_radian']}{N} counts/radian")
        print(f"  dispersion      : {r['dispersion']}")
        print(f"  hypothèse       : {r['hypothese_par_defaut']} (celle employée sans étalonnage)")
        print(f"  {len(r['points'])} points")
        for pt in r["points"][:8]:
            print(f"    moteur {pt['moteur']}  {pt['counts']:>6} counts  "
                  f"{pt['degres']:>7.2f}°  → {pt['counts_par_radian']:.0f}")
        print(f"\n  {D}à repasser ensuite par --counts-par-radian {r['counts_par_radian']}{N}\n")
        if a.json:
            Path(a.json).write_text(json.dumps(r, indent=2, ensure_ascii=False))
        return 0

    cin = Cinematique(counts_par_radian=a.counts_par_radian)
    infos = cin.infos()
    print(f"\n  {infos['robot']} — {infos['articulations_mobiles']} articulations "
          f"mobiles, {infos['moteurs']} moteurs")
    print(f"  {D}{infos['urdf']}{N}")
    c = infos["conversion"]
    if c["etalonne"]:
        print(f"  conversion : {G}{c['counts_par_radian']} counts/radian, étalonnée{N}")
    else:
        print(f"  conversion : {J}{c['counts_par_radian']} counts/radian — "
              f"NON étalonnée ({c['hypothese']}){N}")

    positions = {}
    for item in (a.position or []):
        m, _, v = item.partition(":")
        positions[int(m)] = int(v)
    if not positions:
        positions = {m: 0 for m in (1, 2, 3, 4, 5, 6)}
    r = cin.bouts(positions)
    print(f"\n  bouts de doigts en repère {r['repere']}, pour {positions} :")
    for doigt, p in r["bouts"].items():
        x, y, z = p["position_m"]
        print(f"    {doigt:<8} x={1000*x:7.1f}  y={1000*y:7.1f}  z={1000*z:7.1f}  mm")
    print()
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


# ── analyse ───────────────────────────────────────────────────────────────────

def cmd_analyser(a) -> int:
    """
    Relit les trames EtherCAT enregistrées et dit ce qu'elles contiennent.

    Ne touche à aucun matériel : c'est de la lecture de fichiers, et elle peut
    tourner pendant qu'une acquisition est en cours.
    """
    from .store import analyse  # noqa: PLC0415

    base = Path(a.out)
    cibles = []
    for x in (a.sessions or []):
        p = Path(x)
        cibles.append(p if p.is_dir() else base / x)
    if not cibles:
        cibles = sorted(d for d in base.iterdir() if d.is_dir()) if base.exists() else []
    if not cibles:
        print(f"{J}aucune session dans {base}{N}", file=sys.stderr)
        return 2

    rapports = []
    for racine in cibles:
        if not racine.is_dir():
            print(f"{R}{racine} : introuvable{N}", file=sys.stderr)
            continue
        r = analyse.analyser_session(racine)
        if not r["flux"] and not a.tout:
            continue                     # rien à dire d'une session sans flux
        rapports.append(r)
        print(analyse.rendre(r, verbeux=not a.court))

    if a.json:
        Path(a.json).write_text(json.dumps(rapports, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        print(f"\n{D}rapport écrit dans {a.json}{N}")
    return 0


# ── argparse ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vtctl", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulation", action="store_true",
                    help="banc simulé, avec ses défauts mesurés ; aucun matériel touché")
    ap.add_argument("--rapide", action="store_true",
                    help="en simulation, supprime les 3,5 s d'établissement de la lampe")
    ap.add_argument("--sans-main", action="store_true",
                    help="ne pas monter EtherCAT (dispense des droits root)")
    ap.add_argument("--sans-camera", action="store_true",
                    help="ne pas ouvrir la D405 — diagnostic de la main seule")
    ap.add_argument("--sans-plateau", action="store_true",
                    help="ne pas ouvrir le port série (lampe et plateau)")
    ap.add_argument("--sans-profondeur", action="store_true",
                    help="ne diffuser que la couleur — allège le lien USB, dont "
                         "le trafic isochrone met QEMU en défaut")
    ap.add_argument("--sans-verrous", action="store_true",
                    help="pas de verrou inter-processus (tests parallèles)")
    ap.add_argument("--ignorer-demons", action="store_true",
                    help="passer outre les processus qui tiennent déjà le bus")
    ap.add_argument("--iface", type=int, default=None, help="index de l'interface EtherCAT")
    ap.add_argument("--exclure", action="append", type=int, metavar="MOTEUR",
                    help="moteur à ne pas commander sur cet exemplaire (répétable)")
    ap.add_argument("--aucun-exclu", action="store_true",
                    help="ne rien exclure — après un changement de main, quand "
                         "le doigt donné pour mort remarche")
    ap.add_argument("--exposition", type=int, default=None,
                    help="exposition des images en µs (défaut : celle du profil VT-Light)")
    ap.add_argument("--pwm-aruco", type=int, default=None,
                    help=f"lumière pour la détection de marqueurs (défaut {config.PWM_ARUCO})")
    ap.add_argument("--expo-aruco", type=int, default=None,
                    help=f"exposition pour la détection, en µs (défaut {config.EXPO_ARUCO})")
    ap.add_argument("--gain-aruco", type=int, default=None,
                    help="gain du capteur pour la détection ; à ne relever que "
                         "si l'éclairage du banc est défaillant")
    ap.add_argument("--out", type=Path, default=Path("sessions"),
                    help="dossier des sessions (défaut : ./sessions)")
    ap.add_argument("--log", default="info", help="debug, info, warning")
    sub = ap.add_subparsers(dest="commande", required=True)

    p = sub.add_parser("selftest", help="le banc répond-il ? aucune acquisition")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("angle", help="mesurer, positionner ou calibrer le plateau")
    p.add_argument("--images", type=int, default=90, help="images par mesure")
    p.add_argument("--repeter", type=int, default=1)
    p.add_argument("--aller", type=float, default=None, help="consigne en degrés")
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--arreter", action="store_true", help="immobiliser le plateau")
    p.add_argument("--calibrer", action="store_true", help="réestimer le centre")
    p.add_argument("--duree", type=float, default=40.0)
    p.set_defaults(fn=cmd_angle)

    p = sub.add_parser("pose", help="aider à replacer la caméra")
    p.add_argument("--suivre", type=float, default=None, metavar="SECONDES",
                   help="boucle d'aide au placement : bougez, la ligne suit")
    p.add_argument("--images", type=int, default=10,
                   help="images par observation ponctuelle")
    p.add_argument("--image", default=None, metavar="CHEMIN",
                   help="écrire une image annotée (carreaux et centre calibré)")
    p.set_defaults(fn=cmd_pose)

    p = sub.add_parser("main", help="pilotage direct de la main")
    p.add_argument("--tester", action="store_true",
                   help="quels moteurs répondent sur cet exemplaire ?")
    p.add_argument("--ouvrir", action="store_true")
    p.add_argument("--zero", type=float, default=None, metavar="SECONDES")
    p.add_argument("--pivot", type=int, default=None, help="pivot du pouce, en counts")
    p.add_argument("--position", action="append", metavar="MOTEUR:COUNTS",
                   help="ex. --position 6:4000 (répétable)")
    p.add_argument("--velocity", type=int, default=None)
    p.add_argument("--max-current", type=int, default=None)
    p.add_argument("--suivre", type=float, default=None, metavar="SECONDES",
                   help="afficher la pression en direct")
    p.set_defaults(fn=cmd_main)

    p = sub.add_parser("session", help="protocole complet, en console")
    p.add_argument("--objet", default=None)
    p.add_argument("--nb-angles", type=int, default=6)
    p.add_argument("--pas", type=float, default=60.0)
    p.add_argument("--angles", type=float, nargs="*", default=None)
    p.add_argument("--phases", default="visuelle,tactile")
    p.add_argument("--sans-pouce", action="store_true",
                   help="le critère « pouce stable » n'est pas exigé pour cette session")
    p.add_argument("--auto-valider", action="store_true",
                   help="valider chaque capture sans demander (essais de chaîne)")
    p.add_argument("--tolerance-deg", type=float, default=None)
    p.add_argument("--pivot-depart", type=int, default=None)
    p.add_argument("--max-current", type=int, default=None)
    p.add_argument("--velocity", type=int, default=None)
    p.add_argument("--timeout-fermeture", type=float, default=None)
    p.set_defaults(fn=cmd_session)

    p = sub.add_parser("serve", help="interface web et API locale")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=config.PORT_WEB)
    p.add_argument("--objet", default=None)
    p.add_argument("--nb-angles", type=int, default=6)
    p.add_argument("--pas", type=float, default=60.0)
    p.add_argument("--angles", type=float, nargs="*", default=None)
    p.add_argument("--sans-pouce", action="store_true")
    p.add_argument("--tolerance-deg", type=float, default=None)
    p.add_argument("--pivot-depart", type=int, default=None)
    p.add_argument("--max-current", type=int, default=None)
    p.add_argument("--velocity", type=int, default=None)
    p.add_argument("--timeout-fermeture", type=float, default=None)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("cinematique", help="modèle de la main, et son étalonnage")
    p.add_argument("--position", action="append", metavar="MOTEUR:COUNTS",
                   help="ex. --position 6:4000 (répétable)")
    p.add_argument("--counts-par-radian", type=float, default=None,
                   help="facteur mesuré ; sans lui, une hypothèse est employée")
    p.add_argument("--etalonner", action="store_true",
                   help="mesurer le facteur sur la main réelle")
    p.add_argument("--json", default=None, metavar="FICHIER")
    p.set_defaults(fn=cmd_cinematique)

    p = sub.add_parser("analyser", help="que contiennent les trames enregistrées ?")
    p.add_argument("sessions", nargs="*", help="noms ou chemins ; toutes par défaut")
    p.add_argument("--court", action="store_true", help="le résumé seulement")
    p.add_argument("--tout", action="store_true", help="y compris les sessions sans flux")
    p.add_argument("--json", default=None, metavar="FICHIER",
                   help="écrire le rapport complet en JSON")
    p.set_defaults(fn=cmd_analyser)

    p = sub.add_parser("recover", help="reconstruire un manifeste depuis le journal")
    p.add_argument("sessions", nargs="*", help="noms ou chemins de sessions")
    p.add_argument("--lister", action="store_true", help="lister l'état de chaque session")
    p.add_argument("--force", action="store_true", help="écraser un manifeste existant")
    p.set_defaults(fn=cmd_recover)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    _log(a.log)
    config.install_paths()
    try:
        return a.fn(a)
    except ResourceBusy as e:
        print(f"\n{R}Ressource occupée{N} — {e}\n", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        logging.getLogger("vtctl").exception("échec")
        print(f"\n{R}{type(e).__name__}{N} : {e}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
