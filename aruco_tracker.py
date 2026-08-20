#!/usr/bin/env python3
"""
Module de détection ArUco et d'estimation d'angle pour la table tournante.
Supporte RealSense D405 (pyrealsense2), Webcam OpenCV et Mode Simulation.

Extension visuo-tactile : flux RGBD (couleur + profondeur), extraction
d'intrinsics caméra, et estimation de pose 3D pour le marqueur du pouce (ID 7).
"""
import math
import time
import json
import os
import logging
from collections import deque
import numpy as np
import cv2


# Import facultatif de pyrealsense2
try:
    import pyrealsense2 as rs
except ImportError:
    rs = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Dictionnaires ArUco OpenCV
ARUCO_DICTS = {
    "DICT_4X4_50": cv2.aruco.DICT_4X4_50,
    "DICT_4X4_100": cv2.aruco.DICT_4X4_100,
    "DICT_5X5_50": cv2.aruco.DICT_5X5_50,
    "DICT_6X6_250": cv2.aruco.DICT_6X6_250,
    "DICT_ARUCO_ORIGINAL": cv2.aruco.DICT_ARUCO_ORIGINAL,
}

class CameraUnavailableError(RuntimeError):
    """Aucune camera physique accessible, et la simulation n'a pas ete demandee."""


class ArUcoTracker:
    """
    Gestionnaire de flux vidéo et d'estimation d'angle par marqueurs ArUco.
    """

    #: Nombre d'images sans detection tolerees avant de rendre None. A 30 fps,
    #: 10 images font un tiers de seconde : assez pour traverser un reflet,
    #: trop court pour asservir sur du vent.
    MAX_STALE_FRAMES = 10

    #: Mode de capture : largeur, hauteur, images par seconde. Le rendre
    #: explicite n'est pas cosmetique — les modes de la D405 n'ont pas le meme
    #: champ de vision (78,6 deg en 640x480 contre 88,6 en 1280x720), donc ni
    #: les intrinseques ni le centre du plateau ne se transposent de l'un a
    #: l'autre. En 1280x720 la profondeur retombe par ailleurs a 5 ou 15 fps
    #: derriere la redirection USB de la VM.
    DEFAULT_MODE = (640, 480, 30)

    def __init__(self, config_path="aruco_config.json", simulation=False, camera_id=0,
                 exposure="default", allow_fallback=False):
        self.config_path = config_path
        self.simulation = simulation
        #: Consentir a des images de synthese quand aucune camera n'est
        #: joignable. Faux par defaut : voir _init_camera.
        self.allow_fallback = allow_fallback or simulation
        self.camera_id = camera_id
        self.exposure = self.DEFAULT_EXPOSURE if exposure == "default" else exposure
        
        self.dict_name = "DICT_4X4_50"
        self.marker_angles = {
            1: 0.0,
            2: 60.0,
            3: 120.0,
            4: 180.0,
            5: 240.0,
            6: 300.0
        }
        self.mode = self.DEFAULT_MODE
        self.turntable_center = None # (cx, cy)
        self.smooth_angle = None
        #: Images consecutives sans aucun marqueur decode. Au-dela de
        #: MAX_STALE_FRAMES, l'angle est declare inconnu plutot que repete.
        self._frames_without_detection = 0
        self._angle_history = deque(maxlen=5) # Buffer pour le filtre médian glissant

        self.source_name = "Initialisation..."
        self.invert_colors = True
        self.adaptive_thresh_constant = 11
        self.adaptive_thresh_winsize_min = 5
        
        # RealSense / OpenCV setup
        self.rs_pipeline = None
        self.rs_config = None
        self.rs_align = None  # Alignement depth → color
        self.rs_depth_scale = 1.0  # Facteur d'échelle depth (m)
        self.cap = None

        # Intrinsics caméra (remplies après init RealSense)
        self._camera_intrinsics = None
        
        # Simu state
        self._sim_angle = 0.0
        self._sim_last_time = time.time()
        
        self.load_config()
        if self.simulation:
            self.invert_colors = False
        self._setup_detector()
        self._init_camera()


    def load_config(self):
        """Charge la configuration depuis aruco_config.json."""
        cfg = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                self.dict_name = cfg.get("dictionary", "DICT_4X4_50")
                m_angles = cfg.get("marker_angles", {})
                self.marker_angles = {int(k): float(v) for k, v in m_angles.items()}
                if cfg.get("turntable_center"):
                    self.turntable_center = tuple(cfg["turntable_center"])
                self.invert_colors = cfg.get("invert_colors", True)
                self.adaptive_thresh_constant = cfg.get("adaptive_thresh_constant", 11)
                self.adaptive_thresh_winsize_min = cfg.get("adaptive_thresh_winsize_min", 5)
                logging.info(f"Configuration ArUco chargée depuis {self.config_path}")
            except Exception as e:
                logging.error(f"Erreur chargement aruco_config.json: {e}")

        # Un centre calibre dans un autre mode est pire qu'un centre absent :
        # il donne des angles faux sans que rien ne le signale.
        res = cfg.get("calibrated_resolution")
        if res and tuple(res) != (self.mode[0], self.mode[1]):
            logging.warning(
                "[Config] Le centre du plateau a ete calibre en %sx%s, or on "
                "travaille en %sx%s : les angles seront faux. Relancer "
                "calibrate_center.py.", res[0], res[1], self.mode[0], self.mode[1])

    def save_config(self):
        """Sauvegarde la configuration (ex: centre calibré)."""
        try:
            cfg = {}
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            cfg["dictionary"] = self.dict_name
            cfg["marker_angles"] = {str(k): v for k, v in self.marker_angles.items()}
            cfg["turntable_center"] = list(self.turntable_center) if self.turntable_center else None
            # Le centre est en pixels : il ne vaut que pour la resolution ou il
            # a ete estime. L'ecrire a cote evite de reutiliser en silence un
            # centre calibre dans un autre mode — les modes de la D405 n'ont
            # pas le meme champ de vision.
            cfg["calibrated_resolution"] = [self.mode[0], self.mode[1]]
            cfg["invert_colors"] = self.invert_colors
            cfg["adaptive_thresh_constant"] = self.adaptive_thresh_constant
            cfg["adaptive_thresh_winsize_min"] = self.adaptive_thresh_winsize_min
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            logging.info("Configuration ArUco mise à jour.")
        except Exception as e:
            logging.error(f"Erreur sauvegarde config ArUco: {e}")

    def _setup_detector(self):
        """Initialise le détecteur ArUco OpenCV."""
        dict_id = ARUCO_DICTS.get(self.dict_name, cv2.aruco.DICT_4X4_50)
        self.aruco_dict = cv2.aruco.getPredefinedDictionary(dict_id)
        
        # Traitement compatibilité versions OpenCV
        if hasattr(cv2.aruco, 'DetectorParameters'):
            self.detector_params = cv2.aruco.DetectorParameters()
        else:
            self.detector_params = cv2.aruco.DetectorParameters_create()
            
        self.detector_params.adaptiveThreshWinSizeMin = 3
        self.detector_params.adaptiveThreshWinSizeMax = 31
        self.detector_params.adaptiveThreshWinSizeStep = 4
        self.detector_params.adaptiveThreshConstant = 7
        self.detector_params.minMarkerPerimeterRate = 0.015
        self.detector_params.perspectiveRemovePixelPerCell = 8
        self.detector_params.perspectiveRemoveIgnoredMarginPerCell = 0.13
        if hasattr(cv2.aruco, 'CORNER_REFINE_SUBPIX'):
            self.detector_params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX

            
        if hasattr(cv2.aruco, 'ArucoDetector'):
            self.detector = cv2.aruco.ArucoDetector(self.aruco_dict, self.detector_params)
        else:
            self.detector = None



    def _init_camera(self):
        """Initialise la source vidéo (RealSense D405 -> Webcam -> Simulation)."""
        if self.simulation:
            self.source_name = "Simulation synthétique"
            logging.info("[Camera] Mode simulation actif.")
            return

        # 1. Essai RealSense D405 via pyrealsense2
        if rs is not None:
            try:
                ctx = rs.context()
                devices = ctx.query_devices()
                if len(devices) > 0:
                    dev_name = devices[0].get_info(rs.camera_info.name)
                    logging.info(f"[Camera] Dispositif RealSense détecté : {dev_name}")
                    self.rs_pipeline = rs.pipeline()
                    self.rs_config = rs.config()
                    self.rs_config.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
                    self.rs_config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
                    profile = self.rs_pipeline.start(self.rs_config)

                    # Préparer l'alignement depth → repère couleur
                    self.rs_align = rs.align(rs.stream.color)

                    # Extraire le facteur d'échelle de profondeur
                    depth_sensor = profile.get_device().first_depth_sensor()
                    self.rs_depth_scale = depth_sensor.get_depth_scale()
                    logging.info(f"[Camera] Depth scale = {self.rs_depth_scale:.6f} m/unit")

                    # Extraire les intrinsics de la caméra couleur
                    color_profile = profile.get_stream(rs.stream.color)
                    intrinsics = color_profile.as_video_stream_profile().get_intrinsics()
                    self._camera_intrinsics = {
                        "fx": intrinsics.fx, "fy": intrinsics.fy,
                        "cx": intrinsics.ppx, "cy": intrinsics.ppy,
                        "width": intrinsics.width, "height": intrinsics.height,
                        "model": str(intrinsics.model),
                        "coeffs": list(intrinsics.coeffs),
                        "depth_scale": self.rs_depth_scale,
                    }
                    logging.info(f"[Camera] Intrinsics: fx={intrinsics.fx:.1f} fy={intrinsics.fy:.1f} cx={intrinsics.ppx:.1f} cy={intrinsics.ppy:.1f}")

                    # Récupérer les capteurs pour contrôler l'exposition et la balance des blancs
                    self.rs_device = profile.get_device()
                    self.lock_exposure_and_white_balance(warmup_sec=1.5)

                    self.source_name = f"RealSense D405 RGBD ({dev_name})"
                    logging.info("[Camera] Flux RealSense D405 RGBD démarré avec succès (couleur + profondeur).")
                    return
            except Exception as e:
                logging.warning(f"[Camera] Erreur d'ouverture RealSense D405 : {e}")
                self.rs_pipeline = None

        # 2. Fallback Webcam OpenCV
        try:
            self.cap = cv2.VideoCapture(self.camera_id)
            if self.cap.isOpened():
                self.source_name = f"Webcam OpenCV (Index {self.camera_id})"
                logging.info(f"[Camera] Webcam OpenCV ouverte sur l'index {self.camera_id}.")
                return
        except Exception as e:
            logging.warning(f"[Camera] Erreur webcam : {e}")

        # 3. Aucune caméra physique.
        #
        # Basculer silencieusement en images de synthèse est le pire des
        # comportements : tout continue de fonctionner, les mesures ont l'air
        # normales, et on enregistre un jeu de données de synthèse en croyant
        # filmer le banc. C'est arrivé — cinq des six sessions de juillet sont
        # dans ce cas. On n'y consent donc que si la simulation a été demandée.
        if not self.allow_fallback:
            raise CameraUnavailableError(
                "Aucune caméra physique accessible : ni RealSense, ni webcam. "
                "Vérifier que la D405 est bien redirigée vers la VM. Pour "
                "travailler sans matériel, demander explicitement "
                "simulation=True."
            )
        self.simulation = True
        self.source_name = "Simulation synthétique (Fallback)"
        logging.warning("[Camera] Aucune caméra physique accessible. Bascule en mode simulation.")

    def get_frame(self):
        """Récupère l'image couleur suivante du flux actif (rétrocompatible)."""
        color, _ = self.get_frames()
        return color

    def get_frames(self):
        """Récupère (color, depth). Rétrocompatible."""
        color, depth, _, _ = self.get_all_frames()
        return color, depth

    def get_all_frames(self):
        """
        Récupère toutes les images du flux actif.

        Returns:
            (color_img, depth_img, left_img, right_img)
            left_img et right_img sont les images individuelles des objectifs Gauche et Droit (ou None).
        """
        if self.simulation:
            return self._generate_simulated_frame(), None, None, None

        if self.rs_pipeline is not None:
            try:
                frames = self.rs_pipeline.wait_for_frames(timeout_ms=1000)
                
                # Extraire les images des objectifs bruts Gauche (IR1) et Droit (IR2) avant alignement
                ir1_frame = frames.get_infrared_frame(1)
                ir2_frame = frames.get_infrared_frame(2)
                left_img = np.asanyarray(ir1_frame.get_data()) if ir1_frame else None
                right_img = np.asanyarray(ir2_frame.get_data()) if ir2_frame else None

                # Aligner la profondeur sur le repère couleur
                if self.rs_align is not None:
                    frames = self.rs_align.process(frames)
                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()
                color_img = np.asanyarray(color_frame.get_data()) if color_frame else None
                depth_img = np.asanyarray(depth_frame.get_data()) if depth_frame else None

                if color_img is not None:
                    return color_img, depth_img, left_img, right_img
            except Exception as e:
                logging.error(f"Erreur lecture RealSense: {e}")

        if self.cap is not None and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                return frame, None, None, None

        return self._generate_simulated_frame(), None, None, None

    #: Exposition imposée, en microsecondes. L'automatisme se règle sur
    #: l'objet clair posé sur le plateau et **surexpose les marqueurs** : le
    #: PLA blanc sature et noie le motif noir. Mesuré sur le banc, en
    #: 640x480 : aucun marqueur détecté en automatique, deux à partir d'une
    #: exposition figée entre 1500 et 6000. Mettre à ``None`` pour retrouver
    #: le comportement automatique.
    DEFAULT_EXPOSURE = 4000

    def lock_exposure_and_white_balance(self, warmup_sec=1.5):
        """
        Fige l'exposition et la balance des blancs pour toute la session.

        Si ``self.exposure`` est défini, cette valeur est imposée au lieu de
        celle trouvée par l'automatisme — sans quoi la détection ArUco échoue
        dès qu'un objet clair occupe le champ.
        """
        if self.rs_pipeline is None or rs is None:
            return

        try:
            logging.info(f"[Camera] Préchauffage et ajustement automatique ({warmup_sec}s)...")
            t0 = time.time()
            while time.time() - t0 < warmup_sec:
                self.rs_pipeline.wait_for_frames(timeout_ms=2000)
        except Exception as e:
            logging.warning(f"[Camera] Préchauffage incomplet : {e}")

        try:
            # Parcourir les capteurs (Depth & Color) pour désactiver les modes auto
            for sensor in self.rs_device.query_sensors():
                if self.exposure is not None and sensor.supports(rs.option.exposure):
                    sensor.set_option(rs.option.enable_auto_exposure, 0)
                    sensor.set_option(rs.option.exposure, float(self.exposure))
                    logging.info(f"[Camera] Exposition imposée à {self.exposure} sur "
                                 f"{sensor.get_info(rs.camera_info.name)}")
                    continue
                if sensor.supports(rs.option.enable_auto_exposure):
                    sensor.set_option(rs.option.enable_auto_exposure, 0)
                    logging.info(f"[Camera] Auto-exposition désactivée et figée sur {sensor.get_info(rs.camera_info.name)}")

                if sensor.supports(rs.option.enable_auto_white_balance):
                    sensor.set_option(rs.option.enable_auto_white_balance, 0)
                    logging.info(f"[Camera] Auto-white-balance désactivée et figée sur {sensor.get_info(rs.camera_info.name)}")
        except Exception as e:
            logging.warning(f"[Camera] Impossible de verrouiller l'exposition / balance des blancs : {e}")

    def get_camera_intrinsics(self):
        """
        Retourne les intrinsics de la caméra couleur sous forme de dictionnaire.
        Disponible uniquement après l'initialisation d'une RealSense.

        Returns:
            dict avec fx, fy, cx, cy, width, height, model, coeffs, depth_scale
            ou None si non disponible.
        """
        return self._camera_intrinsics

    def get_camera_matrix_and_dist(self):
        """
        Retourne la matrice caméra (3×3) et les coefficients de distorsion
        au format OpenCV, pour utilisation avec estimatePoseSingleMarkers etc.

        Returns:
            (camera_matrix, dist_coeffs) : tuple de np.ndarray, ou (None, None).
        """
        if self._camera_intrinsics is None:
            return None, None
        intr = self._camera_intrinsics
        camera_matrix = np.array([
            [intr["fx"], 0, intr["cx"]],
            [0, intr["fy"], intr["cy"]],
            [0, 0, 1]
        ], dtype=np.float64)
        dist_coeffs = np.array(intr["coeffs"], dtype=np.float64)
        return camera_matrix, dist_coeffs

    def estimate_marker_pose_3d(self, corners, ids, marker_size_m, target_ids=None):
        """
        Estime la pose 3D des marqueurs détectés via solvePnP.

        Args:
            corners: coins détectés (sortie de detect_markers)
            ids: IDs détectés (sortie de detect_markers)
            marker_size_m: taille physique du côté du marqueur en mètres
            target_ids: set/list d'IDs à traiter (None = tous)

        Returns:
            dict {marker_id: {"rvec": [...], "tvec": [...], "corners": [[x,y]×4]}}
            Les vecteurs rvec et tvec sont en coordonnées caméra (mètres + radians).
        """
        camera_matrix, dist_coeffs = self.get_camera_matrix_and_dist()
        if camera_matrix is None or ids is None or len(ids) == 0:
            return {}

        results = {}
        # Points 3D du marqueur dans son repère local (z=0, centré)
        half = marker_size_m / 2.0
        obj_points = np.array([
            [-half,  half, 0],
            [ half,  half, 0],
            [ half, -half, 0],
            [-half, -half, 0],
        ], dtype=np.float64)

        for i, m_id_arr in enumerate(ids):
            m_id = int(np.ravel(m_id_arr)[0])
            if target_ids is not None and m_id not in target_ids:
                continue
            img_points = corners[i][0].astype(np.float64)
            success, rvec, tvec = cv2.solvePnP(
                obj_points, img_points, camera_matrix, dist_coeffs,
                flags=cv2.SOLVEPNP_IPPE_SQUARE
            )
            if success:
                results[m_id] = {
                    "rvec": rvec.flatten().tolist(),
                    "tvec": tvec.flatten().tolist(),
                    "corners": img_points.tolist(),
                }
        return results

    def set_simulation_rotation(self, speed_deg_per_sec):
        """Simule la rotation de la table en mode simulation."""
        now = time.time()
        dt = now - self._sim_last_time
        self._sim_last_time = now
        self._sim_angle = (self._sim_angle + speed_deg_per_sec * dt) % 360.0

    def _generate_simulated_frame(self):
        """Génère une image 1280x720 synthétique d'un plateau tournant avec marqueurs ArUco."""
        h, w = 720, 1280
        frame = np.ones((h, w, 3), dtype=np.uint8) * 40 # Fond gris foncé
        cx, cy = w // 2, h // 2
        radius = 260
        
        # Dessiner le plateau tournant
        cv2.circle(frame, (cx, cy), radius + 30, (70, 70, 70), -1)
        cv2.circle(frame, (cx, cy), radius, (100, 100, 100), -1)
        cv2.circle(frame, (cx, cy), 15, (180, 180, 180), -1)
        
        # Générer et placer virtuellement les 6 marqueurs ArUco
        for marker_id, angle_ref in self.marker_angles.items():
            current_m_angle_deg = (angle_ref + self._sim_angle) % 360.0
            rad = math.radians(current_m_angle_deg)
            
            # Position du centre du marqueur sur le cercle du plateau
            mx = cx + radius * math.cos(rad)
            my = cy - radius * math.sin(rad) # Y inversé écran
            
            # Générer l'image du marqueur ArUco
            size = 60
            if hasattr(cv2.aruco, 'generateImageMarker'):
                m_img = cv2.aruco.generateImageMarker(self.aruco_dict, marker_id, size)
            else:
                m_img = cv2.aruco.drawMarker(self.aruco_dict, marker_id, size)
            m_img_bgr = cv2.cvtColor(m_img, cv2.COLOR_GRAY2BGR)
            
            # Incruster le marqueur dans l'image synthétique
            x1, y1 = int(mx - size // 2), int(my - size // 2)
            x2, y2 = x1 + size, y1 + size
            
            if 0 <= x1 < w and 0 <= x2 <= w and 0 <= y1 < h and 0 <= y2 <= h:
                frame[y1:y2, x1:x2] = m_img_bgr
                
        return frame

    def detect_markers(self, frame):
        """Détecte les marqueurs ArUco dans l'image."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.invert_colors:
            gray = cv2.bitwise_not(gray)
        if self.detector is not None:
            corners, ids, rejected = self.detector.detectMarkers(gray)
        else:
            corners, ids, rejected = cv2.aruco.detectMarkers(gray, self.aruco_dict, parameters=self.detector_params)
        return corners, ids

    def auto_estimate_center(self, corners, ids):
        """
        Estime automatiquement le centre de la table (cx, cy) à partir des marqueurs détectés.
        """
        if ids is None or len(ids) == 0:
            return None

        m_positions = {}
        for i, m_id_arr in enumerate(ids):
            m_id = int(np.ravel(m_id_arr)[0])
            pts = corners[i][0]
            mx = float(np.mean(pts[:, 0]))
            my = float(np.mean(pts[:, 1]))
            m_positions[m_id] = (mx, my)

        # 1. Chercher des paires de marqueurs opposés (~180°)
        center_estimates = []
        detected_ids = list(m_positions.keys())
        for idx_a in range(len(detected_ids)):
            id_a = detected_ids[idx_a]
            if id_a not in self.marker_angles:
                continue
            angle_a = self.marker_angles[id_a]
            for idx_b in range(idx_a + 1, len(detected_ids)):
                id_b = detected_ids[idx_b]
                if id_b not in self.marker_angles:
                    continue
                angle_b = self.marker_angles[id_b]
                diff_deg = abs((angle_b - angle_a + 180.0) % 360.0 - 180.0)
                if abs(diff_deg - 180.0) < 15.0: # Paires opposées (~180°)
                    xa, ya = m_positions[id_a]
                    xb, yb = m_positions[id_b]
                    center_estimates.append(((xa + xb) / 2.0, (ya + yb) / 2.0))

        if center_estimates:
            avg_cx = int(np.mean([c[0] for c in center_estimates]))
            avg_cy = int(np.mean([c[1] for c in center_estimates]))
            return (avg_cx, avg_cy)

        # 2. Si au moins 3 marqueurs -> Kåsa Circle Fit
        if len(m_positions) >= 3:
            pts = np.array(list(m_positions.values()))
            x = pts[:, 0]
            y = pts[:, 1]
            A = np.column_stack((x, y, np.ones_like(x)))
            b = x**2 + y**2
            try:
                c_fit, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
                cx = int(c_fit[0] / 2.0)
                cy = int(c_fit[1] / 2.0)
                return (cx, cy)
            except Exception:
                pass

        return None

    def estimate_turntable_angle(self, frame, corners, ids):
        """
        Calcule l'angle absolu courant de la table tournante theta in [0, 360[.
        Utilise une fusion multi-marqueurs pondérée, le filtrage des valeurs aberrantes (outliers),
        un filtre médian glissant et un lissage EMA circulaire.
        """
        h, w = frame.shape[:2]

        # Estimation automatique du centre si non défini
        if self.turntable_center is None and ids is not None and len(ids) > 0:
            auto_c = self.auto_estimate_center(corners, ids)
            if auto_c is not None:
                self.turntable_center = auto_c
                self.save_config()
                logging.info(f"[Auto-Center] Centre du plateau automatiquement estimé à {self.turntable_center}")

        if self.turntable_center is None:
            self.turntable_center = (w // 2, h // 2)
            
        cx, cy = self.turntable_center
        
        if ids is None or len(ids) == 0:
            # Ne jamais faire passer une valeur perimee pour une mesure. On
            # tolere quelques images sans detection, le temps d'un reflet ou
            # d'une occultation passagere, puis on avoue ne plus savoir :
            # asservir sur un angle fige revient a croire converger alors
            # qu'on ne mesure plus rien.
            self._frames_without_detection += 1
            if self._frames_without_detection > self.MAX_STALE_FRAMES:
                return None, None
            return self.smooth_angle, None

        self._frames_without_detection = 0
            
        candidates = []
        detected_info = []

        for i, m_id_arr in enumerate(ids):
            m_id = int(np.ravel(m_id_arr)[0])
            if m_id not in self.marker_angles:
                continue
                
            pts = corners[i][0] # 4 coins du marqueur
            mx = float(np.mean(pts[:, 0]))
            my = float(np.mean(pts[:, 1]))
            
            # Poids basé sur le périmètre du marqueur (les marqueurs mieux vus ont un poids plus fort)
            weight = float(cv2.arcLength(pts, True))
            
            # Angle apparent du marqueur dans le repère image (0° = Droite, 90° = Haut)
            # Y écran est vers le bas -> - (my - cy)
            dx = mx - cx
            dy = -(my - cy)
            beta_deg = math.degrees(math.atan2(dy, dx)) % 360.0
            
            # Angle théorique de ce marqueur sur le plateau
            alpha_ref = self.marker_angles[m_id]
            
            # Angle du plateau : theta = beta - alpha_ref
            theta_deg = (beta_deg - alpha_ref) % 360.0
            
            candidates.append((theta_deg, weight))
            
            detected_info.append({
                "id": m_id,
                "center": (mx, my),
                "beta_deg": beta_deg,
                "theta_deg": theta_deg,
                "dist": math.hypot(dx, dy)
            })

        if not candidates:
            return self.smooth_angle, None

        # 1. Élimination des valeurs aberrantes (Outlier Rejection si >= 3 marqueurs)
        if len(candidates) >= 3:
            # Médiane circulaire simple
            angles_rad = [math.radians(c[0]) for c in candidates]
            med_sin = np.median([math.sin(r) for r in angles_rad])
            med_cos = np.median([math.cos(r) for r in angles_rad])
            med_angle_deg = math.degrees(math.atan2(med_sin, med_cos)) % 360.0
            
            inliers = []
            for theta_deg, weight in candidates:
                err = abs((theta_deg - med_angle_deg + 180.0) % 360.0 - 180.0)
                if err <= 5.0: # Rejeter si écart > 5° avec la médiane des marqueurs
                    inliers.append((theta_deg, weight))
            if inliers:
                candidates = inliers

        # 2. Fusion pondérée (Moyenne circulaire avec poids du périmètre)
        sin_sum = sum(w * math.sin(math.radians(t)) for t, w in candidates)
        cos_sum = sum(w * math.cos(math.radians(t)) for t, w in candidates)
        raw_theta_deg = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0

        # 3. Filtre médian glissant sur l'historique temporel (taille 5)
        self._angle_history.append(raw_theta_deg)
        hist_rad = [math.radians(a) for a in self._angle_history]
        hist_med_sin = float(np.median([math.sin(r) for r in hist_rad]))
        hist_med_cos = float(np.median([math.cos(r) for r in hist_rad]))
        med_filtered_deg = math.degrees(math.atan2(hist_med_sin, hist_med_cos)) % 360.0

        # 4. Lissage temporel EMA (Exponential Moving Average circulaire)
        if self.smooth_angle is None:
            self.smooth_angle = med_filtered_deg
        else:
            diff = (med_filtered_deg - self.smooth_angle + 180.0) % 360.0 - 180.0
            # Facteur d'amortissement ultra-stable (alpha = 0.25)
            self.smooth_angle = (self.smooth_angle + 0.25 * diff) % 360.0

        return self.smooth_angle, detected_info


    def draw_overlay(self, frame, current_angle, target_angle=None, status="IDLE"):
        """Incruste les éléments graphiques sur l'image OpenCV."""
        h, w = frame.shape[:2]
        # Le centre peut être en flottants — la calibration par trajectoire
        # l'estime au dixième de pixel — alors que le dessin exige des entiers.
        cx, cy = self.turntable_center if self.turntable_center else (w // 2, h // 2)
        cx, cy = int(round(cx)), int(round(cy))
        
        # Calcul du rayon moyen basé sur le centre réel
        radius = 180
        
        # 1. Centre de la table & cercle repère
        cv2.circle(frame, (cx, cy), 6, (0, 255, 255), -1)
        cv2.circle(frame, (cx, cy), radius, (255, 255, 0), 2, cv2.LINE_AA)
        
        # Axe 0° Référence (vers la droite)
        cv2.line(frame, (cx, cy), (cx + radius + 20, cy), (100, 100, 100), 1, cv2.LINE_AA)
        cv2.putText(frame, "0 deg (Ref)", (cx + radius + 25, cy + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

        # 2. Vecteur Angle Courant (Vert)
        if current_angle is not None:
            rad_curr = math.radians(current_angle)
            vx_curr = int(cx + (radius - 10) * math.cos(rad_curr))
            vy_curr = int(cy - (radius - 10) * math.sin(rad_curr))
            cv2.line(frame, (cx, cy), (vx_curr, vy_curr), (0, 255, 0), 3, cv2.LINE_AA)
            cv2.circle(frame, (vx_curr, vy_curr), 8, (0, 255, 0), -1)

        # 3. Vecteur Angle Cible (Rouge / Orange)
        if target_angle is not None:
            rad_tgt = math.radians(target_angle)
            vx_tgt = int(cx + (radius - 10) * math.cos(rad_tgt))
            vy_tgt = int(cy - (radius - 10) * math.sin(rad_tgt))
            cv2.line(frame, (cx, cy), (vx_tgt, vy_tgt), (0, 0, 255), 2, cv2.LINE_AA)
            cv2.circle(frame, (vx_tgt, vy_tgt), 6, (0, 0, 255), -1)

        # 4. Panneau d'informations en haut à gauche
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (420, 160), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
        cv2.rectangle(frame, (10, 10), (420, 160), (0, 255, 255), 1)

        cv2.putText(frame, f"Source: {self.source_name}", (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        angle_str = f"{current_angle:.1f} deg" if current_angle is not None else "NON DETECTE"
        cv2.putText(frame, f"Angle Courant : {angle_str}", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        
        tgt_str = f"{target_angle:.1f} deg" if target_angle is not None else "--"
        cv2.putText(frame, f"Angle Cible   : {tgt_str}", (20, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)

        if current_angle is not None and target_angle is not None:
            err = (target_angle - current_angle + 180.0) % 360.0 - 180.0
            cv2.putText(frame, f"Ecart Angulaire: {err:+.1f} deg", (20, 116), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

        cv2.putText(frame, f"Etat: {status}", (20, 144), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

        return frame


    def close(self):
        """Ferme proprement la caméra."""
        if self.rs_pipeline:
            try:
                self.rs_pipeline.stop()
            except Exception:
                pass
        if self.cap:
            self.cap.release()
