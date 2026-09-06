#!/usr/bin/env python3
"""
Masques de mesure, construits **une seule fois** puis réutilisés tels quels.

C'est la précaution qui rend les balayages comparables entre eux : si on
resélectionnait « les pixels les plus clairs » à chaque réglage, le masque
suivrait le réglage et on mesurerait un mélange des deux effets. Ici les mêmes
pixels sont suivis d'un bout à l'autre.

Deux masques, deux rôles :

- ``white`` : le PLA blanc des marqueurs ArUco. Surface mate, quasi lambertienne,
  de réflectance neutre : c'est la **référence colorimétrique** et le témoin
  photométrique de la scène.
- ``object`` : l'objet posé sur le plateau. C'est lui qui **écrête** en premier —
  le PLA marbré et plus encore le PLA argenté renvoient bien plus que le
  marqueur. La contrainte de non-écrêtage se juge donc ici.
"""
import logging

import cv2
import numpy as np

from . import metrics

logger = logging.getLogger(__name__)


def object_mask(bgr, exclude=None, rel_threshold=0.45, min_area=500):
    """
    Isole l'objet éclairé posé sur le plateau.

    Le plateau et son support sont noirs, le fond est noir : l'objet est la seule
    grande tache claire. On seuille relativement au 99e centile de luminance
    plutôt qu'en absolu, pour que le même code marche du PLA marbré (très
    réfléchissant) au translucide (sombre).

    Args:
        exclude: masque booléen de pixels à retirer (typiquement le masque blanc).
    Returns:
        masque booléen, ou None si aucune tache assez grande.
    """
    lum = metrics.luminance(bgr)
    high = float(np.percentile(lum, 99.0))
    binary = (lum >= rel_threshold * high).astype(np.uint8)
    if exclude is not None:
        binary[exclude] = 0

    # Refermer les trous du motif marbré avant d'étiqueter, sinon la tache se
    # fragmente en dizaines de composantes.
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if n_labels <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    best = int(np.argmax(areas)) + 1
    if areas[best - 1] < min_area:
        return None
    mask = labels == best
    # Rogner le pourtour : les pixels de bord mélangent objet et fond noir.
    mask = cv2.erode(mask.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    return mask if mask.any() else None


def build(bgr, detector=None, invert_colors=True, bgr_markers=None):
    """
    Construit les deux masques.

    **Il faut deux images, pas une**, et c'est contre-intuitif. Les marqueurs de
    10 mm ne se décodent que sur un plateau étroit de luminance (voir
    ``tools/25_markers.py``) ; à l'éclairage qui montre bien l'objet, la détection
    échoue. Une construction sur une seule image bascule alors sur le repli
    « pixels les plus clairs », qui désigne l'objet lui-même : le masque blanc
    cesse d'être une référence neutre sans que rien ne le signale.

    Args:
        bgr: image au niveau clair, pour délimiter l'objet.
        bgr_markers: image prise dans la fenêtre de détection ArUco, pour le blanc
            de référence. À défaut, ``bgr`` est réutilisée avec le risque ci-dessus.
    Returns:
        (masks: dict, info: dict)
    """
    detector = detector or metrics.make_detector()
    marker_img = bgr_markers if bgr_markers is not None else bgr
    white, n_markers = metrics.white_mask_from_markers(marker_img, detector,
                                                      invert_colors)
    source = "marqueurs ArUco"
    if white is None or white.sum() < 150:
        white = metrics.brightest_mask(marker_img, top_percentile=99.5)
        source = "repli : pixels les plus clairs — NON neutre si un objet est posé"
        n_markers = 0
    obj = object_mask(bgr, exclude=white)

    info = {
        "white_source": source,
        "white_n_markers": int(n_markers),
        "white_n_px": int(white.sum()),
        "white_from_separate_frame": bgr_markers is not None,
        "object_n_px": int(obj.sum()) if obj is not None else 0,
    }
    return {"white": white, "object": obj}, info


def save(path, masks, info=None):
    payload = {k: v for k, v in masks.items() if v is not None}
    np.savez_compressed(path, **payload)
    logger.info("Masques -> %s (%s)", path, ", ".join(payload))
    return path


def load(path):
    data = np.load(path)
    return {key: data[key].astype(bool) for key in data.files}


def overlay(bgr, masks):
    """Aperçu de contrôle : blanc en rouge, objet en vert."""
    out = bgr.copy()
    if masks.get("object") is not None:
        out[masks["object"]] = (0, 255, 0)
    if masks.get("white") is not None:
        out[masks["white"]] = (0, 0, 255)
    return out
