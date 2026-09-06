#!/usr/bin/env python3
"""
acquire.py — acquisition visuo-tactile d'une saisie, étape par étape.

    sudo python3 -m tools.acquire --object cylindre

Déroulé, dans cet ordre :

  0. ``open``        main ouverte, pouce au repos — état de référence
  1. ``thumb_ready`` pouce amené en opposition, doigts encore ouverts.
                     C'est la prise de vue « avant fermeture » : l'objet est
                     visible, la main est en place, rien ne le masque encore.
  2. ``closing``     série d'images pendant la fermeture, tactile à pleine
                     cadence en parallèle
  3. ``grasp``       la main s'est arrêtée : prise établie, plusieurs images
  4. ``released``    après réouverture, pour vérifier l'état de sortie

Puis la main se rouvre et le couple est coupé.

Objet seul, sans la main — à faire main ouverte et écartée, ou main retirée :

    sudo python3 -m tools.acquire --object cylindre --object-only

Images en 640x480 à 30 fps, couleur et profondeur alignées. C'est le mode
imposé par le montage : la D405 est vue en USB 2.1 dans la VM, où le 1280x720
retombe à 5 ou 15 fps.

Les deux flux partagent une **horloge unique** : chaque image et chaque état
tactile porte un ``t`` en secondes depuis le début de la session. C'est ce qui
rend le jeu de données recollable.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# La D405 est déjà emballée proprement dans VT-Light : réglages figés,
# profondeur alignée, intrinsèques exportées. On la réutilise plutôt que
# d'écrire une seconde version qui divergerait.
sys.path.insert(0, str(REPO / "VT-Light"))
# Le plateau tournant vit dans son propre dépôt. Son asservissement ArUco ne
# fonctionne pas dans la pose caméra actuelle : on le pilote en boucle ouverte,
# par les pas fixes de la télécommande infrarouge.
TURNTABLE_REPO = REPO / "Control_Turtable_IR"
sys.path.insert(0, str(TURNTABLE_REPO))

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile.bus import BusError, Hand  # noqa: E402
from vt_tactile.dataset import Session  # noqa: E402
from vt_tactile.envelop import EnvelopConfig, envelop, summarise  # noqa: E402
from vt_tactile.tpdo import TactileReader  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def load_camera(exposure: int | None):
    try:
        from vt_light.camera import CameraSettings, D405  # noqa: PLC0415
    except ImportError as e:
        raise SystemExit(f"Module caméra introuvable ({e}). Attendu dans "
                         f"{REPO / 'VT-Light'}.") from e
    settings = CameraSettings()
    if exposure is not None:
        settings = CameraSettings(exposure_us=exposure)
    return D405(settings)


def capture(session: Session, cam, step: str, n: int, reader=None, hand=None,
            note: str = "") -> None:
    """Une rafale d'images, avec l'état tactile associé si la main est là."""
    print(f"  [{step}] {n} image(s){'  — ' + note if note else ''}")
    for i in range(n):
        color, depth = cam.grab()
        extra = {}
        if hand is not None and reader is not None and reader.zeroed:
            raw = hand.latest_tactile()
            if raw is not None:
                state = reader.decode(raw)
                entry = session.save_tactile(step, state, hand.positions())
                extra["tactile_t"] = entry["t"]
        session.save_frame(step, i, color, depth, extra)


def _first_serial() -> str:
    import glob  # noqa: PLC0415
    ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not ports:
        raise SystemExit("Aucun port série : l'ESP32 n'est pas attaché à la VM.")
    return ports[0]


def open_dimmer():
    """Le variateur, ou ``None`` s'il n'est pas joignable."""
    from vt_light.dimmer import Dimmer  # noqa: PLC0415
    try:
        d = Dimmer()
        print(f"  Variateur sur {d.port}")
        return d
    except Exception as e:  # noqa: BLE001
        print(f"  Variateur indisponible ({e}) : niveaux notés, pas imposés.")
        return None


def capture_light_sweep(session: Session, cam, args, levels) -> None:
    """Objet seul, un angle, plusieurs niveaux de lumière."""
    dimmer = open_dimmer()
    acked = {}
    try:
        for level in levels:
            if dimmer is not None:
                acked[str(level)] = dimmer.set_pwm(level)
                time.sleep(args.light_settle)
                cam.flush(8)   # vider les images prises à l'ancien niveau
            capture(session, cam, f"object_only/pwm_{level:03d}", args.still,
                    note=f"objet seul, PWM {level}")
        session.note("light", {"levels": levels, "acknowledged": acked,
                               "port": getattr(dimmer, "port", None)})
    finally:
        if dimmer is not None:
            dimmer.close()


def capture_angles(session: Session, cam, args) -> None:
    """
    Objet seul, plusieurs poses angulaires du plateau.

    Le variateur et l'émetteur infrarouge partagent la même carte ESP32 et le
    même port série : on règle donc la lumière **avant**, on referme, et le
    plateau garde le port pour toute la série.

    La rotation est en boucle ouverte. Les angles enregistrés sont ceux
    **commandés**, pas mesurés : l'asservissement ArUco ne fonctionne pas dans
    la pose caméra actuelle, et un angle inventé serait pire que pas d'angle.
    """
    if args.light_pwm is not None:
        d = open_dimmer()
        if d is not None:
            print(f"  Lumière fixée à PWM {d.set_pwm(args.light_pwm)}")
            time.sleep(args.light_settle)
            d.close()
            time.sleep(0.5)

    from turntable import TurntableController  # noqa: PLC0415
    # Le port doit être explicite : construit sans port, pyserial rend un objet
    # **non ouvert** et connect() renvoie quand même un succès. Les commandes
    # partent alors dans le vide, avec des images identiques à chaque angle.
    port = args.serial_port or _first_serial()
    tt = TurntableController(
        port=port, config_path=str(TURNTABLE_REPO / "config_telecommande.json"))
    tt.connect()
    if getattr(tt, "simulation", False) or not (tt.ser and tt.ser.is_open):
        raise SystemExit(f"Plateau injoignable sur {port} : le port n'est pas "
                         f"ouvert. Aucune rotation ne partirait.")
    # L'ESP32 en USB CDC natif redémarre à l'ouverture du port : on laisse
    # passer le boot avant d'émettre la moindre trame infrarouge.
    time.sleep(2.5)
    print(f"  Plateau sur {tt.port}")
    rotate = {45: tt.angle_45, 90: tt.angle_90, 180: tt.angle_180}[args.angle_step]
    try:
        for i in range(args.angles):
            angle = (i * args.angle_step) % 360
            capture(session, cam, f"object_only/angle_{angle:03d}", args.still,
                    note=f"objet seul, {angle}° commandés")
            if i < args.angles - 1:
                print(f"  Rotation de {args.angle_step}°…")
                rotate()
                time.sleep(args.rotate_settle)
                cam.flush(8)
        session.note("turntable", {"angle_step_deg": args.angle_step,
                                   "angles_deg": [(i * args.angle_step) % 360
                                                  for i in range(args.angles)],
                                   "driven": True, "closed_loop": False,
                                   "port": tt.port})
        session.note("light", {"pwm": args.light_pwm})
    finally:
        try:
            tt.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--object", required=True, help="nom de l'objet saisi")
    ap.add_argument("--out", type=Path, default=Path("datasets"))
    ap.add_argument("--still", type=int, default=5,
                    help="images par étape fixe (moyennables pour le bruit)")
    ap.add_argument("--closing-every", type=int, default=3,
                    help="une image toutes les N itérations de fermeture")
    ap.add_argument("--exposure", type=int, default=None, help="exposition, µs")
    ap.add_argument("--thumb-pivot", type=int, default=hw.THUMB_OPPOSITION)
    ap.add_argument("--seat", type=int, default=150)
    ap.add_argument("--pressure", type=float, default=0.02)
    ap.add_argument("--max-current", type=int, default=hw.APPROACH_CURRENT)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--zero-seconds", type=float, default=2.0)
    ap.add_argument("--angle-z", type=float, default=0.0,
                    help="angle du plateau autour de Z, en degrés")
    ap.add_argument("--rotation-axis", default="z",
                    help="axe de rotation de l'objet sur le plateau")
    ap.add_argument("--light-pwm", type=int, default=None,
                    help="niveau PWM de la lampe pendant une saisie")
    ap.add_argument("--light-levels", default="48,128,255",
                    help="niveaux PWM balayés en mode objet seul")
    ap.add_argument("--light-settle", type=float, default=1.0,
                    help="délai après changement de niveau, en secondes")
    ap.add_argument("--angles", type=int, default=1,
                    help="nombre de poses angulaires en mode objet seul")
    ap.add_argument("--angle-step", type=int, default=45, choices=(45, 90, 180),
                    help="pas de rotation, limité aux pas de la télécommande")
    ap.add_argument("--serial-port", default=None,
                    help="port de l'ESP32 (variateur et plateau), auto par défaut")
    ap.add_argument("--rotate-settle", type=float, default=7.0,
                    help="attente après une commande de rotation, en secondes")
    ap.add_argument("--object-only", action="store_true",
                    help="objet seul : images sans toucher à la main")
    ap.add_argument("--iface", type=int, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    session = Session.create(args.out, args.object, meta={
        "object": {"name": args.object, "rotation_axis": args.rotation_axis},
        "turntable": {"angle_z_deg": args.angle_z, "driven": False},
        "light": {"pwm": args.light_pwm},
        "grasp": {"thumb_pivot": args.thumb_pivot, "seat": args.seat,
                  "pressure_threshold": args.pressure,
                  "max_current": args.max_current},
        "motors": {str(m): hw.MOTOR_NAMES[m] for m in hw.MOTOR_IDS},
        "excluded_motors": [hw.MOTOR_NAMES[m] for m in hw.BROKEN_MOTORS],
    })
    print(f"{BOLD}Session{RESET} : {session.root}")

    cam = load_camera(args.exposure)
    from vt_light.camera import FPS, HEIGHT, WIDTH  # noqa: PLC0415
    session.note("camera", {"intrinsics": cam.intrinsics,
                            "settings": cam.settings.as_dict(),
                            "width": WIDTH, "height": HEIGHT, "fps": FPS,
                            "align_depth": cam.align_depth})
    print(f"Caméra : {WIDTH}x{HEIGHT} à {FPS} fps, profondeur "
          f"{'alignée' if cam.align_depth else 'non alignée'} sur la couleur.")

    # ── Objet seul : aucun accès au bus, aucune main ──────────────────────────
    if args.object_only:
        levels = [int(v) for v in args.light_levels.split(",") if v.strip()]
        try:
            if args.angles > 1:
                capture_angles(session, cam, args)
            else:
                capture_light_sweep(session, cam, args, levels)
        finally:
            cam.close()
            print(f"\nManifeste : {session.close()}")
        return 0

    hand = Hand()
    reader = TactileReader()
    try:
        hand.connect(iface_index=args.iface)
        print("Réveil de la main (~10 s)…")
        hand.wake()
        # À partir d'ici, **toutes** les trames sont conservées, des deux types
        # et sans sous-échantillonnage. C'est ce qui rend la session
        # réinterprétable : ~900 trames/s, soit 173 ko/s, on peut se le payer.
        hand.start_recording()
        session.mark("recording_start")
        if not hand.open_hand():
            print("La main ne s'ouvre pas : on s'arrête avant d'acquérir quoi "
                  "que ce soit.", file=sys.stderr)
            return 3

        print(f"\nZéro tactile ({args.zero_seconds:.0f} s) — objet déjà posé, "
              f"ne touchez pas la main.")
        frames = hand.collect(args.zero_seconds)
        if not frames:
            raise BusError("aucune trame tactile")
        reader.zero(frames)
        session.note("tactile_baseline", reader.baseline)
        session.note("baseline_frames", len(frames))
        # Trames brutes au repos : la référence qui permettra de tout recalculer
        # si la table de découpage évolue encore — elle a déjà changé une fois.
        session.mark("zero_done", frames=len(frames))


        # ── 0. Main ouverte, pouce au repos ───────────────────────────────────
        capture(session, cam, "00_open", args.still, reader, hand,
                "main ouverte, pouce au repos")

        # ── 1. Pouce en opposition, doigts encore ouverts ─────────────────────
        session.mark("thumb_pivot_start", target=args.thumb_pivot)
        print(f"\n  Pouce en opposition ({args.thumb_pivot})…")
        hand.command({hw.THUMB_PIVOT: args.thumb_pivot},
                     hw.VELOCITY_CLOSE, args.max_current)
        time.sleep(2.5)
        capture(session, cam, "01_thumb_ready", args.still, reader, hand,
                "pouce en place, objet encore dégagé")

        # ── 2. Fermeture, images et tactile en parallèle ──────────────────────
        session.mark("closing_start")
        print(f"\n{BOLD}Fermeture{RESET} — image toutes les "
              f"{args.closing_every} itérations.")
        counter = {"n": 0, "img": 0}

        def on_step(sample, state, contacts):
            counter["n"] += 1
            session.save_tactile("02_closing", state,
                                 motors={"positions": sample["positions"],
                                         "currents": sample["currents"]},
                                 extra={"contacts": sorted(contacts)})
            if counter["n"] % args.closing_every:
                return
            color, depth = cam.grab()
            session.save_frame("02_closing", counter["img"], color, depth,
                               extra={"loop_iteration": counter["n"],
                                      "contacts": sorted(contacts)})
            counter["img"] += 1

        cfg = EnvelopConfig(thumb_pivot=None,   # déjà positionné, on n'y touche plus
                            seat_counts=args.seat,
                            pressure_threshold=args.pressure,
                            max_current=args.max_current,
                            timeout=args.timeout,
                            hold_seconds=0.0)   # le maintien est l'étape 3
        result = envelop(hand, reader, cfg, on_step=on_step)
        session.mark("closing_end", contacts=sorted(result.contacts))
        print()
        print(summarise(result))
        session.note("grasp_result", {k: v for k, v in result.to_dict().items()
                                      if k != "samples"})

        # ── 3. Prise établie ──────────────────────────────────────────────────
        capture(session, cam, "03_grasp", args.still, reader, hand,
                "main arrêtée, prise établie")

        # ── 4. Après réouverture ──────────────────────────────────────────────
        session.mark("reopen_start")
        print("\n  Réouverture…")
        opened = hand.open_hand()
        session.note("reopened", opened)
        time.sleep(0.5)
        capture(session, cam, "04_released", args.still, reader, hand,
                "après réouverture")
        if not opened:
            print("Réouverture incomplète : le couple reste actif.",
                  file=sys.stderr)

    except KeyboardInterrupt:
        print("\nInterrompu.")
        session.note("interrupted", True)
    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        session.note("error", str(e))
        return 2
    finally:
        # Le flux brut est vidé avant de couper : c'est la pièce maîtresse de la
        # session, elle ne doit pas se perdre sur une sortie par erreur.
        try:
            session.mark("recording_end")
            stream = hand.stop_recording()
            if stream:
                entry = session.save_raw_stream("raw", stream)
                print(f"\nFlux brut : {entry['count']} trames "
                      f"{entry['frame_types']} sur {entry['span_s']} s")
        except Exception as e:  # noqa: BLE001
            print(f"Flux brut non enregistré : {e}", file=sys.stderr)
        try:
            hand.release()
        except Exception:  # noqa: BLE001
            pass
        hand.close()
        cam.close()
        path = session.close()
        print(f"\n{len(session.frames)} images, {len(session.tactile)} états "
              f"tactiles.\nManifeste : {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
