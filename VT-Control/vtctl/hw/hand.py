#!/usr/bin/env python3
"""
hand.py — le seul propriétaire du maître EtherCAT.

Ne réécrit **rien** du pilotage : il assemble deux briques éprouvées de
VT-Tactile et garantit qu'une seule instance de chacune existe.

* ``vt_tactile.bus.Hand`` — montage EtherCAT, réveil, flux de trames, et la
  seule implémentation qui lise les **positions signées** dans la trame brute.
  Le getter du SDK écrête les négatifs à zéro : un doigt repoussé sous son zéro
  y paraît immobile, et c'est ce qui a fait passer quatre moteurs sains pour
  muets pendant une nuit entière.
* ``tools.web.Backend`` — l'échantillonnage et **le seul chemin de commande
  moteur reproductible** : verrou SDK, fenêtre de silence autour de chaque
  consigne, vérification du mouvement, réémission, et reconnexion en dernier
  recours. Il vit dans un outil plutôt que dans la bibliothèque ; on l'importe
  tel quel plutôt que de le recopier, une copie divergerait.

Trois choses que ce module ajoute, et qui n'existaient nulle part :

1. **le contrôle des démons concurrents avant de réclamer l'interface** —
   ``lhandpro_service`` tient le maître sans poser de verrou, et sans ce
   contrôle ``connect()`` échoue trente secondes plus tard sur un message qui
   ne dit pas pourquoi ;
2. **l'enregistrement continu du flux brut** attaché au cycle de vie, pour
   qu'un arrêt, même brutal, écrive ce qui a été lu ;
3. **le refus de relancer un doigt bloqué**. Immobile *avec un courant élevé*
   n'est pas un ordre perdu, c'est un obstacle mécanique : relancer, c'est
   forcer dessus. L'annulaire a tiré 1059 ‰ avant de passer en alarme.
"""
from __future__ import annotations

import logging
import threading
import time

from .. import config
from .resources import ETHERCAT, ResourceManager, demons_concurrents

log = logging.getLogger("vtctl.hand")


class HandError(RuntimeError):
    """Défaillance du bus ou d'un moteur, avec de quoi décider quoi faire."""


class HandOwner:
    """
    Détient l'interface EtherCAT, la main et son pilote.

    Args:
        manager: le gestionnaire de ressources.
        fake: une ``FakeHand``. Quand elle est fournie, aucun matériel n'est
            touché et le SDK constructeur n'est pas chargé — mais le ``Backend``
            réel tourne au-dessus, donc la logique de commande est bien exercée.
        ignorer_demons: passer outre le contrôle des processus concurrents.
            À n'utiliser que si l'on sait ce qu'on fait : ces démons sont le
            travail de quelqu'un.
    """

    def __init__(self, manager: ResourceManager, fake=None,
                 iface_index: "int | None" = None, ignorer_demons: bool = False,
                 moteurs_exclus: "tuple | None" = None):
        self._mgr = manager
        self._token = None
        self.hand = None
        self.reader = None
        self.backend = None
        self.fake = fake
        self._iface = iface_index
        self._ignorer_demons = ignorer_demons
        self.demons_vus: list = []
        self._enregistre = False
        #: Moteurs à ne pas commander sur **cet exemplaire** de main.
        #:
        #: ``None`` garde la valeur de ``vt_tactile.hardware``. Une panne
        #: d'actionneur appartient à un exemplaire, pas au modèle : la coder en
        #: dur fait qu'on continue d'exclure un doigt qui remarche après un
        #: changement de main — ce qui est arrivé le 2026-08-26, la flexion du
        #: pouce ayant retrouvé la vie sur la main suivante.
        self.moteurs_exclus = moteurs_exclus

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def open(self, wake: bool = True, zero: bool = True) -> "HandOwner":
        """
        Réclame l'interface, monte le bus, réveille la main.

        Raises:
            HandError: si un démon concurrent tient déjà le maître. On refuse
                plutôt que d'échouer plus tard sur un message obscur — et on
                **ne le tue pas** : c'est le travail de quelqu'un, à lui de
                décider.
        """
        if self.hand is not None:
            return self

        if self.fake is None and not self._ignorer_demons:
            self.demons_vus = demons_concurrents()
            if self.demons_vus:
                liste = ", ".join(f"{d['pid']} ({d['cmd'].split()[0]})"
                                  for d in self.demons_vus)
                raise HandError(
                    f"des processus tiennent déjà le maître EtherCAT : {liste}. "
                    f"Ils ne sont pas arrêtés automatiquement — c'est le travail "
                    f"de quelqu'un. Les arrêter, puis relancer.")

        self._token = self._mgr.acquire(ETHERCAT, "HandOwner")
        try:
            self._appliquer_exclusions()
            from vt_tactile.tpdo import TactileReader  # noqa: PLC0415

            if self.fake is not None:
                self.hand = self.fake
            else:
                from vt_tactile.bus import Hand  # noqa: PLC0415

                self.hand = Hand()
            self.reader = TactileReader()
            self.hand.connect(iface_index=self._iface)
            if wake:
                # Alimentation des moteurs **puis** homing. Sans les deux, les
                # trames circulent avec des en-têtes corrects et une charge
                # utile identiquement nulle : le piège numéro un du banc.
                self.hand.wake()
            self._demarrer_backend()
            if wake and zero:
                # Le zéro se prend **maintenant** : la main vient d'être homée,
                # elle est ouverte, rien ne la touche. C'est le seul instant où
                # l'on est certain des conditions.
                #
                # Sans lui, l'interface affiche les lignes de base brutes — de
                # ~80 à ~180 counts selon le doigt, soit 0,3 à 0,7 une fois
                # normalisées — et les neuf zones ont l'air à moitié sollicitées
                # alors que rien ne les touche. On croit à des capteurs saturés.
                try:
                    n = self.zero(2.0)
                    log.info("zéro tactile au réveil : ligne de base sur %d trames", n)
                except HandError as e:
                    log.warning("zéro tactile impossible au réveil : %s", e)
            log.info("main prête sur %s", getattr(self.hand, "interface", "?"))
        except Exception:
            self.close()
            raise
        return self

    def _appliquer_exclusions(self) -> None:
        """
        Impose la liste des moteurs exclus pour cette session.

        ``vt_tactile.hardware`` la porte dans une constante de module, que
        ``Backend`` et ``envelop`` relisent à chaque appel. La remplacer là est
        donc la façon prévue de la régler à l'exécution — et il faut recalculer
        ``WORKING_FLEXORS`` dans la foulée, sans quoi les deux se contrediraient
        et la fermeture continuerait d'ignorer un doigt redevenu sain.
        """
        if self.moteurs_exclus is None:
            return
        from vt_tactile import hardware as hw  # noqa: PLC0415

        exclus = tuple(sorted(int(m) for m in self.moteurs_exclus))
        hw.BROKEN_MOTORS = exclus
        hw.WORKING_FLEXORS = tuple(m for m in hw.FLEXORS if m not in exclus)
        log.info("moteurs exclus pour cette session : %s ; fléchisseurs "
                 "pilotables : %s", exclus or "aucun", hw.WORKING_FLEXORS)

    def tester_moteurs(self, course: int = 2000, velocity: int = 1500,
                       max_current: int = 400) -> dict:
        """
        Quels moteurs répondent, sur **cet** exemplaire de main ?

        Commande à chacun un aller-retour court et regarde la position dans la
        trame brute. Un moteur qui ne bouge pas *à courant faible* n'exécute
        pas ; immobile *à courant élevé*, il bute sur quelque chose — la
        distinction décide s'il faut l'exclure ou dégager son trajet.

        N'exclut rien de lui-même : il rend un constat, et c'est à l'opérateur
        de décider. Une main dont un doigt est simplement gêné ne doit pas se
        retrouver amputée dans la configuration.
        """
        import time  # noqa: PLC0415

        from vt_tactile import hardware as hw  # noqa: PLC0415

        self._exige_ouverte()
        resultats = {}
        for moteur in hw.MOTOR_IDS:
            depart = self.positions((moteur,))[moteur]
            pic = 0
            try:
                self.hand.enable()
                self.hand.command({moteur: int(course)}, velocity, max_current)
                fin = time.time() + 6.0
                while time.time() < fin:
                    time.sleep(0.3)
                    pic = max(pic, self.courants((moteur,))[moteur])
                    if abs(self.positions((moteur,))[moteur] - depart) > 300:
                        break
                arrivee = self.positions((moteur,))[moteur]
            except Exception as e:  # noqa: BLE001
                resultats[moteur] = {"nom": hw.MOTOR_NAMES[moteur], "erreur": str(e)}
                continue
            course_faite = abs(arrivee - depart)
            resultats[moteur] = {
                "nom": hw.MOTOR_NAMES[moteur],
                "depart": depart, "arrivee": arrivee,
                "course": int(course_faite),
                "courant_max": int(pic),
                "repond": bool(course_faite > 300),
                # Immobile à courant élevé : ce n'est pas une panne d'actionneur,
                # c'est un obstacle. Le dire évite d'exclure un doigt sain.
                "bloque": bool(course_faite <= 300 and pic >= config.SEUIL_BLOQUE),
            }
        self.ouvrir()
        muets = [r["nom"] for r in resultats.values()
                 if not r.get("repond") and not r.get("bloque")]
        return {"moteurs": resultats,
                "repondent": [r["nom"] for r in resultats.values() if r.get("repond")],
                "muets": muets,
                "bloques": [r["nom"] for r in resultats.values() if r.get("bloque")],
                "exclusions_suggerees": sorted(
                    m for m, r in resultats.items()
                    if not r.get("repond") and not r.get("bloque")),
                # Ceux que la session écarte au moment du test : le tour d'essai
                # les commande quand même, pour dire si l'exclusion tient encore.
                "exclus": sorted(hw.BROKEN_MOTORS)}

    def _demarrer_backend(self) -> None:
        """Monte le ``Backend`` de VT-Tactile — tel quel, sans le réécrire."""
        from tools.web import Backend  # noqa: PLC0415

        self.backend = Backend(self.hand, self.reader)
        self.backend.start()

    def close(self) -> None:
        """
        Séquence de sortie, dans l'ordre qui compte.

        Le flux brut passe avant tout le reste : c'est la seule pièce qui ne se
        rejoue pas. Puis on rouvre la main et on coupe le couple — la main n'est
        **pas** rétro-entraînable, couper l'alimentation fige les doigts au lieu
        de les relâcher, et une main fermée sur un objet le reste.
        """
        if self.backend is not None:
            try:
                self.backend.stop()
            except Exception:  # noqa: BLE001
                pass
            self.backend = None
        if self.hand is not None:
            try:
                self.hand.release()
            except Exception:  # noqa: BLE001
                log.warning("relâchement de la main en échec", exc_info=True)
            try:
                self.hand.close()
            except Exception:  # noqa: BLE001
                pass
            self.hand = None
        self.reader = None
        if self._token is not None:
            self._token.release()
            self._token = None

    def __enter__(self) -> "HandOwner":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def ouverte(self) -> bool:
        return self.hand is not None

    # ── Flux brut ─────────────────────────────────────────────────────────────

    def demarrer_enregistrement(self) -> None:
        """
        Conserve **toutes** les trames lues, des deux types, sans filtrage.

        Aucune déduplication : deux trames identiques sont deux trames. Au repos
        la charge utile ne bouge pas d'un octet pendant des secondes, et ces
        périodes sont précisément la ligne de base dont dépend tout décodage
        ultérieur. C'est ce qui rend une session réinterprétable : les valeurs
        décodées dépendent de la table de découpage, les octets non — et cette
        table a déjà changé une fois.
        """
        self._exige_ouverte()
        self.hand.start_recording()
        self._enregistre = True

    def arreter_enregistrement(self) -> list:
        if not self._enregistre or self.hand is None:
            return []
        self._enregistre = False
        return self.hand.stop_recording()

    @property
    def enregistre(self) -> bool:
        return self._enregistre and self.hand is not None and self.hand.recording

    # ── Tactile ───────────────────────────────────────────────────────────────

    def zero(self, secondes: float = 2.0) -> int:
        """
        Prend la ligne de base tactile, objet posé et main au repos.

        Le zéro n'est pas optionnel : les lignes de base vont de ~80 à ~180
        selon le doigt, et sans remise à zéro par canal les zones ne sont pas
        comparables entre elles.

        Returns:
            le nombre de **trames** retenues, pas de canaux. Sur le banc réel la
            main émet une trentaine de trames capteur distinctes par seconde ;
            une poignée suffit à une ligne de base, mais un compte proche de
            zéro signale une main qui n'émet pas — donc un réveil incomplet.
        """
        self._exige_ouverte()
        trames = self.hand.collect(secondes)
        if not trames:
            raise HandError("aucune trame tactile pendant la mise à zéro : "
                            "la main est-elle réveillée ?")
        return self.reader.zero(trames)

    def etat_tactile(self):
        """Le dernier état décodé, ou ``None`` si aucune trame n'est arrivée."""
        self._exige_ouverte()
        brut = self.hand.latest_tactile()
        if brut is None:
            return None
        return self.reader.decode(brut)

    def pression(self, zone: str = "thumb") -> "float | None":
        etat = self.etat_tactile()
        return None if etat is None else float(etat[zone].pressure_max)

    # ── Moteurs ───────────────────────────────────────────────────────────────

    def positions(self, moteurs=None) -> dict:
        self._exige_ouverte()
        from vt_tactile import hardware as hw  # noqa: PLC0415

        return self.hand.positions(tuple(moteurs) if moteurs else hw.MOTOR_IDS)

    def courants(self, moteurs=None) -> dict:
        self._exige_ouverte()
        from vt_tactile import hardware as hw  # noqa: PLC0415

        return self.hand.currents(tuple(moteurs) if moteurs else hw.MOTOR_IDS)

    def bloques(self, moteurs=None) -> list:
        """
        Les moteurs immobiles **à courant élevé** — donc en butée sur quelque
        chose, pas en attente d'un ordre.

        La distinction est celle qui décide s'il faut réémettre la consigne ou
        s'arrêter. Relancer un doigt coincé, c'est forcer dessus.
        """
        return [m for m, c in self.courants(moteurs).items()
                if c >= config.SEUIL_BLOQUE]

    def aller_a(self, cibles: dict, velocity=None, max_current=None) -> dict:
        """
        Applique une consigne de position, par le ``Backend``.

        Refuse d'insister sur un doigt déjà bloqué : le ``Backend`` réémet
        l'ordre quand rien n'a démarré, et cette réémission ne doit pas viser un
        moteur qui pousse déjà.
        """
        self._exige_ouverte()
        coinces = self.bloques(list(cibles))
        if coinces:
            from vt_tactile import hardware as hw  # noqa: PLC0415

            noms = [hw.MOTOR_NAMES.get(m, str(m)) for m in coinces]
            raise HandError(f"doigt(s) bloqué(s) à plus de {config.SEUIL_BLOQUE} ‰ : "
                            f"{', '.join(noms)}. Ouvrir la main avant d'insister.")
        return self.backend.set_targets(cibles, velocity, max_current)

    def pivot_pouce(self, cible: int, velocity=None, max_current=None) -> dict:
        """
        Déplace le pivot du pouce, l'axe qui l'amène face aux autres doigts.

        Distinct de sa flexion (moteur 1), qui l'enroule. Sans opposition, le
        pouce se referme à côté de l'objet et sa zone tactile ne voit rien :
        c'est le pivot qui décide s'il y a préhension. L'opérateur le règle,
        la flexion suit avec les autres doigts à la fermeture.
        """
        from vt_tactile import hardware as hw  # noqa: PLC0415

        cible = max(0, min(int(cible), hw.POSITION_MAX))
        return self.aller_a({hw.THUMB_PIVOT: cible},
                            velocity or hw.VELOCITY_CLOSE,
                            max_current or hw.APPROACH_CURRENT)

    def ouvrir(self) -> bool:
        """Rouvre la main. À rejouer dans tous les cas de sortie, erreur comprise."""
        self._exige_ouverte()
        r = self.backend.open_hand()
        return bool(r.get("ouverte"))

    def reconnecter(self) -> dict:
        """
        Reprend le bus à zéro, comme le ferait un processus neuf.

        Mesuré le 2026-08-19 : le variateur cesse d'exécuter les consignes après
        un temps d'usage — acceptées, cible relue, aucune alarme, rien ne bouge.
        Ni ``enable()`` ni un ``wake()`` complet en cours de processus ne
        rétablissent, alors qu'un processus neuf y parvient à tous les coups.
        C'est donc l'état interne du SDK qu'il faut reprendre.
        """
        self._exige_ouverte()
        return self.backend.reconnecter()

    # ── État ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """L'état complet, tel que la page web le consomme."""
        if self.backend is None:
            return {"prete": False, "tactile": [], "motors": []}
        s = self.backend.snapshot()
        s["prete"] = True
        s["simulee"] = self.fake is not None
        s["enregistrement"] = self.enregistre
        s["zero_fait"] = bool(self.reader and self.reader.zeroed)
        return s

    def infos(self) -> dict:
        from vt_tactile import hardware as hw  # noqa: PLC0415

        return {
            "ouverte": self.ouverte,
            "simulee": self.fake is not None,
            "interface": getattr(self.hand, "interface", None),
            "moteurs": {str(m): hw.MOTOR_NAMES[m] for m in hw.MOTOR_IDS},
            "moteurs_exclus": [hw.MOTOR_NAMES[m] for m in hw.BROKEN_MOTORS],
            "flechisseurs_pilotables": list(hw.WORKING_FLEXORS),
            "zones_mortes": ["ring.pad"],
            "position_max": hw.POSITION_MAX,
            "demons_concurrents": self.demons_vus,
        }

    def _exige_ouverte(self) -> None:
        if self.hand is None:
            raise HandError("main fermée : appeler open() d'abord")
