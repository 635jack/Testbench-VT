#!/usr/bin/env python3
"""
Enveloppe RealSense D405 en contrôle **entièrement manuel**.

Deux particularités de la D405, vérifiées sur le banc, structurent tout ce
module :

1. **Il n'y a qu'un seul capteur** (``Stereo Module``). Exposition, gain et
   balance des blancs sont *partagés* entre le flux couleur et le flux de
   profondeur : on ne peut pas régler la couleur sans agir sur la profondeur.
2. **Il n'y a pas de projecteur infrarouge** (aucune option ``Emitter Enabled``
   ni ``Laser Power``). La stéréo est *passive* : la profondeur est calculée sur
   la texture éclairée par la lumière ambiante. Baisser la lampe dégrade donc
   directement la profondeur, pas seulement la couleur.

On reste en 640x480 : c'est le mode qui tient 30 fps en couleur + profondeur à
travers la redirection USB de la VM.
"""
import logging
import time
from dataclasses import dataclass, asdict

import numpy as np

try:
    import pyrealsense2 as rs
except ImportError:  # pragma: no cover
    rs = None

logger = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 640, 480, 30


@dataclass
class CameraSettings:
    """
    Réglages figés de la D405.

    Tout est explicite, y compris les valeurs laissées au défaut : le but du
    banc est qu'une image du jeu de données soit reproductible, donc aucun
    paramètre ne doit rester implicite ou automatique.
    """
    exposure_us: int = 4000
    gain: int = 16                    # minimum du capteur = bruit de lecture le plus bas
    white_balance_k: int = 4600       # 2800..6500, pas de 10
    auto_exposure: bool = False
    auto_white_balance: bool = False

    # 0 = anti-scintillement désactivé. La lampe du banc est un LED en PWM 5 kHz
    # continu : elle ne scintille pas au rythme du secteur, et laisser le filtre
    # actif contraindrait l'exposition à des multiples de 10 ms.
    power_line_frequency: int = 0

    brightness: int = 0
    contrast: int = 50
    gamma: int = 300
    hue: int = 0
    saturation: int = 64
    sharpness: int = 50

    def as_dict(self):
        return asdict(self)


#: Correspondance nom d'option pyrealsense2 -> champ de CameraSettings.
_OPTION_FIELDS = [
    ("enable_auto_exposure", "auto_exposure", bool),
    ("enable_auto_white_balance", "auto_white_balance", bool),
    ("exposure", "exposure_us", int),
    ("gain", "gain", int),
    ("white_balance", "white_balance_k", int),
    ("power_line_frequency", "power_line_frequency", int),
    ("brightness", "brightness", int),
    ("contrast", "contrast", int),
    ("gamma", "gamma", int),
    ("hue", "hue", int),
    ("saturation", "saturation", int),
    ("sharpness", "sharpness", int),
]


class D405:
    """
    Flux couleur + profondeur 640x480, réglages imposés.

        with D405(CameraSettings(exposure_us=4000)) as cam:
            color, depth = cam.grab()
    """

    def __init__(self, settings=None, warmup_sec=1.0, enable_depth=True,
                 align_depth=True):
        if rs is None:
            raise RuntimeError("pyrealsense2 absent : ce module doit tourner dans la VM.")
        self.settings = settings or CameraSettings()
        self.enable_depth = enable_depth
        # Les deux flux sont en 640x480 mais pas co-registrés : mesuré sur le banc,
        # l'écart atteint ~1,8 mm en médiane sur la face du cube. Un masque défini
        # sur l'image couleur ne tombe donc pas exactement sur les bons pixels de
        # profondeur sans alignement.
        self.align_depth = align_depth and enable_depth
        self._align = rs.align(rs.stream.color) if self.align_depth else None
        self.pipeline = None
        self.device = None
        self.depth_scale = None
        self.intrinsics = None
        self._start(warmup_sec)

    # -------------------------------------------------------------------- cycle

    def _start(self, warmup_sec):
        self.pipeline = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.color, WIDTH, HEIGHT, rs.format.bgr8, FPS)
        if self.enable_depth:
            cfg.enable_stream(rs.stream.depth, WIDTH, HEIGHT, rs.format.z16, FPS)
        profile = self.pipeline.start(cfg)
        self.device = profile.get_device()

        if self.enable_depth:
            self.depth_scale = profile.get_device().first_depth_sensor().get_depth_scale()
            logger.info("depth_scale = %g m/unité", self.depth_scale)

        intr = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
        self.intrinsics = {
            "fx": intr.fx, "fy": intr.fy, "cx": intr.ppx, "cy": intr.ppy,
            "width": intr.width, "height": intr.height,
            "model": str(intr.model), "coeffs": list(intr.coeffs),
            "depth_scale": self.depth_scale,
        }

        # Le capteur a besoin de tourner un instant avant d'accepter les
        # réglages : sur un pipeline tout juste démarré, set_option est
        # silencieusement ignoré sur certaines options.
        t0 = time.time()
        while time.time() - t0 < warmup_sec:
            self.pipeline.wait_for_frames(timeout_ms=5000)

        self.apply(self.settings)

    def close(self):
        if self.pipeline is not None:
            self.pipeline.stop()
            self.pipeline = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ réglages

    def _sensors_supporting(self, option):
        for sensor in self.device.query_sensors():
            if sensor.supports(option):
                yield sensor

    def apply(self, settings, flush=8):
        """
        Impose tous les réglages puis vide les images déjà en file.

        L'ordre importe : couper les automatismes **avant** d'écrire exposition,
        gain et balance des blancs, sinon la valeur écrite est aussitôt écrasée
        par la boucle d'asservissement.
        """
        self.settings = settings
        for opt_name, field_name, cast in _OPTION_FIELDS:
            option = getattr(rs.option, opt_name, None)
            if option is None:
                continue
            value = float(cast(getattr(settings, field_name)))
            for sensor in self._sensors_supporting(option):
                try:
                    sensor.set_option(option, value)
                except Exception as exc:
                    logger.warning("set_option(%s, %s) a échoué : %s", opt_name, value, exc)
        self.flush(flush)

    def set_exposure(self, exposure_us, flush=8):
        """Change la seule exposition, en conservant le reste des réglages."""
        self.settings.exposure_us = int(exposure_us)
        for sensor in self._sensors_supporting(rs.option.exposure):
            sensor.set_option(rs.option.exposure, float(exposure_us))
        self.flush(flush)

    def set_white_balance(self, kelvin, flush=8):
        """Change la seule balance des blancs (2800..6500 K, pas de 10)."""
        kelvin = int(round(kelvin / 10.0) * 10)
        self.settings.white_balance_k = kelvin
        for sensor in self._sensors_supporting(rs.option.white_balance):
            sensor.set_option(rs.option.white_balance, float(kelvin))
        self.flush(flush)

    def set_gain(self, gain, flush=8):
        self.settings.gain = int(gain)
        for sensor in self._sensors_supporting(rs.option.gain):
            sensor.set_option(rs.option.gain, float(gain))
        self.flush(flush)

    def read_back(self):
        """
        Relit les options **depuis le capteur**.

        Indispensable : le firmware borne ou ignore certaines écritures, et une
        valeur acceptée par ``set_option`` n'est pas nécessairement celle qui
        sert à l'acquisition.
        """
        state = {}
        for opt_name, _field, _cast in _OPTION_FIELDS:
            option = getattr(rs.option, opt_name, None)
            if option is None:
                continue
            for sensor in self._sensors_supporting(option):
                state[opt_name] = sensor.get_option(option)
                break
        return state

    # ---------------------------------------------------------------- acquisition

    def flush(self, count=8):
        """Jette ``count`` images : un changement de réglage n'agit pas sur les
        images déjà dans la file du pipeline."""
        for _ in range(max(0, count)):
            self.pipeline.wait_for_frames(timeout_ms=5000)

    def grab(self):
        """
        Retourne ``(color_bgr uint8, depth_u16 ou None)``.

        La profondeur est alignée sur la couleur si ``align_depth``, pour qu'un
        masque tracé sur l'image couleur désigne bien les mêmes points en
        profondeur.
        """
        frames = self.pipeline.wait_for_frames(timeout_ms=5000)
        if self._align is not None:
            frames = self._align.process(frames)
        color = np.asanyarray(frames.get_color_frame().get_data()).copy()
        depth = None
        if self.enable_depth:
            df = frames.get_depth_frame()
            if df:
                depth = np.asanyarray(df.get_data()).copy()
        return color, depth

    def grab_stack(self, n=5):
        """
        ``n`` images consécutives, pour séparer le bruit temporel du signal.

        Returns:
            (colors: (n,H,W,3) uint8, depths: (n,H,W) uint16 ou None)
        """
        colors, depths = [], []
        for _ in range(n):
            c, d = self.grab()
            colors.append(c)
            if d is not None:
                depths.append(d)
        return np.stack(colors), (np.stack(depths) if depths else None)
