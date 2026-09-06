#!/usr/bin/env python3
import sys
import json
import time
import os

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Erreur : La bibliothèque 'pyserial' est requise.")
    print("Vous pouvez l'installer avec : pip install pyserial")
    sys.exit(1)

COMMANDS = [
    "COMMANDE_ON_OFF",
    "COMMANDE_CONTINU",
    "COMMANDE_INTERMITTENT",
    "COMMANDE_VITESSE_PLUS",
    "COMMANDE_ROTATION_DROITE",
    "COMMANDE_START_PAUSE",
    "COMMANDE_ROTATION_GAUCHE",
    "COMMANDE_VITESSE_MOINS",
    "COMMANDE_ANGLE_45",
    "COMMANDE_ANGLE_90",
    "COMMANDE_ANGLE_180",
    "COMMANDE_DEFINIR_ORIGINE",
    "COMMANDE_RETOUR_ORIGINE",
    "COMMANDE_CYCLE_90"
]


def list_serial_ports():
    ports = serial.tools.list_ports.comports()
    return [port.device for port in ports]

def read_serial_frame(ser, timeout=2.0):
    """
    Lit une ligne sur le port série (s'arrête au caractère \n).
    """
    old_timeout = ser.timeout
    ser.timeout = timeout
    try:
        return ser.readline()
    finally:
        ser.timeout = old_timeout


def clean_frame(frame_bytes):
    """
    Nettoie et convertit la trame en chaîne de caractères ou format hexadécimal propre.
    Exclut les trames de relâchement (ex: 32 0x0) pour ne garder que les appuis.
    """
    # Enlever les retours à la ligne et espaces blancs aux extrémités
    stripped = frame_bytes.strip()
    if not stripped:
        return None

    # Tenter de décoder en UTF-8
    try:
        text = stripped.decode('utf-8')
        # Vérifier si c'est de l'ASCII imprimable standard
        if all(32 <= ord(c) < 127 for c in text):
            tokens = text.split()
            if len(tokens) >= 4:
                state = tokens[-1]
                flag = tokens[-2]
                # Si le flag est 32 ou si l'état est 0, c'est un relâchement -> on l'ignore
                if state in ("0", "0x0", "0x00") or flag in ("32", "0x20"):
                    return None
            
            # Nettoyer le préfixe RCV pour ne stocker que le code propre
            if text.startswith("RCV "):
                text = text[4:]
            return text
    except UnicodeDecodeError:
        pass

    # Si ce n'est pas de l'ASCII lisible, on retourne une représentation hexadécimale
    return "0x" + stripped.hex().upper()

def calibrate_command(ser, command_name):
    print(f"\n--- Acquisition pour : {command_name} ---")
    attempts = []
    required_presses = 5
    last_received_time = 0
    last_received_code = None
    
    # Vider le buffer d'entrée au début de la commande
    ser.reset_input_buffer()
    
    while len(attempts) < required_presses:
        print(f"[{len(attempts) + 1}/{required_presses}] Appuyez sur le bouton de la télécommande (En attente de signal série)...", flush=True)
        
        # Attendre la trame (timeout plus long car l'utilisateur doit avoir le temps d'appuyer)
        frame = read_serial_frame(ser, timeout=10.0)
        
        if not frame:
            print("[!] Timeout : Aucun signal détecté. Réessayez.")
            continue
            
        cleaned = clean_frame(frame)
        if not cleaned:
            # Soit trame invalide, soit trame de relâchement ignorée
            continue
            
        # Déduplication temporelle pour éviter de compter les répétitions d'un seul appui continu
        current_time = time.time()
        if cleaned == last_received_code and (current_time - last_received_time) < 1.2:
            # Même touche répétée trop vite -> ignorée
            last_received_time = current_time
            continue
            
        print(f"[+] Reçu : {cleaned} (Brut: {frame.hex()})")
        attempts.append(cleaned)
        last_received_code = cleaned
        last_received_time = current_time
        
        # Laisser un court instant pour que l'utilisateur relâche le bouton
        time.sleep(0.3)
        ser.reset_input_buffer()
        
    # Vérifier la cohérence
    if len(set(attempts)) == 1:
        detected_code = attempts[0]
        print(f"[OK] Touche validée ! Code associé : {detected_code}")
        return detected_code
    else:
        print("[ATTENTION] Les codes reçus ne sont pas identiques entre les pressions :")
        for i, code in enumerate(attempts):
            print(f"  Pression {i+1} : {code}")
        choice = input("Voulez-vous recommencer cette touche (r) ou forcer le premier code reçu (f) ? [r/f] : ").strip().lower()
        if choice == 'f':
            return attempts[0]
        return calibrate_command(ser, command_name)

def main():
    print("=== Outil d'apprentissage du plateau tournant IR ===")
    
    ports = list_serial_ports()
    if not ports:
        print("Aucun port série détecté automatiquement.")
        port_choice = input("Veuillez saisir le chemin du port série (ex: /dev/ttyUSB0) : ").strip()
    else:
        print("Ports série détectés :")
        for idx, port in enumerate(ports):
            print(f"[{idx}] {port}")
        idx_choice = input(f"Choisissez le port [0-{len(ports)-1}] ou saisissez un chemin : ").strip()
        if idx_choice.isdigit() and int(idx_choice) < len(ports):
            port_choice = ports[int(idx_choice)]
        else:
            port_choice = idx_choice

    baud_choice = input("Baudrate [115200 par défaut] : ").strip()
    baudrate = int(baud_choice) if baud_choice.isdigit() else 115200


    print(f"\nOuverture de {port_choice} à {baudrate} bauds...")
    try:
        ser = serial.Serial(port_choice, baudrate, timeout=1.0)
    except Exception as e:
        print(f"Erreur lors de l'ouverture du port série : {e}")
        sys.exit(1)

    config = {}
    
    try:
        for cmd in COMMANDS:
            code = calibrate_command(ser, cmd)
            config[cmd] = code
            
        # Demander si on enregistre
        config_path = "config_telecommande.json"
        print("\n=== Apprentissage Terminé ===")
        print(json.dumps(config, indent=4))
        
        save = input(f"\nEnregistrer la configuration dans '{config_path}' ? [O/n] : ").strip().lower()
        if save != 'n':
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            print(f"[OK] Configuration enregistrée dans '{config_path}'")
        else:
            print("Configuration non enregistrée.")
            
    except KeyboardInterrupt:
        print("\n\nApprentissage interrompu par l'utilisateur.")
    finally:
        ser.close()
        print("Port série fermé.")

if __name__ == "__main__":
    main()
