#!/usr/bin/env python3
"""
protocole.py — acquisition visuo-tactile d'un objet, de bout en bout.

    sudo python3 -m tools.protocole                    # demande le nom de l'objet
    sudo python3 -m tools.protocole --object cylindre
    sudo python3 -m tools.protocole --object cube --phases B

Deux phases, dans cet ordre.

**Phase A — l'objet seul.** Le plateau s'arrête à six angles ; à chacun, une
prise de vue par niveau d'éclairage. La main n'intervient pas et le bus
EtherCAT n'est pas monté.

**Phase B — la saisie.** Aux mêmes six angles, mais sous **un seul** niveau
d'éclairage. À chaque angle, l'opérateur amène le pouce au contact de l'objet
avec les commandes de déplacement ; dès que ce contact est **franc et stable**
pendant une seconde, les autres doigts se referment tout seuls. L'objet est
saisi par les doigts qui le rencontrent, pas nécessairement par tous.

Tout au long de la phase B, **la totalité des trames EtherCAT est conservée** :
les deux types, sans filtrage ni sous-échantillonnage. Les valeurs décodées
dépendent de la table de découpage, les octets non — c'est ce qui rend une
session réinterprétable des mois plus tard.

Sur l'angle du plateau : on enregistre l'angle **mesuré**, jamais l'angle
commandé, et ``null`` quand aucun marqueur ne se décode. Les marqueurs font
10 mm et la caméra est à 29,5° d'élévation : la détection est structurellement
marginale, et un angle inventé serait pire qu'un angle absent.
"""
from __future__ import annotations

import argparse
import logging
import os
import select
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(REPO / "VT-Light"))
sys.path.insert(0, str(REPO / "Control_Turtable_IR"))

from vt_tactile import hardware as hw  # noqa: E402
from vt_tactile.bus import BusError, Hand  # noqa: E402
from vt_tactile.dataset import Session  # noqa: E402
from vt_tactile.declencheur import ContactStable  # noqa: E402
from vt_tactile.envelop import EnvelopConfig, envelop, summarise  # noqa: E402
from vt_tactile.plateau import (  # noqa: E402
    EXPO_ARUCO, PWM_ARUCO, SETTLE_LAMPE, Plateau, calibrer_centre,
)
log = logging.getLogger("protocole")
from vt_tactile.tpdo import TactileReader  # noqa: E402
from tools.web import Backend  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CONFIG_TC = REPO / "Control_Turtable_IR" / "config_telecommande.json"
CONFIG_ARUCO = REPO / "Control_Turtable_IR" / "aruco_config.json"

#: Niveau d'éclairage des deux phases : le niveau **haut** du profil VT-Light.
#:
#: Un seul niveau, et le même partout. Les trois niveaux du profil séparent
#: très bien — remplissage de profondeur 47 % / 29 % / 13 % sur le cube, sans
#: le moindre écrêtage — mais ils répondent à une autre question que celle-ci :
#: ils servent à *étudier* l'effet de la lumière sur la stéréo passive, pas à
#: constituer un jeu de données de saisie. Les garder triplerait le temps de la
#: phase A et le volume, pour des images dont deux tiers sont volontairement
#: dégradées. ``--niveaux moyen,bas`` les rétablit si l'étude reprend.
#:
#: « haut » plutôt qu'un réglage sur mesure : c'est le meilleur remplissage de
#: profondeur sans aucun écrêtage, et le variateur sature au-delà — de PWM 200
#: à 255 l'éclairement ne gagne que 0,06 diaphragme, mesuré. Monter plus haut
#: ne donnerait rien de plus.
NIVEAU_ACQUISITION = "haut"
NIVEAU_SAISIE = NIVEAU_ACQUISITION

#: Pose de départ du pivot du pouce : à mi-course entre le pouce replié (0) et
#: son ouverture maximale atteignable (~6080 mesurés). L'opérateur ajuste
#: ensuite ; cette valeur ne sert qu'à ne pas partir d'une pose absurde.
PIVOT_DEPART = 3000

#: Lumière sous laquelle on vérifie l'immobilité du plateau. Le test compare
#: des images : à la lumière de détection ArUco l'image est presque noire et
#: la différence entre deux images ne dépasse plus le bruit, si bien qu'un
#: plateau en rotation passe pour immobile. Il faut de la lumière pour voir.
PWM_ARRET = 200

#: Délai entre le déclenchement et la fermeture, pour retirer la main.
DELAI_RETRAIT = 3.0


# ────────────────────────────── Caméra ────────────────────────────────────

_PROFIL = None


def profil():
    """Le profil photométrique de VT-Light, chargé une fois.

    Rien n'est recopié ici : ces valeurs ont été mesurées sur le banc et le
    README de VT-Light prévient qu'une copie divergerait.
    """
    global _PROFIL
    if _PROFIL is None:
        from vt_light.profile import LightProfile  # noqa: PLC0415
        _PROFIL = LightProfile.load()
    return _PROFIL


def exposition() -> int:
    """Exposition du jeu de données — la même à tous les niveaux, par principe."""
    return int(profil().camera_settings().exposure_us)


def reglages_jeu_de_donnees(exposure: int):
    """Réglages photométriques figés de VT-Light, à l'exposition demandée."""
    from vt_light.camera import CameraSettings  # noqa: PLC0415
    base = profil().camera_settings()
    return CameraSettings(**{**base.__dict__, "exposure_us": exposure})


def ouvrir_camera(exposure: int):
    from vt_light.camera import D405  # noqa: PLC0415
    return D405(reglages_jeu_de_donnees(exposure))


def noter_camera(session: Session, cam) -> None:
    """
    Consigne intrinsèques et échelle de profondeur, une fois par session.

    Sans elles la profondeur enregistrée n'est qu'une image d'entiers : on ne
    peut ni la convertir en nuage de points, ni la comparer à une autre
    session. Elles dépendent du mode — les modes de la D405 n'ont pas le même
    champ de vision — donc les relire de la caméra plutôt que les supposer.
    """
    if "camera" in session.meta:
        return
    session.note("camera", {
        "intrinsics": cam.intrinsics,
        "depth_scale": cam.depth_scale,
        "depth_unit": "0,1 mm (depth_scale = 1e-4 m)",
        "reglages": cam.settings.as_dict(),
        "align_depth": cam.align_depth,
    })


# ──────────────────────────── Plateau ─────────────────────────────────────

class Positionneur:
    """
    Le plateau, asservi par le contrôleur d'origine — **une seule instance**.

    Deux exigences, mesurées, et qui ne portent pas sur le même objet :

    **Le contrôleur doit survivre à toute la série.** Une instance unique
    enchaînant 60°, 120°, 180° atterrit à 0,5° de la consigne ; un contrôleur
    neuf à chaque angle donne 12 à 31° d'écart. S'y ajoutent une consigne à
    vide au départ — la première d'une série est fausse de 20 à 35°, le sens
    n'étant pas encore fixé — et des cibles toujours croissantes, pour ne
    jamais inverser : ``ROTATION_x`` démarre le plateau autant qu'elle choisit
    son sens, donc chaque inversion rejoue l'imprécision d'un premier ordre.

    **Le tracker doit ouvrir la caméra lui-même.** Alimenté par le flux du jeu
    de données il ne décode rien, alors qu'avec sa propre caméra il rend 46 à
    115 images exploitables sur 120. Les deux flux RealSense ne pouvant
    coexister, le tracker est donc créé puis fermé **autour de chaque
    positionnement**, et la caméra du jeu de données n'est ouverte qu'ensuite.
    C'est le tracker qu'on remplace, jamais le contrôleur.
    """

    def __init__(self, dim, tolerance: float, timeout: float):
        from turntable_position_controller import (  # noqa: PLC0415
            TurntablePositionController,
        )
        self.dim, self.timeout = dim, timeout
        self.ctl = TurntablePositionController(
            simulation=True, tolerance_deg=tolerance, config_path=str(CONFIG_ARUCO))
        self.ctl.tracker.close()
        self.ctl.tracker = None
        self._amorce = False

    def _ouvrir_tracker(self):
        from aruco_tracker import ArUcoTracker  # noqa: PLC0415
        self.dim.set_pwm(PWM_ARUCO)
        time.sleep(SETTLE_LAMPE)
        tracker = ArUcoTracker(config_path=str(CONFIG_ARUCO), exposure=EXPO_ARUCO)
        self.ctl.tracker = tracker
        plateau = Plateau(self.dim, tracker,
                          lambda couleur=False: (tracker.get_frame() if couleur
                                                 else _gris(tracker.get_frame())),
                          CONFIG_TC)
        self.ctl.turntable = plateau.ir
        for _ in range(12):
            tracker.get_frame()
        return tracker, plateau

    def _sortir_de_l_aveugle(self, plateau, tracker, duree_max: float = 25.0):
        """
        Avance jusqu'à ce qu'un marqueur se décode, puis s'arrête.

        Indispensable : ``step()`` du contrôleur abandonne dès que l'angle est
        introuvable, donc il ne peut **pas** quitter une position aveugle — il
        attend un angle qui ne viendra que s'il bouge. Or une position sur deux
        ne décode rien. On avance dans le sens des consignes, jamais l'autre.
        """
        angle, _n = plateau.mesure_angle(30)
        if angle is not None:
            return angle
        log.info("Position aveugle — avance jusqu'à retrouver un marqueur")
        plateau.ir.rotation_droite()
        time.sleep(0.3)
        for _ in range(3):
            plateau.ir.vitesse_moins()
            time.sleep(0.12)
        t0 = time.time()
        while time.time() - t0 < duree_max:
            image = tracker.get_frame()
            coins, ids = tracker.detect_markers(image)
            if ids is None:
                continue
            a, _i = tracker.estimate_turntable_angle(image, coins, ids)
            if a is not None:
                break
        plateau.ir.start_pause()
        time.sleep(1.5)
        angle, _n = plateau.mesure_angle(40)
        log.info("Sortie de l'aveugle : %s",
                 "toujours introuvable" if angle is None else f"angle {angle:.1f}°")
        return angle

    def aller_a(self, cible_deg: float, amorce: bool = False,
                passes: int = 3) -> dict:
        """
        Amène le plateau à ``cible_deg``, en reprenant tant qu'il est loin.

        **Une consigne se réémet.** Mesuré : l'asservissement est excellent sur
        les petits déplacements — 12° demandés, 0,0° d'écart — et rate les
        grands, où il s'arrête court ou part du mauvais côté. Chaque reprise
        partant de plus près que la précédente, elle tombe dans le régime où le
        contrôleur est bon. C'est la seule facon d'exploiter ce qu'il sait
        faire sans réécrire ce qu'il ne sait pas.
        """
        tracker, plateau = self._ouvrir_tracker()
        detail = []
        try:
            self._sortir_de_l_aveugle(plateau, tracker)
            for essai in range(passes):
                self.ctl.set_target_angle(cible_deg)
                atteint = self.ctl.wait_until_reached(timeout=self.timeout)
                # L'arrêt du contrôleur décide sur une vitesse ArUco bruitée et
                # peut renvoyer une bascule, qui **relance** le plateau.
                plateau.arreter()
                angle, n = plateau.mesure_angle()
                ecart = (None if angle is None
                         else round((angle - cible_deg + 180) % 360 - 180, 2))
                detail.append({"passe": essai + 1, "mesure_deg": angle and round(angle, 2),
                               "ecart_deg": ecart, "images": n})
                log.info("consigne %.0f° — passe %d : mesuré %s, écart %s",
                         cible_deg, essai + 1,
                         "—" if angle is None else f"{angle:.1f}°",
                         "—" if ecart is None else f"{ecart:+.1f}°")
                if ecart is not None and abs(ecart) <= self.ctl.tolerance_deg:
                    break
        finally:
            tracker.close()
            self.ctl.tracker = None
        return {"commande_deg": round(cible_deg % 360.0, 2),
                "mesure_deg": None if angle is None else round(angle, 2),
                "ecart_deg": ecart,
                "images_exploitables": n,
                "consigne_atteinte": bool(atteint),
                "passes": len(detail), "detail_passes": detail,
                "amorce": amorce}

    def calibrer(self, duree: float | None = None) -> dict:
        """
        Réestime le centre de rotation, au début de chaque session.

        Le centre est stocké en pixels : il ne vaut que pour la pose caméra où
        il a été mesuré. Une caméra déplacée de quelques centimètres décale
        tous les angles, **sans que rien ne le signale** — d'où la reprise
        systématique plutôt qu'à la demande.
        """
        tracker, plateau = self._ouvrir_tracker()
        try:
            return calibrer_centre(plateau, **({} if duree is None
                                               else {"duree": duree}))
        finally:
            tracker.close()
            self.ctl.tracker = None

    def amorcer(self, depart: float = 30.0) -> None:
        """Consigne à vide, jetée — elle fixe le sens de rotation."""
        self.aller_a(depart, amorce=True)
        self._amorce = True

    def fermer(self) -> None:
        if self.ctl.tracker is not None:
            try:
                self.ctl.tracker.close()
            except Exception:  # noqa: BLE001
                pass


def _gris(couleur):
    import cv2  # noqa: PLC0415
    return cv2.cvtColor(couleur, cv2.COLOR_BGR2GRAY)


# ────────────────────────────── Phase A ───────────────────────────────────

def phase_a(session: Session, dim, angles, niveaux, tolerance, timeout) -> None:
    print(f"\n{BOLD}Phase A — l'objet seul{RESET}  "
          f"({len(angles)} angles × {len(niveaux)} niveaux)")
    pos = Positionneur(dim, tolerance, timeout)
    try:
        print("  calibration du centre de rotation (la caméra a pu bouger)…")
        calib = pos.calibrer()
        session.note("calibration_centre", calib)
        if calib["reussi"]:
            print(f"    centre {calib['centre']}, dispersion {calib['dispersion_px']} px, "
                  f"écart à l'ancien {calib['ecart_px']} px "
                  f"({calib['marqueurs_retenus']} marqueurs)")
        else:
            print("    ÉCHEC — centre inchangé, les angles seront douteux")
        print("  consigne à vide (jetée) — elle fixe le sens de rotation…")
        pos.amorcer()
        for i, cible in enumerate(angles):
            pose = pos.aller_a(cible)
            mesure, ecart = pose["mesure_deg"], pose["ecart_deg"]
            print(f"\n  angle {i + 1}/{len(angles)} — commandé {cible:.0f}°, "
                  f"mesuré {'—' if mesure is None else f'{mesure:.1f}°'}"
                  f"{'' if ecart is None else f' (écart {ecart:+.1f}°)'} "
                  f"[{pose['images_exploitables']}/120 images]")
            session.mark(f"A_angle_{i:02d}", **pose)
            # La caméra du jeu de données ne s'ouvre qu'une fois le tracker
            # fermé : deux flux RealSense ne coexistent pas.
            with ouvrir_camera(exposition()) as cam:
                noter_camera(session, cam)
                for nom, pwm in niveaux:
                    dim.set_pwm(pwm)
                    time.sleep(SETTLE_LAMPE)
                    cam.flush(8)
                    couleur, profondeur = cam.grab()
                    etape = f"A/angle_{i:02d}/{nom}"
                    session.save_frame(etape, 0, couleur, profondeur,
                                       extra={"phase": "A", "niveau": nom,
                                              "pwm": pwm,
                                              "exposure_us": exposition(), **pose})
                    sature = 100.0 * float((couleur >= 255).mean())
                    rempli = 100.0 * float((profondeur > 0).mean())
                    print(f"      {nom:<6} PWM {pwm:3d} : écrêté {sature:.3f} %, "
                          f"profondeur {rempli:.1f} %")
    finally:
        pos.fermer()


# ────────────────────────────── Phase B ───────────────────────────────────

def _ligne_prete() -> str | None:
    """Une ligne tapée est-elle disponible ? Sans bloquer la surveillance."""
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.readline().strip().lower()
    return None


def bouger_pivot(hand: Hand, cible: int, courant: int = hw.APPROACH_CURRENT) -> int:
    """
    Déplace le pivot du pouce, en réémettant tant qu'il n'a pas démarré.

    ``move_motors`` n'est pas toujours pris : la consigne est acceptée, relue
    correctement, et le moteur ne bouge pas d'un count, à courant de repos.
    Sans réémission, une commande sur deux se perd — constaté aujourd'hui, deux
    essais de suite entièrement immobiles.
    """
    cible = max(0, min(int(cible), hw.POSITION_MAX))
    depart = hand.positions((hw.THUMB_PIVOT,))[hw.THUMB_PIVOT]
    duree = abs(cible - depart) / max(hw.VELOCITY_CLOSE, 1) + 4.0
    hand.enable()
    hand.command({hw.THUMB_PIVOT: cible}, hw.VELOCITY_CLOSE, courant)
    t0, relances = time.time(), 0
    while time.time() - t0 < duree:
        time.sleep(0.4)
        pos = hand.positions((hw.THUMB_PIVOT,))[hw.THUMB_PIVOT]
        if relances < 2 and time.time() - t0 > 1.0 and abs(pos - depart) < 100:
            hand.relancer()
            relances += 1
        if abs(pos - cible) <= 120:
            break
    return hand.positions((hw.THUMB_PIVOT,))[hw.THUMB_PIVOT]


def pression_pouce(hand: Hand, reader: TactileReader) -> float | None:
    brut = hand.latest_tactile()
    if brut is None:
        return None
    return float(reader.decode(brut)["thumb"].pressure_max)


def poser_le_pouce(hand: Hand, reader: TactileReader, session: Session,
                   etape: str, args) -> dict:
    """
    L'opérateur amène le pouce au contact ; le contact stable arme la fermeture.

    Le pilotage se fait en lignes tapées plutôt qu'en touches brutes : c'est ce
    que fait déjà ``tools.jog``, et ça survit à une liaison SSH. La surveillance
    tourne en continu pendant ce temps — ``select`` sur l'entrée standard, donc
    rien ne bloque la lecture du capteur.

    Le déclenchement n'est actif qu'après ``go`` : tant que l'opérateur a les
    doigts dans la main, un appui fortuit ne doit rien fermer.
    """
    detecteur = ContactStable(seuil=args.seuil, epsilon=args.epsilon,
                              duree=args.stable)
    pivot = hand.positions((hw.THUMB_PIVOT,))[hw.THUMB_PIVOT]
    arme = False
    t_declenche = None

    print(f"\n  {BOLD}Posez le pouce sur l'objet{RESET}")
    print(f"  {DIM}+N / -N déplacent le pivot · N seul = position absolue · "
          f"go = armer · stop = désarmer · s = sauter cet angle · q = quitter{RESET}")
    print(f"  {DIM}pivot actuel {pivot}{RESET}")

    t0 = time.time()
    dernier_affichage = 0.0
    while True:
        ligne = _ligne_prete()
        if ligne is not None:
            if ligne == "q":
                return {"issue": "abandon_total", "pivot": pivot}
            if ligne == "s":
                return {"issue": "angle_saute", "pivot": pivot}
            if ligne == "go":
                arme = True
                detecteur.reinitialiser()
                print(f"  {BOLD}Armé{RESET} — la fermeture partira au contact stable.")
            elif ligne == "stop":
                arme = False
                t_declenche = None
                print("  Désarmé.")
            elif ligne:
                try:
                    cible = pivot + int(ligne) if ligne[0] in "+-" else int(ligne)
                except ValueError:
                    print("  Entrée non comprise. Exemples : +200, -200, 3000, go, s, q")
                    continue
                arme = False          # tout déplacement désarme : on ne ferme
                t_declenche = None    # pas pendant que le pouce est en route
                pivot = bouger_pivot(hand, cible)
                detecteur.reinitialiser()
                print(f"  pivot -> {pivot}")

        p = pression_pouce(hand, reader)
        if p is None:
            time.sleep(0.05)
            continue
        maintenant = time.time()
        etat = detecteur.ajouter(maintenant, p)

        if maintenant - dernier_affichage > 0.25:
            dernier_affichage = maintenant
            barre = "█" * int(min(p, 1.0) * 30)
            print(f"\r  pouce {p:5.3f} |{barre:<30}| "
                  f"{'ARMÉ  ' if arme else 'désarmé'} {etat.raison:<34}",
                  end="", flush=True)

        if arme and etat.pret:
            if t_declenche is None:
                t_declenche = maintenant
                print(f"\n  {BOLD}Contact stable{RESET} — fermeture dans "
                      f"{DELAI_RETRAIT:.0f} s. `stop` annule.")
                session.mark(f"{etape}/contact_stable",
                             pression=round(p, 4), pivot=pivot)
            elif maintenant - t_declenche >= DELAI_RETRAIT:
                print("  Fermeture.")
                return {"issue": "declenche", "pivot": pivot,
                        "pression_declenchement": round(p, 4),
                        "attente_s": round(maintenant - t0, 1)}
        elif t_declenche is not None:
            print("\n  Contact perdu — déclenchement annulé.")
            t_declenche = None

        if args.timeout_pose and maintenant - t0 > args.timeout_pose:
            return {"issue": "timeout", "pivot": pivot}
        time.sleep(0.02)


def fermer_sur_backend(back, hand: Hand, reader: TactileReader, session: Session,
                       etape: str, args, cam=None, pose=None) -> dict:
    """
    Referme les doigts en s'appuyant sur le ``Backend`` de ``tools.web``.

    Rien n'est réécrit du pilotage : c'est lui qui commande, et lui seul. Il
    apporte trois choses que ma boucle n'avait pas, et dont chacune a été
    mesurée sur ce banc le 2026-08-19 :

    * les **lectures SDK sont tues** autour de chaque consigne — elles écrasent
      la trame de commande avant qu'elle ne parte, au point qu'un moteur qui va
      de 0 à 2600 sans le fil d'échantillonnage ne bouge plus du tout avec lui ;
    * les accès SDK sont **sérialisés**, le SDK n'étant pas réentrant ;
    * un mouvement **refusé en silence** déclenche une reprise du bus et un
      second essai, le variateur cessant d'exécuter après quelques mouvements.

    Vérifié : six saisies espacées de 45 s, six réussites, là où ma boucle
    échouait six fois sur six.

    La logique d'enveloppement reste la nôtre : on avance par paliers et on
    fige chaque doigt dès qu'il touche, pour que la main épouse l'objet au lieu
    de le serrer.
    """
    actifs = list(hw.WORKING_FLEXORS)
    depart = hand.positions(actifs)
    cibles = dict(depart)
    contacts: dict[str, dict] = {}
    t0 = time.time()
    compteur = {"n": 0, "img": 0}

    while actifs and (time.time() - t0) < args.timeout_fermeture:
        for m in actifs:
            cibles[m] = min(cibles[m] + args.pas_fermeture, hw.POSITION_MAX)
        r = back.set_targets({m: cibles[m] for m in actifs},
                             hw.VELOCITY_CLOSE, args.max_current)
        pics = r.get("pics_courant", {})
        # Le tactile se lit par le decodeur, pas par le snapshot : c'est du
        # Python pur, sans un seul appel SDK, et ce sont les noms de zones
        # internes plutot que les etiquettes d'affichage de la page.
        brut = hand.latest_tactile()
        etat = reader.decode(brut) if (brut is not None and reader.zeroed) else None
        pos = hand.positions(actifs)
        compteur["n"] += 1

        etat_zones = {}
        for m in list(actifs):
            noms = [z for z in hw.zones_of(m)
                    if etat is not None and z in etat.zones]
            pression = max((etat[z].pressure_max for z in noms), default=0.0)
            courant = int(pics.get(m, 0))
            etat_zones[hw.MOTOR_NAMES[m]] = {"pression": round(pression, 4),
                                             "courant": courant,
                                             "position": pos.get(m)}
            raison = None
            if pression >= args.pressure:
                raison = "tactile"
            elif courant >= args.seuil_courant:
                raison = "courant"
            elif pos.get(m, 0) >= hw.POSITION_MAX - 40:
                raison = "butée"
            if raison:
                actifs.remove(m)
                cibles[m] = pos.get(m, cibles[m])
                contacts[hw.MOTOR_NAMES[m]] = {
                    "cause": raison, "position": pos.get(m),
                    "courant": courant, "pression": round(pression, 4),
                    "t": round(time.time() - t0, 2)}
                print(f"    {hw.MOTOR_NAMES[m]:<12} {raison:<8} "
                      f"pos {pos.get(m):5d}  {courant:4d} ‰  p{pression:.3f}")

        session.save_tactile(etape, reader.decode(hand.latest_tactile())
                             if hand.latest_tactile() else None,
                             motors={"positions": pos, "pics": pics},
                             extra={"iteration": compteur["n"],
                                    "actifs": sorted(actifs)})             if hand.latest_tactile() else None
        if cam is not None and compteur["n"] % args.image_toutes == 0:
            couleur, profondeur = cam.grab()
            session.save_frame(etape, compteur["img"], couleur, profondeur,
                               extra={"iteration": compteur["n"], **(pose or {})})
            compteur["img"] += 1

    return {"contacts": contacts, "iterations": compteur["n"],
            "duree_s": round(time.time() - t0, 2),
            "restes_actifs": sorted(hw.MOTOR_NAMES[m] for m in actifs)}


def reconnecter(hand: Hand, reader: TactileReader, args) -> list:
    """
    Ferme et rouvre entièrement le bus, comme le ferait un processus neuf.

    Rend le flux brut accumulé jusque-là, pour qu'il soit enregistré avant que
    la pompe reparte — sans quoi la reconnexion le perdrait.
    """
    trames = []
    if not args.no_raw:
        try:
            trames = hand.stop_recording()
        except Exception:  # noqa: BLE001
            pass
    try:
        hand.close()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.0)
    hand.connect(iface_index=args.iface)
    hand.wake()
    if not args.no_raw:
        hand.start_recording()
    return trames


def phase_b(session: Session, dim, hand: Hand, reader: TactileReader,
            angles, args) -> None:
    pwm_saisie = profil().pwm_of(NIVEAU_SAISIE)
    print(f"\n{BOLD}Phase B — la saisie{RESET}  ({len(angles)} angles, niveau "
          f"« {NIVEAU_SAISIE} » = PWM {pwm_saisie})")
    back = Backend(hand, reader)
    back.start()
    pos = Positionneur(dim, args.tolerance, args.timeout_angle)
    print("  calibration du centre de rotation…")
    calib = pos.calibrer()
    session.note("calibration_centre", calib)
    print(f"    centre {calib.get('centre')}, {calib.get('marqueurs_retenus')} marqueurs")
    print("  consigne à vide (jetée)…")
    pos.amorcer()
    cfg = EnvelopConfig(
        thumb_pivot=None,               # déjà posé par l'opérateur
        seat_counts=args.seat,
        pressure_threshold=args.pressure,
        max_current=args.max_current,
        current_margin=args.current_margin,
        timeout=args.timeout_fermeture,
        hold_seconds=args.hold,
    )

    for i, cible in enumerate(angles):
        pose = pos.aller_a(cible)
        mesure = pose["mesure_deg"]
        print(f"\n{BOLD}  angle {i + 1}/{len(angles)}{RESET} — commandé "
              f"{cible:.0f}°, mesuré "
              f"{'—' if mesure is None else f'{mesure:.1f}°'}")
        session.mark(f"B_angle_{i:02d}", **pose)
        dim.set_pwm(pwm_saisie)
        time.sleep(SETTLE_LAMPE)

        etape = f"B/angle_{i:02d}"

        # Reprendre le bus à zéro avant chaque saisie.
        #
        # Le variateur cesse d'exécuter les consignes après un temps d'usage :
        # elles sont acceptées, la cible se relit, aucune alarme n'apparaît, et
        # rien ne bouge. Ni `enable()` ni un `wake()` complet en cours de
        # processus ne rétablissent — un processus neuf, si, à tous les coups.
        # C'est `tools/web.py` qui l'a établi le 2026-08-19, et c'est ce qui
        # faisait échouer six saisies sur six ici pendant que la même fermeture
        # lancée seule réussissait.
        raw_angle = reconnecter(hand, reader, args)
        if raw_angle:
            e = session.save_raw_stream(f"{etape}/raw_precedent", raw_angle)
            print(f"  flux brut de l'angle précédent : {e['count']} trames")

        if not hand.open_hand():
            print("  La main ne s'ouvre pas : angle sauté plutôt que de "
                  "refermer depuis une pose inconnue.", file=sys.stderr)
            session.mark(f"{etape}/refus_ouverture")
            continue

        print(f"  Zéro tactile ({args.zero:.0f} s) — objet posé, ne touchez rien.")
        trames = hand.collect(args.zero)
        if not trames:
            raise BusError("aucune trame tactile")
        reader.zero(trames)
        session.note(f"{etape}/baseline", reader.baseline)

        pivot = bouger_pivot(hand, args.pivot_depart)
        if args.sans_pouce:
            # Sans opérateur, il n'y a personne pour poser le pouce : on ferme
            # depuis la pose de départ. La saisie est moins bonne — c'est tout
            # l'intérêt de la pose manuelle que de garantir que l'objet est
            # dans le volume atteignable — mais la chaîne complète est
            # exercée : images, tactile, flux brut, réouverture.
            resultat = {"issue": "declenche", "pivot": pivot,
                        "mode": "sans_pouce"}
            print(f"  Mode sans pouce : fermeture depuis le pivot {pivot}.")
        else:
            resultat = poser_le_pouce(hand, reader, session, etape, args)
        print()
        session.mark(f"{etape}/pose_pouce", **resultat)
        if resultat["issue"] == "abandon_total":
            print("  Abandon demandé.")
            return
        if resultat["issue"] != "declenche":
            print(f"  Angle sauté ({resultat['issue']}).")
            continue

        with ouvrir_camera(exposition()) as cam:
            noter_camera(session, cam)
            cam.flush(8)
            capture(session, cam, hand, reader, f"{etape}/00_pouce_pose", pose)

            compteur = {"n": 0, "img": 0}

            def pas(echantillon, etat, contacts):
                compteur["n"] += 1
                session.save_tactile(f"{etape}/01_fermeture", etat,
                                     motors={"positions": echantillon["positions"],
                                             "currents": echantillon["currents"]},
                                     extra={"contacts": sorted(contacts)})
                if compteur["n"] % args.image_toutes:
                    return
                couleur, profondeur = cam.grab()
                session.save_frame(f"{etape}/01_fermeture", compteur["img"],
                                   couleur, profondeur,
                                   extra={"iteration": compteur["n"],
                                          "contacts": sorted(contacts)})
                compteur["img"] += 1

            session.mark(f"{etape}/fermeture_debut")
            print("  Fermeture :")
            resultat_saisie = fermer_sur_backend(
                back, hand, reader, session, f"{etape}/01_fermeture", args,
                cam=cam, pose=pose)
            session.mark(f"{etape}/fermeture_fin",
                         contacts=sorted(resultat_saisie["contacts"]))
            print(f"  -> {len(resultat_saisie['contacts'])}/"
                  f"{len(hw.WORKING_FLEXORS)} doigts en contact en "
                  f"{resultat_saisie['duree_s']} s")
            session.note(f"{etape}/saisie", resultat_saisie)

            capture(session, cam, hand, reader, f"{etape}/02_saisi", pose)
            back.open_hand()
            time.sleep(0.5)
            cam.flush(5)
            capture(session, cam, hand, reader, f"{etape}/03_relache", pose)
    back.stop()
    pos.fermer()


def capture(session: Session, cam, hand: Hand, reader: TactileReader,
            etape: str, pose: dict, n: int = 3) -> None:
    """Quelques images d'une étape figée, avec l'état tactile en regard."""
    for k in range(n):
        couleur, profondeur = cam.grab()
        extra = dict(pose)
        brut = hand.latest_tactile()
        if brut is not None and reader.zeroed:
            entree = session.save_tactile(etape, reader.decode(brut),
                                          hand.positions())
            extra["tactile_t"] = entree["t"]
        session.save_frame(etape, k, couleur, profondeur, extra=extra)


# ──────────────────────────────── main ────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--object", default=None, help="nom de l'objet")
    ap.add_argument("--out", type=Path, default=Path("datasets"))
    ap.add_argument("--phases", default="A,B", help="A, B, ou A,B")
    ap.add_argument("--angles", type=int, default=6)
    ap.add_argument("--pas", type=float, default=60.0, help="pas angulaire, °")
    ap.add_argument("--niveaux", default=None,
                    help=f"niveaux de la phase A, par nom (défaut : "
                         f"{NIVEAU_ACQUISITION} seul ; « haut,moyen,bas » pour "
                         f"reprendre l'étude photométrique)")
    ap.add_argument("--tolerance", type=float, default=3.0)
    ap.add_argument("--timeout-angle", type=float, default=60.0)
    ap.add_argument("--pivot-depart", type=int, default=PIVOT_DEPART)
    ap.add_argument("--seuil", type=float, default=0.06,
                    help="pression du pouce déclarant un appui")
    ap.add_argument("--epsilon", type=float, default=0.04,
                    help="amplitude tolérée pour dire que l'appui est stable")
    ap.add_argument("--stable", type=float, default=1.0,
                    help="durée de stabilité exigée, en secondes")
    ap.add_argument("--timeout-pose", type=float, default=0.0,
                    help="abandon de la pose du pouce après N s (0 = jamais)")
    ap.add_argument("--seat", type=int, default=0)
    ap.add_argument("--pressure", type=float, default=0.03)
    ap.add_argument("--max-current", type=int, default=hw.GRASP_CURRENT)
    ap.add_argument("--current-margin", type=int, default=450)
    ap.add_argument("--timeout-fermeture", type=float, default=45.0)
    ap.add_argument("--seuil-courant", type=int, default=400,
                    help="pic de courant, en ‰, au-delà duquel un doigt est "
                         "déclaré en contact. Mesuré sur ce banc : la course "
                         "libre plafonne à 220 ‰, un contact franc monte à "
                         "716 ‰. 400 sépare les deux sans ambiguïté ; le "
                         "plafond de couple reste le garde-fou.")
    ap.add_argument("--pas-fermeture", type=int, default=1200,
                    help="avance de la consigne à chaque palier, en counts. "
                         "Chaque palier coûte ~3 s de surcoût fixe au pilote : "
                         "de petits pas n'affinent rien, ils épuisent le "
                         "temps imparti avant que les doigts n'atteignent "
                         "l'objet — 400 counts ne menaient qu'à 2400 en 25 s.")
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--zero", type=float, default=2.0)
    ap.add_argument("--image-toutes", type=int, default=3,
                    help="une image toutes les N itérations de fermeture")
    ap.add_argument("--iface", type=int, default=None)
    ap.add_argument("--sans-pouce", action="store_true",
                    help="fermer sans attendre qu'un opérateur pose le pouce "
                         "(validation de la chaîne, hors présence humaine)")
    ap.add_argument("--no-raw", action="store_true",
                    help="ne pas conserver le flux EtherCAT brut (déconseillé)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    nom = args.object
    while not nom:
        try:
            nom = input("Nom de l'objet : ").strip()
        except EOFError:
            print("Aucun nom d'objet.", file=sys.stderr)
            return 2
    phases = {p.strip().upper() for p in args.phases.split(",") if p.strip()}
    angles = [(i * args.pas) % 360.0 for i in range(args.angles)]

    from vt_light.dimmer import Dimmer  # noqa: PLC0415

    noms = ([n.strip() for n in args.niveaux.split(",") if n.strip()]
            if args.niveaux else [NIVEAU_ACQUISITION])
    try:
        niveaux = [(n, profil().pwm_of(n)) for n in noms]
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    session = Session.create(args.out, nom, meta={
        "protocole": {"version": 1, "phases": sorted(phases),
                      "angles_commandes_deg": angles},
        "objet": {"nom": nom},
        "eclairage": {"source": "VT-Light results/light_profile.json",
                      "phase_A_niveaux": [{"nom": n, "pwm": p} for n, p in niveaux],
                      "phase_B_niveau": NIVEAU_SAISIE,
                      "phase_B_pwm": profil().pwm_of(NIVEAU_SAISIE),
                      "exposition_us": exposition(),
                      "camera": profil().camera_settings().as_dict(),
                      "aruco_pwm": PWM_ARUCO, "aruco_exposition_us": EXPO_ARUCO},
        "declencheur": ({"mode": "sans_pouce"} if args.sans_pouce else
                        {"seuil": args.seuil, "epsilon": args.epsilon,
                         "duree_s": args.stable, "zone": "thumb"}),
        "moteurs": {str(m): hw.MOTOR_NAMES[m] for m in hw.MOTOR_IDS},
        "moteurs_exclus": [hw.MOTOR_NAMES[m] for m in hw.BROKEN_MOTORS],
    })
    print(f"{BOLD}Objet{RESET}   : {nom}")
    print(f"{BOLD}Session{RESET} : {session.root}")

    dim = Dimmer()
    hand = reader = None
    try:
        dim.connect()
        session.note("plateau", {"port": dim.port})

        if "A" in phases:
            phase_a(session, dim, angles, niveaux, args.tolerance,
                    args.timeout_angle)

        if "B" in phases:
            hand, reader = Hand(), TactileReader()
            hand.connect(iface_index=args.iface)
            print("\nRéveil de la main (~10 s)…")
            hand.wake()
            if not args.no_raw:
                hand.start_recording()
                session.mark("flux_brut_debut")
            phase_b(session, dim, hand, reader, angles, args)

    except KeyboardInterrupt:
        print("\nInterrompu.")
        session.note("interrompu", True)
    except BusError as e:
        print(f"\nBus : {e}", file=sys.stderr)
        session.note("erreur", str(e))
        return 2
    finally:
        if hand is not None and not args.no_raw:
            # Le flux brut passe avant tout le reste : c'est la seule pièce qui
            # ne se rejoue pas.
            try:
                session.mark("flux_brut_fin")
                trames = hand.stop_recording()
                if trames:
                    e = session.save_raw_stream("raw", trames)
                    print(f"\nFlux brut : {e['count']} trames {e['frame_types']} "
                          f"sur {e['span_s']} s")
            except Exception as exc:  # noqa: BLE001
                print(f"Flux brut non enregistré : {exc}", file=sys.stderr)
        if hand is not None:
            try:
                hand.release()
            except Exception:  # noqa: BLE001
                pass
            hand.close()
        try:
            dim.set_pwm(0)
            dim.close()
        except Exception:  # noqa: BLE001
            pass
        chemin = session.close()
        print(f"{len(session.frames)} images, {len(session.tactile)} états "
              f"tactiles.\nManifeste : {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
