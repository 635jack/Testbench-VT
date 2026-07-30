"""VT-Light — contrôle de l'éclairage du banc visuo-tactile et réglage de la D405."""

from .dimmer import Dimmer, PWM_FREQ_HZ, PWM_PERIOD_US, snap_exposure_to_pwm_period
from .camera import D405, CameraSettings, WIDTH, HEIGHT, FPS
from .report import RESULTS_DIR, write_csv, save_json, load_json

__all__ = [
    "Dimmer", "PWM_FREQ_HZ", "PWM_PERIOD_US", "snap_exposure_to_pwm_period",
    "D405", "CameraSettings", "WIDTH", "HEIGHT", "FPS",
    "RESULTS_DIR", "write_csv", "save_json", "load_json",
]
