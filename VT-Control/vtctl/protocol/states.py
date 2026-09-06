#!/usr/bin/env python3
"""
states.py — les états du protocole et les transitions permises.

Que des données : la table ci-dessous est la définition du protocole, et rien
d'autre dans le programme n'a le droit de décider d'un enchaînement. C'est ce
qui rend le déroulé lisible sans lire le code qui l'exécute.

Deux principes de conception :

**Une transition illégale est une erreur, pas un avertissement.** Un protocole
qui « corrige » silencieusement un enchaînement impossible produit un jeu de
données dont personne ne sait dans quel ordre il a été acquis.

**Le critère du pouce a trois issues, et toutes trois sont légales.** Satisfait,
contourné, ou non requis pour cette session. Ce qu'on refuse, c'est le
contournement *silencieux* : ``contourne`` exige un motif. Le pivot du pouce
n'amène pas le pouce en opposition sur tous les objets — mesuré le 2026-08-20,
il reste 7 à 24 mm à droite de la face avant du cylindre — donc le
contournement est un cas courant, pas une avarie.
"""
from __future__ import annotations

# ── États ─────────────────────────────────────────────────────────────────────

REPOS = "REPOS"
PRETE = "PRETE"
POSITIONNEMENT = "POSITIONNEMENT"
STABILISATION = "STABILISATION"
CAPTURE_VISUELLE = "CAPTURE_VISUELLE"
MAIN_AU_DEPART = "MAIN_AU_DEPART"
POUCE_EN_ATTENTE = "POUCE_EN_ATTENTE"
ARME = "ARME"
FERMETURE = "FERMETURE"
SAISIE = "SAISIE"
CAPTURE_TACTILE = "CAPTURE_TACTILE"
RELACHE = "RELACHE"
VALIDATION = "VALIDATION"
ERREUR = "ERREUR"
RECUPERATION = "RECUPERATION"
CLOTURE = "CLOTURE"

ETATS = (REPOS, PRETE, POSITIONNEMENT, STABILISATION, CAPTURE_VISUELLE,
         MAIN_AU_DEPART, POUCE_EN_ATTENTE, ARME, FERMETURE, SAISIE,
         CAPTURE_TACTILE, RELACHE, VALIDATION, ERREUR, RECUPERATION, CLOTURE)

#: États depuis lesquels rien ne repart.
TERMINAUX = (CLOTURE,)

#: États où le protocole attend une action de l'opérateur plutôt que du banc.
#: L'interface les met en évidence : ailleurs, elle n'a qu'à montrer l'avancée.
ATTENTE_OPERATEUR = (PRETE, POUCE_EN_ATTENTE, VALIDATION)

#: Ce que chaque état fait, en une ligne, pour l'interface et le journal.
DESCRIPTION = {
    REPOS: "Aucune session ouverte",
    PRETE: "Session ouverte, en attente d'un angle",
    POSITIONNEMENT: "Le plateau rejoint la consigne",
    STABILISATION: "Vérification que le plateau est bien immobile",
    CAPTURE_VISUELLE: "Images de l'objet seul, sans la main",
    MAIN_AU_DEPART: "La main revient en position de départ",
    POUCE_EN_ATTENTE: "L'opérateur place le pouce sur l'objet",
    ARME: "Conditions minimales réunies, la fermeture peut partir",
    FERMETURE: "Les doigts se referment sur l'objet",
    SAISIE: "Prise établie, doigts arrêtés au contact",
    CAPTURE_TACTILE: "Images et trames de la prise",
    RELACHE: "La main se rouvre et le couple est coupé",
    VALIDATION: "L'opérateur valide, puis refait l'angle ou passe au suivant",
    ERREUR: "Défaillance : le banc est mis en sécurité",
    RECUPERATION: "Reprise après erreur",
    CLOTURE: "Session close, manifeste écrit",
}

#: Ordre des phases par défaut : **tactile d'abord**, visuel ensuite.
#:
#: L'inverse paraît naturel — photographier l'objet intact avant d'y toucher —
#: mais il coûte un tour de plateau complet de plus, et surtout il fait
#: dépendre la partie longue et fragile (la saisie, avec l'opérateur dans la
#: boucle) d'un balayage visuel déjà consommé. En commençant par le tactile, un
#: abandon en cours de session laisse au moins les saisies faites, et les vues
#: de l'objet seul se reprennent quand on veut : elles ne demandent ni la main,
#: ni l'opérateur.
ORDRE_PHASES = ("tactile", "visuelle")

# ── Issues du critère « pouce stable » ────────────────────────────────────────

POUCE_SATISFAIT = "satisfait"
POUCE_CONTOURNE = "contourne"
POUCE_NON_REQUIS = "non_requis"
POUCE_ECHEC = "echec"
ISSUES_POUCE = (POUCE_SATISFAIT, POUCE_CONTOURNE, POUCE_NON_REQUIS, POUCE_ECHEC)

#: Les issues qui autorisent la fermeture. ``echec`` n'en est pas : c'est le
#: cas où le critère était exigé, n'a pas été atteint, et n'a pas été contourné.
ISSUES_ARMANTES = (POUCE_SATISFAIT, POUCE_CONTOURNE, POUCE_NON_REQUIS)

# ── Table des transitions ─────────────────────────────────────────────────────
#
# ``(état, évènement) -> état``. Toute paire absente est refusée.

TRANSITIONS = {
    (REPOS, "ouvrir"): PRETE,

    (PRETE, "positionner"): POSITIONNEMENT,
    (PRETE, "cloturer"): CLOTURE,

    (POSITIONNEMENT, "angle_arrete"): STABILISATION,
    (POSITIONNEMENT, "echec"): ERREUR,

    # Un même angle sert les deux phases : la branche est choisie par
    # l'évènement, pas par un drapeau caché dans l'état.
    (STABILISATION, "capturer_visuel"): CAPTURE_VISUELLE,
    (STABILISATION, "preparer_main"): MAIN_AU_DEPART,
    (STABILISATION, "echec"): ERREUR,

    (CAPTURE_VISUELLE, "angle_termine"): PRETE,
    (CAPTURE_VISUELLE, "echec"): ERREUR,

    (MAIN_AU_DEPART, "main_prete"): POUCE_EN_ATTENTE,
    (MAIN_AU_DEPART, "echec"): ERREUR,

    (POUCE_EN_ATTENTE, "armer"): ARME,
    (POUCE_EN_ATTENTE, "sauter_angle"): PRETE,
    (POUCE_EN_ATTENTE, "echec"): ERREUR,

    # Désarmer est prévu : tout déplacement du pouce doit pouvoir annuler une
    # fermeture imminente, sans quoi l'opérateur a les doigts dans la main
    # quand elle part.
    (ARME, "fermer"): FERMETURE,
    (ARME, "desarmer"): POUCE_EN_ATTENTE,
    (ARME, "echec"): ERREUR,

    (FERMETURE, "saisie_etablie"): SAISIE,
    (FERMETURE, "echec"): ERREUR,

    (SAISIE, "capturer_tactile"): CAPTURE_TACTILE,
    (SAISIE, "echec"): ERREUR,

    (CAPTURE_TACTILE, "relacher"): RELACHE,
    (CAPTURE_TACTILE, "echec"): ERREUR,

    (RELACHE, "a_valider"): VALIDATION,
    (RELACHE, "echec"): ERREUR,

    # Deux sorties distinctes, et c'est délibéré : valider ou invalider **ne
    # fait pas avancer**. Une capture ratée se refait sur le même angle — la
    # main a bougé, l'objet a glissé, la prise était mauvaise — et l'on ne passe
    # à l'angle suivant que lorsque l'opérateur le dit.
    (VALIDATION, "reprendre_angle"): PRETE,
    (VALIDATION, "angle_termine"): PRETE,
    (VALIDATION, "echec"): ERREUR,

    (ERREUR, "recuperer"): RECUPERATION,
    (ERREUR, "cloturer"): CLOTURE,

    # Un angle perdu n'en perd pas d'autres : la reprise repart sur PRETE.
    (RECUPERATION, "reprendre"): PRETE,
    (RECUPERATION, "abandonner"): CLOTURE,
}

#: Depuis n'importe quel état non terminal, une défaillance mène à ERREUR.
#: Déclaré ici plutôt que répété quinze fois dans la table.
EVENEMENT_ECHEC = "echec"


def transitions_depuis(etat: str) -> list:
    """Les évènements acceptés dans cet état, triés."""
    return sorted(e for (s, e) in TRANSITIONS if s == etat)


def cible(etat: str, evenement: str) -> "str | None":
    """L'état d'arrivée, ou ``None`` si la transition n'existe pas."""
    return TRANSITIONS.get((etat, evenement))


def valider_table() -> None:
    """
    Vérifie la cohérence de la table au chargement.

    Un état inatteignable ou un état sans sortie est presque toujours une
    faute de frappe, et elle ne se voit qu'au moment où le protocole s'y
    bloque — c'est-à-dire au milieu d'une acquisition.
    """
    for (etat, evt), arrivee in TRANSITIONS.items():
        if etat not in ETATS:
            raise ValueError(f"état de départ inconnu : {etat!r}")
        if arrivee not in ETATS:
            raise ValueError(f"état d'arrivée inconnu : {arrivee!r} ({etat} --{evt}-->)")
    atteignables = {REPOS} | set(TRANSITIONS.values())
    orphelins = set(ETATS) - atteignables
    if orphelins:
        raise ValueError(f"états inatteignables : {sorted(orphelins)}")
    sans_sortie = {e for e in ETATS if e not in TERMINAUX
                   and not transitions_depuis(e)}
    if sans_sortie:
        raise ValueError(f"états sans sortie : {sorted(sans_sortie)}")


valider_table()
