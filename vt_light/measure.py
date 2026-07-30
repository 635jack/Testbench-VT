#!/usr/bin/env python3
"""
Une condition d'acquisition -> un jeu complet de métriques.

Regroupé ici pour que tous les outils mesurent **exactement la même chose** : le
choix des réglages se fait en comparant des lignes de plusieurs balayages entre
elles, ce qui n'a de sens que si elles sont produites par le même code.
"""
import numpy as np

from . import metrics


def condition(cam, masks, detector, n_avg=3, n_marker_frames=None,
              with_depth=True):
    """
    Args:
        cam: :class:`vt_light.camera.D405` déjà réglée et vidée de sa file.
        masks: dict ``{"white": ..., "object": ...}``.
        n_avg: images moyennées pour les mesures de niveau. La moyenne réduit le
            bruit sans toucher au signal ; l'écart-type entre ces mêmes images
            fournit la mesure de bruit.
        n_marker_frames: images utilisées pour le taux de détection ArUco. Par
            défaut ``n_avg`` — mettre 15 pour une mesure fiable, la détection à
            10 mm étant très variable d'une image à l'autre.
    """
    n_marker_frames = n_marker_frames or n_avg
    n_frames = max(n_avg, n_marker_frames)
    colors, depths = cam.grab_stack(n_frames)
    mean = colors[:n_avg].astype(np.float32).mean(axis=0).astype(np.uint8)

    row = {}
    for name in ("white", "object"):
        mask = masks.get(name)
        if mask is None:
            continue
        for key, value in metrics.patch_stats(mean, mask).items():
            if key != "n_px":
                row[f"{name}_{key}"] = value
        # Le centile haut dit à quelle distance de l'écrêtage on se trouve, alors
        # que la moyenne peut rester basse pendant que les hautes lumières brûlent.
        if mask.any():
            row[f"{name}_p99"] = float(np.percentile(mean.max(axis=2)[mask], 99.0))

    row["frame_mean_lum"] = float(metrics.luminance(mean).mean())
    row["frame_sat_frac"] = metrics.saturation_fraction(mean)
    row["noise_white"] = metrics.temporal_noise(colors[:n_avg], masks.get("white"))
    row["noise_object"] = metrics.temporal_noise(colors[:n_avg], masks.get("object"))
    if row.get("white_mean_lum"):
        row["snr_white"] = row["white_mean_lum"] / max(1e-6, row["noise_white"])
    if row.get("object_mean_lum"):
        row["snr_object"] = row["object_mean_lum"] / max(1e-6, row["noise_object"])

    marker = metrics.marker_detection_rate(colors[:n_marker_frames], detector)
    row["markers_mean"] = marker["markers_mean"]
    row["markers_max"] = marker["markers_max"]
    row["markers_ever"] = "|".join(str(i) for i in marker["markers_ever"])

    if with_depth and depths is not None:
        obj = masks.get("object")
        if obj is not None:
            d = metrics.depth_stats(depths[-1], cam.depth_scale, cam.intrinsics,
                                    mask=obj, fit_plane=True)
            row["depth_fill_object"] = d.get("fill_ratio")
            row["depth_rms_object_mm"] = d.get("plane_rms_mm")
            row["depth_plane_inlier_frac"] = d.get("plane_inlier_frac")
            row["depth_rms_all_mm"] = d.get("plane_rms_all_mm")
            row["depth_median_mm"] = d.get("median_mm")
        frame = metrics.depth_stats(depths[-1], cam.depth_scale, cam.intrinsics)
        row["depth_fill_frame"] = frame.get("fill_ratio")

    return row, colors[-1], (depths[-1] if depths is not None else None)
