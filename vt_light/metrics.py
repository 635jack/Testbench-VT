#!/usr/bin/env python3
"""
Métriques d'image servant à *choisir* les réglages, plutôt qu'à les deviner.

Chaque métrique répond à une question précise :

- ``saturation_fraction``   : est-ce que j'écrête ? (information définitivement perdue)
- ``patch_stats``           : le blanc est-il neutre, et à quel niveau ?
- ``temporal_noise``        : quel rapport signal/bruit reste-t-il en basse lumière ?
- ``count_markers``         : l'asservissement angulaire survit-il à ce réglage ?
- ``depth_stats``           : la stéréo passive tient-elle encore ? (D405 sans projecteur)
"""
import cv2
import numpy as np

#: Seuil d'écrêtage. 254 et non 255 : le pipeline couleur de la D405 applique un
#: gamma qui laisse rarement atteindre exactement 255 alors que le capteur, lui,
#: est déjà saturé.
SAT_LEVEL = 254

#: Coefficients de luminance BT.601, cohérents avec cv2.COLOR_BGR2GRAY.
_LUMA = np.array([0.114, 0.587, 0.299], dtype=np.float32)  # ordre B, G, R


def luminance(bgr):
    """Luminance en float32, même échelle que les canaux (0-255)."""
    return (bgr.astype(np.float32) * _LUMA).sum(axis=2)


def saturation_fraction(bgr, level=SAT_LEVEL):
    """Fraction de pixels dont **au moins un** canal est écrêté."""
    return float((bgr.max(axis=2) >= level).mean())


def clipped_fraction_in(bgr, mask, level=SAT_LEVEL):
    """Fraction de pixels écrêtés à l'intérieur d'un masque."""
    if mask is None or not mask.any():
        return float("nan")
    return float((bgr.max(axis=2)[mask] >= level).mean())


# --------------------------------------------------------------- blanc de référence

def patch_stats(bgr, mask):
    """
    Statistiques colorimétriques sur une zone supposée neutre (PLA blanc).

    ``r_over_g`` et ``b_over_g`` valent 1 exactement quand la balance des blancs
    est juste. ``neutrality_err`` est le pire des deux écarts : c'est le scalaire
    à minimiser lors du balayage de balance des blancs.
    """
    if mask is None or not mask.any():
        return {"n_px": 0}
    px = bgr[mask].astype(np.float32)
    b, g, r = px[:, 0].mean(), px[:, 1].mean(), px[:, 2].mean()
    lum = float((px * _LUMA).sum(axis=1).mean())
    eps = 1e-6
    r_over_g = float(r / (g + eps))
    b_over_g = float(b / (g + eps))
    return {
        "n_px": int(mask.sum()),
        "mean_b": float(b), "mean_g": float(g), "mean_r": float(r),
        "mean_lum": lum,
        "r_over_g": r_over_g,
        "b_over_g": b_over_g,
        "neutrality_err": float(max(abs(r_over_g - 1.0), abs(b_over_g - 1.0))),
        "clipped_frac": clipped_fraction_in(bgr, mask),
    }


def temporal_noise(color_stack, mask=None):
    """
    Écart-type temporel moyen, en niveaux de gris (0-255).

    Mesuré sur une pile d'images d'une scène immobile : tout ce qui varie est du
    bruit. C'est la seule mesure de bruit honnête ici — un écart-type spatial
    mélangerait bruit et texture de l'objet.
    """
    lum = np.stack([luminance(c) for c in color_stack])
    std = lum.std(axis=0, ddof=1) if len(lum) > 1 else np.zeros(lum.shape[1:], np.float32)
    if mask is not None and mask.any():
        return float(std[mask].mean())
    return float(std.mean())


# ------------------------------------------------------------------------- ArUco

def make_detector(dict_name="DICT_4X4_50"):
    """
    Détecteur réglé comme celui de ``Control_Turtable_IR/aruco_tracker.py``,
    pour que le taux de détection mesuré ici prédise bien celui de
    l'asservissement réel. Les marqueurs font 10 mm, soit 4 à 5 pixels par
    cellule : les paramètres permissifs ci-dessous sont nécessaires.
    """
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dict_name))
    params = cv2.aruco.DetectorParameters()
    params.adaptiveThreshWinSizeMin = 3
    params.adaptiveThreshWinSizeMax = 31
    params.adaptiveThreshWinSizeStep = 4
    params.adaptiveThreshConstant = 7
    params.minMarkerPerimeterRate = 0.015
    params.perspectiveRemovePixelPerCell = 8
    params.perspectiveRemoveIgnoredMarginPerCell = 0.13
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return cv2.aruco.ArucoDetector(dictionary, params)


def detect_markers(bgr, detector, invert_colors=True):
    """
    Returns:
        (corners, ids: liste triée d'entiers)

    ``invert_colors=True`` parce que les marqueurs du plateau sont imprimés en
    couleurs inversées (PLA blanc là où l'ArUco standard est noir).
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if invert_colors:
        gray = cv2.bitwise_not(gray)
    corners, ids, _ = detector.detectMarkers(gray)
    id_list = sorted(int(i) for i in ids.flatten()) if ids is not None else []
    return corners, id_list


def count_markers(bgr, detector, invert_colors=True):
    return len(detect_markers(bgr, detector, invert_colors)[1])


def marker_detection_rate(color_stack, detector, invert_colors=True):
    """
    Taux de détection sur une pile d'images, et non un comptage sur une seule.

    Avec des marqueurs de 10 mm, la détection est structurellement marginale : sur
    une image isolée le nombre trouvé varie de 0 à 2 sans que rien n'ait changé.
    Un comptage unique ne permet donc pas de conclure qu'un réglage est meilleur
    qu'un autre — il faut une moyenne et un taux par identifiant.

    Returns:
        dict avec le nombre moyen, le maximum, et le taux de présence par ID.
    """
    counts, seen = [], {}
    for color in color_stack:
        ids = detect_markers(color, detector, invert_colors)[1]
        counts.append(len(ids))
        for marker_id in ids:
            seen[marker_id] = seen.get(marker_id, 0) + 1
    n = max(1, len(counts))
    return {
        "markers_mean": float(np.mean(counts)),
        "markers_max": int(np.max(counts)) if counts else 0,
        "markers_ever": sorted(seen),
        "marker_rates": {int(k): v / n for k, v in sorted(seen.items())},
    }


def white_mask_from_markers(bgr, detector, invert_colors=True,
                            bright_percentile=60.0, erode_px=1):
    """
    Construit un masque de **PLA blanc** à partir des marqueurs détectés.

    Les marqueurs étant imprimés en couleurs inversées, environ 78 % de leur
    surface est du PLA blanc (toute la bordure plus la moitié des cellules
    internes). On garde donc, à l'intérieur de chaque quadrilatère détecté, les
    pixels les plus lumineux — ce sont ceux du PLA blanc.

    Returns:
        (mask booléen, nombre de marqueurs utilisés)
    """
    corners, ids = detect_markers(bgr, detector, invert_colors)
    if not ids:
        return None, 0
    quads = np.zeros(bgr.shape[:2], np.uint8)
    for quad in corners:
        cv2.fillConvexPoly(quads, quad.reshape(-1, 2).astype(np.int32), 255)
    if erode_px > 0:
        # Les bords du marqueur sont flous et mélangent blanc et noir : on rogne.
        quads = cv2.erode(quads, np.ones((2 * erode_px + 1,) * 2, np.uint8))
    inside = quads > 0
    if not inside.any():
        return None, 0
    lum = luminance(bgr)
    threshold = np.percentile(lum[inside], bright_percentile)
    mask = inside & (lum >= threshold)
    return mask, len(ids)


def brightest_mask(bgr, top_percentile=99.0, roi=None):
    """
    Repli quand aucun marqueur n'est détecté : les pixels les plus lumineux.

    Valable **plateau vide uniquement** — le PLA blanc des marqueurs est alors
    l'objet le plus clair du champ. Avec un objet en PLA argenté posé dessus, ses
    reflets spéculaires seraient plus brillants et fausseraient la référence.
    """
    lum = luminance(bgr)
    inside = np.ones(lum.shape, bool)
    if roi is not None:
        x, y, w, h = roi
        inside[:] = False
        inside[y:y + h, x:x + w] = True
    threshold = np.percentile(lum[inside], top_percentile)
    return inside & (lum >= threshold)


# --------------------------------------------------------------------- profondeur

def depth_stats(depth_u16, depth_scale, intrinsics, mask=None, fit_plane=True):
    """
    Qualité de la profondeur, mesurée **sur une surface plane connue**.

    - ``fill_ratio``  : fraction de pixels ayant une mesure. La D405 étant en
      stéréo *passive*, ce nombre chute quand la lumière baisse ou que la surface
      manque de texture — c'est exactement l'effet qu'on veut quantifier.
    - ``plane_rms_mm``: dispersion autour du plan des moindres carrés. **N'a de
      sens que si la zone est effectivement plane** : passer ici une ROI
      contenant tout l'objet mesurerait sa géométrie et non le bruit. Le masque
      objet du banc ne retient que la face supérieure du cube, qui est plane.

    ``mask=None`` retombe sur le quart central de l'image, utile seulement pour un
    remplissage global — le plan n'y veut rien dire.
    """
    if depth_u16 is None:
        return {}
    if mask is None:
        h, w = depth_u16.shape
        mask = np.zeros((h, w), bool)
        mask[h // 4:3 * h // 4, w // 4:3 * w // 4] = True
        fit_plane = False

    inside = mask & (depth_u16 > 0)
    out = {"fill_ratio": float(inside.sum() / max(1, mask.sum())),
           "n_px": int(mask.sum())}
    if inside.sum() < 200:
        out["plane_rms_mm"] = float("nan")
        out["median_mm"] = float("nan")
        return out

    vv, uu = np.nonzero(inside)
    z = depth_u16[inside].astype(np.float64) * depth_scale           # mètres
    out["median_mm"] = float(np.median(z) * 1000.0)
    if not fit_plane:
        out["plane_rms_mm"] = float("nan")
        return out

    X = (uu - intrinsics["cx"]) * z / intrinsics["fx"]
    Y = (vv - intrinsics["cy"]) * z / intrinsics["fy"]

    # Plan z = a.X + b.Y + c par moindres carrés ; le résidu est la rugosité.
    A = np.column_stack([X, Y, np.ones_like(X)])
    coef, *_ = np.linalg.lstsq(A, z, rcond=None)
    residual = z - A @ coef
    # Écarter les 1 % extrêmes : quelques appariements stéréo aberrants suffisent
    # à multiplier l'écart-type par dix et masqueraient l'effet cherché.
    keep = np.abs(residual) <= np.percentile(np.abs(residual), 99.0)
    out["plane_rms_mm"] = float(residual[keep].std() * 1000.0)
    out["outlier_frac"] = float(1.0 - keep.mean())
    return out
