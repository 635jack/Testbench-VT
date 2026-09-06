#!/usr/bin/env python3
"""
Consommation du profil d'éclairage retenu.

C'est l'interface prévue pour ``VT-Acquisition`` : plutôt que de recopier des
constantes d'un dépôt à l'autre — où elles divergeront — on charge le fichier
produit par ``tools/30_choose.py`` et on demande un niveau par son nom.

    profile = LightProfile.load()
    with Dimmer("/dev/ttyACM0") as dim, D405(profile.camera_settings()) as cam:
        for level in profile.level_names:
            profile.apply(cam, dim, level)
            color, depth = cam.grab()
"""
import os

from .camera import CameraSettings
from .dimmer import snap_exposure_to_pwm_period
from .report import RESULTS_DIR, load_json

DEFAULT_PATH = os.path.join(RESULTS_DIR, "light_profile.json")


class LightProfile:
    def __init__(self, payload):
        self.raw = payload
        self.levels = payload["levels"]
        self.pose_frame = payload.get("pose_frame")
        self.ev_step = payload.get("ev_step")

    @classmethod
    def load(cls, path=None):
        return cls(load_json(path or DEFAULT_PATH))

    @property
    def level_names(self):
        return [level["name"] for level in self.levels]

    def pwm_of(self, name):
        for level in self.levels:
            if level["name"] == name:
                return int(level["pwm"])
        raise KeyError(f"Niveau inconnu : {name!r} (connus : {self.level_names})")

    def camera_settings(self):
        return CameraSettings(**self.raw["camera"])

    def apply(self, cam, dim, level_name, flush=10):
        """
        Place le banc dans une condition du jeu de données.

        Le ``flush`` n'est pas décoratif : les images déjà dans la file du pipeline
        ont été prises sous l'éclairage précédent. Sans lui, la première image
        après un changement de niveau porte encore l'ancien.
        """
        pwm = self.pwm_of(level_name)
        dim.set_pwm(pwm)
        cam.flush(flush)
        return pwm

    def apply_pose_frame(self, cam, dim, flush=10):
        """
        Place le banc dans la condition de mesure d'angle.

        À utiliser avant ou après le triplet, plateau immobile : l'angle est le
        même pour les quatre images. Retourne ``None`` si le profil n'a pas de
        prise de vue de pose exploitable.
        """
        if not self.pose_frame:
            return None
        dim.set_pwm(int(self.pose_frame["pwm"]))
        exposure = snap_exposure_to_pwm_period(self.pose_frame["exposure_us"])
        if exposure != cam.settings.exposure_us:
            cam.set_exposure(exposure, flush=0)
        cam.flush(flush)
        return int(self.pose_frame["pwm"])

    def summary(self):
        cam = self.raw["camera"]
        lines = [
            f"exposition {cam['exposure_us']} us | gain {cam['gain']} | "
            f"balance des blancs {cam['white_balance_k']} K | "
            f"{self.raw['resolution'][0]}x{self.raw['resolution'][1]} "
            f"à {self.raw['fps']} fps",
            "niveaux : " + ", ".join(f"{l['name']} = PWM {l['pwm']}"
                                     for l in self.levels)
            + f" (pas de {self.ev_step:.1f} diaphragme)",
        ]
        if self.pose_frame:
            lines.append(f"pose : PWM {self.pose_frame['pwm']} "
                         f"({self.pose_frame['markers_mean']:.2f} marqueur/image)")
        return "\n".join(lines)
