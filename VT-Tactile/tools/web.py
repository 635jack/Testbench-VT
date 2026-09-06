#!/usr/bin/env python3
"""
web.py — interface web de la DH116, servie directement au-dessus de VT-Tactile.

    sudo python3 -m tools.web --port 8080

Remplace le couple ``lhandpro_service`` + ``lhandpro_web``. Motif, mesuré le
2026-08-19 : le service constructeur accepte les consignes de position, les
mémorise (``get_position`` les relit), répond « succès » à ``move_motors`` — et
**ne les émet jamais sur le bus**. Sur 13 Mo de trames journalisées, la valeur
commandée n'apparaît pas une seule fois dans les trames de sortie ; seules des
trames de contrôle identiques pour les six axes circulent. VT-Tactile, lui,
déplace le même moteur de 0 à 2000 en quatre secondes sur le même matériel.

La page est inchangée : elle ne parle que HTTP et Server-Sent Events, elle
n'a jamais rien su de ROS. Seul le moteur derrière change.

Ni ROS, ni rclpy, ni rosbridge : la bibliothèque standard et VT-Tactile.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile.bus import BusError, Hand  # noqa: E402
from vt_tactile.tpdo import PALM, ZONE_NAMES, TactileReader  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

#: Noms de zones attendus par la page. VT-Tactile nomme ses zones en interne
#: (``index.tip``) ; la page les affiche en français. La correspondance vit ici
#: plutôt que dans le décodeur, qui n'a pas à connaître l'affichage.
ZONE_LABELS = {
    "thumb": "Pouce",
    "index.tip": "Index Tip", "index.pad": "Index Pad",
    "middle.tip": "Majeur Tip", "middle.pad": "Majeur Pad",
    "ring.tip": "Annulaire Tip", "ring.pad": "Annulaire Pad",
    "little": "Auriculaire",
    PALM: "Paume",
}

MOTORS = [
    {"id": 1, "name": "Pouce (flexion)", "short": "Pouce", "tactile": "Pouce"},
    {"id": 2, "name": "Pouce (opposition)", "short": "Pivot", "tactile": None},
    {"id": 3, "name": "Auriculaire", "short": "Auric.", "tactile": "Auriculaire"},
    {"id": 4, "name": "Annulaire", "short": "Annul.", "tactile": "Annulaire"},
    {"id": 5, "name": "Majeur", "short": "Majeur", "tactile": "Majeur"},
    {"id": 6, "name": "Index", "short": "Index", "tactile": "Index"},
]

#: Repère hérité d'un diagnostic dépassé — voir tools/pilote.py. Conservé pour
#: l'affichage : la reconnexion n'intervient plus qu'en dernier recours.
MOUVEMENTS_PAR_SESSION = 4
#: Tolérance sur la position atteinte, et seuil en dessous duquel on considère
#: que le moteur n'a pas bougé du tout.
TOLERANCE = 60
SEUIL_IMMOBILE = 100
#: Au-delà, un moteur immobile ne l'est pas faute d'ordre : il pousse contre
#: quelque chose. Relancer la commande dans ce cas, c'est forcer sur un doigt
#: coincé — l'annulaire a tiré 1059 ‰ le 2026-08-19 avant de passer en alarme.
SEUIL_BLOQUE = 500

PRESETS = {
    "open": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0},
    "fist": {1: 8500, 2: 7000, 3: 8500, 4: 8500, 5: 8500, 6: 8500},
    "pinch": {1: 6000, 2: 7000, 3: 0, 4: 0, 5: 0, 6: 6000},
    "point": {1: 6000, 2: 7000, 3: 8500, 4: 8500, 5: 8500, 6: 0},
    "grasp_ready": {1: 0, 2: 7000, 3: 0, 4: 0, 5: 0, 6: 0},
}


class Backend:
    """
    Échantillonne la main en continu et garde le dernier état sous verrou.

    Un fil dédié plutôt qu'une lecture à la demande : le décodage tactile coûte
    plus cher qu'une copie de dictionnaire, et deux navigateurs ouverts ne
    doivent pas doubler la charge sur le bus.
    """

    def __init__(self, hand: Hand, reader: TactileReader, rate: float = 30.0):
        self.hand = hand
        self.reader = reader
        self.period = 1.0 / max(rate, 1.0)
        self._lock = threading.Lock()
        self._zones: list[dict] = []
        self._stamp: float | None = None
        self._motors = {m["id"]: {"position": None, "current": None,
                                  "target": None} for m in MOTORS}
        self._alarms: dict[int, int] = {}
        self._tick = 0
        #: Le SDK constructeur n'est pas garanti réentrant. Le fil
        #: d'échantillonnage l'interroge à 30 Hz pendant que les requêtes HTTP y
        #: écrivent des consignes : sans sérialisation, on a observé des
        #: commandes acceptées et mémorisées que le moteur n'exécutait jamais,
        #: jusqu'au réveil suivant.
        self._sdk = threading.Lock()
        #: Les lectures SDK (positions, courants, alarmes) écrasent la trame de
        #: commande avant qu'elle ne parte : mesuré le 2026-08-19 — le même
        #: moteur va de 0 à 2600 sans ce fil, et ne bouge pas du tout avec lui à
        #: 30 Hz. On les espace, et on les fait taire autour de chaque consigne.
        self._silence_jusqu = 0.0
        self._derniere_lecture = 0.0
        #: Le variateur n'accepte que quatre mouvements par session, puis les
        #: refuse en silence — mesuré le 2026-08-19. On rouvre le bus avant d'y
        #: arriver plutôt que de présenter une commande qui sera ignorée.
        self._mouvements = 0
        self._pics: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.hand.latest_tactile()
                if raw is not None:
                    state = self.reader.decode(raw)
                    zones = []
                    for name in ZONE_NAMES:
                        z = state.zones.get(name)
                        if z is None:
                            continue
                        cells = [round(v, 4) for v in z.pressure]
                        zones.append({
                            "name": ZONE_LABELS[name],
                            "pressure": round(z.pressure_max, 4),
                            "avg": round(z.pressure_mean, 4),
                            "proximity": round(z.proximity or 0.0, 4),
                            # Négative quand la zone n'a pas cette voie : la page
                            # n'affiche alors rien, plutôt qu'un zéro qui se
                            # lirait comme « rien ne s'approche ».
                            "self_proximity": round(z.self_proximity, 4)
                                              if z.self_proximity is not None else -1.0,
                            "normal_force": round(z.normal_force or 0.0, 3),
                            "tangential_force": round(z.tangential_force or 0.0, 3),
                            "direction": z.direction if z.direction is not None else -1.0,
                            "cells": cells,
                            "active_cells": sum(1 for v in cells if v > 0.05),
                        })
                    with self._lock:
                        self._zones = zones
                        self._stamp = time.time()

            except Exception:  # noqa: BLE001 — un hoquet de bus ne doit pas tuer le fil
                pass
            time.sleep(self.period)

    # ── Lecture ──────────────────────────────────────────────────────────────

    def _relire_moteurs(self) -> None:
        """
        Relit position et courant — **parcimonieusement**.

        Mesuré le 2026-08-19 : ces lectures SDK annulent la trame de mouvement.
        Interrogées en continu, même à 5 Hz, le moteur n'avance pas d'un count,
        alors que la consigne est acceptée et mémorisée. Le fil d'échantillonnage
        ne fait donc plus que du tactile — qui, lui, ne coûte aucun appel SDK,
        il lit une trame déjà en cache — et la télémétrie se prend à la demande,
        espacée, et jamais pendant qu'un mouvement est en cours.
        """
        maintenant = time.time()
        if maintenant < self._silence_jusqu or maintenant - self._derniere_lecture < 2.0:
            return
        try:
            with self._sdk:
                pos, cur = self.hand.positions(), self.hand.currents()
            with self._lock:
                for m, d in self._motors.items():
                    d["position"], d["current"] = pos.get(m), cur.get(m)
            self._derniere_lecture = time.time()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> dict:
        self._relire_moteurs()
        with self._lock:
            age = time.time() - self._stamp if self._stamp else None
            motors = [{**m, **self._motors[m["id"]]} for m in MOTORS]
            return {
                "t": round(time.time(), 3),
                "tactile": list(self._zones),
                "tactile_age": round(age, 3) if age is not None else None,
                "motors": motors,
                "max_position": hw.POSITION_MAX,
                "services_ready": self._stamp is not None,
                "presets": sorted(PRESETS),
                "broken_motors": [hw.MOTOR_NAMES[m] for m in hw.BROKEN_MOTORS],
                # Un variateur en défaut refuse les consignes en silence : la
                # commande est acceptée, mémorisée, et rien ne bouge. Sans cette
                # remontée, la panne est indiscernable d'un bug de commande.
                "alarms": dict(self._alarms),
            }

    # ── Commandes ────────────────────────────────────────────────────────────

    def set_targets(self, targets: dict, velocity=None, max_current=None) -> dict:
        """
        Applique une consigne de position.

        Les moteurs en panne sont écartés : leur commander une position les fait
        forcer en butée sans jamais l'atteindre — le moteur 1 tire 213 ‰ en
        permanence quand on le sollicite.
        """
        vel = int(velocity if velocity is not None else hw.VELOCITY_CLOSE)
        cur = int(max_current if max_current is not None else hw.GRASP_CURRENT)
        applied, refuses = {}, []
        for jid, pos in targets.items():
            jid = int(jid)
            if jid in hw.BROKEN_MOTORS:
                refuses.append(hw.MOTOR_NAMES[jid])
                continue
            applied[jid] = max(0, min(int(pos), hw.POSITION_MAX))
        if applied:
            # Le variateur n'accepte que quatre mouvements par session, puis
            # les refuse sans rien signaler. On rouvre le bus avant d'y arriver,
            # et on vérifie chaque mouvement pour rattraper le cas où il aurait
            # cessé plus tôt.
            atteint = self._executer(applied, vel, cur)
            bloques = [hw.MOTOR_NAMES[m] for m in applied
                       if self._pics.get(m, 0) >= SEUIL_BLOQUE]
            if not atteint and not bloques:
                # Refusé en silence : on reprend le bus à zéro et on réessaie.
                self.reconnecter()
                atteint = self._executer(applied, vel, cur)
                bloques = [hw.MOTOR_NAMES[m] for m in applied
                           if self._pics.get(m, 0) >= SEUIL_BLOQUE]
            resultat = {"applied": applied, "ignores": refuses,
                        "atteint": atteint,
                        "bloques": bloques,
                        "pics_courant": dict(self._pics),
                        "mouvements": self._mouvements}
            with self._lock:
                for jid, pos in applied.items():
                    self._motors[jid]["target"] = pos
            return resultat
        return {"applied": applied, "ignores": refuses}

    def _executer(self, cibles: dict, vel: int, cur: int) -> bool:
        """Envoie la consigne, attend la course, et dit si elle a été suivie."""
        with self._lock:
            depart = {m: (self._motors[m]["position"] or 0) for m in cibles}
        course = max(abs(cibles[m] - depart[m]) for m in cibles)
        duree = course / max(vel, 1) + 3.0
        self._silence_jusqu = time.time() + duree
        with self._sdk:
            self.hand.enable()
            self.hand.command(cibles, vel, cur)
            self._mouvements += 1
        pics = {m: 0 for m in cibles}
        fin, debut, relances = time.time() + duree, time.time(), 0
        while time.time() < fin:
            time.sleep(0.4)
            with self._sdk:
                cur_l, pos_l = self.hand.currents(), self.hand.positions()
            for m in cibles:
                pics[m] = max(pics[m], cur_l.get(m, 0))
            # Le mouvement n'a pas démarré : on réémet l'ordre. ``move_motors``
            # n'est pas toujours pris du premier coup, et c'est ce qu'on
            # prenait à tort pour une limite du variateur.
            if (relances < 2 and time.time() - debut > 1.0
                    and all(abs(pos_l.get(m, 0) - depart[m]) < SEUIL_IMMOBILE
                            for m in cibles)):
                with self._sdk:
                    self.hand.relancer()
                relances += 1
        with self._sdk:
            pos = self.hand.positions()
        self._silence_jusqu = 0.0
        with self._lock:
            for m, d in self._motors.items():
                if m in pos:
                    d["position"] = pos[m]
        self._pics = pics
        return all(abs(pos.get(m, 0) - c) <= TOLERANCE
                   or abs(pos.get(m, 0) - depart[m]) >= SEUIL_IMMOBILE
                   for m, c in cibles.items())

    def preset(self, name: str, velocity=None, max_current=None) -> dict:
        if name not in PRESETS:
            raise ValueError(f"preset inconnu : {name}")
        return self.set_targets(PRESETS[name], velocity, max_current)

    def open_hand(self) -> dict:
        with self._sdk:
            return {"ouverte": self.hand.open_hand()}

    def reconnecter(self) -> dict:
        """
        Ferme et rouvre entièrement le bus, comme le ferait un processus neuf.

        Mesuré le 2026-08-19 : le variateur cesse d'exécuter les consignes après
        un temps d'usage — acceptées, cible relue, aucune alarme, rien ne bouge.
        Ni ``enable()`` ni un ``wake()`` complet en cours de processus ne
        rétablissent, alors qu'un processus neuf y parvient à tous les coups.
        C'est donc l'état interne du SDK qu'il faut reprendre à zéro.
        """
        self._silence_jusqu = time.time() + 40.0
        with self._sdk:
            try:
                self.hand.close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
            self.hand.connect()
            # Le tactile ne revient pas toujours dans les dix secondes qui
            # suivent une reprise du bus. Ce n'est pas une raison d'abandonner
            # le mouvement en cours : les moteurs, eux, sont pilotables sans
            # lui, et le zero tactile qui suit rattrapera ce qu'il faut.
            self.hand.wake(require_tactile=False)
            frames = self.hand.collect(2.0)
        if frames:
            self.reader.zero(frames)
        self._silence_jusqu = time.time() + 1.0
        self._mouvements = 0
        return {"reconnectee": True, "trames_zero": len(frames)}

    def reveil(self) -> dict:
        """Réveil complet — homing compris — puis nouveau zéro tactile."""
        self._silence_jusqu = time.time() + 25.0
        with self._sdk:
            self.hand.wake()
            frames = self.hand.collect(2.0)
        if frames:
            self.reader.zero(frames)
        self._silence_jusqu = time.time() + 1.0
        return {"reveillee": True, "trames_zero": len(frames)}

    def zero(self, seconds: float = 2.0) -> dict:
        with self._sdk:
            frames = self.hand.collect(seconds)
        if not frames:
            raise BusError("aucune trame tactile pendant la mise à zéro")
        self.reader.zero(frames)
        return {"trames": len(frames)}


def make_handler(back: Backend):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence
            pass

        def _send(self, code, body: bytes, ctype: str, **extra):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra.items():
                self.send_header(k.replace("_", "-"), v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, payload):
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                # La page n'a aucune ressource externe ; seul le navigateur
                # réclame ce fichier. Un 204 évite un 404 rouge en console.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/api/state":
                self._json(200, back.snapshot())
            elif path == "/api/stream":
                self._stream()
            else:
                self._json(404, {"error": "not found"})

        def _stream(self):
            """
            Flux Server-Sent Events, en **encodage par blocs**.

            En HTTP/1.1, un corps sans ``Content-Length`` doit être soit délimité
            par la fermeture de connexion, soit découpé en blocs. La première
            forme fonctionne avec curl mais fait tomber puis reconnecter
            ``EventSource`` en boucle ; la seconde est celle que les navigateurs
            attendent, et elle garde la connexion vivante.
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    corps = f"data: {json.dumps(back.snapshot())}\n\n".encode()
                    self.wfile.write(f"{len(corps):X}\r\n".encode())
                    self.wfile.write(corps)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.05)  # 20 Hz
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # onglet fermé, cas nominal

        def do_POST(self):
            if self.path.split("?")[0] != "/api/command":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"JSON invalide : {exc}"})
                return
            try:
                self._json(200, {"ok": True, "result": self._dispatch(payload)})
            except Exception as exc:  # noqa: BLE001 — remonte au navigateur
                self._json(500, {"ok": False, "error": str(exc)})

        def _dispatch(self, p):
            cmd = p.get("cmd")
            if cmd == "position":
                return back.set_targets(p.get("targets", {}),
                                        p.get("velocity"), p.get("max_current"))
            if cmd == "preset":
                return back.preset(p.get("name"), p.get("velocity"),
                                   p.get("max_current"))
            if cmd == "enable":
                # Conservé pour la page : la main est déjà réveillée au démarrage.
                return {"note": "la main est réveillée au lancement du serveur"}
            if cmd == "home":
                return back.open_hand()
            if cmd == "reconnexion":
                return back.reconnecter()
            if cmd == "reveil":
                return back.reveil()
            if cmd == "zero":
                return back.zero(float(p.get("seconds", 2.0)))
            raise ValueError(f"commande inconnue : {cmd!r}")

    return Handler


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # un flux SSE coupé n'est pas une erreur


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--zero-seconds", type=float, default=2.0)
    ap.add_argument("--iface", type=int, default=None)
    ap.add_argument("--sdk-dir", default=None)
    ap.add_argument("--sans-telemetrie", action="store_true",
                    help="ne pas lancer le fil d'échantillonnage (diagnostic)")
    args = ap.parse_args()

    hand = Hand(sdk_dir=args.sdk_dir)
    reader = TactileReader()
    try:
        hand.connect(iface_index=args.iface)
        # Le réveil échoue par intermittence : la main reste en trames moteur et
        # ne bascule jamais en mode capteur. Constaté plusieurs fois le
        # 2026-08-19, et une nouvelle tentative suffit. Autant la faire ici
        # plutôt que de laisser l'utilisateur relancer le serveur.
        for essai in range(1, 4):
            print(f"Réveil de la main (tentative {essai}/3, ~10 s)…", flush=True)
            try:
                hand.wake()
                break
            except BusError as e:
                if essai == 3:
                    raise
                print(f"  échec : {e} — nouvelle tentative", flush=True)
                time.sleep(2.0)
        print(f"Zéro tactile ({args.zero_seconds:.0f} s) — ne touchez pas la main…",
              flush=True)
        frames = hand.collect(args.zero_seconds)
        if not frames:
            raise BusError("aucune trame tactile : la main n'a pas été réveillée")
        reader.zero(frames)
        print(f"Zéro fait sur {len(frames)} trames.", flush=True)

        back = Backend(hand, reader)
        if not args.sans_telemetrie:
            back.start()
        else:
            print("Fil d'échantillonnage désactivé (diagnostic).", flush=True)
        server = QuietServer((args.host, args.port), make_handler(back))
        print(f"Interface sur http://{args.host}:{args.port}  (Ctrl-C pour quitter)",
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            back.stop()
    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        return 2
    finally:
        hand.release()
        hand.close()
        print("\nMain relâchée, bus fermé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
