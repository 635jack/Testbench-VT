"""
VT-Tactile — lecture des capteurs tactiles de la main DH116.

Le décodage vit dans :mod:`vt_tactile.tpdo` et ne dépend d'aucune bibliothèque
constructeur. La mise en route du bus vit dans :mod:`vt_tactile.bus`, qui
n'utilise le SDK que pour monter EtherCAT et réveiller la main.

La table de découpage a été **mesurée sur la main**, pas lue dans le binaire du
SDK — voir l'en-tête de ``tpdo.py``.
"""

from .tpdo import (  # noqa: F401
    FINGERS,
    FRAME_MOTOR,
    FRAME_TACTILE,
    PALM,
    PALM_SILENT,
    ZONE_NAMES,
    TactileReader,
    TactileState,
    TpdoError,
    Zone,
    is_tactile,
)

__all__ = [
    "FINGERS", "FRAME_MOTOR", "FRAME_TACTILE", "PALM", "PALM_SILENT",
    "ZONE_NAMES", "TactileReader", "TactileState", "TpdoError", "Zone",
    "is_tactile",
]
