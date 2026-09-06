"""
Journal des étalonnages de la cinématique.

Le facteur counts/radian n'est pas une propriété du modèle de main : il se
mesure, et il peut bouger — jeu qui s'installe, courroie qui se détend,
exemplaire remplacé. Le garder en mémoire vive, comme c'était le cas, revient
à le remesurer à chaque démarrage et à ne jamais savoir s'il a dérivé.

Le journal est en **ajout seul** : chaque étalonnage s'écrit à la suite, avec
son horodatage et les points qui l'ont produit. On ne réécrit jamais une
entrée. C'est ce qui permet de répondre à la seule question qui compte —
« est-ce que ça bouge dans le temps ? » — au lieu de constater un jour un
facteur différent sans savoir depuis quand.

    from vtctl.store import etalonnage
    etalonnage.enregistrer(resultat, main="DH116 #2")
    etalonnage.dernier()["counts_par_radian"]
    etalonnage.derive()
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Nom du journal dans le dossier d'état.
NOM = "etalonnages.jsonl"

#: Écart relatif au-delà duquel une dérive mérite d'être signalée. 2 % de
#: 7162 counts/rad font 0.3° sur une course de 80° : en deçà, c'est le bruit
#: de mesure, au-delà la main a changé.
SEUIL_DERIVE = 0.02


def chemin(dossier: "Path | None" = None) -> Path:
    """Où vit le journal. ``VT_ETAT`` le déplace, pour les tests."""
    if dossier is not None:
        return Path(dossier) / NOM
    racine = os.environ.get("VT_ETAT")
    base = Path(racine) if racine else Path.home() / ".vt-control"
    base.mkdir(parents=True, exist_ok=True)
    return base / NOM


def enregistrer(resultat: dict, main: str = "", note: str = "",
                dossier: "Path | None" = None) -> dict:
    """
    Ajoute un étalonnage au journal.

    Args:
        resultat: ce que rend :func:`vtctl.hw.cinematique.etalonner`.
        main: de quel exemplaire il s'agit. Sans cette mention, deux
            étalonnages qui diffèrent sont indiscernables d'une dérive et d'un
            changement de main — et c'est arrivé.
        note: ce que l'opérateur veut retenir de cette séance.

    Returns:
        l'entrée écrite.
    """
    entree = {
        "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "t_unix": round(time.time(), 3),
        "main": main,
        "note": note,
        "counts_par_radian": resultat.get("counts_par_radian"),
        "dispersion": resultat.get("dispersion"),
        "points": resultat.get("points", []),
        "hypothese_par_defaut": resultat.get("hypothese_par_defaut"),
    }
    f = chemin(dossier)
    # Ouverture en ajout et ``fsync`` : un étalonnage dure plusieurs minutes de
    # mouvements de main, le reperdre sur une coupure serait bête.
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entree, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entree


def historique(dossier: "Path | None" = None) -> list:
    """Tous les étalonnages, du plus ancien au plus récent."""
    f = chemin(dossier)
    if not f.exists():
        return []
    entrees = []
    for ligne in f.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            entrees.append(json.loads(ligne))
        except json.JSONDecodeError:
            # Une ligne tronquée par une coupure ne doit pas rendre tout le
            # journal illisible : on la saute et on garde le reste.
            continue
    return entrees


def dernier(main: str = "", dossier: "Path | None" = None) -> "dict | None":
    """
    Le dernier étalonnage, éventuellement restreint à un exemplaire.

    C'est celui que le banc reprend au démarrage. Restreindre par ``main``
    évite de repartir sur le facteur d'une main qu'on a démontée.
    """
    entrees = [e for e in historique(dossier)
               if not main or e.get("main") == main]
    return entrees[-1] if entrees else None


def derive(main: str = "", dossier: "Path | None" = None) -> dict:
    """
    Le facteur a-t-il bougé depuis le premier étalonnage ?

    Returns:
        ``{"n", "premier", "dernier", "ecart_relatif", "significative",
        "etendue_relative"}``. ``significative`` compare à
        :data:`SEUIL_DERIVE`. ``n < 2`` rend ``significative`` faux : on ne
        conclut pas à une dérive sur un seul point.
    """
    entrees = [e for e in historique(dossier)
               if not main or e.get("main") == main]
    valeurs = [e["counts_par_radian"] for e in entrees
               if e.get("counts_par_radian")]
    if len(valeurs) < 2:
        return {"n": len(valeurs), "premier": valeurs[0] if valeurs else None,
                "dernier": valeurs[-1] if valeurs else None,
                "ecart_relatif": None, "significative": False,
                "etendue_relative": None}
    ecart = (valeurs[-1] - valeurs[0]) / valeurs[0]
    return {
        "n": len(valeurs),
        "premier": valeurs[0],
        "dernier": valeurs[-1],
        "ecart_relatif": round(ecart, 4),
        # L'étendue dit si la série est stable ou si elle oscille : un aller
        # et retour rend un écart nul entre premier et dernier alors que rien
        # n'est stable.
        "etendue_relative": round((max(valeurs) - min(valeurs)) / valeurs[0], 4),
        "significative": abs(ecart) >= SEUIL_DERIVE,
    }
