#!/usr/bin/env python3
"""
machine.py — le moteur de la machine à états, et son journal.

Trois garanties, et elles ne sont pas décoratives :

**Une transition illégale lève.** Elle n'est ni ignorée, ni corrigée en
silence. Un protocole qui rattrape tout seul un enchaînement impossible produit
un jeu de données dont personne ne sait dans quel ordre il a été acquis.

**Tout est journalisé, y compris les refus.** Les transitions réussies, les
gardes qui bloquent, les échecs. Le journal est écrit avant que la transition
ne soit appliquée : si le programme meurt en pleine transition, le journal
montre l'intention, ce qui est exactement ce qu'il faut pour comprendre.

**Toute défaillance mène à ``ERREUR``, depuis n'importe où.** :meth:`echouer`
n'a pas besoin d'être déclarée dans la table pour chaque état : c'est une
sortie universelle, et elle porte la cause.

Les gardes sont des fonctions ``() -> (bool, raison)`` attachées à une
transition. Elles vivent ici plutôt que dans le code d'exécution pour qu'on
puisse lire les conditions du protocole sans lire le pilotage matériel.
"""
from __future__ import annotations

import logging
import threading

from . import states as S

log = logging.getLogger("vtctl.machine")


class TransitionRefusee(RuntimeError):
    """
    L'évènement n'est pas permis dans cet état, ou une garde s'y oppose.

    Porte l'état, l'évènement et la raison : « transition interdite » sans plus
    n'aide personne à comprendre où le protocole s'est arrêté.
    """

    def __init__(self, etat: str, evenement: str, raison: str):
        self.etat, self.evenement, self.raison = etat, evenement, raison
        super().__init__(f"{etat} --{evenement}--> refusé : {raison}")


class Machine:
    """
    L'état du protocole, et le seul objet autorisé à le changer.

    Args:
        journal: un :class:`vtctl.store.journal.Journal`, ou ``None`` pour une
            machine sans trace (les tests de table pure).
        etat: état de départ, ``REPOS`` sauf reprise.
    """

    def __init__(self, journal=None, etat: str = S.REPOS):
        if etat not in S.ETATS:
            raise ValueError(f"état inconnu : {etat!r}")
        self._etat = etat
        self._journal = journal
        self._lock = threading.RLock()
        self._gardes: dict = {}
        self.historique: list = []
        #: Contexte courant : numéro d'angle, phase, statut du pouce… Tout ce
        #: que l'interface montre et que le journal doit porter.
        self.contexte: dict = {}
        self.derniere_erreur: "dict | None" = None

    # ── Lecture ───────────────────────────────────────────────────────────────

    @property
    def etat(self) -> str:
        with self._lock:
            return self._etat

    @property
    def description(self) -> str:
        return S.DESCRIPTION.get(self.etat, "")

    @property
    def attend_operateur(self) -> bool:
        return self.etat in S.ATTENTE_OPERATEUR

    @property
    def termine(self) -> bool:
        return self.etat in S.TERMINAUX

    def evenements_possibles(self) -> list:
        """
        Ce qui est permis maintenant — gardes comprises.

        C'est ce que l'interface utilise pour n'afficher que les boutons qui
        marcheront : proposer une action qui sera refusée est une façon sûre de
        faire douter l'opérateur du banc plutôt que du logiciel.
        """
        etat = self.etat
        sortis = []
        for evt in S.transitions_depuis(etat):
            ok, _raison = self.verifier(evt)
            if ok:
                sortis.append(evt)
        return sortis

    # ── Gardes ────────────────────────────────────────────────────────────────

    def garde(self, etat: str, evenement: str, fn) -> None:
        """
        Attache une condition à une transition.

        ``fn`` rend ``(True, "")`` ou ``(False, raison)``. Une garde ne doit
        avoir aucun effet de bord : elle est appelée aussi par
        :meth:`evenements_possibles`, à chaque rafraîchissement de la page.
        """
        if (etat, evenement) not in S.TRANSITIONS:
            raise ValueError(f"garde sur une transition inexistante : "
                             f"{etat} --{evenement}-->")
        self._gardes[(etat, evenement)] = fn

    def verifier(self, evenement: str) -> tuple:
        """La transition passerait-elle ? Sans rien changer."""
        etat = self.etat
        if S.cible(etat, evenement) is None:
            return False, (f"« {evenement} » n'est pas permis dans l'état {etat} "
                           f"(permis : {', '.join(S.transitions_depuis(etat)) or 'aucun'})")
        fn = self._gardes.get((etat, evenement))
        if fn is None:
            return True, ""
        try:
            ok, raison = fn()
        except Exception as e:  # noqa: BLE001 — une garde qui lève interdit, sans casser
            return False, f"garde en erreur : {e}"
        return bool(ok), ("" if ok else str(raison))

    # ── Transition ────────────────────────────────────────────────────────────

    def declencher(self, evenement: str, **contexte) -> str:
        """
        Applique une transition, ou lève.

        Returns:
            le nouvel état.

        Raises:
            TransitionRefusee: transition absente de la table, ou garde opposée.
                Le refus est journalisé : un protocole bloqué doit laisser la
                trace de ce qui l'a bloqué.
        """
        with self._lock:
            depart = self._etat
            ok, raison = self.verifier(evenement)
            if not ok:
                self._noter("transition_refusee", depuis=depart,
                            evenement=evenement, raison=raison, **contexte)
                raise TransitionRefusee(depart, evenement, raison)

            arrivee = S.cible(depart, evenement)
            # Journaliser **avant** d'appliquer : si le programme meurt ici, le
            # journal montre l'intention, et c'est ce qu'il faut pour comprendre.
            self._noter("transition", depuis=depart, evenement=evenement,
                        vers=arrivee, **contexte)
            self._etat = arrivee
            self.contexte.update(contexte)
            self.historique.append({"depuis": depart, "evenement": evenement,
                                    "vers": arrivee, "contexte": dict(contexte)})
            log.info("%s --%s--> %s%s", depart, evenement, arrivee,
                     f"  {contexte}" if contexte else "")
            return arrivee

    def echouer(self, cause: str, **contexte) -> str:
        """
        Sortie universelle vers ``ERREUR``, depuis n'importe quel état.

        Ne lève jamais : c'est la voie qu'emprunte la gestion d'erreur, et elle
        ne doit pas pouvoir échouer à son tour. Depuis un état terminal, ou
        depuis ``ERREUR`` lui-même, elle se contente de noter la cause.
        """
        with self._lock:
            depart = self._etat
            self.derniere_erreur = {"cause": cause, "depuis": depart, **contexte}
            if depart in S.TERMINAUX or depart in (S.ERREUR, S.RECUPERATION):
                self._noter("erreur_ignoree", depuis=depart, cause=cause, **contexte)
                return depart
            self._noter("transition", depuis=depart, evenement=S.EVENEMENT_ECHEC,
                        vers=S.ERREUR, cause=cause, **contexte)
            self._etat = S.ERREUR
            self.historique.append({"depuis": depart, "evenement": S.EVENEMENT_ECHEC,
                                    "vers": S.ERREUR, "contexte": {"cause": cause}})
            log.error("%s --echec--> ERREUR : %s", depart, cause)
            return S.ERREUR

    def forcer(self, etat: str, raison: str) -> str:
        """
        Impose un état hors table. **Réservé à la reprise après plantage.**

        Reconstruire une machine depuis un journal demande de la replacer où
        elle était ; il n'y a pas de chemin légal pour cela, et en inventer un
        ouvrirait la porte à toutes les corrections silencieuses.
        """
        if etat not in S.ETATS:
            raise ValueError(f"état inconnu : {etat!r}")
        with self._lock:
            self._noter("etat_force", depuis=self._etat, vers=etat, raison=raison)
            self._etat = etat
            return etat

    # ── Journal ───────────────────────────────────────────────────────────────

    def _noter(self, kind: str, **champs) -> None:
        if self._journal is not None:
            self._journal.ecrire(kind, **champs)

    def snapshot(self) -> dict:
        """L'état complet, tel que l'API le sert."""
        with self._lock:
            return {
                "etat": self._etat,
                "description": self.description,
                "attend_operateur": self.attend_operateur,
                "termine": self.termine,
                "evenements": self.evenements_possibles(),
                "contexte": dict(self.contexte),
                "erreur": self.derniere_erreur,
                "transitions": len(self.historique),
            }


def rejouer(evenements) -> Machine:
    """
    Reconstruit une machine depuis un journal relu.

    Sert à savoir où une session tuée s'est arrêtée — et donc si le banc a été
    laissé main fermée sur l'objet, ce qui demande une intervention avant de
    relancer quoi que ce soit.
    """
    m = Machine(journal=None)
    for e in evenements:
        if e.get("kind") == "transition":
            vers = e.get("vers")
            if vers in S.ETATS:
                m._etat = vers  # noqa: SLF001 — reconstruction, pas transition
                m.historique.append({"depuis": e.get("depuis"),
                                     "evenement": e.get("evenement"),
                                     "vers": vers, "contexte": {}})
        elif e.get("kind") == "etat_force" and e.get("vers") in S.ETATS:
            m._etat = e["vers"]  # noqa: SLF001
    return m
