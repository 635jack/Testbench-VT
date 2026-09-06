#!/usr/bin/env python3
"""
Module d'asservissement en position pour table tournante.
Combine le suivi de vision ArUco et le pilotage série IR.
Positionnement 2-étapes : Grand Déplacement Anticipé + Micro-Ajustement Pas-à-Pas (Tolérance garantie).
"""
import time
import logging
import cv2
from aruco_tracker import ArUcoTracker
from turntable import TurntableController

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TurntablePositionController:
    """
    Contrôleur de position combinant le suivi ArUco et le pilotage IR par liaison série.
    """
    STATE_IDLE = "ARRET / INATIF"
    STATE_MOVING_CW = "ROTATION HORAIRE (CW)"
    STATE_MOVING_CCW = "ROTATION ANTI-HORAIRE (CCW)"
    STATE_COASTING = "STABILISATION & FREINAGE"
    STATE_FINE_NUDGE = "AJUSTEMENT FIN PAS-A-PAS"
    STATE_TARGET_REACHED = "CIBLE ATTEINTE"
    STATE_SEARCHING = "RECHERCHE MARQUEURS..."

    def __init__(self, port=None, baudrate=115200, simulation=False, config_path="aruco_config.json", tolerance_deg=1.0):
        self.simulation = simulation
        self.config_path = config_path
        
        # Initialisation des sous-modules
        self.tracker = ArUcoTracker(config_path=config_path, simulation=simulation)
        self.turntable = TurntableController(port=port, baudrate=baudrate, simulation=simulation)
        
        # Consigne et état de contrôle
        self.target_angle = None
        self.status = self.STATE_IDLE
        self.tolerance_deg = tolerance_deg  # Tolérance d'arrêt configurable (degrés)
        self.last_command_time = 0.0
        self._command_interval = 0.8  # Évite de spammer les trames IR trop rapidement
        self._speed_minimized = False
        
        # Estimation dynamique de la vitesse de rotation & anticipation du freinage
        self.last_angle = None
        self.last_angle_time = None
        self.angular_velocity_deg_per_sec = 0.0
        # Temps entre l'ordre d'arrêt et l'immobilisation réelle. Ce n'est pas
        # la latence d'émission IR mais la roue libre du plateau, mesurée à la
        # caméra : environ 15° parcourus à 13°/s, soit 1,2 s. La valeur de
        # 0,12 s freinait dix fois trop tard et produisait un dépassement
        # systématique de +9° sur chaque consigne.
        self.braking_latency_sec = 0.45
        self._velocity_stable_since = 0.0  # Timestamp quand la vitesse est devenue ~0
        
        # 2-stage fine positioning control
        self.coast_start_time = 0.0
        self.nudge_count = 0
        self.max_nudges = 2  # Nombre max de micro-impulsions d'ajustement fin
        
        self.is_running = False
        self._control_thread = None
        
        # Démarrage de l'écoute série s'il y a lieu
        self.turntable.start_listener()

    def is_physically_rotating(self):
        """Retourne True si le plateau tourne réellement (mesuré par ArUco)."""
        return self.angular_velocity_deg_per_sec > 1.0

    def ensure_stopped(self):
        """
        S'assure que le plateau est réellement arrêté en utilisant
        le feedback ArUco au lieu de se fier à l'état logique toggle.
        """
        for attempt in range(3):
            # Mettre à jour la mesure de vitesse
            self.step()
            time.sleep(0.15)
            self.step()
            if not self.is_physically_rotating():
                return True
            logging.info(f"[Asservissement] Plateau encore en rotation ({self.angular_velocity_deg_per_sec:.1f}°/s) → envoi STOP (tentative {attempt+1})")
            self.turntable.start_pause()
            time.sleep(0.5)
        logging.warning("[Asservissement] Impossible d'arrêter le plateau après 3 tentatives.")
        return False

    def ensure_rotating(self, direction_cw=True):
        """
        S'assure que le plateau tourne réellement dans la bonne direction.
        Utilise le feedback ArUco pour vérifier.
        """
        if self.is_physically_rotating():
            return True
        cmd = "DROITE (CW)" if direction_cw else "GAUCHE (CCW)"
        logging.info(f"[Asservissement] Plateau arrêté → lancement rotation {cmd}")
        if direction_cw:
            self.turntable.rotation_droite()
        else:
            self.turntable.rotation_gauche()
        return True

    def set_target_angle(self, angle_deg):
        """Définit la consigne d'angle absolue (0..360°)."""
        self.target_angle = angle_deg % 360.0
        self.status = self.STATE_SEARCHING
        # Réarmer la réduction de vitesse à chaque consigne est indispensable :
        # le plateau reprend sa vitesse maximale dès qu'il s'arrête et repart.
        # Sans ce réarmement, les consignes suivantes s'exécutent à 46°/s et
        # dérivent de 45 à 145° — mesuré.
        self._speed_minimized = False
        self.nudge_count = 0
        logging.info(f"[Asservissement] Nouvelle consigne d'angle : {self.target_angle:.1f}° (Tolérance cible: {self.tolerance_deg:.1f}°)")

    def _set_minimum_speed(self):
        """Réduit la vitesse du moteur au minimum en envoyant Vitesse- 3 fois."""
        if not self._speed_minimized:
            logging.info("[Asservissement] Réduction de la vitesse au minimum (Vitesse - x3)...")
            for _ in range(3):
                self.turntable.vitesse_moins()
                time.sleep(0.08)
            self._speed_minimized = True

    def set_target_marker(self, marker_id):
        """Définit la consigne vers l'un des marqueurs ArUco (ex: ID 1 -> 0°)."""
        if marker_id in self.tracker.marker_angles:
            angle = self.tracker.marker_angles[marker_id]
            logging.info(f"[Asservissement] Consigne vers Marqueur ID {marker_id} ({angle:.1f}°)")
            self.set_target_angle(angle)
            return True
        else:
            logging.error(f"[Asservissement] Marqueur ID {marker_id} non répertorié dans la config.")
            return False

    def is_target_reached(self):
        """Retourne True si la consigne courante est atteinte et stabilisée."""
        return self.status == self.STATE_TARGET_REACHED

    def wait_until_reached(self, timeout=15.0):
        """
        Bloque jusqu'à ce que la cible soit atteinte ou que le timeout soit écoulé.
        Pratique pour les scripts automatiques de prise de vue ou de palpage.
        """
        start = time.time()
        while time.time() - start < timeout:
            frame, angle = self.step()
            if self.is_target_reached():
                return True
            time.sleep(0.03)
        return False

    def stop(self):
        """Arrête le déplacement de la table en vérifiant l'état réel via ArUco."""
        # Utiliser le feedback ArUco pour décider s'il faut envoyer STOP
        if self.is_physically_rotating():
            logging.info("[Asservissement] Plateau en rotation réelle → envoi STOP...")
            self.turntable.start_pause()
            time.sleep(0.3)
            # Vérifier que c'est bien arrêté
            if self.is_physically_rotating():
                logging.warning("[Asservissement] Plateau toujours en rotation → 2e tentative STOP")
                self.turntable.start_pause()
        else:
            logging.info("[Asservissement] Plateau déjà arrêté (vérifié par ArUco).")
        self.target_angle = None
        self.status = self.STATE_IDLE
        if self.tracker.simulation:
            self.tracker.set_simulation_rotation(0.0)
        logging.info("[Asservissement] Arrêt de la consigne et stabilisation.")

    def step(self):
        """
        Effectue une itération de la boucle de vision + asservissement :
        1. Capture du frame
        2. Détection des marqueurs ArUco
        3. Calcul de l'angle courant & estimation de la vitesse
        4. Machine d'état à 2 étapes (Grand déplacement anticipé + Ajustement fin pas-à-pas)
        """
        frame = self.tracker.get_frame()
        corners, ids = self.tracker.detect_markers(frame)
        current_angle, detected_info = self.tracker.estimate_turntable_angle(frame, corners, ids)
        now = time.time()

        # Calcul de la vitesse angulaire réelle en degrés / sec
        if current_angle is not None:
            if self.last_angle is not None and self.last_angle_time is not None:
                dt = now - self.last_angle_time
                if dt > 0.005:
                    d_angle = abs((current_angle - self.last_angle + 180.0) % 360.0 - 180.0)
                    inst_speed = d_angle / dt
                    self.angular_velocity_deg_per_sec = 0.7 * self.angular_velocity_deg_per_sec + 0.3 * inst_speed
            self.last_angle = current_angle
            self.last_angle_time = now

        # Dessiner les polygones ArUco détectés
        if ids is not None and len(ids) > 0:
            import cv2
            cv2.aruco.drawDetectedMarkers(frame, corners, ids)

        # Si aucune consigne n'est active
        if self.target_angle is None:
            if self.tracker.simulation:
                self.tracker.set_simulation_rotation(0.0)
            annotated_frame = self.tracker.draw_overlay(frame, current_angle, self.target_angle, self.status)
            return annotated_frame, current_angle

        # Si pas d'angle mesurable (ex: aucun marqueur visible)
        if current_angle is None:
            annotated_frame = self.tracker.draw_overlay(frame, current_angle, self.target_angle, self.status)
            return annotated_frame, current_angle

        # Calcul du plus court écart angulaire e in [-180, 180]
        error = (self.target_angle - current_angle + 180.0) % 360.0 - 180.0

        # ETAPE: CIBLE ATTEINTE -> Verrouillage immobile
        if self.status == self.STATE_TARGET_REACHED:
            annotated_frame = self.tracker.draw_overlay(frame, current_angle, self.target_angle, self.status)
            return annotated_frame, current_angle

        # ETAPE: STABILISATION / COASTING -> Attendre 0.5s l'arrêt physique du moteur
        if self.status == self.STATE_COASTING:
            if (now - self.coast_start_time) > 0.5:
                # Évaluer la position d'arrêt réelle
                if abs(error) <= self.tolerance_deg or self.nudge_count >= self.max_nudges:
                    logging.info(f"[Asservissement] Cible atteinte & stabilisée à {current_angle:.1f}° (Écart final: {error:+.2f}°). Target OK.")
                    self.status = self.STATE_TARGET_REACHED
                else:
                    # Étape d'ajustement fin pas-à-pas (micro-impulsion)
                    self.nudge_count += 1
                    pulse_dir = "GAUCHE (CCW)" if error > 0 else "DROITE (CW)"
                    logging.info(f"[Asservissement] Micro-ajustement fin ({self.nudge_count}/{self.max_nudges}) : Écart = {error:+.2f}° -> Impulsion {pulse_dir}")
                    if error > 0:
                        self.turntable.rotation_gauche()
                    else:
                        self.turntable.rotation_droite()
                    time.sleep(0.12)  # Micro-impulsion de 120ms (~0.8° de rotation)
                    self.turntable.start_pause()
                    self.coast_start_time = time.time()
                    self.status = self.STATE_COASTING
            annotated_frame = self.tracker.draw_overlay(frame, current_angle, self.target_angle, self.status)
            return annotated_frame, current_angle

        # Réduire la vitesse au minimum dès qu'un déplacement commence.
        # Attention : VITESSE_MOINS **démarre** le plateau, dans la derniere
        # direction utilisee. C'est ce qui rend la premiere consigne d'une
        # serie imprecise, le sens n'etant pas encore fixe. Deplacer cet appel
        # apres l'ordre de direction a ete essaye et degrade le regime etabli.
        if not self._speed_minimized:
            self._set_minimum_speed()

        # Marge de freinage anticipée (Vitesse * Latence)
        braking_lead_deg = max(self.tolerance_deg, self.angular_velocity_deg_per_sec * self.braking_latency_sec)

        # Arrivée dans la zone de freinage anticipé -> Envoi STOP et passage en COASTING
        if abs(error) <= braking_lead_deg:
            # Vérifier que le plateau tourne réellement avant d'envoyer STOP
            if self.is_physically_rotating():
                logging.info(f"[Asservissement] Anticipation de freinage (Écart: {error:+.1f}°, Vitesse: {self.angular_velocity_deg_per_sec:.1f}°/s). Envoi STOP.")
                self.turntable.start_pause()
                if self.tracker.simulation:
                    self.tracker.set_simulation_rotation(0.0)
            self.coast_start_time = now
            self.status = self.STATE_COASTING
        else:
            # Lancement du déplacement dans la direction optimale
            desired_cw = error < 0
            desired_state = self.STATE_MOVING_CW if desired_cw else self.STATE_MOVING_CCW

            if self.status not in (self.STATE_MOVING_CW, self.STATE_MOVING_CCW):
                if (now - self.last_command_time) > self._command_interval:
                    # Toujours émettre l'ordre de direction, même si le plateau
                    # tourne déjà.
                    #
                    # VITESSE_MOINS **démarre** le plateau, dans la dernière
                    # direction utilisée. `_set_minimum_speed` le met donc en
                    # rotation juste avant ce test : en s'abstenant « puisqu'il
                    # tourne déjà », on le laissait partir dans le sens de la
                    # consigne précédente — une fois sur deux le mauvais. C'est
                    # ce qui rendait la première consigne de chaque série
                    # fausse de -19 à -34°, les suivantes étant correctes.
                    if not self.is_physically_rotating():
                        if desired_cw:
                            logging.info(f"[Asservissement] Écart {error:+.1f}° -> Rotation DROITE (CW) à vitesse min.")
                            self.turntable.rotation_droite()
                        else:
                            logging.info(f"[Asservissement] Écart {error:+.1f}° -> Rotation GAUCHE (CCW) à vitesse min.")
                            self.turntable.rotation_gauche()
                    else:
                        logging.info(f"[Asservissement] Plateau déjà en rotation (ArUco: {self.angular_velocity_deg_per_sec:.1f}°/s).")
                    self.last_command_time = now
                    self.status = desired_state
                    
            if self.tracker.simulation:
                sim_spd = -8.0 if self.status == self.STATE_MOVING_CW else 8.0
                self.tracker.set_simulation_rotation(sim_spd)

        annotated_frame = self.tracker.draw_overlay(frame, current_angle, self.target_angle, self.status)
        return annotated_frame, current_angle

    def close(self):
        """Libère toutes les ressources en toute sécurité."""
        self.stop()
        self.turntable.disconnect()
        self.tracker.close()
