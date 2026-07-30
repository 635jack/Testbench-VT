#!/usr/bin/env python3
"""Écriture des résultats : un CSV par balayage, un JSON pour le profil retenu."""
import csv
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "results")


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_csv(path, rows, fieldnames=None):
    """Écrit une liste de dicts. Les colonnes suivent l'union des clés, en
    conservant l'ordre d'apparition."""
    if not rows:
        logger.warning("Aucune ligne à écrire dans %s", path)
        return path
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("%d lignes -> %s", len(rows), path)
    return path


def save_json(path, payload):
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    logger.info("Écrit %s", path)
    return path


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
