#!/usr/bin/env python3
"""
Script d'auto-calibration des angles des marqueurs ArUco.
Fait tourner la table et mesure la position angulaire relative de chaque marqueur par rapport à l'ID 1.
"""
import sys
import os
import time
import json
import argparse
import logging
import cv2
import math
from collections import defaultdict, deque
import numpy as np

# Importer nos modules
sys.path.insert(0, "/Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR")
from turntable_position_controller import TurntablePositionController

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_calibration():
    parser = argparse.ArgumentParser(description="Auto-calibration des angles des marqueurs ArUco.")
    parser.add_argument("-p", "--port", type=str, default=None, help="Port série de la table tournante (ex: /dev/tty.usbmodem1201).")
    parser.add_argument("-d", "--duration", type=float, default=20.0, help="Durée de la calibration en secondes.")
    parser.add_argument("--config", type=str, default="aruco_config.json", help="Chemin vers le fichier config.")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("=== AUTO-CALIBRATION DES ANGLES ARUCO ===")
    print("="*60)
    print(" Ce script va faire tourner le plateau et enregistrer la position")
    print(" relative des marqueurs dans le plan image pour calculer leurs angles.")
    print(" IMPORTANT : Assurez-vous que l'ID 1 est présent (il sert de référence 0.0°).")
    print("="*60 + "\n")

    # Initialisation du contrôleur (Désactiver la consigne de position pour contrôle manuel)
    pos_controller = TurntablePositionController(port=args.port, simulation=args.port is None, config_path=args.config)
    
    # S'assurer que l'inversion de couleur est activée pour la caméra réelle
    if pos_controller.tracker.simulation:
        pos_controller.tracker.invert_colors = False
    else:
        pos_controller.tracker.invert_colors = True
    
    # Démarrer la rotation
    print("[1/3] Lancement de la rotation lente...")
    pos_controller.turntable.rotation_droite() # Rotation horaire
    if pos_controller.tracker.simulation:
        pos_controller.tracker.set_simulation_rotation(20.0)
        
    time.sleep(1.0) # Attendre que la rotation commence

    print(f"[2/3] Acquisition des trames pendant {args.duration} secondes (Ne pas masquer la caméra)...")
    
    # Dictionnaire de stockage des écarts relatifs observés : { (id_a, id_b): [liste des ecarts en degres] }
    observations = defaultdict(list)
    
    start_time = time.time()
    frames_processed = 0
    
    window_name = "Calibration en cours..."
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)

    try:
        while time.time() - start_time < args.duration:
            frame = pos_controller.tracker.get_frame()
            if frame is None:
                time.sleep(0.01)
                continue
                
            corners, ids = pos_controller.tracker.detect_markers(frame)
            
            # Dessiner le retour vidéo
            frame_draw = frame.copy()
            if ids is not None and len(ids) > 0:
                cv2.aruco.drawDetectedMarkers(frame_draw, corners, ids)
                
                # Calculer les angles apparents de chaque marqueur dans l'image
                cx, cy = pos_controller.tracker.turntable_center if pos_controller.tracker.turntable_center else (frame.shape[1]//2, frame.shape[0]//2)
                cv2.circle(frame_draw, (cx, cy), 6, (0, 255, 255), -1)
                
                detected_markers = {}
                for idx, m_id_arr in enumerate(ids):
                    m_id = int(np.ravel(m_id_arr)[0])
                    pts = corners[idx][0]

                    mx = float(np.mean(pts[:, 0]))
                    my = float(np.mean(pts[:, 1]))
                    
                    dx = mx - cx
                    dy = -(my - cy) # Y écran inversé
                    beta_deg = math.degrees(math.atan2(dy, dx)) % 360.0
                    detected_markers[m_id] = beta_deg
                    
                    # Tracer les angles apparents
                    rad = math.radians(beta_deg)
                    cv2.line(frame_draw, (cx, cy), (int(cx + 200*math.cos(rad)), int(cy - 200*math.sin(rad))), (255, 0, 0), 1)
                    cv2.putText(frame_draw, f"ID {m_id}", (int(mx), int(my - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                
                # Enregistrer les écarts relatifs entre toutes les paires co-visibles
                for id_a, beta_a in detected_markers.items():
                    for id_b, beta_b in detected_markers.items():
                        if id_a != id_b:
                            # Calcul de l'écart angulaire relatif (a - b) % 360
                            diff = (beta_a - beta_b) % 360.0
                            observations[(id_a, id_b)].append(diff)
            
            # Afficher des infos sur l'image
            elapsed = time.time() - start_time
            cv2.putText(frame_draw, f"Calibration : {elapsed:.1f}s / {args.duration}s", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.imshow(window_name, frame_draw)
            cv2.waitKey(30)
            
            if pos_controller.tracker.simulation:
                pos_controller.tracker.set_simulation_rotation(20.0)
                
            frames_processed += 1
            
    finally:
        print("\n[3/3] Arrêt de la table et traitement des données...")
        pos_controller.stop()
        pos_controller.close()
        cv2.destroyAllWindows()

    print(f"Acquisition terminée. {frames_processed} trames traitées.")
    if len(observations) == 0:
        print("Erreur : Aucun marqueur détecté pendant la calibration. Vérifiez l'éclairage et la caméra.")
        return

    # 1. Calculer la moyenne circulaire des écarts pour chaque paire
    avg_diffs = {}
    for (id_a, id_b), diffs in observations.items():
        if len(diffs) < 5: # Ignorer les paires trop peu vues ensemble
            continue
        sin_sum = sum(math.sin(math.radians(d)) for d in diffs)
        cos_sum = sum(math.cos(math.radians(d)) for d in diffs)
        avg_diffs[(id_a, id_b)] = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0

    # 2. Reconstruire la position de tous les marqueurs relative à l'ID 1 par parcours de graphe (BFS)
    # L'ID 1 sert de racine avec un angle absolu sur table de 0.0°
    calibrated_angles = {1: 0.0}
    
    # Construire la liste d'adjacence du graphe : { node: [(neighbor, diff_node_to_neighbor), ...] }
    graph = defaultdict(list)
    for (id_a, id_b), val in avg_diffs.items():
        graph[id_b].append((id_a, val)) # Si diff = a - b, alors a = b + diff
        
    queue = deque([1])
    visited = {1}
    
    while queue:
        curr = queue.popleft()
        curr_angle = calibrated_angles[curr]
        
        for neighbor, diff_val in graph[curr]:
            if neighbor not in visited:
                visited.add(neighbor)
                # L'angle sur table du voisin est : (angle_curr + diff_val) % 360
                calibrated_angles[neighbor] = round((curr_angle + diff_val) % 360.0, 1)
                queue.append(neighbor)

    print("\n" + "="*45)
    print("=== RÉSULTATS DE L'AUTO-CALIBRATION ===")
    print("="*45)
    
    all_possible_ids = range(1, 7)
    missing_ids = []
    
    for m_id in all_possible_ids:
        if m_id in calibrated_angles:
            print(f"  Marqueur ID {m_id} : {calibrated_angles[m_id]:.1f}°")
        else:
            missing_ids.append(m_id)
            print(f"  Marqueur ID {m_id} : NON DÉTECTÉ (Utilisation valeur théorique)")
            # Fallback théorique par défaut si non vu
            calibrated_angles[m_id] = round((m_id - 1) * 60.0, 1)
            
    print("="*45)

    if missing_ids:
        print(f"Note : Les marqueurs {missing_ids} n'ont pas pu être calibrés et ont gardé leur valeur théorique.")

    # 3. Enregistrer les résultats dans aruco_config.json
    # Lire l'ancienne config
    config_file = args.config
    cfg = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            pass
            
    # Mettre à jour les angles
    cfg["marker_angles"] = {str(k): v for k, v in calibrated_angles.items()}
    
    # Sauvegarder
    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        print(f"\n[+] Configuration mise à jour avec succès dans '{config_file}' !")
    except Exception as e:
        print(f"\n[-] Erreur lors de l'écriture du fichier config : {e}")

if __name__ == "__main__":
    run_calibration()
