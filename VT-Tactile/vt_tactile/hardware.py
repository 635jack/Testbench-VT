#!/usr/bin/env python3
"""
hardware.py — topologie et bornes de la main DH116.

Que des données, aucune logique. La correspondance moteur → zone tactile a été
recoupée avec le découpage mesuré des trames : elles concordent.
"""
from __future__ import annotations

MOTOR_IDS = (1, 2, 3, 4, 5, 6)

MOTOR_NAMES = {
    1: "pouce_flexion",
    2: "pouce_pivot",
    3: "auriculaire",
    4: "annulaire",
    5: "majeur",
    6: "index",
}

THUMB_PIVOT = 2

#: Consigne du pivot qui amène le pouce en opposition, face au majeur et
#: dirigé vers l'auriculaire. Trouvée à l'œil au banc le 2026-07-31 : à 0 le
#: pouce est relevé vers le haut, hors du plan de préhension. Avec cette pose
#: le pouce entre réellement en contact avec l'objet et sa pulpe remonte de la
#: pression — alors même que sa flexion est en panne.
THUMB_OPPOSITION = 8000

#: Moteurs à exclure des fermetures et du homing. Vide depuis le 2026-08-26.
#:
#: La flexion du pouce y a figuré du 2026-07-27 au 2026-08-26. Sur la main
#: d'alors la panne était réelle et attestée côté SDK : ``get_now_status`` figé
#: à ``LST_HOMING``, ``position_reached`` jamais vrai. Ce qui a fait durer
#: l'exclusion après le changement de main, c'est un défaut de décodage — les
#: deux créneaux du pouce étaient lus 36 octets trop loin (voir
#: ``tpdo.MOTOR_ORDRE``), donc le pouce lisait zéro de toute façon et
#: l'exclusion semblait toujours fondée. Sur la main montée le 2026-08-25,
#: homing compris, les six moteurs atteignent leur consigne et la trame suit.
#:
#: Reste utile : ``Hand(moteurs_exclus=...)`` permet d'écarter un moteur au vol
#: sans toucher à ce fichier, si l'un d'eux tombe en cours de campagne.
BROKEN_MOTORS = ()

#: Moteurs qui referment la main. Le pivot du pouce n'en est pas un.
FLEXORS = tuple(m for m in MOTOR_IDS if m != THUMB_PIVOT)

#: Fléchisseurs réellement pilotables aujourd'hui.
WORKING_FLEXORS = tuple(m for m in FLEXORS if m not in BROKEN_MOTORS)

#: Moteur → préfixe des zones tactiles qu'il met en contact.
MOTOR_TO_ZONE = {1: "thumb", 3: "little", 4: "ring", 5: "middle", 6: "index"}

#: Zones surveillées pour un moteur donné : la pulpe touche en premier quand
#: l'objet repose à la base des doigts, le bout quand la phalange s'enroule.
def zones_of(motor: int) -> tuple[str, ...]:
    prefix = MOTOR_TO_ZONE.get(motor)
    if prefix is None:
        return ()
    # Le pouce et l'auriculaire n'ont qu'un capteur, sans pulpe distincte.
    if prefix in ("thumb", "little"):
        return (prefix,)
    return (f"{prefix}.pad", f"{prefix}.tip")


# ── Bornes ────────────────────────────────────────────────────────────────────

POSITION_OPEN = 0
#: Garde-fou mécanique de fermeture (le SDK accepte 10000, on n'y va pas).
POSITION_MAX = 8500

VELOCITY_OPEN = 10000
VELOCITY_CLOSE = 500

#: Courant maximum, en pour-mille. 1000 est le plafond du variateur.
FULL_CURRENT = 1000
#: Maintien après saisie : les moteurs calés sur un objet chauffent.
HOLD_CURRENT = 150
#: Approche d'un objet inconnu. Volontairement sous les seuils de contact
#: relevés (270-580 ‰) : le doigt cale contre l'objet au lieu de l'écraser.
APPROACH_CURRENT = 400

#: Courant au-delà duquel un doigt est considéré en appui, par moteur.
#: Valeurs relevées expérimentalement lors d'une campagne précédente ; servent
#: de filet quand le contact se produit hors zone instrumentée.
CURRENT_CONTACT = {1: 580, 2: 380, 3: 390, 4: 500, 5: 340, 6: 270}

#: Plafond de couple pour une saisie qui serre vraiment. APPROACH_CURRENT (400)
#: ne suffit pas : mesuré le 2026-08-19, les doigts s'arrêtent alors sur leur
#: propre plafond entre 3200 et 4200 counts sur 8500, sans jamais toucher
#: l'objet — et toutes les zones lisent zéro, ce qui imite une panne de capteur.
GRASP_CURRENT = 800
