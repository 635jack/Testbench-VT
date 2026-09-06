#!/usr/bin/env python3
"""
fake.py — le banc simulé, avec ses défauts.

Un simulateur qui ne reproduit que le cas nominal ne prouve rien : il valide un
programme contre un matériel qui n'existe pas. Celui-ci rejoue les pièges qui
ont réellement coûté des heures sur ce banc, chacun mesuré et documenté :

* la main **n'émet rien** tant qu'elle n'a pas reçu ``set_enable`` *puis*
  ``home_motors`` — trames aux en-têtes corrects et charge utile nulle ;
* le homing du **moteur 1** ne se termine jamais, et il n'exécute aucune
  consigne (panne d'actionneur confirmée le 2026-07-27) ;
* **``move_motors`` n'est pas toujours pris** : consigne acceptée, relue
  correctement, moteur immobile. Un seul envoi rate une fois sur deux ;
* la **position est signée** dans la trame brute : un doigt repoussé sous son
  zéro rend 63993 pour −1543 ;
* **``START_PAUSE`` est une bascule** : une de trop relance le plateau ;
* **``VITESSE_MOINS`` démarre le plateau** dans le dernier sens utilisé ;
* la lampe met **plus de deux secondes** à s'établir, et le firmware démarre
  à PWM 255 ;
* les marqueurs ArUco ne se décodent qu'**une fois sur deux** environ ;
* **``ring.pad`` est mort**, et la paume ne répond que sur 14 points sur 26.

Tout est déterministe pour une graine donnée : deux exécutions du même test
donnent la même chose, y compris les échecs intermittents.
"""
from __future__ import annotations

import math
import random
import threading
import time

import numpy as np

from .. import config

config.install_paths()

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile import tpdo  # noqa: E402

# ── Plateau et lampe simulés ──────────────────────────────────────────────────


class FakeDimmer:
    """
    L'ESP32 du banc : variateur PWM et pont infrarouge sur le même port.

    Expose la surface de ``vt_light.dimmer.Dimmer`` — ``connect``, ``set_pwm``,
    ``send_ir``, ``pwm``, ``port`` — plus l'état du plateau, que le vrai
    matériel ne rend pas mais que le simulateur doit bien tenir quelque part.
    """

    #: Vitesses mesurées sur le banc, en degrés par seconde.
    VITESSE_MAX = 46.0
    VITESSE_MIN = 13.0
    #: Roue libre après l'ordre d'arrêt. Contrainte dure : toute impulsion,
    #: même de 120 ms, déplace d'au moins autant.
    ROUE_LIBRE_DEG = 15.0

    def __init__(self, port: str = "/dev/ttyACM0-simule", angle_initial: float = 0.0,
                 seed: int = 0, settle_s: float = 3.4):
        self.port = port
        #: Temps d'établissement de la lampe. La valeur par défaut est celle du
        #: banc ; les tests la mettent à zéro pour ne pas attendre 3,5 s par
        #: changement de niveau. Le mettre à zéro **en acquisition** rendrait le
        #: simulateur complaisant : c'est précisément ce délai qui a produit des
        #: tableaux entiers de mesures fausses le 2026-08-20.
        self.settle_s = float(settle_s)
        #: Le firmware démarre à ``ledcWrite(255)`` : la lampe est à pleine
        #: puissance dès la mise sous tension, avant toute commande.
        self._pwm = 255
        self._pwm_cible = 255
        self._t_consigne = time.perf_counter()
        self.connected = False
        self.ir_envoyes: list = []
        #: ``IRviaDimmer`` recopie ce champ. Le vrai ``Dimmer`` y met son objet
        #: ``serial.Serial`` ; ici il n'y en a pas, et personne ne s'en sert.
        self.ser = None

        self.angle = float(angle_initial) % 360.0
        self.tourne = False
        self.horaire = True
        self.vitesse = self.VITESSE_MAX
        self._t = time.perf_counter()
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.connected = False

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ── Lampe ─────────────────────────────────────────────────────────────────

    @property
    def pwm(self) -> int:
        """
        Le PWM **réellement établi**, pas la consigne.

        La lampe met plus de deux secondes à se stabiliser après un saut. Lire
        la consigne au lieu de l'état a produit des tableaux entiers de mesures
        fausses le 2026-08-20 ; le simulateur refuse de mentir de la même façon.
        """
        self._avancer()
        return self._pwm

    def set_pwm(self, value, retries: int = 2) -> str:
        value = max(0, min(255, int(value)))
        with self._lock:
            self._pwm_cible = value
            self._t_consigne = time.perf_counter()
        return f"ACK PWM {value}"

    def off(self):
        return self.set_pwm(0)

    def full(self):
        return self.set_pwm(255)

    def _avancer(self) -> None:
        """Fait courir le temps : établissement de la lampe et rotation."""
        maintenant = time.perf_counter()
        with self._lock:
            # Lampe : première ordre, constante ~1,2 s, donc établie à 3,5 s.
            ecoule = maintenant - self._t_consigne
            if self.settle_s <= 0 or ecoule >= self.settle_s:
                self._pwm = self._pwm_cible
            else:
                k = 1.0 - math.exp(-ecoule / (self.settle_s / 2.8))
                self._pwm = int(round(self._pwm + (self._pwm_cible - self._pwm) * k))

            dt = maintenant - self._t
            self._t = maintenant
            if self.tourne and dt > 0:
                pas = self.vitesse * dt * (1.0 if self.horaire else -1.0)
                self.angle = (self.angle + pas) % 360.0

    # ── Infrarouge ────────────────────────────────────────────────────────────

    def send_ir(self, code: str, retries: int = 1) -> str:
        """
        Émet une trame IR — et applique la sémantique réelle de la télécommande.

        ``ROTATION_x`` **démarre** le plateau autant qu'elle choisit son sens.
        ``VITESSE_MOINS`` le démarre aussi, dans le dernier sens utilisé : c'est
        pourquoi réduire la vitesse avant d'avoir fixé le sens envoie le plateau
        à contresens une fois sur deux.
        """
        self._avancer()
        self.ir_envoyes.append(code)
        nom = self._nom_de(code)
        with self._lock:
            if nom == "COMMANDE_ROTATION_DROITE":
                self.horaire, self.tourne = True, True
            elif nom == "COMMANDE_ROTATION_GAUCHE":
                self.horaire, self.tourne = False, True
            elif nom == "COMMANDE_VITESSE_MOINS":
                self.vitesse = max(self.VITESSE_MIN, self.vitesse - 11.0)
                self.tourne = True          # démarre, dans le dernier sens
            elif nom == "COMMANDE_VITESSE_PLUS":
                self.vitesse = min(self.VITESSE_MAX, self.vitesse + 11.0)
                self.tourne = True
            elif nom == "COMMANDE_START_PAUSE":
                # La bascule. Sur un plateau à l'arrêt, elle le **relance**.
                if self.tourne:
                    sens = 1.0 if self.horaire else -1.0
                    self.angle = (self.angle + sens * self.ROUE_LIBRE_DEG) % 360.0
                    self.tourne = False
                else:
                    self.tourne = True
        return f"ACK {code}"

    _CODES: dict = {}

    def _nom_de(self, code: str) -> str:
        """Retrouve le nom de commande depuis la trame NEC."""
        if not FakeDimmer._CODES:
            import json  # noqa: PLC0415

            try:
                table = json.loads(config.CONFIG_TELECOMMANDE.read_text())
                FakeDimmer._CODES = {v: k for k, v in table.items()}
            except (OSError, ValueError):
                FakeDimmer._CODES = {}
        return FakeDimmer._CODES.get(code, code)


# ── Caméra simulée ────────────────────────────────────────────────────────────


class FakeD405:
    """
    Une D405 de synthèse : plateau, objet, marqueurs ArUco au bon angle.

    Les marqueurs sont **de vrais marqueurs** ``DICT_4X4_50``, dessinés en
    couleurs inversées comme ceux du banc (``invert_colors: true``) : le
    détecteur réel les décode, et la chaîne de mesure d'angle est donc
    exercée pour de bon, pas court-circuitée.

    Deux fidélités importantes :

    * seuls les marqueurs de la **moitié avant** du plateau se décodent, et pas
      systématiquement — sur le banc, à 29,5° d'élévation, la moitié des images
      seulement rend un marqueur, et jamais plus de deux à la fois ;
    * la détection dépend de l'**exposition** : au-delà de ~1500 µs le blanc du
      marqueur sort de la bande utile et plus rien ne se décode. C'est la
      courbe en U mesurée le 2026-07-30.
    """

    LARGEUR, HAUTEUR, FPS = 640, 480, 30
    #: Unité de profondeur de la D405 : le dixième de millimètre.
    DEPTH_SCALE = 1e-4

    def __init__(self, dimmer: FakeDimmer, settings=None, seed: int = 0,
                 rayon_px: "float | None" = None, taille_marqueur: int = 30,
                 fiabilite_marqueur: float = 0.55, max_marqueurs: int = 2):
        self.dim = dimmer
        self.settings = settings or _reglages_par_defaut()
        self.enable_depth = True
        self.align_depth = True
        self.depth_scale = self.DEPTH_SCALE
        #: Le centre est lu dans ``aruco_config.json``, celui-là même que le
        #: détecteur utilise. Le coder en dur ici les ferait diverger le jour où
        #: le centre est recalibré, et un centre décalé fausse tous les angles
        #: **sans que rien ne le signale**.
        self.centre = _centre_plateau()
        #: Rayon de la couronne de carreaux. Borné pour qu'elle tienne dans le
        #: cadre : le centre du plateau est bas dans l'image, et un rayon trop
        #: grand fait sortir les carreaux par le bas — ils disparaissent alors
        #: en silence, ce qui imite exactement une panne de détection.
        marge = taille_marqueur // 2 + taille_marqueur // 6 + 4
        libre = min(self.centre[0], self.LARGEUR - self.centre[0],
                    self.centre[1], self.HAUTEUR - self.centre[1]) - marge
        self.rayon = float(rayon_px) if rayon_px else max(40.0, libre)
        self.taille = taille_marqueur
        #: Probabilité qu'un carreau visible se décode sur une image donnée, et
        #: nombre maximum décodés simultanément. Mesuré sur le banc : à 29,5°
        #: d'élévation avec des carreaux de 10 mm, la moitié des images
        #: seulement rend un marqueur, et **jamais plus de deux à la fois**.
        #: Mettre la fiabilité à 1.0 rend le simulateur déterministe, au prix
        #: de ne plus exercer le chemin « angle introuvable » — qui est un
        #: résultat légitime et non une erreur.
        self.fiabilite_marqueur = float(fiabilite_marqueur)
        self.max_marqueurs = int(max_marqueurs)
        self._rng = random.Random(seed)
        self._n = 0
        self.closed = False
        self.intrinsics = {
            "fx": 421.6, "fy": 421.6, "cx": 321.4, "cy": 238.2,
            "width": self.LARGEUR, "height": self.HAUTEUR,
            "model": "distortion.brown_conrady", "coeffs": [0.0] * 5,
            "depth_scale": self.DEPTH_SCALE, "simule": True,
        }
        self._angles_marqueurs = _angles_marqueurs()
        #: Position de la main dans l'image, alimentée par la main simulée pour
        #: que la fermeture des doigts se voie. Sans cela l'image serait
        #: identique avant et après la saisie, et une vérification visuelle
        #: passerait sur un banc qui ne bouge pas.
        self.fermeture = 0.0

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def flush(self, count: int = 8):
        for _ in range(max(0, count)):
            self._n += 1

    def set_exposure(self, exposure_us, flush: int = 8):
        from vt_light.dimmer import snap_exposure_to_pwm_period  # noqa: PLC0415

        cale = snap_exposure_to_pwm_period(int(exposure_us))
        self.settings = _avec(self.settings, exposure_us=cale)
        self.flush(flush)
        return cale

    def apply(self, settings, flush: int = 8):
        self.settings = settings
        self.flush(flush)

    def read_back(self):
        return dict(self.settings.__dict__)

    # ── Images ────────────────────────────────────────────────────────────────

    def grab(self):
        """Retourne ``(couleur BGR uint8, profondeur uint16)``."""
        import cv2  # noqa: PLC0415

        self._n += 1
        pwm = self.dim.pwm
        angle = self.dim.angle
        expo = int(getattr(self.settings, "exposure_us", 2200))

        # Éclairement reçu ≈ PWM x exposition, avec le gamma 0,653 du pipeline
        # couleur de la D405 et la saturation du variateur au-delà de 200.
        eclairement = (min(pwm, 200) / 200.0) * (expo / 2200.0)
        fond = int(min(255, 26 + 150 * eclairement ** 0.653))

        img = np.full((self.HAUTEUR, self.LARGEUR, 3), fond, dtype=np.uint8)
        prof = np.full((self.HAUTEUR, self.LARGEUR), 4000, dtype=np.uint16)

        cx, cy = self.centre
        # Le plateau, vu à 29,5° d'élévation : un cercle projeté en ellipse.
        aplat = math.sin(math.radians(29.5))
        cv2.ellipse(img, (int(cx), int(cy)), (int(self.rayon * 1.25),
                                              int(self.rayon * 1.25 * aplat)),
                    0, 0, 360, (int(fond * 0.75),) * 3, -1, cv2.LINE_AA)

        self._dessiner_marqueurs(img, cx, cy, aplat, angle, eclairement)
        self._dessiner_objet(img, prof, cx, cy, angle)

        # Bruit du capteur : croît quand la lumière baisse, comme une vraie
        # stéréo passive. C'est ce qui rend la détection difficile en bas.
        sigma = 1.5 + 6.0 * (1.0 - min(1.0, eclairement))
        rng = np.random.default_rng(self._n)
        img = np.clip(img.astype(np.int16)
                      + rng.normal(0, sigma, img.shape).astype(np.int16),
                      0, 255).astype(np.uint8)
        return img, prof

    def _dessiner_marqueurs(self, img, cx, cy, aplat, angle, eclairement):
        """
        Pose les carreaux à l'angle apparent que le tracker doit retrouver.

        Les marqueurs sont placés sur un **cercle** et non sur l'ellipse du
        plateau, alors que celui-ci est bien dessiné aplati. Ce n'est pas une
        approximation par paresse : sur le banc, ``marker_angles`` a été
        réestimé *depuis les images* en accumulant les écarts entre marqueurs
        vus ensemble, ce qui absorbe la déformation de projection. Un
        simulateur qui la réintroduirait mesurerait un biais que le banc
        calibré n'a pas, et ferait échouer l'asservissement pour une mauvaise
        raison.

        Ce qui reste, et qui compte : la moitié arrière ne se décode pas, la
        détection dépend de l'exposition, et un bruit angulaire subsiste.
        """
        import cv2  # noqa: PLC0415

        # Hors de la bande d'exposition utile, aucun marqueur ne se décode.
        # Mesuré : 96 % d'images utiles à 600 µs, 25 % à 4000 µs.
        expo = int(getattr(self.settings, "exposure_us", 2200))
        lisible = expo <= 1500 and self.dim.pwm <= 200
        if not lisible:
            return

        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        s = self.taille
        marge = max(4, s // 6)              # zone de garde : ArUco l'exige
        dessines = 0
        for mid, alpha in self._angles_marqueurs.items():
            if dessines >= self.max_marqueurs:
                break
            if self._rng.random() > self.fiabilite_marqueur:
                continue                    # trop petit, trop flou : illisible
            beta = math.radians((angle + alpha) % 360.0)
            # Bruit de pose du carreau : c'est lui qui donne à l'angle mesuré
            # sa dispersion, de l'ordre du degré quand un seul marqueur est vu.
            gigue = math.radians(self._rng.gauss(0.0, 0.9))
            mx = cx + self.rayon * math.cos(beta + gigue)
            my = cy - self.rayon * math.sin(beta + gigue)
            # Moitié arrière : incidence rasante, le motif ne se décode pas.
            if math.sin(beta) > 0.15:
                continue
            motif = cv2.aruco.generateImageMarker(d, mid, s)
            # Zone de garde claire autour du motif, puis inversion : les
            # carreaux du banc sont imprimés en couleurs inversées, d'où
            # ``invert_colors: true`` dans la configuration.
            tuile = np.full((s + 2 * marge, s + 2 * marge), 255, dtype=np.uint8)
            tuile[marge:marge + s, marge:marge + s] = motif
            tuile = cv2.bitwise_not(tuile)
            c = tuile.shape[0]
            x0, y0 = int(mx - c // 2), int(my - c // 2)
            if x0 < 0 or y0 < 0 or x0 + c > self.LARGEUR or y0 + c > self.HAUTEUR:
                continue
            img[y0:y0 + c, x0:x0 + c] = cv2.cvtColor(tuile, cv2.COLOR_GRAY2BGR)
            dessines += 1

    def _dessiner_objet(self, img, prof, cx, cy, angle):
        """Un cube sur le plateau, et la main qui se referme dessus."""
        import cv2  # noqa: PLC0415

        cote = 62
        # Le cube tourne avec le plateau : sa largeur apparente varie, ce qui
        # rend les angles distinguables sur l'image.
        largeur = int(cote * (0.72 + 0.28 * abs(math.cos(math.radians(angle * 2)))))
        x0, y0 = int(cx - largeur / 2), int(cy - cote * 1.35)
        cv2.rectangle(img, (x0, y0), (x0 + largeur, y0 + cote), (118, 118, 124), -1)
        cv2.rectangle(img, (x0, y0), (x0 + largeur, y0 + cote), (86, 86, 92), 2)
        prof[y0:y0 + cote, x0:x0 + largeur] = 1250      # 12,5 cm en 0,1 mm

        if self.fermeture > 0.01:
            # Quatre doigts qui descendent sur l'objet, d'autant plus bas que
            # la fermeture avance.
            for k in range(4):
                fx = x0 - 26 + k * (largeur + 40) // 4
                fy = int(y0 - 58 + 52 * min(1.0, self.fermeture))
                cv2.rectangle(img, (fx, fy), (fx + 15, fy + 46), (58, 62, 138), -1)
                prof[max(0, fy):fy + 46, max(0, fx):fx + 15] = 1150


def _reglages_par_defaut():
    from vt_light.camera import CameraSettings  # noqa: PLC0415

    return CameraSettings()


def _avec(settings, **kw):
    from vt_light.camera import CameraSettings  # noqa: PLC0415

    return CameraSettings(**{**settings.__dict__, **kw})


def _centre_plateau() -> tuple:
    """Le centre du plateau, lu dans la configuration du banc."""
    import json  # noqa: PLC0415

    try:
        cfg = json.loads(config.CONFIG_ARUCO.read_text())
        c = cfg.get("turntable_center")
        if c:
            return (float(c[0]), float(c[1]))
    except (OSError, ValueError, TypeError):
        pass
    return (320.0, 240.0)


def _angles_marqueurs() -> dict:
    """Les angles des carreaux, lus dans la configuration du banc."""
    import json  # noqa: PLC0415

    try:
        cfg = json.loads(config.CONFIG_ARUCO.read_text())
        return {int(k): float(v) for k, v in cfg.get("marker_angles", {}).items()}
    except (OSError, ValueError):
        # Les carreaux sont en trois paires serrées espacées de 120°, et non
        # régulièrement tous les 60° comme le README l'affirmait.
        return {1: 0.0, 2: 121.7, 3: 234.6, 4: 345.9, 5: 119.3, 6: 240.1}


# ── Main simulée ──────────────────────────────────────────────────────────────


class FakeHand:
    """
    Une DH116 de synthèse, avec ses pannes.

    Expose la surface de ``vt_tactile.bus.Hand`` utilisée par l'orchestrateur.
    Les trames produites sont de **vraies trames** au format mesuré : elles
    passent par ``tpdo.build_frame`` et par le découpage moteur, si bien que le
    décodeur réel les lit sans savoir qu'elles sont simulées.
    """

    #: Butée mécanique, celle de ``hardware.POSITION_MAX``.
    MAX = hw.POSITION_MAX
    #: Où l'objet arrête les doigts, en counts. Le majeur et l'annulaire
    #: rencontrent le cube ; l'auriculaire va jusqu'à sa butée sans rien
    #: toucher, comme sur le banc le 2026-08-20.
    CONTACT = {3: None, 4: 5200, 5: 4900, 6: 5400}

    def __init__(self, camera: "FakeD405 | None" = None, seed: int = 0,
                 fiabilite_commande: float = 0.55):
        self._rng = random.Random(seed)
        self.camera = camera
        #: Probabilité qu'un ``move_motors`` isolé soit pris. Mesuré : un seul
        #: envoi tient quatre mouvements d'affilée, le double en tient neuf sur
        #: dix, et le double avec réémission douze sur douze.
        self.fiabilite = fiabilite_commande

        self.connected = False
        self.enabled = False
        self.homed = False
        self.interface = "enx-simule"
        self.dof = (6, 6)

        self.positions_reelles = {m: 0 for m in hw.MOTOR_IDS}
        self.cibles = {m: 0 for m in hw.MOTOR_IDS}
        self.courants = {m: 0 for m in hw.MOTOR_IDS}
        self.velocity = hw.VELOCITY_CLOSE
        self.max_current = hw.GRASP_CURRENT
        self._alarmes = {m: 0 for m in hw.MOTOR_IDS}

        self._latest = None
        self._latest_tactile = None
        self._latest_motor = None
        self._recording = False
        self._record: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pump: "threading.Thread | None" = None
        self._t_pas = time.perf_counter()
        self._n_pas = 0
        self.commandes_emises = 0

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def connect(self, iface_index=None):
        self.connected = True
        self._stop.clear()
        self._pump = threading.Thread(target=self._boucle, daemon=True)
        self._pump.start()

    def close(self):
        self._stop.set()
        if self._pump is not None:
            self._pump.join(timeout=2.0)
            self._pump = None
        self.connected = False

    def __enter__(self):
        self.connect()
        self.wake()
        return self

    def __exit__(self, *exc):
        self.release()
        self.close()

    def wake(self, home_wait: float = 0.2, timeout: float = 1.0,
             require_tactile: bool = True) -> bool:
        """
        Alimente les moteurs puis lance le homing — dans cet ordre.

        Sans les deux, la charge utile des trames reste identiquement nulle.
        C'est le piège numéro un du banc : on croit à une panne d'alimentation.
        Le homing du moteur 1 ne se termine jamais, et ça n'empêche pas le reste.
        """
        if not self.connected:
            raise RuntimeError("wake() sans connect()")
        self.enabled = True
        time.sleep(min(home_wait, 0.2))
        self.homed = True
        for m in hw.MOTOR_IDS:
            if m in hw.BROKEN_MOTORS:
                # Panne d'actionneur : le homing ne se termine jamais, le
                # variateur maintient un courant sans que rien ne bouge.
                self.courants[m] = 190
                continue
            self.positions_reelles[m] = 0
            self.cibles[m] = 0
        return True

    def enable(self):
        self.enabled = True

    def release(self, force: bool = False):
        """
        Ramène les consignes à zéro puis coupe le couple.

        La main n'est **pas** rétro-entraînable : couper l'alimentation fige les
        doigts au lieu de les relâcher, et une main fermée sur un objet le reste.
        Il faut ramener activement les consignes à 0.
        """
        for m in hw.MOTOR_IDS:
            if m not in hw.BROKEN_MOTORS:
                self.cibles[m] = 0
        self.enabled = False

    def alarms(self) -> dict:
        return dict(self._alarmes)

    def clear_alarms(self) -> dict:
        self._alarmes = {m: 0 for m in hw.MOTOR_IDS}
        return dict(self._alarmes)

    # ── Boucle de simulation ──────────────────────────────────────────────────

    def _boucle(self):
        while not self._stop.is_set():
            self._pas()
            trame = self._trame_tactile() if (self._n_pas % 2) else self._trame_moteur()
            t = time.perf_counter()
            with self._lock:
                self._latest = trame
                if trame[0] == tpdo.FRAME_TACTILE:
                    self._latest_tactile = trame
                else:
                    self._latest_motor = trame
                if self._recording:
                    self._record.append(_Frame(t, trame))
            time.sleep(0.001)

    def _pas(self):
        """Fait avancer les moteurs d'un pas de temps."""
        maintenant = time.perf_counter()
        dt = maintenant - self._t_pas
        self._t_pas = maintenant
        self._n_pas += 1

        for m in hw.MOTOR_IDS:
            if m in hw.BROKEN_MOTORS:
                continue                    # n'exécute aucune consigne
            pos, cible = self.positions_reelles[m], self.cibles[m]
            ecart = cible - pos
            if abs(ecart) < 2:
                # Un doigt calé sur l'objet continue de tirer du courant.
                bute = self.CONTACT.get(m) is not None and pos >= self.CONTACT[m] - 5
                self.courants[m] = self.max_current if bute else self._rng.randint(8, 40)
                continue
            pas = min(abs(ecart), self.velocity * dt)
            nouveau = pos + math.copysign(pas, ecart)

            butoir = self.CONTACT.get(m)
            if butoir is not None and nouveau > butoir:
                # L'objet arrête le doigt : la position n'avance plus, le
                # courant monte au plafond de couple.
                nouveau = butoir
                self.courants[m] = self.max_current
            else:
                self.courants[m] = self._rng.randint(120, 260)
            self.positions_reelles[m] = nouveau

        if self.camera is not None:
            flechisseurs = [m for m in hw.WORKING_FLEXORS if m in self.positions_reelles]
            if flechisseurs:
                moy = sum(self.positions_reelles[m] for m in flechisseurs) / len(flechisseurs)
                self.camera.fermeture = max(0.0, min(1.0, moy / 5200.0))

    # ── Trames ────────────────────────────────────────────────────────────────

    def _trame_tactile(self) -> bytes:
        """
        Une trame capteur au format mesuré, avec l'état réel des zones.

        Tant que la main n'est pas réveillée, l'en-tête est correct et la charge
        utile identiquement nulle — exactement ce que fait le matériel, et ce
        qui fait croire à une panne d'alimentation.
        """
        if not (self.enabled and self.homed):
            buf = bytearray(tpdo.TPDO_SIZE)
            buf[0], buf[1] = tpdo.FRAME_TACTILE, tpdo.SLOT_COUNT
            return bytes(buf)

        zones: dict = {}
        for m, prefixe in hw.MOTOR_TO_ZONE.items():
            butoir = self.CONTACT.get(m)
            if butoir is None or m in hw.BROKEN_MOTORS:
                continue
            avance = self.positions_reelles[m] - (butoir - 300)
            if avance <= 0:
                continue
            p = int(min(255, 40 + avance * 0.7))
            if prefixe in ("thumb", "little"):
                zones[prefixe] = {"touch": [p] * 5, "nf": p * 8}
            else:
                zones[f"{prefixe}.tip"] = {"touch": [p] * 4, "nf": p * 8, "prox": 255}
                # ring.pad est mort : 0 count, mesuré le 2026-08-19.
                if prefixe != "ring":
                    zones[f"{prefixe}.pad"] = {"touch": [p // 2] * 4, "nf": p * 4}

        # Le pouce : sa zone ne répond que s'il est amené en opposition. La
        # flexion seule le referme dans le vide, à côté de l'objet.
        pivot = self.positions_reelles.get(hw.THUMB_PIVOT, 0)
        if pivot > 2500:
            p = int(min(255, (pivot - 2500) * 0.05))
            if p > 0:
                zones["thumb"] = {"touch": [p] * 5, "nf": p * 8}

        # La paume : 14 points sur 26 répondent, à gain plus faible, et le
        # quadrant thénar est totalement muet.
        vivants = [i for i in range(26) if i not in tpdo.PALM_SILENT]
        touche = [0] * 26
        if any(self.positions_reelles[m] > 4000 for m in hw.WORKING_FLEXORS):
            for i in vivants:
                touche[i] = self._rng.randint(4, 51)
        zones[tpdo.PALM] = {"touch": touche}

        # Ligne de base par doigt : de ~80 à ~180 selon la zone. C'est elle que
        # TactileReader.zero() doit apprendre ; sans elle les zones ne sont pas
        # comparables entre elles.
        buf = bytearray(tpdo.build_frame(zones))
        for k, slot in enumerate(tpdo.SLOT_ORDER):
            if slot == tpdo.PALM:
                continue
            base = tpdo.slot_offset(k)
            socle = 80 + 20 * k
            for i in range(9):
                buf[base + i] = min(255, buf[base + i] + socle)
        return bytes(buf)

    def _trame_moteur(self) -> bytes:
        """
        Une trame d'état moteur au découpage mesuré.

        La position y est un **u16 signé** : un doigt repoussé sous son zéro
        s'écrit 63993 pour −1543. Le getter du SDK écrête ces valeurs à zéro,
        et c'est ce qui a fait passer quatre moteurs sains pour muets.
        """
        buf = bytearray(tpdo.TPDO_SIZE)
        buf[0] = tpdo.FRAME_MOTOR
        if not (self.enabled and self.homed):
            return bytes(buf)
        for m in hw.MOTOR_IDS:
            o = tpdo.motor_slot(m)
            pos = int(round(self.positions_reelles[m]))
            buf[o], buf[o + 1] = (pos & 0xFFFF) & 0xFF, ((pos & 0xFFFF) >> 8) & 0xFF
            cur = int(self.courants[m]) & 0xFFFF
            buf[o + 4], buf[o + 5] = cur & 0xFF, (cur >> 8) & 0xFF
        return bytes(buf)

    # ── Lecture ───────────────────────────────────────────────────────────────

    def latest(self):
        with self._lock:
            return self._latest

    def latest_tactile(self):
        with self._lock:
            return self._latest_tactile

    def latest_motor(self):
        with self._lock:
            return self._latest_motor

    def collect(self, seconds: float, tactile_only: bool = True) -> list:
        fin = time.perf_counter() + seconds
        vues, dernier = [], None
        while time.perf_counter() < fin:
            brut = self.latest_tactile() if tactile_only else self.latest()
            if brut is not None and brut is not dernier:
                vues.append(brut)
                dernier = brut
            time.sleep(0.002)
        return vues

    def positions(self, motors=hw.MOTOR_IDS) -> dict:
        """Position signée, lue dans la trame brute — pas par le getter du SDK."""
        brut = self.latest_motor()
        if brut is None:
            return {m: 0 for m in motors}
        d = tpdo.decode_motor(brut)["positions"]
        return {m: d.get(m, 0) for m in motors}

    def positions_sdk(self, motors=hw.MOTOR_IDS) -> dict:
        """Le comportement du SDK, écrêtage des négatifs compris."""
        return {m: max(0, v) for m, v in self.positions(motors).items()}

    def currents(self, motors=hw.MOTOR_IDS) -> dict:
        brut = self.latest_motor()
        if brut is None:
            return {m: 0 for m in motors}
        d = tpdo.decode_motor(brut)["currents"]
        return {m: d.get(m, 0) for m in motors}

    currents_sdk = currents

    # ── Commande ──────────────────────────────────────────────────────────────

    def command(self, targets: dict, velocity: int, max_current: int) -> None:
        """
        Émet la consigne **deux fois**, à 60 ms d'intervalle.

        ``move_motors`` n'est pas toujours pris : la consigne est acceptée,
        relue correctement, et le moteur ne bouge pas d'un count. Le double
        envoi fait passer de quatre mouvements enchaînés sans échec à neuf sur
        dix ; c'est aussi ce que fait le service constructeur.
        """
        self.velocity = int(velocity)
        self.max_current = int(max_current)
        self._derniere_consigne = dict(targets)
        self._emettre(targets)
        time.sleep(0.06)
        self._emettre(targets)

    _derniere_consigne: dict = {}

    def _emettre(self, targets: dict) -> None:
        self.commandes_emises += 1
        for m, pos in targets.items():
            m = int(m)
            if m in hw.BROKEN_MOTORS:
                continue                        # n'exécute aucune consigne
            if self._rng.random() > self.fiabilite:
                continue                        # perdue en silence
            self.cibles[m] = max(0, min(int(pos), self.MAX))

    def relancer(self) -> None:
        """Réémet la dernière consigne, quand rien n'a démarré."""
        if self._derniere_consigne:
            self._emettre(self._derniere_consigne)

    def open_hand(self, timeout: float = 8.0, tolerance: int = 120,
                  velocity: int = hw.VELOCITY_OPEN) -> bool:
        self.enable()
        cibles = {m: 0 for m in hw.MOTOR_IDS if m not in hw.BROKEN_MOTORS}
        self.command(cibles, velocity, hw.FULL_CURRENT)
        fin = time.perf_counter() + timeout
        while time.perf_counter() < fin:
            pos = self.positions(tuple(cibles))
            if all(abs(v) <= tolerance for v in pos.values()):
                return True
            if all(abs(self.positions_reelles[m]) > tolerance for m in cibles):
                self.relancer()
            time.sleep(0.05)
        return all(abs(v) <= tolerance for v in self.positions(tuple(cibles)).values())

    # ── Enregistrement ────────────────────────────────────────────────────────

    def start_recording(self) -> None:
        with self._lock:
            self._record = []
            self._recording = True

    def stop_recording(self) -> list:
        self._recording = False
        time.sleep(0.05)
        with self._lock:
            trames, self._record = self._record, []
        return trames

    @property
    def recording(self) -> bool:
        return self._recording


class _Frame:
    """Même forme que ``vt_tactile.bus.Frame`` : ``.t`` et ``.data``."""

    __slots__ = ("t", "data")

    def __init__(self, t: float, data: bytes):
        self.t = t
        self.data = data
