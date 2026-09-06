#!/usr/bin/env python3
"""
declencheur.py — « le pouce est posé sur l'objet, et il y reste ».

L'opérateur amène le pouce au contact de l'objet ; c'est la **stabilité** de
ce contact qui autorise les autres doigts à se refermer. Deux conditions,
toutes deux nécessaires :

* **appui franc** — la pression de la zone ``thumb`` dépasse un seuil sur
  *toute* la fenêtre. Le minimum, pas la moyenne : un contact qui retombe à
  zéro au milieu n'est pas un contact stable, et une moyenne le masquerait ;
* **appui stable** — l'amplitude sur la fenêtre reste sous ``epsilon``.

C'est cette seconde condition qui distingue un pouce **posé** d'un pouce **en
train de bouger** : pendant que l'opérateur l'approche, la pression varie ;
quand il est en appui sur la surface, elle se fige.

La logique est volontairement sans matériel, pour être vérifiable sans banc.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

#: Pression au-delà de laquelle on parle d'appui. Le bruit de fond mesuré est
#: sous 1 LSB (0,004) et ``envelop`` déclare contact à 0,03 ; on prend le
#: double, parce qu'ici l'appui est **délibéré** et qu'un faux départ ferme la
#: main pendant que l'opérateur a les doigts dedans.
SEUIL_APPUI = 0.06

#: Amplitude tolérée sur la fenêtre. Le capteur sature à 1,0 et les paliers
#: utiles vont de 0,05 à 0,5 : 0,04 laisse passer le tremblement de la main
#: sans laisser passer un mouvement d'approche.
EPSILON_STABLE = 0.04

#: Durée pendant laquelle les deux conditions doivent tenir.
DUREE_STABLE = 1.0


@dataclass
class EtatDeclencheur:
    """Ce que le déclencheur voit à l'instant présent."""

    pret: bool
    #: Pourquoi ce n'est pas encore déclenché, en clair, pour l'affichage.
    raison: str
    pression: float
    minimum: float
    amplitude: float
    #: Fraction de la fenêtre déjà remplie, de 0 à 1.
    remplissage: float


class ContactStable:
    """
    Fenêtre glissante sur la pression du pouce.

    Args:
        seuil: pression minimale sur toute la fenêtre.
        epsilon: amplitude maximale tolérée sur la fenêtre.
        duree: durée que les deux conditions doivent couvrir, en secondes.

    L'horodatage est fourni par l'appelant plutôt que lu ici : c'est la même
    horloge que celle du reste de la session, et ça rend la classe testable
    sans attendre en temps réel.
    """

    def __init__(self, seuil: float = SEUIL_APPUI,
                 epsilon: float = EPSILON_STABLE,
                 duree: float = DUREE_STABLE):
        if duree <= 0:
            raise ValueError("la durée de stabilité doit être positive")
        self.seuil = seuil
        self.epsilon = epsilon
        self.duree = duree
        self._points: deque[tuple[float, float]] = deque()

    def reinitialiser(self) -> None:
        """Vide la fenêtre — après un déclenchement, ou un abandon."""
        self._points.clear()

    def ajouter(self, t: float, pression: float) -> EtatDeclencheur:
        """Ajoute une mesure et rend l'état courant."""
        self._points.append((t, float(pression)))
        # On garde un point *avant* le début de fenêtre : sans lui, une fenêtre
        # qui vient d'être remplie serait déclarée complète alors qu'elle ne
        # couvre pas encore la durée demandée.
        while len(self._points) > 2 and self._points[1][0] < t - self.duree:
            self._points.popleft()

        valeurs = [p for _t, p in self._points]
        couvert = self._points[-1][0] - self._points[0][0]
        remplissage = min(1.0, couvert / self.duree) if self.duree else 1.0
        mini, maxi = min(valeurs), max(valeurs)
        amplitude = maxi - mini

        if couvert < self.duree:
            raison = f"fenêtre remplie à {100 * remplissage:.0f} %"
        elif mini <= self.seuil:
            raison = f"appui trop faible ({mini:.3f} ≤ {self.seuil:.3f})"
        elif amplitude >= self.epsilon:
            raison = f"encore en mouvement (amplitude {amplitude:.3f})"
        else:
            raison = "contact stable"

        return EtatDeclencheur(
            pret=(couvert >= self.duree and mini > self.seuil
                  and amplitude < self.epsilon),
            raison=raison,
            pression=valeurs[-1],
            minimum=mini,
            amplitude=amplitude,
            remplissage=remplissage,
        )
