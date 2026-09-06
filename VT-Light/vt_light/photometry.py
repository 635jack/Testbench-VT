#!/usr/bin/env python3
"""
Passage des niveaux de pixels à l'**éclairement relatif**.

Le problème : le pipeline couleur de la D405 applique un gamma (``Gamma = 300``).
Un blanc à 90 ne reçoit donc pas « la moitié » de la lumière d'un blanc à 180. Or
choisir trois niveaux de lumière « bien espacés » n'a de sens qu'en éclairement,
pas en niveaux de pixels.

La solution ne demande aucun photomètre : **le temps d'exposition est une
référence linéaire gratuite.** L'exposition lumineuse reçue par le capteur vaut
H = E x t (éclairement x durée). À éclairement fixe, balayer t donne donc la
courbe de réponse de la caméra, v = f(H), à un facteur d'échelle près.

Une fois f connue, toute mesure (t, v) se convertit en éclairement relatif :

    E_rel = f^-1(v) / t

C'est la brique qui permet d'annoncer « ces trois niveaux sont espacés d'un
diaphragme » et de le savoir vrai.
"""
import logging

import numpy as np

logger = logging.getLogger(__name__)


class OECF:
    """
    Courbe de réponse opto-électronique, inversible.

    Construite à partir d'un balayage d'exposition à éclairement constant. Les
    « expositions équivalentes » qu'elle retourne sont exprimées en
    microsecondes *à l'éclairement de calibration* : seul leur rapport a un sens.
    """

    def __init__(self, exposures_us, values, clip_value=245.0, min_value=2.0):
        exposures = np.asarray(exposures_us, float)
        values = np.asarray(values, float)
        order = np.argsort(exposures)
        exposures, values = exposures[order], values[order]

        # Écarter les points inutilisables : écrêtés en haut (l'information y est
        # perdue, la courbe y est plate) et noyés dans le bruit en bas.
        keep = (values < clip_value) & (values > min_value)
        exposures, values = exposures[keep], values[keep]
        if len(values) < 4:
            raise ValueError("Trop peu de points exploitables pour ajuster la réponse.")

        # Forcer la stricte croissance, requise par l'interpolation inverse.
        keep = np.concatenate([[True], np.diff(values) > 1e-6])
        self.values = values[keep]
        self.exposures = exposures[keep]
        self.log_exposures = np.log(self.exposures)
        self.value_range = (float(self.values[0]), float(self.values[-1]))

    def equivalent_exposure(self, value):
        """
        f^-1(v) : exposition, en us à l'éclairement de calibration, qui produit ``v``.

        Interpolation linéaire en log-exposition — la réponse est une loi de
        puissance, donc quasi droite dans cet espace, ce qui limite l'erreur
        d'interpolation entre deux points de mesure.
        """
        v = np.asarray(value, float)
        lo, hi = self.value_range
        out = np.exp(np.interp(np.clip(v, lo, hi), self.values, self.log_exposures))
        return np.where((v < lo) | (v > hi), np.nan, out)

    def in_range(self, value):
        lo, hi = self.value_range
        return (np.asarray(value, float) >= lo) & (np.asarray(value, float) <= hi)

    def relative_illuminance(self, value, exposure_us):
        """Éclairement relatif déduit d'une mesure (v, t). Unité arbitraire."""
        return self.equivalent_exposure(value) / float(exposure_us)

    def value_at(self, exposure_us):
        """f(H) : niveau attendu pour une exposition équivalente donnée."""
        t = np.asarray(exposure_us, float)
        return np.interp(np.log(np.clip(t, self.exposures[0], self.exposures[-1])),
                         self.log_exposures, self.values)

    def gamma_estimate(self):
        """
        Pente de log(v) contre log(t) : le gamma effectif du pipeline couleur.

        Vaut ~1 si la sortie est linéaire, ~0,45 pour un encodage sRGB classique.
        Utile comme contrôle de vraisemblance de la courbe mesurée.
        """
        slope, _ = np.polyfit(self.log_exposures, np.log(self.values), 1)
        return float(slope)

    def as_dict(self):
        return {"values": self.values.tolist(),
                "equivalent_exposures_us": self.exposures.tolist(),
                "value_range": list(self.value_range),
                "gamma_estimate": self.gamma_estimate()}

    @classmethod
    def from_dict(cls, payload):
        obj = cls.__new__(cls)
        obj.values = np.asarray(payload["values"], float)
        obj.exposures = np.asarray(payload["equivalent_exposures_us"], float)
        obj.log_exposures = np.log(obj.exposures)
        obj.value_range = tuple(payload["value_range"])
        return obj


def ev(ratio):
    """Écart en diaphragmes (stops) correspondant à un rapport d'éclairement."""
    return float(np.log2(ratio))


def pick_levels(pwms, illuminances, top_pwm, ev_step, n_levels=3):
    """
    Choisit ``n_levels`` PWM dont les éclairements sont espacés de ``ev_step``
    diaphragmes, en partant de ``top_pwm``.

    L'espacement se fait en éclairement et non en PWM parce que la réponse du
    variateur n'est pas linéaire, ni en PWM ni en niveaux de pixels : trois PWM
    régulièrement répartis donneraient des niveaux de lumière n'ayant rien de
    régulier.

    Returns:
        liste de dicts, du plus clair au plus sombre.
    """
    pwms = np.asarray(pwms, float)
    illum = np.asarray(illuminances, float)
    valid = np.isfinite(illum) & (illum > 0)
    pwms, illum = pwms[valid], illum[valid]

    # Le variateur doit être monotone croissant pour qu'on puisse l'inverser ;
    # on garde l'enveloppe croissante.
    order = np.argsort(pwms)
    pwms, illum = pwms[order], illum[order]
    running = np.maximum.accumulate(illum)
    keep = np.concatenate([[True], np.diff(running) > 0])
    pwms, illum = pwms[keep], running[keep]

    top = float(np.interp(top_pwm, pwms, illum))
    levels = []
    for k in range(n_levels):
        target = top / (2.0 ** (ev_step * k))
        if target < illum[0]:
            logger.warning("Niveau %d (%.4g) sous le plancher mesurable (%.4g)",
                           k, target, illum[0])
            pwm = float(pwms[0])
        else:
            pwm = float(np.interp(target, illum, pwms))
        pwm = int(round(pwm))
        achieved = float(np.interp(pwm, pwms, illum))
        levels.append({
            "pwm": pwm,
            "target_illuminance": float(target),
            "illuminance": achieved,
            "ev_below_top": -ev(achieved / top),
        })
    return levels
