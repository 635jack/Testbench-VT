#!/usr/bin/env python3
"""
Rend ``vtctl`` et ses dépôts voisins importables depuis la suite de tests.

Aucun test n'a besoin de matériel : ``pyrealsense2`` et ``pyserial`` restent
absents, et les modules de VT-Light qui les utilisent les importent déjà sous
``try/except``. C'est ce qui permet de valider le protocole sur un poste de
travail, sans immobiliser le banc.
"""
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))

from vtctl import config  # noqa: E402

config.install_paths()
