#!/usr/bin/env python3
import json
import logging
import threading
import time
import os
import sys

try:
    import serial
except ImportError:
    logging.warning("La bibliothèque 'pyserial' n'est pas installée. Le mode simulation sera activé par défaut.")
    serial = None

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TurntableController:
    """
    Bibliothèque de pilotage du plateau tournant d'exposition via liaison série UART.
    """
    
    # Noms de commandes normalisés
    CMD_ON_OFF = "COMMANDE_ON_OFF"
    CMD_CONTINU = "COMMANDE_CONTINU"
    CMD_INTERMITTENT = "COMMANDE_INTERMITTENT"
    CMD_VITESSE_PLUS = "COMMANDE_VITESSE_PLUS"
    CMD_ROTATION_DROITE = "COMMANDE_ROTATION_DROITE"
    CMD_START_PAUSE = "COMMANDE_START_PAUSE"
    CMD_ROTATION_GAUCHE = "COMMANDE_ROTATION_GAUCHE"
    CMD_VITESSE_MOINS = "COMMANDE_VITESSE_MOINS"
    CMD_ANGLE_45 = "COMMANDE_ANGLE_45"
    CMD_ANGLE_90 = "COMMANDE_ANGLE_90"
    CMD_ANGLE_180 = "COMMANDE_ANGLE_180"
    CMD_DEFINIR_ORIGINE = "COMMANDE_DEFINIR_ORIGINE"
    CMD_RETOUR_ORIGINE = "COMMANDE_RETOUR_ORIGINE"
    CMD_CYCLE_90 = "COMMANDE_CYCLE_90"


    def __init__(self, port=None, baudrate=115200, config_path="config_telecommande.json", simulation=False):
        self.port = port
        self.baudrate = baudrate
        if config_path == "config_telecommande.json" and not os.path.exists(config_path):
            dir_of_file = os.path.dirname(os.path.abspath(__file__))
            rel_config = os.path.join(dir_of_file, "config_telecommande.json")
            if os.path.exists(rel_config):
                config_path = rel_config
        self.config_path = config_path
        self.simulation = simulation or (serial is None)
        
        self.ser = None
        self.commands_map = {}      # Mappe {Commande_Name: Code}
        self.reverse_map = {}       # Mappe {Code: Commande_Name}
        self.callbacks = {}         # Mappe {Commande_Name: callback_func}
        
        # Variables de déduplication temporelle
        self._last_trigger_time = 0
        self._last_trigger_code = None
        
        # État logique estimé du plateau tournant
        self.state = {
            "power_on": False,
            "running": False,
            "speed_index": 1,        # Plage 0, 1, 2 correspondant à 30s, 15s, 8s
            "direction": "CW",       # "CW" (Horaire / Droite) ou "CCW" (Anti-horaire / Gauche)
            "mode": "continu",       # "continu", "intermittent", "cycle_90", "oscillation"
            "oscillation_angle": 90, # 45, 90, 180
            "origin_set": False,
        }
        
        self._running_listener = False
        self._listener_thread = None

        
        self.load_config()
        if not self.simulation and self.port:
            self.connect()

    def load_config(self):
        """Charge la configuration des touches depuis le fichier JSON."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.commands_map = json.load(f)
                self.reverse_map = {v: k for k, v in self.commands_map.items()}
                logging.info(f"Configuration chargée depuis {self.config_path} ({len(self.commands_map)} commandes)")
            except Exception as e:
                logging.error(f"Erreur lors du chargement de la configuration: {e}")
        else:
            logging.warning(f"Fichier de configuration {self.config_path} absent. Mode apprentissage recommandé.")

    def save_config(self):
        """Enregistre la configuration actuelle dans le fichier JSON."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.commands_map, f, indent=4, ensure_ascii=False)
            logging.info(f"Configuration sauvegardée dans {self.config_path}")
        except Exception as e:
            logging.error(f"Erreur de sauvegarde de la configuration: {e}")

    def connect(self):
        """Établit la connexion série."""
        if self.simulation:
            logging.info("[Simulateur] Connexion série simulée active.")
            return True
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=0.5)
            logging.info(f"Connecté au plateau tournant sur {self.port} à {self.baudrate} bauds.")
            return True
        except Exception as e:
            # Tenter de chercher un port alternatif (ex: /dev/ttyACM1 si /dev/ttyACM0 échoue)
            import glob
            candidate_ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
            for alt_port in candidate_ports:
                if alt_port != self.port:
                    try:
                        self.ser = serial.Serial(alt_port, self.baudrate, timeout=0.5)
                        self.port = alt_port
                        logging.info(f"Bascule automatique réussie sur le port série {self.port} !")
                        return True
                    except Exception:
                        pass
            logging.error(f"Impossible d'ouvrir le port série {self.port}: {e}")
            logging.warning("Bascule automatique en mode simulation.")
            self.simulation = True
            return False

    def disconnect(self):
        """Ferme la connexion série et arrête le thread d'écoute."""
        self.stop_listener()
        if self.ser and self.ser.is_open:
            self.ser.close()
            logging.info("Port série fermé.")

    def start_listener(self):
        """Démarre le thread d'écoute pour intercepter les commandes de la télécommande."""
        if self._listener_thread and self._listener_thread.is_alive():
            return
        self._running_listener = True
        self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener_thread.start()
        logging.info("Thread d'écoute série démarré.")

    def stop_listener(self):
        """Arrête le thread d'écoute."""
        self._running_listener = False
        if self._listener_thread:
            self._listener_thread.join(timeout=1.0)
            logging.info("Thread d'écoute série arrêté.")

    def register_callback(self, command_name, callback):
        """Associe une fonction de callback à la réception d'une commande."""
        self.callbacks[command_name] = callback

    def _listen_loop(self):
        """Boucle de lecture en tâche de fond."""
        buffer = bytearray()
        while self._running_listener:
            if self.simulation:
                time.sleep(0.1)
                continue
            
            try:
                if self.ser and self.ser.in_waiting > 0:
                    char = self.ser.read(1)
                    if char:
                        buffer.extend(char)
                        if char in (b'\n', b'\r'):
                            line = buffer.strip()
                            buffer.clear()
                            if line:
                                self._handle_received_frame(line)
                else:
                    time.sleep(0.01)
            except Exception as e:
                logging.error(f"Erreur dans la boucle de lecture série : {e}")
                time.sleep(1)

    def _handle_received_frame(self, frame_bytes):
        """Analyse la trame brute reçue du port série."""
        # Tenter le décodage UTF-8, sinon fallback hex
        try:
            decoded = frame_bytes.decode('utf-8').strip()
        except UnicodeDecodeError:
            decoded = "0x" + frame_bytes.strip().hex().upper()

        logging.debug(f"Trame série reçue : {decoded}")

        # 1. Ignorer les trames de relâchement (ex: contient 32 0x0 ou finit par 0)
        tokens = decoded.split()
        if len(tokens) >= 4:
            state = tokens[-1]
            flag = tokens[-2]
            if state in ("0", "0x0", "0x00") or flag in ("32", "0x20"):
                logging.debug("Trame de relâchement ignorée.")
                return

        # 2. Nettoyer le préfixe RCV pour comparaison avec notre map
        cleaned_code = decoded
        if cleaned_code.startswith("RCV "):
            cleaned_code = cleaned_code[4:]

        # 3. Rechercher dans notre reverse_map (correspondance exacte)
        command = self.reverse_map.get(cleaned_code)
        
        # Fallback correspondance partielle si besoin
        if not command:
            for code, cmd_name in self.reverse_map.items():
                if code in cleaned_code or cleaned_code in code:
                    command = cmd_name
                    break

        if command:
            # 4. Déduplication temporelle pour éviter les répétitions rapides (ex: 1.0 seconde)
            current_time = time.time()
            if cleaned_code == self._last_trigger_code and (current_time - self._last_trigger_time) < 1.0:
                logging.debug(f"Répétition de la commande {command} ignorée (filtre temporel).")
                return

            self._last_trigger_code = cleaned_code
            self._last_trigger_time = current_time

            logging.info(f"Commande détectée : {command}")
            self._update_state_from_command(command)
            
            # Déclencher le callback s'il existe
            if command in self.callbacks:
                try:
                    self.callbacks[command]()
                except Exception as e:
                    logging.error(f"Erreur dans le callback pour {command}: {e}")
        else:
            logging.warning(f"Trame reçue non reconnue : {decoded}")


    def _update_state_from_command(self, command):
        """Met à jour l'état logique interne basé sur la commande reçue ou émise."""
        if command == self.CMD_ON_OFF:
            self.state["power_on"] = not self.state["power_on"]
            if not self.state["power_on"]:
                self.state["running"] = False
        elif command == self.CMD_START_PAUSE:
            if self.state["power_on"]:
                self.state["running"] = not self.state["running"]
        elif command == self.CMD_VITESSE_PLUS:
            self.state["speed_index"] = min(2, self.state["speed_index"] + 1)
        elif command == self.CMD_VITESSE_MOINS:
            self.state["speed_index"] = max(0, self.state["speed_index"] - 1)
        elif command == self.CMD_ROTATION_GAUCHE:
            self.state["direction"] = "CCW"
            self.state["running"] = True
        elif command == self.CMD_ROTATION_DROITE:
            self.state["direction"] = "CW"
            self.state["running"] = True
        elif command == self.CMD_CONTINU:
            self.state["mode"] = "continu"
            self.state["running"] = True
        elif command == self.CMD_INTERMITTENT:
            self.state["mode"] = "intermittent"
            self.state["running"] = True
        elif command == self.CMD_CYCLE_90:
            self.state["mode"] = "cycle_90"
            self.state["running"] = True
        elif command == self.CMD_ANGLE_45:
            self.state["oscillation_angle"] = 45
            self.state["mode"] = "oscillation"
            self.state["running"] = True
        elif command == self.CMD_ANGLE_90:
            self.state["oscillation_angle"] = 90
            self.state["mode"] = "oscillation"
            self.state["running"] = True
        elif command == self.CMD_ANGLE_180:
            self.state["oscillation_angle"] = 180
            self.state["mode"] = "oscillation"
            self.state["running"] = True
        elif command == self.CMD_DEFINIR_ORIGINE:
            self.state["origin_set"] = True
        elif command == self.CMD_RETOUR_ORIGINE:
            self.state["running"] = True


    def format_send_frame(self, code):
        """
        Formate le code brut pour l'émission.
        Si RCV est présent, le remplace par SEND.
        Sinon, si SEND n'est pas déjà présent, ajoute 'SEND ' en préfixe.
        """
        if "RCV" in code:
            return code.replace("RCV", "SEND")
        elif "rcv" in code:
            return code.replace("rcv", "send")
        elif "SEND" not in code and "send" not in code:
            return f"SEND {code}"
        else:
            return code


    def send_command(self, command_name):
        """Envoie la trame série correspondant à la commande demandée."""
        code = self.commands_map.get(command_name)
        if not code:
            logging.error(f"Impossible d'envoyer la commande {command_name} : code non configuré.")
            return False

        send_code = self.format_send_frame(code)
        
        logging.info(f"Émission de la commande {command_name} -> Trame émise : {send_code}")
        
        # Mettre à jour l'état logique interne
        self._update_state_from_command(command_name)

        if self.simulation:
            return True

        try:
            # Ajout du retour à la ligne si la trame d'origine en avait un (ou \r\n par défaut pour la liaison série)
            payload = send_code
            if not payload.endswith('\n') and not payload.endswith('\r'):
                payload += '\n'
            
            self.ser.write(payload.encode('utf-8'))
            self.ser.flush()
            return True
        except Exception as e:
            logging.error(f"Erreur d'écriture sur le port série : {e}")
            return False

    # --- Raccourcis pour les méthodes de contrôle ---
    
    def on_off(self):
        return self.send_command(self.CMD_ON_OFF)

    def mode_continu(self):
        return self.send_command(self.CMD_CONTINU)

    def mode_intermittent(self):
        return self.send_command(self.CMD_INTERMITTENT)

    def vitesse_plus(self):
        return self.send_command(self.CMD_VITESSE_PLUS)

    def rotation_droite(self):
        return self.send_command(self.CMD_ROTATION_DROITE)

    def start_pause(self):
        return self.send_command(self.CMD_START_PAUSE)

    def rotation_gauche(self):
        return self.send_command(self.CMD_ROTATION_GAUCHE)

    def vitesse_moins(self):
        return self.send_command(self.CMD_VITESSE_MOINS)

    def angle_45(self):
        return self.send_command(self.CMD_ANGLE_45)

    def angle_90(self):
        return self.send_command(self.CMD_ANGLE_90)

    def angle_180(self):
        return self.send_command(self.CMD_ANGLE_180)

    def definir_origine(self):
        return self.send_command(self.CMD_DEFINIR_ORIGINE)

    def retour_origine(self):
        return self.send_command(self.CMD_RETOUR_ORIGINE)

    def cycle_90(self):
        return self.send_command(self.CMD_CYCLE_90)


    def get_speed_seconds(self):
        """Retourne la durée théorique d'un cycle de rotation en secondes."""
        speeds = [30, 15, 8]
        return speeds[self.state["speed_index"]]
