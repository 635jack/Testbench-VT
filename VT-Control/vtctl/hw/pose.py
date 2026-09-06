#!/usr/bin/env python3
"""
pose.py — aider à replacer la caméra, en direct.

La caméra bouge. Elle est sur un bras, on la pousse en changeant d'objet, et
rien dans le jeu de données ne le signale : le centre du plateau est stocké **en
pixels**, donc une caméra déplacée de quelques centimètres rend tous les angles
faux sans qu'aucune alarme ne se déclenche.

Pire, la panne se déguise. Le 2026-08-23, après un déplacement, le détecteur
trouvait encore les quadrilatères mais lisait le motif de travers et rendait des
identifiants arbitraires — 12, 17, 26, 34 au lieu de 1 à 6. Puis, la caméra
descendue encore, plus rien du tout, à **toutes** les expositions de 2200 à
60000 µs. Deux symptômes différents pour une seule cause géométrique.

Ce module ne corrige rien : il **montre**, en continu, pendant qu'on bouge la
caméra à la main. Trois chiffres suffisent à décider :

* combien de carreaux du plateau se décodent — c'est le seul critère qui compte ;
* leur taille apparente, en pixels de périmètre — en dessous d'un seuil, le
  motif n'a plus assez de pixels par case pour être lu ;
* l'aplatissement de la couronne, qui donne l'élévation de la caméra.
  ``FONCTIONNEMENT.md`` en demande 50° pour être à l'aise ; à 29,5° on ne voit
  déjà que deux carreaux à la fois.
"""
from __future__ import annotations

import logging
import math
import time

import numpy as np

log = logging.getLogger("vtctl.pose")

#: Périmètre apparent, en pixels, en dessous duquel un carreau de 4x4 cases plus
#: sa bordure n'a plus assez de pixels par case pour se décoder de façon fiable.
#: Un carreau se décode bien au-delà de ~120 px de périmètre, soit 30 px de côté.
PERIMETRE_LISIBLE = 120.0

#: Élévation en dessous de laquelle l'incidence devient rasante au point que la
#: moitié arrière du plateau ne rend plus rien.
ELEVATION_CONFORTABLE = 35.0

#: Aplatissement toléré : rapport largeur/hauteur de la boîte englobante d'un
#: carreau. Un carreau **est un carré** ; vu de biais il se projette en
#: rectangle, et le rapport mesure directement l'incidence.
#:
#: Mesuré le 2026-08-23, caméra trop basse : rapports de **1,5 à 3,1**, et
#: aucun carreau décodable à aucune exposition entre 2200 et 60000 µs. Avec
#: 27 px de haut pour six cases de motif, il reste 4,5 pixels par case — la
#: limite — et 8 px de haut ne laissent plus rien à lire.
#:
#: C'est la seule mesure qui fonctionne **quand rien ne se décode**, donc la
#: seule utilisable pour guider le replacement.
APLATISSEMENT_MAX = 1.35

#: Hauteur apparente minimale d'un carreau, en pixels. Six cases de motif plus
#: la bordure demandent au moins quatre pixels par case pour se lire.
HAUTEUR_MIN_PX = 26


class EtatPose:
    """Ce que la caméra voit du plateau, à un instant."""

    __slots__ = ("table", "hors_table", "perimetres", "positions", "centre",
                 "luminance", "p99", "elevation", "aplatissement", "hauteur_px",
                 "taches")

    def __init__(self, table, hors_table, perimetres, positions, centre,
                 luminance, p99, elevation, aplatissement=None,
                 hauteur_px=None, taches=0):
        self.table = table
        self.hors_table = hors_table
        self.perimetres = perimetres
        self.positions = positions
        self.centre = centre
        self.luminance = luminance
        self.p99 = p99
        self.elevation = elevation
        #: Rapport largeur/hauteur médian des taches claires, décodées ou non.
        self.aplatissement = aplatissement
        #: Hauteur apparente médiane, en pixels.
        self.hauteur_px = hauteur_px
        #: Nombre de taches claires de la taille d'un carreau.
        self.taches = taches

    @property
    def utilisable(self) -> bool:
        """
        Au moins deux carreaux du plateau décodés, et une géométrie saine.

        Deux et non un : avec un seul carreau l'écart-type de l'angle passe de
        0,004° à 2,2°, et la vitesse — qui en est la dérivée — devient si
        bruitée que l'asservissement freine au hasard.
        """
        if len(self.table) < 2:
            return False
        if self.aplatissement is not None and self.aplatissement > APLATISSEMENT_MAX:
            return False
        return self.perimetre_median >= PERIMETRE_LISIBLE

    @property
    def perimetre_median(self) -> float:
        return float(np.median(self.perimetres)) if self.perimetres else 0.0

    def conseil(self) -> str:
        """
        Que faire, maintenant, pour améliorer la situation.

        L'ordre des vérifications est celui de l'utilité : la géométrie d'abord,
        parce qu'une pose rasante ne se rattrape par aucun réglage d'image, et
        qu'on perd des heures à régler l'exposition d'un problème mécanique.
        """
        # La géométrie prime : elle se mesure même quand rien ne se décode.
        if self.aplatissement is not None and self.aplatissement > APLATISSEMENT_MAX:
            return (f"carreaux vus en rectangles {self.aplatissement:.1f}:1 alors "
                    f"que ce sont des carrés → caméra trop basse, la RELEVER "
                    f"(viser moins de {APLATISSEMENT_MAX:.2f}:1)")
        if self.hauteur_px is not None and 0 < self.hauteur_px < HAUTEUR_MIN_PX:
            return (f"carreaux hauts de {self.hauteur_px:.0f} px, il en faut "
                    f"{HAUTEUR_MIN_PX} → RAPPROCHER la caméra")
        if not self.table and self.hors_table:
            return ("motifs lus de travers → la caméra est trop rasante : "
                    "la RELEVER")
        if not self.table:
            if self.p99 >= 250:
                return "image écrêtée → baisser l'exposition ou la lampe"
            if self.p99 <= 60:
                return "image trop sombre → monter l'exposition ou la lampe"
            if not self.taches:
                return "rien qui ressemble à un carreau → le plateau est-il dans le champ ?"
            return "aucun carreau → RELEVER et RECULER la caméra"
        if self.perimetre_median < PERIMETRE_LISIBLE:
            return (f"carreaux trop petits ({self.perimetre_median:.0f} px de "
                    f"périmètre, il en faut {PERIMETRE_LISIBLE:.0f}) → "
                    f"RAPPROCHER la caméra")
        if self.elevation is not None and self.elevation < ELEVATION_CONFORTABLE:
            return (f"élévation {self.elevation:.0f}° → RELEVER pour voir "
                    f"plus de carreaux à la fois")
        if len(self.table) < 2:
            return "un seul carreau vu → l'angle sera bruité, RELEVER un peu"
        return "pose correcte — recalibrer le centre avant d'acquérir"

    def ligne(self) -> str:
        """Une ligne d'affichage, pour la boucle en direct."""
        marque = "OK " if self.utilisable else "-- "
        table = ",".join(str(i) for i in sorted(self.table)) or "aucun"
        hors = (f" +{len(self.hors_table)} aberrants" if self.hors_table else "")
        apl = "—" if self.aplatissement is None else f"{self.aplatissement:.2f}:1"
        haut = "—" if self.hauteur_px is None else f"{self.hauteur_px:.0f}px"
        return (f"{marque} carreaux {table:<12}{hors:<16} "
                f"forme {apl:>7} haut {haut:>6}  "
                f"lum {self.luminance:5.1f} p99 {self.p99:3.0f}   {self.conseil()}")

    def to_dict(self) -> dict:
        return {
            "carreaux_plateau": sorted(self.table),
            "identifiants_hors_table": sorted(self.hors_table),
            "perimetre_median_px": round(self.perimetre_median, 1),
            "aplatissement": None if self.aplatissement is None else round(self.aplatissement, 2),
            "hauteur_carreau_px": None if self.hauteur_px is None else round(self.hauteur_px, 1),
            "taches_candidates": self.taches,
            "elevation_deg": None if self.elevation is None else round(self.elevation, 1),
            "luminance": round(self.luminance, 1),
            "p99": round(self.p99, 0),
            "centre_calibre_px": list(self.centre) if self.centre else None,
            "utilisable": self.utilisable,
            "conseil": self.conseil(),
        }


def observer(camera, meter, n: int = 8) -> EtatPose:
    """
    Regarde ``n`` images et rend ce que la caméra voit du plateau.

    Args:
        camera: le ``CameraOwner``, déjà ouvert.
        meter: l'``AngleMeter``, déjà ouvert — on lui emprunte son détecteur.

    Plusieurs images plutôt qu'une : la détection est intermittente, et juger
    une pose sur une seule image ne dit rien. C'est la même raison qui fait
    mesurer l'angle sur une centaine.
    """
    import cv2  # noqa: PLC0415

    tracker = meter.tracker
    connus = set(tracker.marker_angles)
    table, hors, perims, positions = set(), set(), [], {}
    lum, p99, taches = [], [], []
    # Deux sources pour la forme, et l'ordre de préférence compte : les coins
    # d'un carreau **décodé** disent sa géométrie exactement, les taches claires
    # ne font que l'approcher. On ne se rabat sur les secondes que faute des
    # premiers — c'est-à-dire précisément quand plus rien ne se décode, le seul
    # cas où l'on n'a pas le choix.
    formes_decodees, hauteurs_decodees = [], []
    formes_taches, hauteurs_taches = [], []

    for _ in range(max(1, n)):
        couleur, _prof, _t = camera.grab_aruco()
        gris = cv2.cvtColor(couleur, cv2.COLOR_BGR2GRAY)
        lum.append(float(gris.mean()))
        p99.append(float(np.percentile(gris, 99)))

        f, h, t = _taches_claires(gris)
        formes_taches += f
        hauteurs_taches += h
        taches.append(t)

        coins, ids = tracker.detect_markers(couleur)
        if ids is None or not len(ids):
            continue
        for i, brut in enumerate(ids):
            mid = int(np.ravel(brut)[0])
            pts = coins[i][0]
            if mid in connus:
                table.add(mid)
                perims.append(float(cv2.arcLength(pts, True)))
                positions.setdefault(mid, []).append(
                    (float(np.mean(pts[:, 0])), float(np.mean(pts[:, 1]))))
                forme, haut = _forme_du_quadrilatere(pts)
                formes_decodees.append(forme)
                hauteurs_decodees.append(haut)
            else:
                hors.add(mid)

    formes = formes_decodees or formes_taches
    hauteurs = hauteurs_decodees or hauteurs_taches
    return EtatPose(
        table, hors, perims, positions, tracker.turntable_center,
        float(np.mean(lum)), float(np.mean(p99)),
        _elevation(positions, tracker.turntable_center),
        aplatissement=float(np.median(formes)) if formes else None,
        hauteur_px=float(np.median(hauteurs)) if hauteurs else None,
        taches=int(np.median(taches)) if taches else 0)


def _forme_du_quadrilatere(pts):
    """
    Aplatissement et hauteur d'un carreau décodé, depuis ses quatre coins.

    On mesure les **côtés** plutôt que la boîte englobante : un carré tourné de
    45° a une boîte englobante carrée, ce qui masquerait complètement
    l'aplatissement. Le rapport du plus long côté au plus court, lui, ne dépend
    pas de l'orientation dans l'image.
    """
    cotes = [float(np.hypot(*(pts[(i + 1) % 4] - pts[i]))) for i in range(4)]
    court = max(min(cotes), 1e-6)
    return max(cotes) / court, court


def _taches_claires(gris):
    """
    Mesure la forme des carreaux **sans les décoder**.

    C'est le point décisif de ce module : quand la caméra est trop rasante, plus
    rien ne se décode, et tous les indicateurs tirés de la détection tombent à
    zéro — on ne sait alors plus si c'est la lumière, le cadrage ou la pose. Les
    carreaux restent pourtant des taches claires bien visibles, et leur boîte
    englobante suffit à trancher : **un carré vu en rectangle 3:1 dit que la
    caméra est trop basse**, quelle que soit l'exposition.

    Returns:
        ``(rapports largeur/hauteur, hauteurs en px, nombre de taches)``.
    """
    import cv2  # noqa: PLC0415

    # Seuil relatif : la scène va du quasi-noir au surexposé selon l'éclairage,
    # et un seuil absolu ne survivrait pas à ce balayage.
    plancher = max(20.0, float(np.percentile(gris, 99)) * 0.45)
    _r, binaire = cv2.threshold(gris, plancher, 255, cv2.THRESH_BINARY)
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(binaire, 8)

    formes, hauteurs = [], []
    for i in range(1, n):
        aire = stats[i, cv2.CC_STAT_AREA]
        w, h = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        # Fenêtre de taille : au-dessous c'est du bruit, au-dessus c'est l'objet
        # ou la main, qui ne renseignent en rien sur l'incidence des carreaux.
        if not (60 <= aire <= 3000) or min(w, h) < 3:
            continue
        formes.append(max(w, h) / max(min(w, h), 1))
        hauteurs.append(float(min(w, h)))
    return formes, hauteurs, len(formes)


def _elevation(positions: dict, centre) -> "float | None":
    """
    Élévation de la caméra, par l'aplatissement de la couronne de carreaux.

    Le plateau est un cercle ; vu de biais il se projette en ellipse, et le
    rapport des demi-axes vaut le sinus de l'élévation. Il faut au moins trois
    carreaux à des azimuts différents pour que le rapport ait un sens — avec
    moins, on préfère ne rien annoncer plutôt qu'un chiffre inventé.
    """
    if centre is None or len(positions) < 3:
        return None
    cx, cy = centre
    dx = [abs(p[0] - cx) for pts in positions.values() for p in pts]
    dy = [abs(p[1] - cy) for pts in positions.values() for p in pts]
    if not dx or max(dx) <= 1e-6:
        return None
    rapport = max(dy) / max(dx)
    return float(math.degrees(math.asin(min(1.0, rapport))))


def annoter(camera, meter, chemin: str) -> dict:
    """
    Écrit une image annotée : carreaux entourés, centre calibré marqué.

    Sert à regarder ce que la caméra voit sans être devant le banc — un
    diagnostic à distance, ce que la seule ligne de texte ne donne pas.
    """
    import cv2  # noqa: PLC0415

    couleur, _prof, _t = camera.grab_aruco()
    coins, ids = meter.tracker.detect_markers(couleur)
    image = couleur.copy()
    if ids is not None and len(ids):
        cv2.aruco.drawDetectedMarkers(image, coins, ids)
    centre = meter.tracker.turntable_center
    if centre:
        cx, cy = int(round(centre[0])), int(round(centre[1]))
        cv2.drawMarker(image, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 26, 2)
        cv2.putText(image, "centre calibre", (cx + 14, cy - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.imwrite(chemin, image)
    return {"chemin": chemin,
            "marqueurs": [] if ids is None else sorted(int(x) for x in ids.ravel())}


def suivre(camera, meter, secondes: float, periode: float = 1.2,
           afficher=print) -> EtatPose:
    """
    Boucle d'aide au placement : on bouge la caméra, la ligne se met à jour.

    C'est le mode qui sert vraiment. Un contrôle ponctuel dit que la pose est
    mauvaise ; celui-ci dit si ce qu'on vient de faire l'améliore.
    """
    fin = time.time() + secondes
    dernier = None
    while time.time() < fin:
        dernier = observer(camera, meter, n=6)
        afficher(dernier.ligne())
        time.sleep(periode)
    return dernier
