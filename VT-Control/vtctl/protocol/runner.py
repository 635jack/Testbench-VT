#!/usr/bin/env python3
"""
runner.py — l'exécution du protocole, pilotée par la machine à états.

Le protocole se déroule dans un fil de fond et **s'arrête aux points où
l'opérateur décide** : la pose du pouce, et la validation de chaque capture.
Partout ailleurs il avance seul.

Deux phases, dans cet ordre :

**Phase visuelle** — l'objet seul, à tous les angles, sans la main. C'est ce
qui donne les vues non occultées de l'objet, et elle n'a pas besoin du bus
EtherCAT.

**Phase tactile** — aux mêmes angles. L'opérateur amène le pouce au contact ;
dès que le contact est franc et stable, ou qu'il choisit de passer outre, les
autres doigts se referment. La totalité des trames EtherCAT est conservée
pendant toute la phase, des deux types, sans filtrage : les valeurs décodées
dépendent de la table de découpage, les octets non.

Sur le critère du pouce, trois issues et toutes légales : ``satisfait``,
``contourne`` (avec motif obligatoire), ``non_requis`` (choisi à l'ouverture de
la session). Le pivot du pouce n'amène pas le pouce en opposition sur tous les
objets — il reste 7 à 24 mm à droite de la face avant du cylindre, mesuré le
2026-08-20 — donc le contournement est un cas courant, pas une avarie. Ce
qu'on refuse, c'est de le passer sous silence.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
import traceback

from .. import config
from ..hw.hand import HandError
from ..store.session import Session
from . import states as S
from .machine import Machine, TransitionRefusee

log = logging.getLogger("vtctl.runner")

PHASE_VISUELLE = "visuelle"
PHASE_TACTILE = "tactile"

#: Images prises à chaque étape figée. Trois plutôt qu'une : le bruit temporel
#: de la stéréo passive se sépare du signal en comparant des images successives.
IMAGES_PAR_ETAPE = 3

#: Délai entre le contact stable et le départ de la fermeture. L'opérateur doit
#: pouvoir retirer sa main, et annuler s'il s'est trompé.
DELAI_RETRAIT = 3.0


class Runner:
    """
    Exécute le protocole sur un :class:`vtctl.hw.banc.Banc`.

    Un seul déroulé à la fois. Les méthodes de décision (:meth:`armer`,
    :meth:`valider`, :meth:`sauter_angle`) sont appelées depuis l'interface ou
    la ligne de commande pendant que le fil de fond attend.
    """

    def __init__(self, banc, reglages: config.Reglages, base_sessions,
                 phases=S.ORDRE_PHASES):
        self.banc = banc
        self.reglages = reglages
        self.base = base_sessions
        self.phases = tuple(phases)

        self.session: "Session | None" = None
        self.machine: "Machine | None" = None
        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()

        #: Étapes où la sécurité a ouvert la main d'office. Non vide = au moins
        #: une capture à écarter.
        self.serrages_bornes: list = []

        #: Portes où le fil de fond attend une décision de l'opérateur.
        self._porte_pouce = threading.Event()
        self._porte_validation = threading.Event()
        self._decision_pouce: dict = {}
        self._decision_validation: dict = {}

        #: Ce que l'interface affiche du critère du pouce, rafraîchi en continu
        #: tant qu'on est dans POUCE_EN_ATTENTE.
        self.suivi_pouce: dict = {}
        self.phase: "str | None" = None
        self.index_angle = -1
        self.angle_courant: dict = {}
        self.progression: list = []
        self.derniere_image: dict = {}
        self._detecteur = None
        #: Numéro d'essai sur l'angle courant. Une capture ratée se refait sur
        #: le même angle, et chaque essai a son propre dossier : écraser le
        #: précédent priverait de la comparaison qui dit *pourquoi* il était raté.
        self.essai = 0
        #: Ce que la caméra voit du carreau du pouce, rafraîchi pendant l'attente.
        self.pouce_vu: dict = {}
        #: Cinématique directe de la main, chargée une fois. Sans elle on
        #: enregistre des counts que rien ne situe dans l'espace.
        self._cinematique = None
        #: Décision de suite après validation : reprendre l'angle, ou avancer.
        self._porte_suite = threading.Event()
        self._decision_suite: dict = {}

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def demarrer(self) -> "Session":
        """Ouvre la session et lance le protocole en fond."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("un protocole est déjà en cours")
        self.session = Session.create(self.base, self.reglages.objet, meta={
            "protocole": {"version": 2, "phases": list(self.phases),
                          "angles_commandes_deg": list(self.reglages.angles)},
            "reglages": self.reglages.to_dict(),
            "banc": self.banc.infos(),
        })
        self.machine = Machine(journal=self.session.journal)
        self._installer_gardes()
        self._stop.clear()
        self.progression = []
        self._thread = threading.Thread(target=self._boucle, name="protocole",
                                        daemon=True)
        self._thread.start()
        return self.session

    def arreter(self, motif: str = "arrêt demandé") -> None:
        """
        Demande l'arrêt. Le fil termine l'étape en cours puis clôture proprement.

        Ne tue rien : une interruption au milieu d'une écriture laisserait un
        flux brut tronqué, et c'est la seule pièce qui ne se rejoue pas.
        """
        self._stop.set()
        self._decision_pouce = {"issue": "arret", "motif": motif}
        self._decision_validation = {"validation": "en_attente"}
        self._porte_pouce.set()
        self._porte_validation.set()

    def attendre(self, timeout: "float | None" = None) -> bool:
        """Attend la fin du protocole. Rend ``True`` s'il est terminé."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def en_cours(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Gardes ────────────────────────────────────────────────────────────────

    def _installer_gardes(self) -> None:
        """
        Les conditions du protocole, attachées à leurs transitions.

        Elles sont ici et pas dans le code d'exécution pour qu'on puisse lire
        les conditions sans lire le pilotage. Aucune n'a d'effet de bord : elles
        sont appelées à chaque rafraîchissement de la page.
        """
        m = self.machine

        def main_disponible():
            if self.banc.hand is None or not self.banc.hand.ouverte:
                return False, "la main n'est pas montée (phase visuelle seule)"
            return True, ""

        def angle_stabilise():
            if not self.angle_courant:
                return False, "aucun angle en cours"
            return True, ""

        def pouce_decide():
            issue = self._decision_pouce.get("issue")
            if issue not in S.ISSUES_ARMANTES:
                return False, f"le critère du pouce n'est pas tranché ({issue or 'en attente'})"
            if issue == S.POUCE_CONTOURNE and not self._decision_pouce.get("motif"):
                # Un contournement sans motif est un contournement silencieux :
                # exactement ce que ce protocole existe pour empêcher.
                return False, "un contournement doit porter un motif"
            return True, ""

        m.garde(S.STABILISATION, "preparer_main", main_disponible)
        m.garde(S.MAIN_AU_DEPART, "main_prete", main_disponible)
        m.garde(S.POUCE_EN_ATTENTE, "armer", pouce_decide)
        m.garde(S.STABILISATION, "capturer_visuel", angle_stabilise)

    # ── Boucle principale ─────────────────────────────────────────────────────

    def _boucle(self) -> None:
        complet = False
        try:
            self.machine.declencher("ouvrir", objet=self.reglages.objet)
            self.session.note("camera", self.banc.camera.infos())
            self.session.note("aruco", self.banc.angle.infos())
            self.session.note("pose_camera", self._mesurer_pose())
            self.session.note("carreaux_3d", self._poses_carreaux())
            if self.banc.hand is not None:
                self.session.note("cinematique", self._infos_cinematique())
            if self.banc.hand is not None:
                self.session.note("main", self.banc.hand.infos())

            # L'ordre vient de ``self.phases`` et non d'un enchaînement figé :
            # le tactile passe d'abord par défaut, mais une campagne peut vouloir
            # l'inverse, et le manifeste porte l'ordre réellement suivi.
            for phase in self.phases:
                if self._stop.is_set():
                    break
                if phase == PHASE_VISUELLE:
                    self._phase_visuelle()
                elif phase == PHASE_TACTILE:
                    self._phase_tactile()
            complet = not self._stop.is_set()
        except Exception as e:  # noqa: BLE001 — le fil ne doit jamais mourir en silence
            log.exception("protocole interrompu")
            self.machine.echouer(f"{type(e).__name__}: {e}",
                                 trace=traceback.format_exc(limit=4))
            self.session.journal.ecrire("exception", message=str(e),
                                        trace=traceback.format_exc())
        finally:
            self._cloturer(complet)

    def _cloturer(self, complet: bool) -> None:
        """
        Sortie propre. Le flux brut d'abord : c'est ce qui ne se rejoue pas.
        """
        try:
            if self.banc.hand is not None and self.banc.hand.enregistre:
                trames = self.banc.hand.arreter_enregistrement()
                if trames:
                    e = self.session.save_raw_stream("brut", trames)
                    log.info("flux brut : %s trames sur %s s",
                             e.get("count"), e.get("span_s"))
        except Exception:  # noqa: BLE001
            log.warning("flux brut non enregistré", exc_info=True)
        try:
            if self.machine.etat not in S.TERMINAUX:
                if self.machine.etat == S.ERREUR:
                    self.machine.declencher("cloturer", complet=complet)
                else:
                    self.machine.forcer(S.CLOTURE, "clôture du protocole")
        except TransitionRefusee:
            pass
        try:
            self.session.close(complet=complet)
        except Exception:  # noqa: BLE001
            log.warning("manifeste non écrit", exc_info=True)

    # ── Phase visuelle ────────────────────────────────────────────────────────

    def _phase_visuelle(self) -> None:
        self.phase = PHASE_VISUELLE
        self.session.mark("phase_visuelle_debut",
                          angles=list(self.reglages.angles))
        profil = self.banc.camera.profil()
        pwm = profil.pwm_of(self.reglages.niveau)

        self.banc.amorcer()
        for i, consigne in enumerate(self.reglages.angles):
            if self._stop.is_set():
                return
            self.index_angle = i
            pose = self._positionner(i, consigne)
            self.machine.declencher("capturer_visuel", angle=i)

            etape = f"visuel/angle_{i:02d}"
            self.banc.bench.lumiere(pwm)
            self.banc.camera.set_mode("dataset")
            self.banc.camera.flush(8)
            for k in range(IMAGES_PAR_ETAPE):
                self._capturer_image(etape, k, phase=PHASE_VISUELLE,
                                     niveau=self.reglages.niveau, pwm=pwm, **pose)
            self.session.capture(etape, {
                "phase": PHASE_VISUELLE, "angle_index": i,
                "angle": pose, "images": IMAGES_PAR_ETAPE,
                "validation": "valide",
                "pouce": {"critere": "pouce_stable", "statut": S.POUCE_NON_REQUIS,
                          "requis": False,
                          "motif": "phase visuelle : la main n'intervient pas"},
            })
            self._noter_progression(PHASE_VISUELLE, i, pose, "valide")
            self.machine.declencher("angle_termine", angle=i)

    # ── Phase tactile ─────────────────────────────────────────────────────────

    def _phase_tactile(self) -> None:
        if self.banc.hand is None:
            log.warning("phase tactile demandée sans main : ignorée")
            return
        self.phase = PHASE_TACTILE
        from vt_tactile.declencheur import ContactStable  # noqa: PLC0415

        self._detecteur = ContactStable(seuil=self.reglages.seuil_pouce,
                                        epsilon=self.reglages.epsilon_pouce,
                                        duree=self.reglages.duree_pouce)
        profil = self.banc.camera.profil()
        pwm = profil.pwm_of(self.reglages.niveau)

        # Le flux brut couvre **toute** la phase, sans filtrage : c'est ce qui
        # rend la session réinterprétable des mois plus tard.
        self.banc.hand.demarrer_enregistrement()
        self.session.mark("flux_brut_debut")
        self.banc.amorcer()

        i = 0
        essais = {}
        while i < len(self.reglages.angles) and not self._stop.is_set():
            consigne = self.reglages.angles[i]
            self.index_angle = i
            self.essai = essais.get(i, 0)
            etape = f"tactile/angle_{i:02d}/essai_{self.essai:02d}"
            try:
                suite = self._un_angle_tactile(i, consigne, etape, pwm)
            except HandError as e:
                # Un angle perdu n'en perd pas d'autres.
                log.error("angle %d abandonné : %s", i, e)
                self.machine.echouer(str(e), angle=i)
                self.session.mark(f"{etape}/abandon", raison=str(e))
                self._recuperer(i, str(e))
                suite = "suivant"
            self._vider_flux_brut(etape)
            if suite == "reprendre":
                essais[i] = self.essai + 1
                log.info("angle %d repris — essai %d", i, essais[i])
                continue
            i += 1
        self.essai = 0

    def _un_angle_tactile(self, i: int, consigne: float, etape: str,
                          pwm: int) -> str:
        """
        Un angle, du positionnement à la validation.

        Returns:
            ``"reprendre"`` pour refaire le même angle, ``"suivant"`` sinon.
        """
        pose = self._positionner(i, consigne)
        self.machine.declencher("preparer_main", angle=i)

        self.banc.bench.lumiere(pwm)
        self.banc.camera.set_mode("dataset")

        # Reprendre la main à zéro avant chaque saisie. Le variateur cesse
        # d'exécuter les consignes après un temps d'usage — acceptées, cible
        # relue, aucune alarme, rien ne bouge — et seul un bus repris à zéro
        # rétablit. Mesuré le 2026-08-19.
        self.session.mark(f"{etape}/reprise_bus")
        self.banc.hand.reconnecter()

        if not self.banc.hand.ouvrir():
            raise HandError("la main ne s'ouvre pas : on ne referme jamais "
                            "depuis une pose inconnue")
        self.session.mark(f"{etape}/main_ouverte")

        self.banc.hand.zero(self.reglages.zero_secondes)
        self.session.write_json(f"{etape}/baseline.json",
                                {"t": self.session.t,
                                 "baseline": self.banc.hand.reader.baseline})

        # Pose de départ du pivot : à mi-course entre le pouce replié et son
        # ouverture maximale atteignable. L'opérateur l'ajuste ensuite ; cette
        # valeur ne sert qu'à ne pas partir d'une pose absurde.
        depart = self.reglages.pivot_depart
        self.banc.hand.pivot_pouce(depart)
        self.machine.declencher("main_prete", angle=i, pivot=depart)

        pouce = self._attendre_le_pouce(etape)
        if pouce.get("issue") == "arret":
            self.machine.declencher("sauter_angle", angle=i, raison="arrêt demandé")
            return "suivant"
        if pouce.get("issue") == "saut":
            self.session.capture(etape, {
                "phase": PHASE_TACTILE, "angle_index": i, "angle": pose,
                "pouce": pouce["pouce"], "validation": "invalide",
                "commentaire": "angle sauté par l'opérateur"})
            self._noter_progression(PHASE_TACTILE, i, pose, "invalide")
            self.machine.declencher("sauter_angle", angle=i, raison="sauté")
            return "suivant"

        self.machine.declencher("armer", angle=i, **{
            k: v for k, v in pouce["pouce"].items()
            if k in ("statut", "motif", "pression_au_declenchement")})

        # Prise de vue « avant fermeture » : l'objet est visible, la main est
        # en place, rien ne le masque encore.
        self.banc.camera.flush(5)
        for k in range(IMAGES_PAR_ETAPE):
            self._capturer_image(f"{etape}/00_avant", k, phase=PHASE_TACTILE,
                                 pwm=pwm, **pose)

        # Le garde-fou couvre **tout** l'intervalle serré — la fermeture et les
        # photos de la saisie —, pas seulement la recherche du contact.
        self.machine.declencher("fermer", angle=i)
        with self._serrage_borne(etape) as garde:
            saisie = self._fermer(etape)
            self.machine.declencher("saisie_etablie", angle=i,
                                    contacts=saisie["contacts"])

            self.machine.declencher("capturer_tactile", angle=i)
            self.banc.camera.flush(3)
            for k in range(IMAGES_PAR_ETAPE):
                self._capturer_image(f"{etape}/01_saisi", k, phase=PHASE_TACTILE,
                                     pwm=pwm, contacts=saisie["contacts"],
                                     serrage_borne=garde["declenche"], **pose)
        saisie["serrage_borne"] = garde["declenche"]

        self.machine.declencher("relacher", angle=i)
        self.banc.hand.ouvrir()
        self.session.mark(f"{etape}/relache")
        time.sleep(0.5)
        self.banc.camera.flush(5)
        for k in range(IMAGES_PAR_ETAPE):
            self._capturer_image(f"{etape}/02_relache", k, phase=PHASE_TACTILE,
                                 pwm=pwm, **pose)

        self.machine.declencher("a_valider", angle=i)
        self.session.capture(etape, {
            "phase": PHASE_TACTILE, "angle_index": i, "essai": self.essai,
            "angle": pose, "pouce": pouce["pouce"], "saisie": saisie,
            "pouce_vu": dict(self.pouce_vu), "validation": "en_attente"})

        decision = self._attendre_validation(etape)
        self.session.valider(etape, decision["validation"],
                             commentaire=decision.get("commentaire", ""))
        self._noter_progression(PHASE_TACTILE, i, pose, decision["validation"])

        suite = decision.get("suite", "suivant")
        if suite == "reprendre":
            self.machine.declencher("reprendre_angle", angle=i, essai=self.essai,
                                    validation=decision["validation"])
        else:
            self.machine.declencher("angle_termine", angle=i,
                                    validation=decision["validation"])
        return suite

    # ── Étapes élémentaires ───────────────────────────────────────────────────

    def _positionner(self, i: int, consigne: float) -> dict:
        """
        Amène le plateau, puis **mesure** l'angle atteint.

        On enregistre l'angle mesuré, jamais l'angle commandé : la caméra donne
        la vérité terrain, la consigne n'est qu'une intention. ``null`` est un
        résultat légitime — à certaines positions aucun marqueur ne se décode,
        et un angle inventé serait pire qu'un angle absent.
        """
        self.machine.declencher("positionner", angle=i, consigne=consigne)
        r = self.banc.aller_a(consigne, timeout=self.reglages.timeout_angle)
        arrete = self.banc.arreter_plateau()
        mesure = self.banc.angle.mesurer(90)

        pose = {"consigne_deg": round(float(consigne) % 360.0, 2),
                "passes": r.get("passes"),
                "arret_confirme": bool(arrete)}
        pose.update(mesure.to_dict())
        if mesure.connu:
            pose["ecart_deg"] = round((mesure.angle - consigne + 180) % 360 - 180, 2)
        else:
            pose["ecart_deg"] = None

        self.angle_courant = pose
        etape = f"{'visuel' if self.phase == PHASE_VISUELLE else 'tactile'}/angle_{i:02d}"
        self.session.write_json(f"{etape}/angle.json", pose)
        self.session.mark(f"{etape}/positionne", **pose)

        if not arrete:
            # Un plateau qui tourne invalide toutes les captures qui suivent.
            raise HandError("le plateau n'a pas pu être immobilisé")
        self.machine.declencher("angle_arrete", angle=i,
                                mesure=pose["mesure_deg"], ecart=pose["ecart_deg"])
        return pose

    def _infos_cinematique(self) -> dict:
        """Le modèle employé, et s'il est étalonné — écrit une fois par session."""
        try:
            from ..hw.cinematique import Cinematique  # noqa: PLC0415

            if self._cinematique in (None, False):
                self._cinematique = Cinematique(
                    counts_par_radian=self.reglages.counts_par_radian)
            return self._cinematique.infos()
        except Exception as e:  # noqa: BLE001
            return {"disponible": False, "erreur": str(e)}

    def _bouts_doigts(self, positions: dict) -> dict:
        """
        Position 3D des bouts de doigts, en repère main.

        Chargée paresseusement : une session visuelle n'en a que faire, et
        l'URDF peut manquer sans que cela doive empêcher d'acquérir.
        """
        if self._cinematique is False:
            return {}
        try:
            if self._cinematique is None:
                from ..hw.cinematique import Cinematique  # noqa: PLC0415

                self._cinematique = Cinematique(
                    counts_par_radian=self.reglages.counts_par_radian)
            return self._cinematique.bouts(positions)
        except Exception as e:  # noqa: BLE001
            log.warning("cinématique indisponible : %s", e)
            self._cinematique = False        # on n'y revient pas à chaque image
            return {}

    def _mesurer_pose(self) -> dict:
        """
        Consigne la géométrie de la caméra au début de la session.

        Le centre du plateau est stocké **en pixels** : une caméra déplacée rend
        tous les angles faux, et rien dans le jeu de données ne le dit. En
        gardant la mesure — aplatissement des carreaux, taille apparente,
        élévation — on peut au moins savoir *après coup* si une session a été
        acquise dans une pose valable, et écarter celles qui ne l'étaient pas
        plutôt que de les mélanger aux bonnes.
        """
        try:
            from ..hw import pose as _pose  # noqa: PLC0415

            return _pose.observer(self.banc.camera, self.banc.angle, n=12).to_dict()
        except Exception as e:  # noqa: BLE001 — une pose non mesurée n'arrête rien
            log.warning("pose caméra non mesurée : %s", e)
            return {"mesuree": False, "erreur": str(e)}

    def _poses_carreaux(self) -> dict:
        """
        Pose 3D des carreaux du plateau, dans le repère caméra.

        Le centre de rotation est connu **en pixels** : cela suffit à mesurer un
        angle, mais pas à situer le plateau dans l'espace. Or une reconstruction
        multi-vues a besoin de l'axe de rotation en 3D — sans lui, six images
        prises à six angles ne se recollent pas.

        Les carreaux sont sur un solide rigide autour de cet axe : leurs poses
        suffisent à le retrouver après coup. On les enregistre donc au début de
        la session, quand la caméra n'a pas encore eu l'occasion de bouger.

        Comme pour le pouce, la taille supposée voyage avec la pose : la
        distance rendue lui est proportionnelle, et une mesure au pied à
        coulisse permettra de tout corriger d'un coup.
        """
        from ..hw.angle import TAILLE_MARQUEUR_POUCE_M  # noqa: PLC0415

        try:
            tr = self.banc.angle.tracker
            connus = set(tr.marker_angles)
            poses: dict = {}
            for _ in range(10):
                couleur, _p, _t = self.banc.camera.grab_aruco()
                coins, ids = tr.detect_markers(couleur)
                if ids is None or not len(ids):
                    continue
                for mid, pose in tr.estimate_marker_pose_3d(
                        coins, ids, TAILLE_MARQUEUR_POUCE_M,
                        target_ids=connus).items():
                    poses.setdefault(str(mid), []).append(pose)
            from ..hw.angle import _pose_mediane  # noqa: PLC0415

            return {"taille_marqueur_m": TAILLE_MARQUEUR_POUCE_M,
                    "carreaux": {k: _pose_mediane(v) for k, v in poses.items()}}
        except Exception as e:  # noqa: BLE001
            log.warning("poses 3D des carreaux non mesurées : %s", e)
            return {"mesurees": False, "erreur": str(e)}

    def _vider_flux_brut(self, etape: str) -> dict:
        """
        Écrit le flux accumulé et repart à zéro, à la fin de chaque angle.

        Le tampon d'enregistrement plafonne à 400 000 trames — 77 Mo — et
        au-delà il **cesse d'accumuler** plutôt que de remplir la mémoire de la
        machine virtuelle. À ~600 trames par seconde cela fait onze minutes,
        alors qu'une session de six angles en demande quarante : deux sessions
        déjà enregistrées ont été tronquées exactement à 400 000, et la fin de
        leur acquisition n'existe nulle part.

        Vider par angle supprime le plafond comme problème, et donne en prime un
        flux par angle plutôt qu'un seul bloc à découper après coup. La
        continuité n'y perd rien : chaque bloc reste intégral, sans filtrage ni
        déduplication, et les repères horodatés disent où l'on est.
        """
        if self.banc.hand is None or not self.banc.hand.enregistre:
            return {}
        trames = self.banc.hand.arreter_enregistrement()
        e = self.session.save_raw_stream(f"{etape}/brut", trames) if trames else {}
        if e:
            log.info("flux brut de %s : %s trames sur %s s",
                     etape, e.get("count"), e.get("span_s"))
        # On relance aussitôt : le silence entre deux angles fait partie de la
        # ligne de base, et l'interrompre priverait le décodage de sa référence.
        self.banc.hand.demarrer_enregistrement()
        return e

    def _capturer_image(self, etape: str, index: int, **extra) -> dict:
        """
        Une image, horodatée **à la prise de vue** et non à l'écriture.

        Chaque image porte ses propres conditions d'acquisition plutôt que de
        renvoyer aux métadonnées de session. Trois d'entre elles peuvent changer
        en cours de route, et une image prise dans de mauvaises conditions est
        indiscernable d'une bonne une fois écrite :

        * l'**exposition** bascule entre 600 µs pour lire les marqueurs et celle
          du profil pour le jeu de données ; une image prise du mauvais côté de
          la bascule est sombre sans raison apparente ;
        * la **lampe met plus de deux secondes à s'établir**, et le variateur
          acquitte tout de suite — une image prise pendant ce temps est à un
          éclairement inconnu, ni l'ancien ni le nouveau ;
        * le **PWM** lui-même peut avoir été changé à la main entre deux étapes.
        """
        couleur, profondeur, t_perf = self.banc.camera.grab_dataset()
        cam, bench = self.banc.camera, self.banc.bench
        extra = dict(extra)
        extra["acquisition"] = {
            "mode": cam.mode,
            "exposition_us": cam.exposition_dataset(),
            "pwm": bench.pwm if bench.ouvert else None,
            "lampe_etablie": bench.etablie if bench.ouvert else None,
            "reste_a_etablir_s": (round(bench.reste_a_etablir, 1)
                                  if bench.ouvert else None),
        }
        if self.banc.hand is not None and self.banc.hand.ouverte:
            etat = self.banc.hand.etat_tactile()
            if etat is not None and self.banc.hand.reader.zeroed:
                extra["tactile"] = etat.to_dict()
                positions = self.banc.hand.positions()
                extra["positions"] = positions
                # Les bouts de doigts en 3D, à côté de l'image qui les montre.
                # C'est ce qui permet, plus tard, de dire *où* un contact a eu
                # lieu — un count seul ne situe rien.
                bouts = self._bouts_doigts(positions)
                if bouts:
                    extra["cinematique"] = bouts
        e = self.session.save_image(etape, index, couleur, profondeur,
                                    t=self.session.rel(t_perf), **extra)
        self.derniere_image = {"etape": etape, "index": index, "t": e["t"],
                               "chemin": e["color"]}
        return e

    def _attendre_le_pouce(self, etape: str) -> dict:
        """
        Laisse l'opérateur poser le pouce, en surveillant la stabilité.

        Rend la main dès que le critère est satisfait — ou dès que l'opérateur
        décide de passer outre. Trois issues, toutes trois écrites.
        """
        if not self.reglages.pouce_requis:
            pouce = {"critere": "pouce_stable", "requis": False,
                     "statut": S.POUCE_NON_REQUIS,
                     "motif": "critère non exigé pour cette session",
                     "reglages": self._reglages_pouce()}
            self._decision_pouce = {"issue": S.POUCE_NON_REQUIS}
            self.session.write_json(f"{etape}/pouce.json", pouce)
            return {"issue": S.POUCE_NON_REQUIS, "pouce": pouce}

        self._porte_pouce.clear()
        self._decision_pouce = {}
        self._detecteur.reinitialiser()
        t_arme = self.session.t
        self.session.mark(f"{etape}/pouce_attente")

        auto = None
        t_declenche = None
        prochain_regard = 0.0
        while not self._porte_pouce.is_set():
            # Le carreau du pouce, régulièrement : il ne conditionne rien, mais
            # savoir que le pouce est hors champ **avant** de fermer évite une
            # capture où l'on ne voit pas ce qui touche l'objet. Espacé, parce
            # qu'une douzaine d'images coûte plus qu'une lecture de pression.
            if time.time() > prochain_regard:
                prochain_regard = time.time() + 3.0
                try:
                    from ..hw.angle import voir_le_pouce  # noqa: PLC0415

                    self.pouce_vu = voir_le_pouce(self.banc.camera,
                                                  self.banc.angle, n=6)
                except Exception:  # noqa: BLE001 — un regard raté n'arrête rien
                    pass
            p = self.banc.hand.pression("thumb")
            if p is not None:
                etat = self._detecteur.ajouter(time.perf_counter(), p)
                self.suivi_pouce = {
                    "pression": round(etat.pression, 4),
                    "minimum": round(etat.minimum, 4),
                    "amplitude": round(etat.amplitude, 4),
                    "remplissage": round(etat.remplissage, 3),
                    "raison": etat.raison, "pret": etat.pret,
                    "pivot": self.banc.hand.positions((2,)).get(2),
                }
                if etat.pret:
                    if t_declenche is None:
                        t_declenche = time.perf_counter()
                        self.session.mark(f"{etape}/contact_stable",
                                          pression=round(p, 4))
                    elif time.perf_counter() - t_declenche >= DELAI_RETRAIT:
                        auto = {"issue": S.POUCE_SATISFAIT,
                                "pression": round(p, 4),
                                "minimum": round(etat.minimum, 4),
                                "amplitude": round(etat.amplitude, 4)}
                        break
                elif t_declenche is not None:
                    t_declenche = None
                    self.session.mark(f"{etape}/contact_perdu")
            time.sleep(0.05)

        d = auto or dict(self._decision_pouce)
        issue = d.get("issue", S.POUCE_ECHEC)
        pouce = {
            "critere": "pouce_stable",
            "requis": True,
            "statut": issue if issue in S.ISSUES_POUCE else S.POUCE_ECHEC,
            "decide_par": "detecteur" if auto else "operateur",
            "motif": d.get("motif", ""),
            "reglages": self._reglages_pouce(),
            "pression_au_declenchement": d.get("pression"),
            "minimum_fenetre": d.get("minimum"),
            "amplitude_fenetre": d.get("amplitude"),
            "pivot_counts": self.banc.hand.positions((2,)).get(2),
            "t_arme": t_arme, "t_declenche": self.session.t,
        }
        self.session.write_json(f"{etape}/pouce.json", pouce)
        self.suivi_pouce = {}
        if issue == "arret":
            return {"issue": "arret", "pouce": pouce}
        if issue == "saut":
            pouce["statut"] = S.POUCE_ECHEC
            return {"issue": "saut", "pouce": pouce}
        self._decision_pouce = {"issue": pouce["statut"], "motif": pouce["motif"]}
        return {"issue": pouce["statut"], "pouce": pouce}

    def _reglages_pouce(self) -> dict:
        return {"seuil": self.reglages.seuil_pouce,
                "epsilon": self.reglages.epsilon_pouce,
                "duree_s": self.reglages.duree_pouce, "zone": "thumb"}

    @contextlib.contextmanager
    def _serrage_borne(self, etape: str):
        """
        Ouvre la main d'office si elle reste serrée trop longtemps.

        La DH116 n'est pas rétro-entraînable : une main fermée sur un objet
        continue de pousser jusqu'à ce qu'on lui dise d'arrêter, et une
        exception levée entre la fermeture et le relâchement la laisserait
        ainsi. ``timeout_fermeture`` ne couvre pas ce cas — il borne la
        recherche du contact, pas ce qui vient après : les photos de la saisie,
        la mesure de pose, et tout ce qu'on ajoutera un jour à cet endroit.

        Le minuteur tourne sur un fil séparé et peut donc ouvrir la main
        pendant que la boucle de fermeture la commande encore. C'est assumé :
        des deux issues d'une course, une main qui s'ouvre est la bonne.

        Le déclenchement est **journalisé et signalé**, jamais silencieux : une
        prise relâchée en cours de capture donne un enregistrement incohérent,
        et il faut pouvoir l'écarter plutôt que le découvrir plus tard.
        """
        plafond = float(getattr(self.reglages, "duree_serrage_max", 0) or 0)
        etat = {"declenche": False}
        if plafond <= 0:
            yield etat
            return

        def liberer():
            etat["declenche"] = True
            try:
                if self.banc.hand is not None and self.banc.hand.ouverte:
                    self.banc.hand.ouvrir()
            except Exception:  # noqa: BLE001 — la sécurité ne doit rien relever
                log.exception("ouverture de sécurité impossible")
            self.session.mark(f"{etape}/serrage_borne",
                              plafond_s=plafond, motif="duree_depassee")
            log.error("Main ouverte d'office : serrage de plus de %.0f s "
                      "à l'étape %s", plafond, etape)
            # Le serveur relit cette liste pour la remonter en bannière ; le
            # journal de session en garde la trace horodatée quoi qu'il arrive.
            self.serrages_bornes.append(
                {"etape": etape, "t": round(self.session.t, 2),
                 "plafond_s": plafond})

        minuteur = threading.Timer(plafond, liberer)
        minuteur.daemon = True
        minuteur.start()
        try:
            yield etat
        finally:
            minuteur.cancel()

    def _fermer(self, etape: str) -> dict:
        """
        Referme les doigts et s'arrête au contact, **en photographiant en route**.

        Passe par ``vt_tactile.envelop``, qui arrête chaque doigt dès qu'il
        touche : un doigt arrêté ne pousse plus, les autres continuent, et la
        main épouse l'objet au lieu de le serrer.

        Les images de la fermeture sont ce qui manquait le plus. Le tactile et
        les positions sont enregistrés en continu, mais entre « avant » et
        « saisi » il n'y avait aucune vue — or c'est précisément pendant la
        fermeture que les doigts se déplacent et rencontrent l'objet. Sans ces
        images, un état tactile n'a rien à quoi être mis en regard : on sait
        qu'un doigt a touché, pas ce qu'il a touché.

        Une image toutes les ``images_fermeture`` itérations : la boucle tourne
        à ~16 Hz et une fermeture dure une vingtaine de secondes, donc un pas de
        3 donne une cinquantaine de vues — assez pour suivre le mouvement, sans
        que l'écriture ne ralentisse la boucle de contact.
        """
        from vt_tactile.envelop import EnvelopConfig, envelop  # noqa: PLC0415

        self.session.mark(f"{etape}/fermeture_debut")
        cfg = EnvelopConfig(
            max_current=self.reglages.max_current,
            velocity=self.reglages.velocity,
            timeout=self.reglages.timeout_fermeture,
            thumb_pivot=None,           # déjà posé par l'opérateur
        )
        compte = {"pas": 0, "images": 0}
        pas_image = max(0, int(self.reglages.images_fermeture))

        def pendant(echantillon, etat, contacts):
            """Rappel de la boucle d'enveloppement, à chaque itération."""
            compte["pas"] += 1
            if not pas_image or compte["pas"] % pas_image:
                return
            try:
                self._capturer_image(
                    f"{etape}/01_fermeture", compte["images"],
                    phase=PHASE_TACTILE, iteration=compte["pas"],
                    contacts=sorted(contacts),
                    positions=echantillon.get("positions"),
                    courants=echantillon.get("currents"))
                compte["images"] += 1
            except Exception:  # noqa: BLE001 — une image ratée n'arrête pas la prise
                log.warning("image de fermeture non écrite", exc_info=True)

        t0 = self.session.t
        resultat = envelop(self.banc.hand.hand, self.banc.hand.reader, cfg,
                           on_step=pendant if pas_image else None)
        d = resultat.to_dict() if hasattr(resultat, "to_dict") else dict(resultat)
        contacts = sorted(d.get("contacts", {}) or {})
        self.session.mark(f"{etape}/fermeture_fin", contacts=contacts,
                          images_fermeture=compte["images"])
        return {"contacts": contacts, "duree_s": round(self.session.t - t0, 2),
                "images_fermeture": compte["images"], "detail": d}

    def _attendre_validation(self, etape: str) -> dict:
        self._porte_validation.clear()
        self._decision_validation = {}
        self.session.mark(f"{etape}/validation_attente")
        self._porte_validation.wait()
        d = dict(self._decision_validation) or {"validation": "en_attente"}
        if d.get("validation") not in ("valide", "invalide"):
            d["validation"] = "en_attente"
        return d

    def _recuperer(self, i: int, cause: str) -> None:
        """
        Remet le banc en état après une erreur, sans perdre la session.

        Un angle perdu n'en perd pas d'autres. La main est rouverte — c'est la
        seule action vraiment nécessaire, puisqu'elle n'est pas
        rétro-entraînable et resterait fermée sur l'objet.
        """
        try:
            self.machine.declencher("recuperer", angle=i, cause=cause)
            if self.banc.hand is not None and self.banc.hand.ouverte:
                self.banc.hand.ouvrir()
            self.machine.declencher("reprendre", angle=i)
        except TransitionRefusee:
            log.warning("récupération impossible depuis %s", self.machine.etat)

    def _noter_progression(self, phase: str, i: int, pose: dict,
                           validation: str) -> None:
        self.progression.append({
            "phase": phase, "angle_index": i,
            "consigne_deg": pose.get("consigne_deg"),
            "mesure_deg": pose.get("mesure_deg"),
            "ecart_deg": pose.get("ecart_deg"),
            "validation": validation, "t": self.session.t,
        })

    # ── Décisions de l'opérateur ──────────────────────────────────────────────

    def armer(self, issue: str = S.POUCE_SATISFAIT, motif: str = "") -> dict:
        """
        Tranche le critère du pouce et laisse partir la fermeture.

        Args:
            issue: ``satisfait``, ``contourne`` ou ``non_requis``.
            motif: **obligatoire** pour ``contourne``. Un contournement sans
                motif est un contournement silencieux, et c'est exactement ce
                que ce protocole existe pour empêcher.
        """
        if issue not in S.ISSUES_ARMANTES:
            raise ValueError(f"issue inconnue : {issue!r} "
                             f"(attendu : {', '.join(S.ISSUES_ARMANTES)})")
        if issue == S.POUCE_CONTOURNE and not motif.strip():
            raise ValueError("un contournement doit porter un motif")
        etat = dict(self.suivi_pouce)
        self._decision_pouce = {
            "issue": issue, "motif": motif.strip(),
            "pression": etat.get("pression"), "minimum": etat.get("minimum"),
            "amplitude": etat.get("amplitude"),
        }
        self._porte_pouce.set()
        return dict(self._decision_pouce)

    def sauter_angle(self, motif: str = "") -> dict:
        """Passe cet angle sans saisie. La capture est écrite comme invalide."""
        self._decision_pouce = {"issue": "saut", "motif": motif.strip()}
        self._porte_pouce.set()
        return dict(self._decision_pouce)

    def valider(self, validation: str, commentaire: str = "",
                suite: str = "suivant") -> dict:
        """
        Valide ou invalide la capture, et dit ce qu'on fait ensuite.

        Args:
            suite: ``"suivant"`` pour passer à l'angle suivant, ``"reprendre"``
                pour refaire **le même angle**. Une capture ratée ne condamne
                pas l'angle : la main a pu glisser, l'objet bouger, la prise
                être mauvaise. Chaque reprise écrit son propre essai, à côté du
                précédent — écraser priverait de la comparaison qui dit pourquoi
                le premier était raté.
        """
        if validation not in ("valide", "invalide"):
            raise ValueError("validation attendue : « valide » ou « invalide »")
        if suite not in ("suivant", "reprendre"):
            raise ValueError("suite attendue : « suivant » ou « reprendre »")
        self._decision_validation = {"validation": validation,
                                     "commentaire": commentaire.strip(),
                                     "suite": suite}
        self._porte_validation.set()
        return dict(self._decision_validation)

    def pivot_pouce(self, counts: int) -> dict:
        """
        Déplace l'opposition du pouce, à tout moment.

        C'est l'axe qui amène le pouce face aux autres doigts, et donc celui
        qui décide s'il y a préhension : sans opposition, la flexion referme le
        pouce à côté de l'objet. Disponible en permanence, y compris pendant que le protocole
        attend : c'est précisément à ce moment-là qu'on s'en sert.
        """
        if self.banc.hand is None or not self.banc.hand.ouverte:
            raise RuntimeError("la main n'est pas montée")
        self.banc.hand.pivot_pouce(int(counts))
        pos = self.banc.hand.positions((2,)).get(2)
        return {"pivot": pos, "pression_pouce": self.banc.hand.pression("thumb")}

    # ── État ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Tout ce que l'interface affiche du protocole."""
        m = self.machine.snapshot() if self.machine else {"etat": S.REPOS}
        angles = list(self.reglages.angles)
        return {
            "en_cours": self.en_cours,
            "machine": m,
            "phase": self.phase,
            "objet": self.reglages.objet,
            "session": str(self.session.root) if self.session else None,
            "angle_index": self.index_angle,
            "angles_total": len(angles),
            "angles": angles,
            "angle_courant": dict(self.angle_courant),
            "pouce": dict(self.suivi_pouce),
            "pouce_requis": self.reglages.pouce_requis,
            "pouce_vu": dict(self.pouce_vu),
            "essai": self.essai,
            "pivot_depart": self.reglages.pivot_depart,
            "progression": list(self.progression),
            "derniere_image": dict(self.derniere_image),
            "attend_pouce": (self.machine is not None
                             and self.machine.etat == S.POUCE_EN_ATTENTE),
            "attend_validation": (self.machine is not None
                                  and self.machine.etat == S.VALIDATION),
        }
