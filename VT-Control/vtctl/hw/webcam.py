#!/usr/bin/env python3
"""
webcam.py — la source d'angle du plateau, sur une caméra dédiée.

Pourquoi une seconde caméra. Mesurer l'angle du plateau et photographier la
saisie sont deux métiers qui réclament des réglages opposés, et les faire tenir
à la D405 imposait trois compromis :

1. **le cadrage** — placée pour la saisie, elle voit le plateau de si biais que
   quatre carreaux tombent à 8-13 px de haut et que les deux gros sont coupés
   par le bord bas de l'image. Mesuré le 2026-08-26 : **0 détection sur 40**,
   quel que soit l'éclairage, de PWM 60 à 200 et de 600 à 5000 µs ;
2. **l'exposition** — il fallait basculer entre 600 µs pour les marqueurs et
   2200 µs pour le jeu de données, à chaque angle ;
3. **la lumière** — et donc changer le PWM, puis attendre les 3,5 s
   d'établissement de la lampe, deux fois par angle.

Une C920 en vue plongeante lève les trois d'un coup. Mesuré le 2026-09-03 :
**la lampe n'y change rien**, parce que son auto-exposition compense — de PWM
21 à 255 la luminance reste plate à ~107. Ce qui varie avec le PWM n'est pas la
clarté mais le reflet spéculaire sur les pastilles, et le meilleur score est
obtenu **lampe éteinte** (5,0 carreaux par image). Le chemin de rotation n'a
donc plus à toucher à la lampe du tout.

Le décodage, lui, ne peut pas passer par ``detect_markers`` du tracker. Les
pastilles du plateau sont imprimées **sans cadre noir** : le motif flotte au
milieu du blanc. OpenCV trouve très bien les quadrilatères — il les trouve tous
les six — mais les rejette au **contrôle de bordure**, avant même de regarder la
charge utile. Relu à la main sur les candidats rejetés, cinq carreaux sur six se
lisaient déjà juste, à 0-2 bits près.

D'où :func:`decoder` — quadrilatères par OpenCV, grille 4×4 lue directement,
bordure ignorée, appariement sur les seuls identifiants du plateau.

**Pourquoi tolérer deux bits faux est sûr.** La distance de Hamming minimale
entre les six carreaux employés vaut **6**, rotations comprises, et **8** entre
un carreau et ses propres rotations : il faudrait trois bits faux pour confondre
deux carreaux. C'est la restriction aux six identifiants qui achète cette
marge — sur les cinquante du dictionnaire la distance minimale tombe à 4, et
deux bits deviendraient dangereux.

Résultat mesuré, 30 images en 1280×720 : les **six** carreaux retrouvés, médiane
de **4 par image**, là où les réglages du projet n'en rendaient **aucun**.
"""
from __future__ import annotations

import logging
import math
import os
import time

import numpy as np

log = logging.getLogger("vtctl.webcam")

#: Comment reconnaître la webcam d'angle parmi les nœuds v4l2.
#:
#: **Jamais par numéro.** ``/dev/video0`` est la D405 dès qu'elle est routée :
#: la RealSense expose six nœuds (video0 à video5) et la C920 les deux suivants.
#: Mesuré le 2026-09-03 — mes premiers essais visaient l'index 0 et tombaient
#: sur la bonne caméra uniquement parce que la D405 n'était pas encore attachée.
#: Une numérotation qui dépend de l'ordre de branchement est un piège silencieux.
NOM_WEBCAM = "C920"

#: 1280×720 plutôt que 1920×1080. Le champ est **identique** — corrélation
#: 0,996 entre les deux, et recadrer la 1080p la fait tomber à 0,81, donc le
#: 720p est un simple sous-échantillonnage et non un recadrage, contrairement
#: aux modes de la D405. À 1080p le flux MJPEG sature la redirection USB de la
#: VM et sort des trames tronquées (« Corrupt JPEG data ») dès qu'on lit en
#: continu. À 720p les carreaux font encore ~35 px de côté, largement assez.
LARGEUR, HAUTEUR = 1280, 720

#: Exposition figée, en unités v4l2. Mesuré lampe à 200 : 78 → 4,8 carreaux par
#: image, 156 → 4,6, 312 → 2,0, 625 → **0**. Même loi que sur la D405 : à
#: luminance de marqueur égale, l'exposition la plus courte gagne, parce
#: qu'elle tue le reflet et non le signal.
EXPOSITION = 78

#: Mise au point figée. L'autofocus se cale sur l'objet du plateau, qui est plus
#: proche que les carreaux, et le laisse dériver d'une série à l'autre.
FOCUS = 0

#: Les carreaux réellement collés sur le plateau. Cette liste **est** ce qui
#: rend la tolérance à deux bits sûre : ne pas l'élargir sans refaire le calcul
#: de distance de Hamming.
MARQUEURS = (1, 2, 3, 4, 5, 6)

#: Bits faux tolérés par carreau. Voir l'en-tête : 2 est sûr pour six
#: identifiants, 1 le serait pour cinquante.
MAX_BITS = 2

#: Aire minimale d'un candidat, en pixels². Sous ce seuil la grille 4×4 n'a plus
#: assez de pixels par cellule pour être lue.
AIRE_MIN = 400

_REF: "dict[int, np.ndarray] | None" = None


def trouver_peripherique(nom: str = NOM_WEBCAM) -> "str | None":
    """
    Le nœud de capture de la webcam d'angle, cherché **par nom**.

    Rend le plus petit nœud dont ``/sys/class/video4linux/*/name`` contient
    ``nom`` : une UVC en expose plusieurs, le premier est celui qui capture,
    les suivants portent les métadonnées.
    """
    base = "/sys/class/video4linux"
    if not os.path.isdir(base):
        return None
    trouves = []
    for entree in os.listdir(base):
        if not entree.startswith("video"):
            continue
        try:
            with open(os.path.join(base, entree, "name"), encoding="utf-8") as f:
                if nom.lower() not in f.read().lower():
                    continue
        except OSError:
            continue
        with __import__("contextlib").suppress(ValueError):
            trouves.append((int(entree.removeprefix("video")), f"/dev/{entree}"))
    if not trouves:
        return None
    return sorted(trouves)[0][1]


def disponible(nom: str = NOM_WEBCAM) -> bool:
    """La webcam est-elle là ? Sinon l'angle retombe sur la D405."""
    return trouver_peripherique(nom) is not None


def _reference() -> "dict[int, np.ndarray]":
    """
    Les motifs 4×4 du dictionnaire, relus depuis les images rendues.

    Plutôt que ``bytesList`` : sa mise en forme a changé entre OpenCV 4 et 5, et
    la VM tourne en 5 quand le poste de travail est en 4.
    """
    global _REF  # noqa: PLW0603
    if _REF is not None:
        return _REF
    import cv2  # noqa: PLC0415

    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    out = {}
    for i in range(50):
        img = cv2.aruco.generateImageMarker(d, i, 120)
        g = np.zeros((4, 4), np.uint8)
        for r in range(4):
            for c in range(4):
                bloc = img[(r + 1) * 20 + 5:(r + 2) * 20 - 5,
                           (c + 1) * 20 + 5:(c + 2) * 20 - 5]
                g[r, c] = 1 if bloc.mean() > 127 else 0
        out[i] = g
    _REF = out
    return out


def _parametres():
    """Réglages du détecteur de quadrilatères. Le décodage se fait après."""
    import cv2  # noqa: PLC0415

    p = cv2.aruco.DetectorParameters()
    p.adaptiveThreshWinSizeMin = 3
    p.adaptiveThreshWinSizeMax = 45
    p.adaptiveThreshWinSizeStep = 6
    p.adaptiveThreshConstant = 11
    p.minMarkerPerimeterRate = 0.005
    p.polygonalApproxAccuracyRate = 0.05
    return p


def _lire_grille(inv, quad, taille: int = 20) -> np.ndarray:
    """Redresse un quadrilatère et rend sa grille 4×4, bordure ignorée."""
    import cv2  # noqa: PLC0415

    n = 6                                   # 4 cellules + une de bordure
    N = n * taille
    dst = np.array([[0, 0], [N, 0], [N, N], [0, N]], np.float32)
    H = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    w = cv2.warpPerspective(inv, H, (N, N))
    # Otsu par carreau : l'éclairage du plateau n'est pas uniforme, un seuil
    # global laisse les carreaux du fond sous le seuil des carreaux proches.
    _, b = cv2.threshold(w, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    grille = np.zeros((4, 4), np.uint8)
    for i in range(4):
        for j in range(4):
            bloc = b[(i + 1) * taille + 6:(i + 2) * taille - 6,
                     (j + 1) * taille + 6:(j + 2) * taille - 6]
            grille[i, j] = 1 if bloc.mean() > 127 else 0
    return grille


def decoder(gris, marqueurs=MARQUEURS, max_bits: int = MAX_BITS,
            aire_min: int = AIRE_MIN) -> tuple:
    """
    Les carreaux du plateau dans une image en niveaux de gris.

    Args:
        gris: image telle que la caméra la rend, **non inversée**.

    Returns:
        ``(coins, ids)`` au format d'``ArUcoTracker.detect_markers`` — une liste
        de ``(1, 4, 2)`` et un tableau ``(n, 1)`` — pour se substituer à lui
        sans que la suite du calcul d'angle ne change.
    """
    import cv2  # noqa: PLC0415

    ref = _reference()
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    inv = cv2.bitwise_not(gris)
    detecteur = cv2.aruco.ArucoDetector(d, _parametres())
    acceptes, ids, rejetes = detecteur.detectMarkers(inv)

    # Les rejetés comptent autant que les acceptés : c'est justement là que se
    # trouvent nos carreaux, écartés au contrôle de bordure.
    quads = [q.reshape(-1, 2) for q in list(acceptes) + list(rejetes)]
    trouves: dict = {}
    for q in quads:
        if cv2.contourArea(q.astype(np.float32)) < aire_min:
            continue
        grille = _lire_grille(inv, q)
        meilleur = None
        for rot in range(4):
            m = np.rot90(grille, rot)
            for i in marqueurs:
                err = int((m != ref[i]).sum())
                if meilleur is None or err < meilleur[0]:
                    meilleur = (err, i)
        err, i = meilleur
        if err <= max_bits and (i not in trouves or err < trouves[i][0]):
            trouves[i] = (err, q)

    if not trouves:
        return [], None
    coins = [trouves[i][1].reshape(1, 4, 2).astype(np.float32)
             for i in sorted(trouves)]
    tab = np.array([[i] for i in sorted(trouves)], dtype=np.int32)
    return coins, tab


def centre_par_carreaux(positions) -> "tuple | None":
    """
    Le centre de rotation, depuis les positions des carreaux d'une seule pose.

    Les six carreaux sont sur un même cercle autour de l'axe ; vus de biais ce
    cercle se projette en **ellipse**, dont le centre est celui de la rotation.
    Cinq points suffisent à ajuster une ellipse, d'où le repli sur un cercle
    en dessous.

    Moins précis que :func:`vt_tactile.plateau.calibrer_centre`, qui accumule
    une révolution entière — mais il n'exige pas de faire tourner le plateau,
    et il évite surtout de réemployer le centre de la D405, qui donnerait des
    angles faux sans que rien ne le signale.
    """
    import cv2  # noqa: PLC0415

    pts = np.asarray(positions, dtype=np.float32)
    if len(pts) >= 5:
        (cx, cy), _axes, _ang = cv2.fitEllipse(pts)
        return float(cx), float(cy)
    if len(pts) >= 3:
        # Kåsa : ajustement de cercle linéaire.
        x, y = pts[:, 0].astype(float), pts[:, 1].astype(float)
        A = np.c_[2 * x, 2 * y, np.ones(len(x))]
        sol, *_ = np.linalg.lstsq(A, x ** 2 + y ** 2, rcond=None)
        return float(sol[0]), float(sol[1])
    return None


class SourceWebcam:
    """
    Une caméra dédiée à l'angle du plateau, tous réglages figés.

    Se substitue au couple ``CameraOwner`` + ``tracker.detect_markers`` dans
    :class:`~vtctl.hw.angle.AngleMeter` : mêmes deux méthodes, mêmes formats.

        with SourceWebcam() as src:
            couleur, _p, _t = src.grab_aruco()
            coins, ids = src.detect_markers(couleur)
    """

    def __init__(self, peripherique: "str | None" = None,
                 largeur: int = LARGEUR, hauteur: int = HAUTEUR,
                 exposition: int = EXPOSITION, focus: int = FOCUS):
        self.peripherique = peripherique or trouver_peripherique()
        self.largeur, self.hauteur = largeur, hauteur
        self.exposition, self.focus = exposition, focus
        self.cap = None
        self.reglages: dict = {}

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def open(self) -> "SourceWebcam":
        import cv2  # noqa: PLC0415

        if self.cap is not None:
            return self
        if not self.peripherique:
            raise RuntimeError(f"aucune webcam « {NOM_WEBCAM} » parmi les nœuds v4l2")
        index = 0
        if self.peripherique.startswith("/dev/video"):
            with __import__("contextlib").suppress(ValueError):
                index = int(self.peripherique.removeprefix("/dev/video"))
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"webcam {self.peripherique} : ouverture impossible")
        # MJPEG et non YUYV : en 720p le flux non compressé dépasse la bande
        # passante de la redirection USB et rend des trames vertes tronquées.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.largeur)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.hauteur)
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
        cap.set(cv2.CAP_PROP_FOCUS, self.focus)
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)     # 1 = manuel sur la C920
        cap.set(cv2.CAP_PROP_EXPOSURE, self.exposition)
        self.cap = cap
        # Le capteur applique ses réglages avec un retard de quelques images :
        # les premières sortent encore sous les précédents.
        time.sleep(1.0)
        self.flush(8)
        self.reglages = {
            "peripherique": self.peripherique,
            "largeur": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "hauteur": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "exposition": self.exposition,
            "focus": self.focus,
        }
        log.info("webcam d'angle prête : %(largeur)sx%(hauteur)s, exposition "
                 "%(exposition)s, mise au point %(focus)s figées", self.reglages)
        return self

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self) -> "SourceWebcam":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def ouverte(self) -> bool:
        return self.cap is not None

    # ── Images ────────────────────────────────────────────────────────────────

    def flush(self, n: int = 4) -> None:
        """Jette les images en file. Le tampon v4l2 en retient plusieurs."""
        for _ in range(max(0, n)):
            self.cap.read()

    def grab_aruco(self):
        """
        Une image, au format qu'attend ``AngleMeter``.

        Returns:
            ``(couleur, None, t_perf)``. Pas de profondeur : ce n'en est pas la
            fonction, et l'annoncer ``None`` évite qu'un appelant la croie
            disponible.
        """
        if self.cap is None:
            raise RuntimeError("webcam fermée : appeler open() d'abord")
        ok, img = self.cap.read()
        if not ok or img is None:
            raise RuntimeError("webcam : aucune image")
        return img, None, time.perf_counter()

    # ── Détection ─────────────────────────────────────────────────────────────

    def detect_markers(self, couleur):
        """Même signature que ``ArUcoTracker.detect_markers``, décodage tolérant."""
        import cv2  # noqa: PLC0415

        gris = (couleur if couleur.ndim == 2
                else cv2.cvtColor(couleur, cv2.COLOR_BGR2GRAY))
        return decoder(gris)

    # ── Centre du plateau ─────────────────────────────────────────────────────

    def estimer_centre(self, n: int = 12) -> "tuple | None":
        """
        Le centre de rotation dans **cette** vue, accumulé sur ``n`` images.

        Le centre calibré du fichier vaut pour la D405 en 640×480 : réemployé
        ici il donnerait des angles faux en silence. On le réestime donc, et on
        moyenne la position de chaque carreau sur plusieurs images pour ne pas
        ajouter le bruit d'une détection isolée.
        """
        vus: dict = {}
        for _ in range(max(1, n)):
            try:
                couleur, _p, _t = self.grab_aruco()
            except RuntimeError:
                continue
            coins, ids = self.detect_markers(couleur)
            if ids is None:
                continue
            for k, brut in enumerate(ids):
                i = int(np.ravel(brut)[0])
                vus.setdefault(i, []).append(coins[k][0].mean(axis=0))
        if len(vus) < 3:
            log.warning("centre du plateau non estimable : %d carreau(x) vu(s)",
                        len(vus))
            return None
        moyennes = [np.mean(v, axis=0) for v in vus.values()]
        centre = centre_par_carreaux(moyennes)
        if centre:
            log.info("centre du plateau estimé à (%.1f, %.1f) sur %d carreaux",
                     centre[0], centre[1], len(moyennes))
        return centre

    # ── État ──────────────────────────────────────────────────────────────────

    def infos(self) -> dict:
        return {"ouverte": self.ouverte, "role": "angle du plateau",
                "decodage": f"tolerant, {MAX_BITS} bits, carreaux {list(MARQUEURS)}",
                **self.reglages}
