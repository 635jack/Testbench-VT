#!/usr/bin/env python3
"""
calibrate_center.py — Centre de rotation du plateau, par trajectoire.

Le centre est stocké en pixels : il n'est donc valable que pour la
résolution où il a été estimé. Les modes de la D405 n'ayant pas le même
champ de vision, il n'est pas transposable par une simple mise à l'échelle
— il faut le réestimer à chaque changement de mode ou de position caméra.

Méthode : pendant que le plateau tourne, chaque marqueur décrit un cercle
centré sur l'axe de rotation. On enregistre les trajectoires et on y ajuste
un cercle. Contrairement à une estimation à partir d'une seule image, celle-ci
ne dépend ni du nombre de marqueurs visibles simultanément, ni de leur
disposition supposée.

    python3 calibrate_center.py -p /dev/ttyACM0 -d 25
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict

import cv2
import numpy as np

from turntable_position_controller import TurntablePositionController

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")


def fit_trajectoire(points: np.ndarray):
    """
    Ajuste une ellipse à la trajectoire d'un marqueur.

    La trajectoire est un cercle dans le plan du plateau, mais la caméra le
    regarde de biais : son image est une ellipse, dont le petit axe vaut le
    grand multiplié par le sinus de l'élévation. Ajuster un cercle laisserait
    un résidu systématique de plusieurs pixels, sans rapport avec la qualité
    de la mesure.

    Returns:
        (cx, cy, grand_demi_axe, petit_demi_axe, residu_rms) ou None.
    """
    if len(points) < 8:
        return None
    pts = points.astype(np.float32)
    (cx, cy), (d1, d2), angle = cv2.fitEllipse(pts)
    a, b = max(d1, d2) / 2.0, min(d1, d2) / 2.0
    if a < 1e-6 or b < 1e-6:
        return None

    # Residu : distance a l'ellipse, mesuree dans le repere propre.
    th = np.radians(angle)
    dx, dy = pts[:, 0] - cx, pts[:, 1] - cy
    u = dx * np.cos(th) + dy * np.sin(th)
    v = -dx * np.sin(th) + dy * np.cos(th)
    # (u/b)^2 + (v/a)^2 = 1 : cv2 rend l'angle du premier axe (d1).
    if d1 <= d2:
        u, v = v, u
    rho = np.sqrt((u / a) ** 2 + (v / b) ** 2)
    residu = float(np.sqrt(np.mean((rho - 1.0) ** 2)) * b)
    return float(cx), float(cy), float(a), float(b), residu


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibration du centre du plateau")
    parser.add_argument("-p", "--port", default="/dev/ttyACM0")
    parser.add_argument("-d", "--duration", type=float, default=25.0,
                        help="durée de rotation observée, en secondes")
    parser.add_argument("--config", default="aruco_config.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="ne pas écrire le résultat dans la config")
    args = parser.parse_args()

    ctl = TurntablePositionController(port=args.port, simulation=False,
                                      config_path=args.config)
    tracker = ctl.tracker
    # Le code d'origine ne porte pas d'attribut de mode : on lit la
    # taille reelle d'une image, qui est la seule verite utile ici.
    _essai = tracker.get_frame()
    height, width = _essai.shape[:2]
    print(f"source : {tracker.source_name}")
    print(f"centre actuellement configuré : {tracker.turntable_center}")

    def demarrer_rotation(essais=4) -> bool:
        """
        Lance la rotation et **vérifie** qu'elle a bien démarré.

        `ROTATION_DROITE` ne suffit pas toujours : selon l'état interne de la
        table, elle ne fait que fixer le sens, et il faut `START_PAUSE` pour
        lancer. Sans vérification, on enregistre 25 s de trajectoires
        immobiles — constaté, avec des arcs de 1 pixel.
        """
        for essai in range(essais):
            commande = ctl.turntable.rotation_droite if essai % 2 == 0 \
                else ctl.turntable.start_pause
            print(f"  démarrage, essai {essai + 1} : {commande.__name__}")
            commande()
            time.sleep(1.0)
            debut = None
            t0 = time.time()
            while time.time() - t0 < 3.0:
                ctl.step()
                if ctl.last_angle is not None and debut is None:
                    debut = ctl.last_angle
            if ctl.last_angle is not None and debut is not None:
                parcouru = abs((ctl.last_angle - debut + 180.0) % 360.0 - 180.0)
                print(f"     angle parcouru en 3 s : {parcouru:.1f}°")
                if parcouru > 4.0:
                    return True
        return False

    trajectoires = defaultdict(list)
    try:
        print(f"\nDémarrage de la rotation…")
        if not demarrer_rotation():
            print("Impossible de faire tourner le plateau. Abandon.")
            ctl.close()
            return 1
        print(f"Rotation confirmée. Enregistrement des trajectoires "
              f"pendant {args.duration:.0f} s…")

        t0 = time.time()
        derniere_trace = 0.0
        while time.time() - t0 < args.duration:
            frame = tracker.get_frame()
            corners, ids = tracker.detect_markers(frame)
            if ids is not None:
                for i, m in enumerate(ids.ravel()):
                    pts = corners[i][0]
                    trajectoires[int(m)].append(
                        [float(pts[:, 0].mean()), float(pts[:, 1].mean())])
            ecoule = time.time() - t0
            if ecoule - derniere_trace > 5.0:
                derniere_trace = ecoule
                vus = {k: len(v) for k, v in sorted(trajectoires.items())}
                print(f"  {ecoule:4.0f}s — points par marqueur : {vus}")
    finally:
        print("\nArrêt du plateau…")
        ctl.ensure_stopped()

    print("\n=== AJUSTEMENT DES TRAJECTOIRES")
    estimations = []
    for m_id, pts in sorted(trajectoires.items()):
        arr = np.asarray(pts, dtype=np.float64)
        etendue = float(np.hypot(*(arr.max(axis=0) - arr.min(axis=0))))
        res = fit_trajectoire(arr)
        if res is None:
            print(f"  marqueur {m_id} : {len(arr)} points, trop peu")
            continue
        cx, cy, a, b, residu = res
        # Un arc trop court rend l'ajustement instable : l'ellipse peut
        # glisser le long de la tangente sans que le residu n'augmente.
        fiable = etendue > 0.6 * a and residu < 0.06 * a
        elevation = np.degrees(np.arcsin(min(b / a, 1.0)))
        print(f"  marqueur {m_id} : {len(arr):4d} points, arc {etendue:5.0f} px, "
              f"axes {a:5.0f}/{b:5.0f} px (elevation {elevation:4.1f} deg), "
              f"residu {residu:5.2f} px -> centre ({cx:6.1f}, {cy:6.1f}) "
              f"{'retenu' if fiable else 'ECARTE'}")
        if fiable:
            estimations.append((cx, cy, a, len(arr)))

    if not estimations:
        print("\nAucune trajectoire exploitable. Vérifier que des marqueurs "
              "sont détectés pendant toute la rotation.")
        ctl.close()
        return 1

    poids = np.array([e[3] for e in estimations], dtype=np.float64)
    cx = float(np.average([e[0] for e in estimations], weights=poids))
    cy = float(np.average([e[1] for e in estimations], weights=poids))
    dispersion = (float(np.std([e[0] for e in estimations])),
                  float(np.std([e[1] for e in estimations])))

    print(f"\n=== RESULTAT")
    print(f"  centre estimé : ({cx:.1f}, {cy:.1f})  en {width}x{height}")
    print(f"  dispersion entre marqueurs : {dispersion[0]:.1f}, {dispersion[1]:.1f} px")
    ancien = tracker.turntable_center
    if ancien:
        print(f"  ancien centre : {ancien}  — écart "
              f"{np.hypot(cx - ancien[0], cy - ancien[1]):.0f} px")
    if not (0 <= cx < width and 0 <= cy < height):
        print("  ATTENTION : le centre estimé tombe hors de l'image.")

    if args.dry_run:
        print("\n--dry-run : configuration inchangée.")
    else:
        tracker.turntable_center = (cx, cy)
        tracker.save_config()
        print(f"\nConfiguration mise à jour dans {args.config}.")

    ctl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
