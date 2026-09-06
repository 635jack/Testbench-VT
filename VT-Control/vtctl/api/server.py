#!/usr/bin/env python3
"""
server.py — l'API locale et la page qui va avec.

HTTP et Server-Sent Events, bibliothèque standard seule. Ni framework, ni
websocket, ni dépendance : la même recette que ``dh116-web``, qui tourne déjà
sur ce banc.

**Une seule instance possède le matériel** : le serveur ne parle qu'au
:class:`~vtctl.hw.banc.Banc` et au :class:`~vtctl.protocol.runner.Runner` qu'on
lui passe. Il n'ouvre rien lui-même, et deux navigateurs ouverts ne doublent
pas la charge sur le bus — l'échantillonnage est fait une fois, et le flux SSE
recopie un état déjà calculé.

Les images sont servies depuis le **cache** du ``CameraOwner``, jamais par une
lecture parallèle : le pipeline n'admet qu'un lecteur.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config
from ..protocol import states as S
from ..protocol.runner import PHASE_TACTILE, PHASE_VISUELLE, Runner
from ..store import recover
from .surveillance import AVERT, ERREUR, Surveillance

log = logging.getLogger("vtctl.api")

STATIC = Path(__file__).resolve().parent.parent / "ui"

#: Cadence du flux d'état, en hertz. Vingt suffit pour que le tactile paraisse
#: continu, et coûte moins qu'un rafraîchissement à chaque trame.
CADENCE_SSE = 20.0

#: Cadence de rafraîchissement de l'image quand aucun protocole ne tourne.
#: Volontairement basse : une image de 640x480 encodée en JPEG à 30 Hz sature
#: le tunnel SSH avant de servir à quoi que ce soit.
CADENCE_APERCU = 4.0


class Service:
    """
    Ce que l'API expose : le banc, le protocole, et l'état consolidé.

    Args:
        banc: le :class:`~vtctl.hw.banc.Banc`, déjà ouvert.
        base_sessions: où écrire les sessions.
    """

    def __init__(self, banc, base_sessions, reglages: "config.Reglages | None" = None):
        self.banc = banc
        self.base = Path(base_sessions)
        self.reglages = reglages or config.Reglages()
        self.runner: "Runner | None" = None
        self._lock = threading.Lock()
        self._apercu = threading.Event()
        self._fil_apercu: "threading.Thread | None" = None
        self.journal_ui: list = []
        self.surveillance = Surveillance(banc, self.noter,
                                         runner=lambda: self.runner)
        #: Cinématique de la main, chargée une fois. ``False`` = indisponible,
        #: et l'on n'y revient pas à chaque rafraîchissement.
        self._cinematique = None

        #: L'entrée du journal reprise au démarrage, ``None`` si aucune.
        self._etalonnage_repris = None
        # Reprise du dernier étalonnage. Sans ça, le facteur mesuré meurt avec
        # le processus et le squelette repart sur l'hypothèse par défaut — π/2
        # de course, là où la main en fait 80°, soit 12 % d'erreur sur chaque
        # doigt affiché. Un étalonnage explicite passé en réglages garde la
        # main : c'est une décision de l'opérateur, elle prime sur l'historique.
        if self.reglages.counts_par_radian is None:
            from ..store import etalonnage as _et  # noqa: PLC0415

            try:
                dernier = _et.dernier()
            except OSError:
                dernier = None      # journal illisible : on ne bloque pas le banc
            if dernier and dernier.get("counts_par_radian"):
                self.reglages.counts_par_radian = dernier["counts_par_radian"]
                self._etalonnage_repris = dernier

    # ── Aperçu caméra ─────────────────────────────────────────────────────────

    def demarrer_apercu(self) -> None:
        """
        Alimente le cache d'images quand le protocole n'en lit pas.

        Le fil s'efface dès qu'un protocole tourne : c'est lui qui lit alors, et
        deux lecteurs sur le même pipeline sont exactement ce qu'on évite.
        """
        if self._fil_apercu is not None:
            return
        self._apercu.clear()

        def boucle():
            periode = 1.0 / CADENCE_APERCU
            while not self._apercu.is_set():
                try:
                    if (self.banc.camera.ouverte
                            and not (self.runner and self.runner.en_cours)):
                        self.banc.camera.rafraichir()
                except Exception:  # noqa: BLE001 — l'aperçu ne doit jamais tuer le serveur
                    pass
                time.sleep(periode)

        self._fil_apercu = threading.Thread(target=boucle, name="apercu", daemon=True)
        self._fil_apercu.start()
        self.surveillance.demarrer()

    def arreter_apercu(self) -> None:
        self._apercu.set()
        self._fil_apercu = None
        self.surveillance.arreter()

    # ── État ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """L'état consolidé : protocole, main, banc, caméra, angle."""
        etat = {
            "t": round(time.time(), 3),
            "simulation": self.banc.simulation,
            "reglages": self.reglages.to_dict(),
            "banc": self.banc.bench.infos(),
            "camera": {"ouverte": self.banc.camera.ouverte,
                       "mode": self.banc.camera.mode,
                       "bascules": self.banc.camera.bascules,
                       "exposition_us": (self.banc.camera.exposition_dataset()
                                         if self.banc.camera.ouverte else None)},
            "ressources": self.banc.mgr.tenues(),
            "niveaux": self._niveaux(),
            "alertes": self.surveillance.actives(),
            "journal": self.journal_ui[-40:],
        }
        derniere = (self.banc.camera.derniere_image()
                    if self.banc.camera.ouverte else None)
        etat["image"] = {"disponible": derniere is not None,
                         "t": None if derniere is None else round(derniere[2], 3),
                         "mode": None if derniere is None else derniere[3]}
        etat["main"] = (self.banc.hand.snapshot() if self.banc.hand
                        else {"prete": False, "tactile": [], "motors": []})
        etat["squelette"] = self._squelette(etat["main"])
        etat["protocole"] = (self.runner.snapshot() if self.runner
                             else {"en_cours": False,
                                   "machine": {"etat": S.REPOS,
                                               "description": S.DESCRIPTION[S.REPOS],
                                               "evenements": [], "contexte": {}},
                                   "progression": []})
        return etat

    _cache_niveaux: "list | None" = None

    def _niveaux(self) -> list:
        """
        Les niveaux d'éclairage du profil VT-Light, plus le point ArUco.

        Lus une fois dans ``light_profile.json`` : les recopier ici les ferait
        diverger du profil, et c'est le profil qui fait foi.
        """
        if self._cache_niveaux is None:
            try:
                profil = self.banc.camera.profil()
                self._cache_niveaux = [
                    {"nom": n, "pwm": profil.pwm_of(n)} for n in profil.level_names
                ]
            except Exception:  # noqa: BLE001 — l'interface vit sans les niveaux
                self._cache_niveaux = []
        return (self._cache_niveaux
                + [{"nom": "aruco", "pwm": self.banc.pwm_aruco},
                   {"nom": "éteinte", "pwm": 0}])

    def _squelette(self, main: dict) -> dict:
        """
        Le squelette de la main aux positions courantes, pour l'affichage.

        Calculé côté serveur : la cinématique y est déjà, et refaire le calcul
        en JavaScript demanderait d'y recopier l'URDF — donc de le faire
        diverger le jour où la géométrie change.
        """
        if self._cinematique is False or not main.get("prete"):
            return {}
        positions = {m["id"]: m.get("position") for m in main.get("motors", [])
                     if m.get("position") is not None}
        if not positions:
            return {}
        try:
            if self._cinematique is None:
                from ..hw.cinematique import Cinematique  # noqa: PLC0415

                self._cinematique = Cinematique(
                    counts_par_radian=self.reglages.counts_par_radian)
            return self._cinematique.squelette(positions)
        except Exception as e:  # noqa: BLE001
            log.warning("squelette indisponible : %s", e)
            self._cinematique = False
            return {}

    def noter(self, message: str, niveau: str = "info") -> None:
        """Une ligne pour le bandeau de l'interface. Le vrai journal est ailleurs."""
        self.journal_ui.append({"t": round(time.time(), 3),
                                "niveau": niveau, "message": message})
        del self.journal_ui[:-200]

    # ── Commandes ─────────────────────────────────────────────────────────────

    def commande(self, p: dict) -> dict:
        cmd = p.get("cmd")
        fn = getattr(self, f"_cmd_{cmd}", None) if cmd else None
        if fn is None:
            raise ValueError(f"commande inconnue : {cmd!r}")
        with self._lock:
            return fn(p)

    # -- protocole --

    def _cmd_session_start(self, p: dict) -> dict:
        if self.runner and self.runner.en_cours:
            raise RuntimeError("une session est déjà en cours")
        r = self.reglages
        if p.get("objet"):
            r.objet = str(p["objet"]).strip()
        if p.get("angles"):
            r.angles = [float(a) % 360.0 for a in p["angles"]]
        elif p.get("nb_angles"):
            n = max(1, int(p["nb_angles"]))
            pas = float(p.get("pas", 360.0 / n))
            r.angles = [(i * pas) % 360.0 for i in range(n)]
        if "pouce_requis" in p:
            r.pouce_requis = bool(p["pouce_requis"])
        for cle in ("tolerance_deg", "max_current", "velocity", "pivot_depart",
                    "seuil_pouce", "epsilon_pouce", "duree_pouce",
                    "objet_reference", "objet_echelle", "objet_materiau",
                    "objet_pose"):
            if cle in p:
                setattr(r, cle, type(getattr(r, cle))(p[cle]))

        # L'ordre vient de states.ORDRE_PHASES : tactile d'abord. Le répéter en
        # dur ici l'aurait fait diverger, et c'est exactement ce qui est arrivé.
        phases = p.get("phases") or list(S.ORDRE_PHASES)
        if self.banc.hand is None and PHASE_TACTILE in phases:
            phases = [x for x in phases if x != PHASE_TACTILE]
            self.noter("Phase tactile écartée : la main n'est pas montée.", "avert")
        self.runner = Runner(self.banc, r, self.base, phases=tuple(phases))
        session = self.runner.demarrer()
        self.noter(f"Session « {r.objet} » démarrée — {len(r.angles)} angles, "
                   f"phases {', '.join(phases)}.")
        return {"session": str(session.root), "angles": r.angles,
                "phases": list(phases)}

    def _cmd_session_stop(self, p: dict) -> dict:
        self._exige_runner()
        self.runner.arreter(p.get("motif", "arrêt demandé depuis l'interface"))
        self.noter("Arrêt demandé — le protocole termine l'étape en cours.", "avert")
        return {"arret": True}

    def _cmd_armer(self, p: dict) -> dict:
        self._exige_runner()
        r = self.runner.armer(p.get("issue", S.POUCE_SATISFAIT), p.get("motif", ""))
        self.noter(f"Pouce : {r['issue']}"
                   + (f" — {r['motif']}" if r.get("motif") else ""))
        return r

    def _cmd_sauter_angle(self, p: dict) -> dict:
        self._exige_runner()
        r = self.runner.sauter_angle(p.get("motif", ""))
        self.noter("Angle sauté.", "avert")
        return r

    def _cmd_valider(self, p: dict) -> dict:
        self._exige_runner()
        r = self.runner.valider(p.get("validation", "valide"),
                                p.get("commentaire", ""),
                                p.get("suite", "suivant"))
        suite = "on refait cet angle" if r["suite"] == "reprendre" else "angle suivant"
        self.noter(f"Capture {r['validation']} — {suite}"
                   + (f" · {r['commentaire']}" if r.get("commentaire") else ""))
        return r

    def _cmd_pivot(self, p: dict) -> dict:
        """Déplace l'opposition du pouce. Disponible à tout moment."""
        if self.runner is not None and self.runner.en_cours:
            return self.runner.pivot_pouce(int(p.get("counts", 0)))
        self._exige_main()
        self.banc.hand.pivot_pouce(int(p.get("counts", 0)))
        return {"pivot": self.banc.hand.positions((2,)).get(2),
                "pression_pouce": self.banc.hand.pression("thumb")}

    def _cmd_pouce_vu(self, p: dict) -> dict:
        """Le carreau du pouce est-il dans le champ de la caméra ?"""
        from ..hw.angle import voir_le_pouce  # noqa: PLC0415

        r = voir_le_pouce(self.banc.camera, self.banc.angle, n=int(p.get("n", 12)))
        self.noter(f"Carreau du pouce : {'vu' if r['vu'] else 'PAS vu'} "
                   f"sur {r['images']}/{r['tentees']} images.",
                   "info" if r["vu"] else "avert")
        return r

    # -- main : pilotage direct, disponible en permanence --

    def _cmd_position(self, p: dict) -> dict:
        """
        Consigne de position, moteur par moteur.

        Disponible **à tout moment**, y compris pendant que le protocole attend
        le pouce : c'est ainsi que l'opérateur amène le pouce au contact, et
        qu'il ajuste les autres doigts.
        """
        self._exige_main()
        cibles = {int(k): int(v) for k, v in (p.get("targets") or {}).items()}
        if not cibles:
            raise ValueError("aucune cible")
        return self.banc.hand.aller_a(cibles, p.get("velocity"), p.get("max_current"))

    def _cmd_preset(self, p: dict) -> dict:
        self._exige_main()
        return self.banc.hand.backend.preset(p.get("name"), p.get("velocity"),
                                             p.get("max_current"))

    def _cmd_ouvrir_main(self, p: dict) -> dict:
        self._exige_main()
        return {"ouverte": self.banc.hand.ouvrir()}

    def _cmd_zero(self, p: dict) -> dict:
        self._exige_main()
        n = self.banc.hand.zero(float(p.get("seconds", 2.0)))
        self.noter(f"Zéro tactile : ligne de base sur {n} trames.")
        return {"trames": n}

    def _cmd_reconnexion(self, p: dict) -> dict:
        self._exige_main()
        self.noter("Reprise du bus EtherCAT…", "avert")
        return self.banc.hand.reconnecter()

    def _cmd_reconnecter_camera(self, p: dict) -> dict:
        self._refuser_pendant_acquisition(p, "la caméra")
        self.noter("Reconnexion de la caméra…", "avert")
        r = self.banc.camera.reconnecter()
        self.surveillance.resoudre("camera_muette")
        self.noter(f"Caméra reconnectée (ouverture n°{r['ouvertures']}).")
        return r

    def _cmd_reconnecter_serie(self, p: dict) -> dict:
        self._refuser_pendant_acquisition(p, "le port série")
        self.noter("Reconnexion du port série…", "avert")
        r = self.banc.bench.reconnecter()
        self.noter(f"Port série reconnecté sur {r['port']}, PWM réémis à "
                   f"{r['pwm_reemis']} — le firmware redémarre lampe à fond.")
        return r

    def _cmd_reconnecter_main(self, p: dict) -> dict:
        self._exige_main()
        self._refuser_pendant_acquisition(p, "la main")
        self.noter("Reprise du bus EtherCAT…", "avert")
        r = self.banc.hand.reconnecter()
        self.surveillance.resoudre("main_muette")
        self.noter("Bus EtherCAT repris.")
        return r

    def _refuser_pendant_acquisition(self, p: dict, quoi: str) -> None:
        """
        Une reconnexion pendant une acquisition la casse. On la refuse.

        Sauf demande explicite : quand le matériel est déjà tombé, reconnecter
        est justement ce qu'il faut faire, et la session est de toute façon
        perdue. Mais ce doit être une décision, pas un effet de bord.
        """
        if self.runner is not None and self.runner.en_cours and not p.get("force"):
            raise RuntimeError(
                f"une acquisition est en cours : reconnecter {quoi} la casserait. "
                f"Arrêter la session, ou renvoyer la commande avec force=true.")

    def _cmd_reveil(self, p: dict) -> dict:
        self._exige_main()
        return self.banc.hand.backend.reveil()

    # -- banc : lumière et plateau --

    def _cmd_lumiere(self, p: dict) -> dict:
        """
        Règle la lampe, par valeur brute ou par **niveau nommé** du profil.

        Les niveaux nommés sont ceux de VT-Light — ils ont été choisis par la
        mesure, espacés de deux diaphragmes en éclairement, et c'est eux qu'il
        faut employer pour une acquisition. Une valeur brute ne sert qu'au
        réglage.

        Ne bloque pas les 3,5 s d'établissement : l'interface doit rester
        réactive, et l'état renvoyé dit combien de temps il reste.
        """
        if p.get("niveau"):
            pwm = self.banc.camera.profil().pwm_of(str(p["niveau"]))
        elif p.get("aruco"):
            pwm = self.banc.pwm_aruco
        else:
            pwm = int(p.get("pwm", 0))
        avant_pwm = self.banc.bench.pwm or 0
        avant_lum = self.surveillance.luminance
        applique = self.banc.bench.lumiere(pwm, attendre=bool(p.get("attendre", False)))

        # Juger l'effet **après** établissement, dans un fil : la lampe met plus
        # de deux secondes, et bloquer la requête HTTP le temps de l'attendre
        # rendrait l'interface poussive à chaque clic.
        def juger():
            time.sleep(self.banc.bench.settle + 0.5)
            apres = self.surveillance.luminance
            if avant_lum is not None and apres is not None:
                self.surveillance.juger_lampe(avant_lum, apres, avant_pwm, applique)

        if avant_lum is not None:
            threading.Thread(target=juger, daemon=True).start()

        return {"pwm": applique,
                "reste_a_etablir_s": round(self.banc.bench.reste_a_etablir, 1),
                "niveau": p.get("niveau")}

    def _cmd_plateau(self, p: dict) -> dict:
        """
        Commande directe du plateau. ``arreter`` est le seul arrêt sûr.

        ``start_pause`` est une **bascule** : sur un plateau immobile elle le
        relance. L'interface ne l'expose donc pas comme un bouton « stop ».
        """
        action = p.get("action")
        if action == "arreter":
            ok = self.banc.arreter_plateau()
            self.surveillance.etat("plateau_bloque", ok, ERREUR,
                                   "Le plateau ne s'arrête pas — un plateau qui "
                                   "tourne invalide toutes les captures suivantes.")
            self.noter("Plateau arrêté." if ok else "Plateau NON arrêté.",
                       "info" if ok else "erreur")
            return {"arrete": ok}
        if action == "aller_a":
            cible = float(p.get("angle", 0.0))
            avant = self.banc.angle.mesurer(30)
            r = self.banc.aller_a(cible, timeout=self.reglages.timeout_angle)
            apres = self.banc.angle.mesurer(60)
            self.surveillance.juger_pose(apres)
            if avant.connu and apres.connu:
                from ..hw.angle import ecart as _ecart  # noqa: PLC0415

                bouge = abs(_ecart(apres.angle, avant.angle))
                self.surveillance.juger_plateau(bouge > 3.0, bouge, demande=True)
            self.noter(f"Plateau : consigne {cible:.0f}° → mesuré "
                       f"{r.get('mesure_deg')}° (écart {r.get('ecart_deg')}°)")
            return r
        if action in ("rotation_droite", "rotation_gauche", "vitesse_moins",
                      "vitesse_plus", "start_pause"):
            return {"ack": getattr(self.banc.bench.ir, action)()}
        raise ValueError(f"action plateau inconnue : {action!r}")

    def _cmd_mesurer_angle(self, p: dict) -> dict:
        self.banc.bench.lumiere_aruco(self.banc.pwm_aruco)
        m = self.banc.angle.mesurer(int(p.get("n", 60)))
        self.surveillance.juger_pose(m)
        if m.connu:
            self.noter(f"Angle mesuré : {m.angle:.1f}° sur {m.images} images"
                       + (f" — attention, identifiants hors table "
                          f"{sorted(m.hors_table)}" if m.hors_table else "."))
        else:
            self.noter(f"Angle introuvable — {m.diagnostic()}",
                       "erreur" if m.pose_suspecte else "avert")
        return m.to_dict()

    def _cmd_cinematique(self, p: dict) -> dict:
        """Le modèle employé et son état d'étalonnage, pour l'interface."""
        from ..hw.cinematique import Cinematique  # noqa: PLC0415

        if self._cinematique in (None, False):
            self._cinematique = Cinematique(
                counts_par_radian=self.reglages.counts_par_radian)
        infos = self._cinematique.infos()

        # D'où vient le facteur, et a-t-il bougé. Un chiffre d'étalonnage sans
        # sa date ne dit pas s'il vaut encore pour la main actuellement montée.
        from ..store import etalonnage as _et  # noqa: PLC0415

        try:
            infos["etalonnage"] = {
                "repris": self._etalonnage_repris,
                "journal": str(_et.chemin()),
                "derive": _et.derive(),
            }
        except OSError as e:
            infos["etalonnage"] = {"erreur": str(e)}
        return infos

    def _cmd_etalonner_cinematique(self, p: dict) -> dict:
        """
        Mesure le facteur counts/radian sur la main.

        Déplace quelques doigts et confronte les counts de la trame brute aux
        degrés que rend le SDK. Refusé pendant une acquisition : cela bouge la
        main pour de bon.
        """
        self._exige_main()
        self._refuser_pendant_acquisition(p, "la main")
        from ..hw.cinematique import etalonner  # noqa: PLC0415

        self.noter("Étalonnage de la cinématique — la main va bouger…", "avert")
        r = etalonner(self.banc.hand)
        self.reglages.counts_par_radian = r["counts_par_radian"]
        self._cinematique = None          # rechargé avec le facteur mesuré

        # Au journal, en ajout seul : un facteur qui ne vit qu'en mémoire est à
        # remesurer à chaque démarrage, et sa dérive éventuelle est invisible.
        from ..store import etalonnage as _et  # noqa: PLC0415

        _et.enregistrer(r, main=str(p.get("main") or ""),
                        note=str(p.get("note") or ""))
        d = _et.derive(main=str(p.get("main") or ""))
        r["derive"] = d
        self.noter(f"Facteur mesuré : {r['counts_par_radian']} counts/radian "
                   f"(dispersion {r['dispersion']}, {len(r['points'])} points).")
        if d.get("significative"):
            self.noter(f"Le facteur a dérivé de {d['ecart_relatif'] * 100:+.1f} % "
                       f"depuis le premier étalonnage ({d['premier']} → "
                       f"{d['dernier']} counts/rad, {d['n']} relevés). "
                       f"À regarder avant de s'y fier.", "avert")
        return r

    def _cmd_pose(self, p: dict) -> dict:
        """
        La caméra est-elle bien placée ? Et sinon, que faire ?

        Disponible depuis l'interface parce que la caméra bouge : elle est sur
        un bras, on la pousse en changeant d'objet, et un déplacement rend tous
        les angles faux sans qu'aucune alarme ne se déclenche.
        """
        from ..hw import pose as _pose  # noqa: PLC0415

        self.banc.bench.lumiere(self.banc.pwm_aruco)
        etat = _pose.observer(self.banc.camera, self.banc.angle,
                              n=int(p.get("n", 12)))
        d = etat.to_dict()
        self.noter(f"Pose caméra : {d['conseil']}",
                   "info" if etat.utilisable else "avert")
        self.surveillance.etat(
            "pose_camera", etat.utilisable, AVERT,
            f"Pose caméra à corriger — {d['conseil']}", **d)
        return d

    def _cmd_calibrer_centre(self, p: dict) -> dict:
        self.noter("Calibration du centre de rotation…", "avert")
        r = self.banc.calibrer_centre(duree=float(p.get("duree", 40.0)))
        self.noter(f"Centre : {r.get('centre')}")
        return r

    def _cmd_selftest(self, p: dict) -> dict:
        r = self.banc.selftest(verbeux=False)
        for c in r["controles"]:
            self.noter(f"{'OK' if c['ok'] else 'ÉCHEC'} — {c['nom']} : {c['detail']}",
                       "info" if c["ok"] else "erreur")
        return r

    def _cmd_sessions(self, p: dict) -> dict:
        """Liste les sessions du disque, et signale celles sans manifeste."""
        if not self.base.exists():
            return {"sessions": []}
        rows = []
        for d in sorted(self.base.iterdir(), reverse=True):
            if d.is_dir():
                rows.append(recover.inspecter(d))
        return {"sessions": rows[:60]}

    def _cmd_contenu(self, p: dict) -> dict:
        """
        Le détail d'une session : ses captures, leurs angles, leurs images.

        Alimente l'onglet qui montre le jeu de données. Lit le manifeste quand
        il existe — une session terminée — et retombe sur les ``capture.json``
        du disque sinon, pour qu'une session **en cours** se regarde aussi.
        """
        import json as _json  # noqa: PLC0415

        racine = self.base / str(p["nom"])
        if not racine.is_dir():
            raise FileNotFoundError(f"session inconnue : {p['nom']}")

        captures = []
        manifeste = racine / "manifest.json"
        if manifeste.exists():
            try:
                captures = _json.loads(manifeste.read_text())["captures"]
            except (ValueError, KeyError, OSError):
                captures = []
        if not captures:
            for c in sorted(racine.rglob("capture.json")):
                try:
                    captures.append(_json.loads(c.read_text()))
                except (ValueError, OSError):
                    continue

        for c in captures:
            etape = c.get("etape", "")
            d = racine / etape
            c["images"] = sorted(
                str(x.relative_to(racine)) for x in d.rglob("*_color.png")) if d.is_dir() else []
        return {"nom": p["nom"], "racine": str(racine), "captures": captures,
                "compteurs": {
                    "captures": len(captures),
                    "valides": sum(1 for c in captures if c.get("validation") == "valide"),
                    "images": sum(len(c["images"]) for c in captures)}}

    def _cmd_recuperer(self, p: dict) -> dict:
        r = recover.recuperer(self.base / str(p["nom"]), force=bool(p.get("force")))
        self.noter(f"Manifeste reconstruit pour {p['nom']} : "
                   f"{r['compteurs']['images']} images.")
        return {"compteurs": r["compteurs"], "recuperation": r["recuperation"]}

    def _exige_runner(self) -> None:
        if self.runner is None:
            raise RuntimeError("aucune session en cours")

    def _exige_main(self) -> None:
        if self.banc.hand is None or not self.banc.hand.ouverte:
            raise RuntimeError("la main n'est pas montée")


# ── Serveur HTTP ──────────────────────────────────────────────────────────────


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):     # silence : le journal est ailleurs
            pass

        # -- envoi --

        def _send(self, code, body: bytes, ctype: str, **extra):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra.items():
                self.send_header(k.replace("_", "-"), v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, payload):
            self._send(code, json.dumps(payload, ensure_ascii=False,
                                        default=str).encode(), "application/json")

        # -- GET --

        def do_GET(self):
            chemin = self.path.split("?")[0]
            if chemin in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif chemin == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif chemin == "/api/state":
                self._json(200, service.snapshot())
            elif chemin == "/api/stream":
                self._stream()
            elif chemin in ("/api/image/color", "/api/image/depth"):
                self._image(chemin.endswith("depth"))
            elif chemin == "/api/capture":
                self._image_enregistree()
            else:
                self._json(404, {"error": "not found"})

        def _stream(self):
            """
            Flux Server-Sent Events, en **encodage par blocs**.

            En HTTP/1.1, un corps sans ``Content-Length`` doit être soit
            délimité par la fermeture de connexion, soit découpé en blocs. La
            première forme marche avec curl mais fait tomber puis reconnecter
            ``EventSource`` en boucle ; la seconde est celle que les navigateurs
            attendent, et elle garde la connexion vivante.
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            periode = 1.0 / CADENCE_SSE
            try:
                while True:
                    corps = f"data: {json.dumps(service.snapshot(), ensure_ascii=False, default=str)}\n\n".encode()
                    self.wfile.write(f"{len(corps):X}\r\n".encode())
                    self.wfile.write(corps)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    time.sleep(periode)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                    # onglet fermé : cas nominal

        def _image_enregistree(self):
            """
            Sert une image **déjà écrite** d'une session, pour la relecture.

            Volontairement bornée à l'arborescence des sessions : le chemin vient
            du navigateur, et servir un fichier arbitraire du disque parce qu'une
            requête le demande serait une faute.
            """
            from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

            q = parse_qs(urlparse(self.path).query)
            nom, rel = (q.get("session") or [""])[0], (q.get("chemin") or [""])[0]
            base = service.base.resolve()
            try:
                cible = (base / nom / rel).resolve()
                cible.relative_to(base)          # lève si l'on sort de la base
            except (ValueError, OSError):
                self._json(400, {"error": "chemin hors des sessions"})
                return
            if not cible.is_file():
                self._json(404, {"error": "image introuvable"})
                return
            import cv2  # noqa: PLC0415

            img = cv2.imread(str(cible))
            if img is None:
                self._json(415, {"error": "image illisible"})
                return
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                self._json(500, {"error": "encodage impossible"})
                return
            self._send(200, buf.tobytes(), "image/jpeg")

        def _image(self, profondeur: bool):
            """
            Sert la dernière image du cache, encodée en JPEG pour l'affichage.

            L'encodage est un artefact d'affichage : sur le disque, la couleur
            reste en PNG sans perte et la profondeur en ``uint16`` brut. Ce qui
            est montré n'est jamais ce qui est enregistré.
            """
            derniere = (service.banc.camera.derniere_image()
                        if service.banc.camera.ouverte else None)
            if derniere is None:
                self._json(503, {"error": "aucune image"})
                return
            couleur, prof, t, mode = derniere
            import cv2  # noqa: PLC0415
            import numpy as np  # noqa: PLC0415

            if profondeur:
                if prof is None:
                    self._json(503, {"error": "aucune profondeur"})
                    return
                p = np.asarray(prof)
                valides = p[p > 0]
                if valides.size:
                    lo, hi = np.percentile(valides, (2, 98))
                    norm = np.clip((p.astype(np.float32) - lo) / max(hi - lo, 1), 0, 1)
                else:
                    norm = np.zeros_like(p, dtype=np.float32)
                img = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
                img[p == 0] = (30, 30, 30)      # trous : gris, pas une fausse mesure
            else:
                img = couleur
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                self._json(500, {"error": "encodage impossible"})
                return
            self._send(200, buf.tobytes(), "image/jpeg",
                       X_Frame_T=str(round(t, 3)), X_Frame_Mode=str(mode))

        # -- POST --

        def do_POST(self):
            if self.path.split("?")[0] != "/api/command":
                self._json(404, {"error": "not found"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError as e:
                self._json(400, {"ok": False, "error": f"JSON invalide : {e}"})
                return
            try:
                self._json(200, {"ok": True, "result": service.commande(payload)})
            except Exception as e:  # noqa: BLE001 — remonte au navigateur
                log.warning("commande %s en échec : %s", payload.get("cmd"), e)
                service.noter(f"{payload.get('cmd')} : {e}", "erreur")
                self._json(200, {"ok": False, "error": str(e)})

    return Handler


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass                            # connexions coupées : cas nominal


def servir(service: Service, host: str = "127.0.0.1",
           port: int = config.PORT_WEB) -> QuietServer:
    """Monte le serveur et le rend, déjà en écoute dans un fil."""
    service.demarrer_apercu()
    srv = QuietServer((host, port), make_handler(service))
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    log.info("interface sur http://%s:%d", host, port)
    return srv
