#!/usr/bin/env python3
"""
Application principale de contrôle en position de la table tournante
par vision ArUco et RealSense D405.
"""
import sys
import os
import argparse
import time
import cv2
import logging
import threading
from turntable_position_controller import TurntablePositionController

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Variable globale pour quitter proprement depuis le thread d'entrée
should_quit = False

def on_mouse_click(event, x, y, flags, param):
    """Callback de la souris pour calibrer le centre de la table tournante par clic."""
    if event == cv2.EVENT_LBUTTONDOWN:
        controller = param
        controller.tracker.turntable_center = (x, y)
        controller.tracker.save_config()
        print(f"\n[CALIBRATION] Nouveau centre défini sur l'image : ({x}, {y})")

def print_help_menu():
    print("\n" + "="*55)
    print("=== CONTRÔLE DE POSITION ARUCO - TABLE TOURNANTE ===")
    print("="*55)
    print("  Commandes Console (Saisissez puis appuyez sur Entrée) :")
    print("    1 à 6         : Aller vers le marqueur ID 1 à 6")
    print("    [Nombre 0-360]: Aller vers l'angle cible en degrés (ex: 45, 90, 180)")
    print("    s / stop /   : Arrêter le mouvement")
    print("    c / calibrate : Calibrer le centre (cliquez sur le retour vidéo)")
    print("    q / quit / esc: Quitter l'application")
    print("\n  Raccourcis clavier (Fenêtre Vidéo active) :")
    print("    1 à 6         : Aller vers le marqueur ID 1 à 6")
    print("    a             : Saisir un angle personnalisé")
    print("    ESPACE / s    : Arrêter le mouvement")
    print("    q / ESC       : Quitter")
    print("="*55 + "\n")

def terminal_input_loop(pos_controller):
    """Boucle d'écoute d'entrée terminal en arrière-plan."""
    global should_quit
    while not should_quit:
        try:
            line = sys.stdin.readline().strip().lower()
            if not line:
                continue
                
            if line in ('q', 'quit', 'exit', 'esc'):
                print("Fermeture demandée via console...")
                should_quit = True
                break
                
            elif line in ('s', 'stop', ' '):
                pos_controller.stop()
                
            elif line in ('c', 'calibrate'):
                print("\n[CALIBRATION] Cliquez avec le bouton GAUCHE de la souris sur le centre du plateau dans la fenêtre vidéo.")
                
            elif len(line) == 1 and '1' <= line <= '6':
                m_id = int(line)
                pos_controller.set_target_marker(m_id)
                
            else:
                # Tenter d'interpréter comme un angle cible
                try:
                    angle_val = float(line)
                    pos_controller.set_target_angle(angle_val)
                except ValueError:
                    print(f"Commande ou angle non reconnu : '{line}'")
        except Exception as e:
            logging.error(f"Erreur dans le thread console: {e}")
            time.sleep(1)

def main():
    global should_quit
    parser = argparse.ArgumentParser(description="Contrôle en position de la table tournante via ArUco & RealSense D405.")
    parser.add_argument("-p", "--port", type=str, default=None, help="Port série (ex: /dev/ttyUSB0 ou COM3). Mode simulation par défaut si non spécifié.")
    parser.add_argument("-s", "--simulation", action="store_true", help="Forcer le mode simulation (pas de caméra ni port série).")
    parser.add_argument("--config", type=str, default="aruco_config.json", help="Chemin vers le fichier de config ArUco.")
    parser.add_argument("-t", "--tolerance", type=float, default=1.2, help="Tolérance d'arrêt angulaire en degrés (défaut: 1.2°).")
    args = parser.parse_args()


    print_help_menu()

    # Initialisation du contrôleur de position
    pos_controller = TurntablePositionController(
        port=args.port,
        simulation=args.simulation or (args.port is None and not os.path.exists("/dev/ttyUSB0")),
        config_path=args.config,
        tolerance_deg=args.tolerance
    )


    window_name = "Table Tournante ArUco - RealSense D405"
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window_name, on_mouse_click, pos_controller)

    # Démarrage du thread d'écoute console
    input_thread = threading.Thread(target=terminal_input_loop, args=(pos_controller,), daemon=True)
    input_thread.start()

    try:
        while not should_quit:
            # Exécuter une étape de vision + asservissement
            frame, current_angle = pos_controller.step()

            # Affichage de l'image annotée
            cv2.imshow(window_name, frame)

            # Traitement des touches clavier (fenêtre vidéo active)
            key = cv2.waitKey(20) & 0xFF

            if key == ord('q') or key == 27: # Quitter
                print("Fermeture de l'application...")
                should_quit = True
                break

            elif ord('1') <= key <= ord('6'): # Marqueur 1 à 6
                m_id = key - ord('0')
                pos_controller.set_target_marker(m_id)

            elif key == ord(' ') or key == ord('s'): # Arrêt
                pos_controller.stop()

            elif key == ord('a'): # Saisie console d'un angle
                try:
                    val_str = input("\nEntrez l'angle cible en degrés (0 à 360) : ").strip()
                    if val_str:
                        angle_val = float(val_str)
                        pos_controller.set_target_angle(angle_val)
                except ValueError:
                    print("Valeur angulaire invalide.")

            elif key == ord('c'): # Instruction calibration
                print("\n[CALIBRATION] Cliquez avec le bouton GAUCHE de la souris sur le centre du plateau dans la fenêtre vidéo.")

    except KeyboardInterrupt:
        print("\nInterruption utilisateur.")
    finally:
        should_quit = True
        pos_controller.close()
        cv2.destroyAllWindows()
        print("Fin du programme.")

if __name__ == "__main__":
    main()
