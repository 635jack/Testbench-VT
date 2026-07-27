#!/usr/bin/env python3
import time
import sys
from turntable import TurntableController

def print_status(controller):
    print("\n--- État actuel du plateau ---")
    for key, value in controller.state.items():
        print(f"  {key}: {value}")
    print(f"  Vitesse cycle active: {controller.get_speed_seconds()}s / rotation")
    print("------------------------------\n")

def main():
    print("=== Exemple de contrôle du plateau tournant ===")
    
    # Mode simulation activé par défaut s'il n'y a pas d'arguments
    port = None
    if len(sys.argv) > 1:
        port = sys.argv[1]
        print(f"Connexion au port série : {port}")
    else:
        print("Aucun port série spécifié. Démarrage en mode SIMULATION.")
        print("Pour spécifier un port : python3 example.py /dev/ttyUSB0")
    
    # Initialisation du contrôleur (baudrate par défaut à 115200)
    controller = TurntableController(port=port, baudrate=115200, config_path="config_telecommande.json")

    
    # Enregistrement de quelques callbacks pour intercepter l'appui sur la télécommande physique
    def on_on_off():
        print("[ÉVÉNEMENT] La télécommande physique a déclenché ON/OFF")
        print_status(controller)

    def on_speed_up():
        print("[ÉVÉNEMENT] La télécommande physique a demandé VITESSE +")
        print_status(controller)

    controller.register_callback(controller.CMD_ON_OFF, on_on_off)
    controller.register_callback(controller.CMD_VITESSE_PLUS, on_speed_up)
    
    # Démarrage du thread d'écoute en arrière-plan
    controller.start_listener()
    
    try:
        while True:
            print("Menu de commande (simulation d'émission de trames) :")
            print("1 : ON/OFF")
            print("2 : Rotation Continue")
            print("3 : Rotation Intermittente")
            print("4 : Vitesse +")
            print("5 : Tourner Droite (CW)")
            print("6 : Start / Pause")
            print("7 : Tourner Gauche (CCW)")
            print("8 : Vitesse -")
            print("9 : Ajustement Angle 45°")
            print("10: Ajustement Angle 90°")
            print("11: Ajustement Angle 180°")
            print("12: Définir Origine")
            print("13: Retour Origine")
            print("14: Rotation 90° avec Arrêt Automatique")
            print("s : Afficher l'état")
            print("q : Quitter")
            
            choice = input("\nEntrez votre choix : ").strip().lower()
            
            if choice == '1':
                controller.on_off()
            elif choice == '2':
                controller.mode_continu()
            elif choice == '3':
                controller.mode_intermittent()
            elif choice == '4':
                controller.vitesse_plus()
            elif choice == '5':
                controller.rotation_droite()
            elif choice == '6':
                controller.start_pause()
            elif choice == '7':
                controller.rotation_gauche()
            elif choice == '8':
                controller.vitesse_moins()
            elif choice == '9':
                controller.angle_45()
            elif choice == '10':
                controller.angle_90()
            elif choice == '11':
                controller.angle_180()
            elif choice == '12':
                controller.definir_origine()
            elif choice == '13':
                controller.retour_origine()
            elif choice == '14':
                controller.cycle_90()
            elif choice == 's':
                print_status(controller)
            elif choice == 'q':
                break
            else:
                print("Choix invalide.")
            
            # Attendre un court instant pour l'affichage de l'état
            time.sleep(0.5)

            
    except KeyboardInterrupt:
        pass
    finally:
        controller.disconnect()
        print("Fin du programme.")

if __name__ == "__main__":
    main()
