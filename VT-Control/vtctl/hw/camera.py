#!/usr/bin/env python3
"""
camera.py — le seul propriétaire de la D405.

**Un seul ``rs.pipeline`` par appareil.** Deux prétendants existent dans le
dépôt : ``vt_light.camera.D405`` pour le jeu de données et ``ArUcoTracker``
pour la mesure d'angle. Jusqu'ici on les alternait, en ouvrant et refermant le
tracker autour de chaque positionnement — deux établissements de lampe par
angle, soit sept secondes perdues, et un état de lissage remis à zéro sans que
rien ne le signale.

Ici la caméra est ouverte **une fois pour la session**, et c'est
l'**exposition** qui bascule. C'est possible parce que ``detect_markers`` et
``estimate_turntable_angle`` prennent une image en argument : ils n'ouvrent
aucune caméra. Le seul besoin propre du tracker est une exposition courte.

Deux modes, et une raison de fond de ne pas les confondre :

* ``ARUCO`` — 600 µs, lampe basse. Les marqueurs se décodent d'autant mieux que
  l'exposition est courte : 96 % d'images utiles et 4,45° d'écart-type à
  600 µs, contre 25 % et 11,8° à 4000 µs. Le gain vient de la netteté du motif.
* ``DATASET`` — l'exposition du profil VT-Light, la même à tous les niveaux
  d'éclairage par principe. Si elle variait, elle compenserait l'éclairement et
  les images ne montreraient plus l'effet cherché.

L'exposition est toujours calée sur un multiple de 200 µs, période de la
porteuse PWM à 5 kHz du variateur. Sinon l'obturateur intègre un nombre
fractionnaire de créneaux et l'éclairement varie de ±2,4 % d'une image à
l'autre.
"""
from __future__ import annotations

import logging
import time

import threading

from .. import config
from .resources import CAMERA, ResourceManager

log = logging.getLogger("vtctl.camera")

ARUCO = "aruco"
DATASET = "dataset"


class CameraOwner:
    """
    Détient la caméra, et sert les deux usages du banc.

    Args:
        manager: le gestionnaire de ressources. La caméra lui est réclamée à
            l'ouverture et rendue à la fermeture.
        settings: réglages du mode ``DATASET``. ``None`` = ceux du profil.
        exposition_dataset: pour forcer une exposition différente de celle du
            profil sans modifier le profil — le README de VT-Light prévient
            qu'une copie de ses constantes divergerait.
        fake: une caméra simulée. Quand elle est fournie, aucun matériel n'est
            touché et ``pyrealsense2`` n'est pas importé.
    """

    def __init__(self, manager: ResourceManager, settings=None,
                 exposition_dataset: "int | None" = None, fake=None,
                 exposition_aruco: "int | None" = None,
                 gain_aruco: "int | None" = None,
                 avec_profondeur: bool = True):
        self._mgr = manager
        self._token = None
        self.cam = None
        self.fake = fake
        self.mode: "str | None" = None
        self._profil = None
        self._settings = settings
        self._expo_dataset = exposition_dataset
        #: Point de fonctionnement de la détection. Réglable parce que c'est une
        #: propriété **du banc**, pas du logiciel : il dépend de l'éclairage
        #: réel, et le défaut (120/600 µs) suppose la lampe en état.
        self._expo_aruco = exposition_aruco or config.EXPO_ARUCO
        self._gain_aruco = gain_aruco if gain_aruco is not None else config.GAIN_ARUCO
        #: Diffuser la profondeur ou non. Indispensable au jeu de données,
        #: inutile à la détection de marqueurs — et c'est elle qui charge le
        #: lien USB au point de faire tomber la machine virtuelle.
        self.avec_profondeur = avec_profondeur
        self.ouvertures = 0
        self.bascules = 0
        #: Dernière image lue, servie à l'interface sans reprendre la caméra.
        #: Sans ce cache, afficher le flux dans un navigateur voudrait dire
        #: tirer des images en parallèle du protocole — deux lecteurs sur un
        #: pipeline qui n'en admet qu'un, et le conflit revient par la fenêtre.
        self._derniere = None
        self._verrou_image = threading.Lock()

    # ── Profil ────────────────────────────────────────────────────────────────

    def profil(self):
        """Le profil photométrique de VT-Light, chargé une fois."""
        if self._profil is None:
            from vt_light.profile import LightProfile  # noqa: PLC0415

            self._profil = LightProfile.load(str(config.LIGHT_PROFILE))
        return self._profil

    def exposition_aruco(self) -> int:
        """Exposition de la détection de marqueurs, en microsecondes."""
        from vt_light.dimmer import snap_exposure_to_pwm_period  # noqa: PLC0415

        return int(snap_exposure_to_pwm_period(self._expo_aruco))

    def exposition_dataset(self) -> int:
        """
        Exposition des images du jeu de données, en microsecondes.

        Celle du profil, sauf surcharge explicite. La mesure du 2026-08-20
        conclut à 5000 µs plutôt que les 2200 du profil — écrêtage 0,033 % pour
        un critère à 0,1 %, p99 à 215 pour un critère à 240, neuf points de
        remplissage de profondeur gagnés — mais cette mesure n'a jamais été
        portée dans le fichier. On ne la force pas ici : les deux divergeraient,
        et c'est exactement ce que le README de VT-Light met en garde de faire.
        """
        from vt_light.dimmer import snap_exposure_to_pwm_period  # noqa: PLC0415

        brut = (self._expo_dataset if self._expo_dataset
                else int(self.profil().camera_settings().exposure_us))
        return int(snap_exposure_to_pwm_period(brut))

    def reglages_dataset(self):
        from vt_light.camera import CameraSettings  # noqa: PLC0415

        base = self._settings or self.profil().camera_settings()
        return CameraSettings(**{**base.__dict__,
                                 "exposure_us": self.exposition_dataset()})

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def open(self) -> "CameraOwner":
        """Réclame la caméra et démarre le flux. Idempotent."""
        if self.cam is not None:
            return self
        self._token = self._mgr.acquire(CAMERA, "CameraOwner")
        try:
            if self.fake is not None:
                self.cam = self.fake
            else:
                from vt_light.camera import D405  # noqa: PLC0415

                self.cam = D405(self.reglages_dataset(),
                                enable_depth=self.avec_profondeur,
                                align_depth=self.avec_profondeur)
            self.mode = DATASET
            self.ouvertures += 1
            log.info("caméra ouverte, mode %s à %d µs", self.mode,
                     self.exposition_dataset())
        except Exception:
            self._token.release()
            self._token = None
            raise
        return self

    def close(self) -> None:
        if self.cam is not None:
            try:
                self.cam.close()
            except Exception:  # noqa: BLE001
                log.warning("fermeture caméra en échec", exc_info=True)
            self.cam = None
            self.mode = None
        if self._token is not None:
            self._token.release()
            self._token = None

    def __enter__(self) -> "CameraOwner":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def ouverte(self) -> bool:
        return self.cam is not None

    # ── Bascule de mode ───────────────────────────────────────────────────────

    def set_mode(self, mode: str, flush: int = 8) -> int:
        """
        Bascule l'exposition, et jette les images déjà en file.

        Le ``flush`` n'est pas décoratif : les images déjà dans la file du
        pipeline ont été prises sous l'ancien réglage. Sans lui, la première
        image après la bascule porte encore l'exposition précédente.

        Returns:
            l'exposition effectivement appliquée, en microsecondes.
        """
        self._exige_ouverte()
        aruco = mode == ARUCO
        cible = self.exposition_aruco() if aruco else self.exposition_dataset()
        if self.mode == mode:
            return cible
        if self._gain_aruco is not None:
            # Le gain fait partie du point de fonctionnement quand on doit
            # relever la sensibilité : le régler après l'exposition, sur le même
            # vidage, évite une image intermédiaire à réglages mixtes.
            self.cam.set_gain(self._gain_aruco if aruco
                              else int(self.reglages_dataset().gain), flush=0)
        self.cam.set_exposure(cible, flush=flush)
        self.mode = mode
        self.bascules += 1
        log.debug("caméra → mode %s (%d µs)", mode, cible)
        return cible

    # ── Images ────────────────────────────────────────────────────────────────

    def grab(self, mode: "str | None" = None):
        """
        Une image couleur + profondeur, avec l'instant de sa prise de vue.

        Returns:
            ``(couleur, profondeur, t_perf)`` où ``t_perf`` est un
            ``time.perf_counter`` relevé **au retour de la caméra**, avant tout
            encodage. Horodater après l'écriture PNG ajoute des dizaines de
            millisecondes au délai réel — c'est le défaut du format 1.
        """
        self._exige_ouverte()
        if mode is not None:
            self.set_mode(mode)
        couleur, profondeur = self.cam.grab()
        t = time.perf_counter()
        with self._verrou_image:
            self._derniere = (couleur, profondeur, t, self.mode)
        return couleur, profondeur, t

    def grab_dataset(self):
        """Une image du jeu de données, à l'exposition du profil."""
        return self.grab(DATASET)

    def grab_aruco(self):
        """Une image pour la détection de marqueurs, à exposition courte."""
        return self.grab(ARUCO)

    def flush(self, count: int = 8) -> None:
        self._exige_ouverte()
        self.cam.flush(count)

    def reconnecter(self) -> dict:
        """
        Ferme et rouvre le flux, sans lâcher la ressource.

        Le jeton reste tenu pendant l'opération : le relâcher ouvrirait une
        fenêtre où un autre processus pourrait s'emparer de la caméra, et l'on
        se retrouverait à deux dessus — exactement ce que ce module empêche.

        Sert quand le pipeline se fige : la redirection USB de la machine
        virtuelle décroche sous trafic isochrone soutenu, et le flux ne
        redémarre pas tout seul.
        """
        self._exige_ouverte()
        mode = self.mode
        try:
            self.cam.close()
        except Exception:  # noqa: BLE001
            log.warning("fermeture caméra en échec pendant la reconnexion",
                        exc_info=True)
        self.cam = None
        with self._verrou_image:
            self._derniere = None
        time.sleep(1.0)
        if self.fake is not None:
            self.cam = self.fake
        else:
            from vt_light.camera import D405  # noqa: PLC0415

            self.cam = D405(self.reglages_dataset(),
                            enable_depth=self.avec_profondeur,
                            align_depth=self.avec_profondeur)
        self.mode = DATASET
        self.ouvertures += 1
        if mode == ARUCO:
            self.set_mode(ARUCO)
        log.info("caméra reconnectée (ouverture n°%d)", self.ouvertures)
        return {"reconnectee": True, "ouvertures": self.ouvertures,
                "mode": self.mode}

    def derniere_image(self):
        """
        La dernière image lue, sans toucher à la caméra.

        C'est ce que sert l'interface. Elle ne déclenche aucune lecture : le
        pipeline n'admet qu'un lecteur, et en ouvrir un second pour l'affichage
        ferait revenir par la fenêtre le conflit que ce module supprime.

        Returns:
            ``(couleur, profondeur, t, mode)`` ou ``None`` si rien n'a encore
            été lu.
        """
        with self._verrou_image:
            return self._derniere

    def rafraichir(self, mode: "str | None" = None):
        """Lit une image pour l'affichage, quand le protocole n'en lit pas."""
        return self.grab(mode)

    # ── Métadonnées ───────────────────────────────────────────────────────────

    def infos(self) -> dict:
        """
        Intrinsèques et échelle de profondeur, pour le manifeste.

        Sans elles la profondeur enregistrée n'est qu'une image d'entiers : on
        ne peut ni la convertir en nuage de points, ni la comparer à une autre
        session. Elles dépendent du mode — les modes de la D405 n'ont pas le
        même champ de vision — donc on les relit de la caméra plutôt que de les
        supposer.
        """
        if self.cam is None:
            return {"ouverte": False}
        return {
            "ouverte": True,
            "simulee": self.fake is not None,
            "intrinsics": self.cam.intrinsics,
            "depth_scale": self.cam.depth_scale,
            "depth_unit": "0,1 mm (depth_scale = 1e-4 m)",
            "align_depth": getattr(self.cam, "align_depth", None),
            "exposition_dataset_us": self.exposition_dataset(),
            "exposition_aruco_us": self.exposition_aruco(),
            "gain_aruco": self._gain_aruco,
            "mode": self.mode,
            "reglages": self.cam.settings.as_dict()
                        if hasattr(self.cam.settings, "as_dict")
                        else dict(self.cam.settings.__dict__),
        }

    def _exige_ouverte(self) -> None:
        if self.cam is None:
            raise RuntimeError("caméra fermée : appeler open() d'abord")
