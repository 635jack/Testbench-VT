#!/usr/bin/env python3
"""
Le banc simulé, vérifié contre les vrais décodeurs.

Ces tests ne valident pas seulement le simulateur : ils font passer ses trames
et ses images par le **décodeur tactile réel**, le **découpage moteur réel** et
le **détecteur ArUco réel**. Si l'un d'eux change, ils cassent — ce qui est le
but : c'est ce qui rend le mode simulation digne de confiance pour valider le
protocole sans immobiliser le banc.

Ils vérifient aussi que les défauts mesurés du matériel sont bien rejoués. Un
simulateur qui ne reproduit que le cas nominal valide un matériel qui n'existe
pas.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from vtctl import config
from vtctl.hw.angle import AngleMeter, ecart, median_circulaire
from vtctl.hw.bench import BenchOwner
from vtctl.hw.camera import ARUCO, DATASET, CameraOwner
from vtctl.hw.fake import FakeD405, FakeDimmer, FakeHand
from vtctl.hw.hand import HandOwner
from vtctl.hw.resources import ResourceManager


@pytest.fixture
def banc():
    """Caméra, port série et détecteur, sans verrou système ni matériel."""
    mgr = ResourceManager(lock_dir=None)
    dim = FakeDimmer(settle_s=0.0)
    bench = BenchOwner(mgr, fake=dim, settle=0.0).open()
    bench.lumiere_aruco()
    cam = CameraOwner(mgr, fake=FakeD405(dim, fiabilite_marqueur=1.0)).open()
    am = AngleMeter(cam).open()
    yield mgr, dim, bench, cam, am
    am.close(); cam.close(); bench.close()


# ── Angle ─────────────────────────────────────────────────────────────────────

def test_l_angle_passe_par_le_vrai_detecteur(banc):
    """
    De l'image de synthèse à l'angle, par ``ArUcoTracker`` sans modification.

    Les carreaux dessinés sont de vrais marqueurs ``DICT_4X4_50``, en couleurs
    inversées comme ceux du banc. Si le décodage marche ici, la chaîne de mesure
    est exercée pour de bon, pas court-circuitée.
    """
    _mgr, dim, _b, _c, am = banc
    for verite in (0.0, 47.0, 123.0, 271.0):
        dim.angle = verite
        m = am.mesurer(40)
        assert m.connu, f"aucun marqueur à {verite}°"
        assert abs(ecart(m.angle, verite)) < 5.0
        assert m.marqueurs, "les identifiants de carreaux doivent remonter"


def test_angle_introuvable_est_un_resultat():
    """
    ``None`` est légitime, pas une erreur.

    Sur le banc, la moitié des images seulement rend un marqueur, et certaines
    positions n'en rendent aucune. Un angle inventé serait pire qu'un angle
    absent : l'asservissement croirait converger.
    """
    mgr = ResourceManager(lock_dir=None)
    dim = FakeDimmer(settle_s=0.0)
    bench = BenchOwner(mgr, fake=dim, settle=0.0).open()
    cam = CameraOwner(mgr, fake=FakeD405(dim, fiabilite_marqueur=0.0)).open()
    am = AngleMeter(cam).open()
    try:
        m = am.mesurer(20)
        assert not m.connu
        assert m.angle is None and m.images == 0
        assert m.to_dict()["mesure_deg"] is None
        assert m.tentees == 20
    finally:
        am.close(); cam.close(); bench.close()


def test_mediane_circulaire_juste_au_passage_zero():
    """359° et 1° ont pour médiane 0°, pas 180°."""
    assert abs(ecart(median_circulaire([359.0, 1.0, 0.0]), 0.0)) < 0.5
    assert abs(ecart(median_circulaire([179.0, 181.0]), 180.0)) < 0.5
    assert median_circulaire([]) is None


def test_ecart_signe_borne():
    assert ecart(10.0, 350.0) == pytest.approx(20.0)
    assert ecart(350.0, 10.0) == pytest.approx(-20.0)


# ── Caméra ────────────────────────────────────────────────────────────────────

def test_un_seul_pipeline_pour_les_deux_usages(banc):
    """
    Le conflit historique disparaît : c'est l'exposition qui bascule.

    Auparavant le tracker ouvrait sa propre caméra et il fallait l'ouvrir puis
    la refermer autour de chaque angle — sept secondes d'établissement de lampe
    par angle, et un lissage remis à zéro sans que rien ne le signale.
    """
    _mgr, _dim, _b, cam, am = banc
    assert cam.ouvertures == 1
    am.mesurer(3)
    assert cam.mode == ARUCO
    cam.grab_dataset()
    assert cam.mode == DATASET
    assert cam.ouvertures == 1              # jamais rouverte
    assert cam.bascules >= 2


def test_la_camera_est_exclusive(banc):
    from vtctl.hw.resources import ResourceBusy

    mgr, dim, _b, _c, _a = banc
    with pytest.raises(ResourceBusy):
        CameraOwner(mgr, fake=FakeD405(dim)).open()


def test_horodatage_pris_au_retour_de_grab(banc):
    _mgr, _dim, _b, cam, _a = banc
    avant = time.perf_counter()
    _c, _d, t = cam.grab_dataset()
    apres = time.perf_counter()
    assert avant <= t <= apres


def test_profondeur_en_uint16(banc):
    _mgr, _dim, _b, cam, _a = banc
    couleur, prof, _t = cam.grab_dataset()
    assert couleur.dtype == np.uint8 and couleur.shape == (480, 640, 3)
    assert prof.dtype == np.uint16
    assert cam.infos()["depth_scale"] == pytest.approx(1e-4)


def test_le_cache_dimage_ne_relit_pas_la_camera(banc):
    """L'interface sert le cache : un second lecteur ferait revenir le conflit."""
    _mgr, _dim, _b, cam, _a = banc
    cam.grab_dataset()
    n = cam.cam._n                          # noqa: SLF001 — compteur du simulateur
    d = cam.derniere_image()
    assert d is not None and cam.cam._n == n  # noqa: SLF001


# ── Lampe et plateau ──────────────────────────────────────────────────────────

def test_la_lampe_demarre_a_fond():
    """
    Le firmware démarre à ``ledcWrite(255)``.

    C'est pourquoi le banc baisse la lumière **avant** toute mesure d'angle : à
    PWM 255, le blanc des marqueurs sort de la bande de détection.
    """
    assert FakeDimmer(settle_s=0.0).pwm == 255


def test_la_lampe_met_du_temps_a_setablir():
    """
    Plus de deux secondes après un saut.

    Lire la consigne au lieu de l'état a produit des tableaux entiers de mesures
    fausses le 2026-08-20 — cause n°1 des conclusions erronées de la journée.
    """
    dim = FakeDimmer(settle_s=3.4)
    dim.set_pwm(20)
    assert dim.pwm > 100                    # pas encore établie
    dim._t_consigne -= 4.0                  # noqa: SLF001 — on avance le temps
    assert dim.pwm == 20


def test_start_pause_est_une_bascule():
    """Sur un plateau immobile, elle le **relance**."""
    dim = FakeDimmer(settle_s=0.0)
    import json

    codes = json.loads(config.CONFIG_TELECOMMANDE.read_text())
    assert not dim.tourne
    dim.send_ir(codes["COMMANDE_START_PAUSE"])
    assert dim.tourne, "une bascule sur un plateau arrêté doit le lancer"
    dim.send_ir(codes["COMMANDE_START_PAUSE"])
    assert not dim.tourne


def test_vitesse_moins_demarre_le_plateau():
    """
    Ce n'est pas qu'un réglage de vitesse.

    D'où l'ordre imposé par ``BenchOwner.demarrer`` : le sens d'abord, la
    vitesse ensuite. L'inverse envoie le plateau à contresens une fois sur deux.
    """
    import json

    codes = json.loads(config.CONFIG_TELECOMMANDE.read_text())
    dim = FakeDimmer(settle_s=0.0)
    dim.send_ir(codes["COMMANDE_VITESSE_MOINS"])
    assert dim.tourne


def test_arret_verifie_avant_dagir(banc):
    """
    On ne renvoie une bascule que si la mesure montre encore du mouvement.

    En envoyer une « pour être sûr » relance le plateau : c'est ce qui a fait
    tourner le banc pendant une heure de mesures sans que personne ne le sache.
    """
    from vtctl.hw.banc import Banc

    b = Banc(simulation=True, avec_main=False, settle=0.0, verrous=False).open()
    try:
        assert not b.bench.dim.tourne
        emises = len(b.bench.dim.ir_envoyes)
        assert b.arreter_plateau() is True
        assert len(b.bench.dim.ir_envoyes) == emises, \
            "aucune bascule ne doit partir sur un plateau déjà immobile"
        assert not b.bench.dim.tourne
    finally:
        b.close()


# ── Main ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def main():
    mgr = ResourceManager(lock_dir=None)
    h = HandOwner(mgr, fake=FakeHand(seed=5)).open()
    time.sleep(0.3)
    yield h
    h.close()


def test_la_main_nemet_rien_avant_le_reveil():
    """
    Le piège numéro un : en-têtes corrects, charge utile identiquement nulle.

    On croit à une panne d'alimentation. C'est l'absence de ``set_enable``
    **puis** ``home_motors``.
    """
    from vt_tactile.tpdo import is_motor, is_tactile

    h = FakeHand(seed=1)
    h.connect()
    try:
        time.sleep(0.15)
        t, m = h.latest_tactile(), h.latest_motor()
        assert is_tactile(t) and is_motor(m)      # en-têtes corrects
        assert not any(t[2:]) and not any(m[2:])  # et rien dedans
        h.wake()
        time.sleep(0.15)
        assert any(h.latest_tactile()[2:])
    finally:
        h.close()


def test_les_trames_passent_le_vrai_decodeur(main):
    """Le décodeur de VT-Tactile lit les trames simulées sans le savoir."""
    from vt_tactile.tpdo import ZONE_NAMES

    n = main.zero(0.4)
    assert n > 0
    etat = main.etat_tactile()
    assert etat is not None
    assert set(etat.zones) == set(ZONE_NAMES)


def test_position_signee_dans_la_trame(main):
    """
    Un doigt repoussé sous son zéro rend 63993 pour −1543.

    Le getter du SDK écrête ces valeurs à zéro, et c'est ce qui a fait passer
    quatre moteurs sains pour muets pendant une nuit entière : « parti de 0,
    arrivé à 0, immobile », alors qu'ils remontaient bel et bien.
    """
    main.hand.positions_reelles[6] = -1543
    main.hand.cibles[6] = -1543             # sinon la boucle le ramène à zéro
    time.sleep(0.08)
    assert main.positions((6,))[6] == pytest.approx(-1543, abs=3)
    assert main.hand.positions_sdk((6,))[6] == 0


def test_aucun_moteur_nest_declare_en_panne():
    """
    La liste des moteurs en panne est vide, et c'est un état vérifié.

    La flexion du pouce y a figuré du 2026-07-27 au 2026-08-26. La panne était
    réelle sur la main d'alors — ``LST_HOMING`` sans fin, côté SDK. Elle a
    survécu au changement de main parce que la trame était lue 36 octets trop
    loin sur le pouce : il rendait zéro quoi qu'il arrive, l'exclusion semblait
    donc toujours fondée. Créneaux corrigés, les six moteurs confrontés au SDK
    un par un concordent.
    """
    from vt_tactile import hardware as hw

    assert hw.BROKEN_MOTORS == ()
    assert set(hw.WORKING_FLEXORS) == set(hw.FLEXORS)


def test_un_moteur_exclu_a_lexecution_ne_recoit_plus_rien():
    """
    Le mécanisme d'exclusion reste opérant, pour la panne du jour.

    On ne fige plus de moteur en panne dans le code : ``moteurs_exclus``
    permet d'en écarter un au vol si l'un lâche en cours de campagne. Le
    simulateur relit ``hw.BROKEN_MOTORS``, donc l'exclusion se voit sur le
    matériel simulé exactement comme sur le vrai.
    """
    from vt_tactile import hardware as hw

    mgr = ResourceManager(lock_dir=None)
    h = HandOwner(mgr, fake=FakeHand(seed=5), moteurs_exclus=(4,)).open()
    try:
        time.sleep(0.3)
        assert hw.BROKEN_MOTORS == (4,)
        assert 4 not in hw.WORKING_FLEXORS
        # Par ``aller_a``, donc par le ``Backend`` : c'est lui qui réémet la
        # consigne quand rien ne démarre, et une consigne isolée rate une fois
        # sur deux sur ce bus. Un ``hand.command`` brut rendrait le test
        # aléatoire pour une raison sans rapport avec l'exclusion.
        h.aller_a({4: 4000}, velocity=500, max_current=800)
        time.sleep(1.2)
        assert h.positions((4,))[4] == 0
        # Un voisin non exclu bouge, sans quoi le test passerait sur une main
        # entièrement inerte.
        h.aller_a({5: 4000}, velocity=500, max_current=800)
        time.sleep(1.2)
        assert h.positions((5,))[5] > 100
    finally:
        h.close()
        hw.BROKEN_MOTORS = ()
        hw.WORKING_FLEXORS = tuple(m for m in hw.FLEXORS
                                   if m not in hw.BROKEN_MOTORS)


def test_un_doigt_bloque_nest_pas_relance(main):
    """
    Immobile **à courant élevé** = obstacle mécanique, pas ordre perdu.

    Relancer, c'est forcer dessus : l'annulaire a tiré 1059 ‰ le 2026-08-19
    avant de passer en alarme.
    """
    from vtctl.hw.hand import HandError

    # On le fait vraiment buter sur l'objet : le simulateur place l'annulaire
    # en butée à 5200 counts, où son courant monte au plafond de couple. La
    # consigne est réémise tant que rien ne démarre — ``move_motors`` n'est pas
    # toujours pris, et le simulateur le rejoue.
    main.hand.command({4: 8000}, 6000, 900)
    fin = time.time() + 15.0
    while time.time() < fin and 4 not in main.bloques():
        if main.positions((4,))[4] < 100:
            main.hand.relancer()
        time.sleep(0.2)
    assert 4 in main.bloques(), (
        f"l'annulaire n'a pas buté : position {main.positions((4,))[4]}, "
        f"courants {main.courants()}")
    with pytest.raises(HandError, match="bloqué"):
        main.aller_a({4: 5000})


def test_le_flux_brut_conserve_tout(main):
    """Aucun filtrage, aucun sous-échantillonnage, aucune déduplication."""
    main.demarrer_enregistrement()
    time.sleep(0.5)
    trames = main.arreter_enregistrement()
    assert len(trames) > 50
    assert all(len(f.data) == 192 for f in trames)
    types = {f.data[0] for f in trames}
    assert types == {0x00, 0x40}, "les deux types de trames doivent être gardés"
    t = [f.t for f in trames]
    assert t == sorted(t)


def test_ring_pad_est_mort(main):
    """
    Huit zones sur neuf à pleine échelle ; ``ring.pad`` à zéro count.

    Mesuré le 2026-08-19. Le simulateur le rejoue pour que rien ne dépende
    silencieusement d'une zone qui ne répondra jamais.
    """
    from vt_tactile import hardware as hw

    main.zero(0.3)
    main.hand.command({m: 6000 for m in hw.WORKING_FLEXORS}, 4000, 800)
    time.sleep(2.5)
    etat = main.etat_tactile()
    assert etat["ring.pad"].pressure_max == 0.0


def test_un_plateau_quon_a_lance_est_toujours_arrete():
    """
    Défaut trouvé sur le banc réel le 2026-08-21, et la règle qui en sort.

    Le contrôle du plateau lançait la rotation, puis s'en remettait à la mesure
    ArUco pour décider d'arrêter. Ce jour-là la mesure était aveugle — la lampe
    du banc ne répondait plus — donc ``arreter()`` répondait « immobile »,
    n'envoyait aucune bascule, et **laissait le plateau tourner**.

    Une connaissance positive prime sur une mesure absente : on ne devine jamais
    qu'il tourne, mais on n'oublie pas qu'on l'a démarré.
    """
    from vtctl.hw.banc import Banc

    b = Banc(simulation=True, avec_main=False, settle=0.0, verrous=False).open()
    try:
        # Détection rendue impossible : exactement le cas du banc ce jour-là.
        b.camera.cam.fiabilite_marqueur = 0.0
        b.bench.demarrer(horaire=True)
        assert b.bench.dim.tourne and b.bench.lance

        assert b.arreter_plateau() is True
        assert not b.bench.dim.tourne, \
            "un plateau lancé par nous doit s'arrêter même sans mesure d'angle"
        assert not b.bench.lance
    finally:
        b.close()


def test_pas_de_bascule_sur_un_plateau_quon_na_pas_lance():
    """
    L'autre moitié de la règle : sans connaissance positive, on ne devine pas.

    Une bascule envoyée « pour être sûr » sur un plateau immobile le **relance**.
    C'est ce qui a fait tourner le banc pendant une heure de mesures.
    """
    from vtctl.hw.banc import Banc

    b = Banc(simulation=True, avec_main=False, settle=0.0, verrous=False).open()
    try:
        b.camera.cam.fiabilite_marqueur = 0.0      # mesure aveugle
        assert not b.bench.dim.tourne and not b.bench.lance
        emises = len(b.bench.dim.ir_envoyes)
        b.arreter_plateau()
        assert len(b.bench.dim.ir_envoyes) == emises
        assert not b.bench.dim.tourne, "aucune bascule ne doit relancer le plateau"
    finally:
        b.close()


# ── Pose caméra ───────────────────────────────────────────────────────────────

def test_une_camera_deplacee_se_distingue_dun_manque_de_lumiere():
    """
    Deux pannes qui se ressemblent, et qu'il faut séparer.

    Le 2026-08-23, la caméra a été déplacée et s'est retrouvée trop rasante. Le
    détecteur trouvait encore les quadrilatères mais lisait le motif de travers :
    identifiants **12, 17, 26, 34** au lieu de 1 à 6. Sans distinction, le
    symptôme est « aucun angle » — le même qu'un défaut d'éclairage — et l'on
    cherche des heures du mauvais côté.
    """
    from vtctl.hw.angle import Mesure

    # Rien décodé du tout : c'est la lumière ou l'exposition qu'il faut voir.
    rien = Mesure(None, 0, 40, None, set(), 0.0, 1.0, hors_table=set())
    assert not rien.pose_suspecte
    assert "aucun motif" in rien.diagnostic()

    # Des motifs lus, aucun du plateau : c'est la géométrie.
    devie = Mesure(None, 0, 40, None, set(), 0.0, 1.0, hors_table={12, 17, 26, 34})
    assert devie.pose_suspecte
    d = devie.diagnostic()
    assert "caméra" in d and "recalibrer" in d
    assert "12" in d, "les identifiants lus doivent apparaître : ils sont la preuve"

    # Le manifeste porte les deux, pour qu'une session relue le dise encore.
    assert devie.to_dict()["identifiants_hors_table"] == [12, 17, 26, 34]
    assert devie.to_dict()["pose_suspecte"] is True


def test_des_carreaux_valides_avec_du_bruit_restent_exploitables():
    """
    Un identifiant aberrant isolé ne condamne pas la mesure.

    On le signale — la pose se dégrade peut-être — mais tant que des carreaux du
    plateau se décodent, l'angle vaut.
    """
    from vtctl.hw.angle import Mesure

    m = Mesure(87.4, 30, 40, 1.2, {1, 4}, 0.0, 1.0, hors_table={34})
    assert m.connu and not m.pose_suspecte
    assert m.diagnostic() == ""
    assert m.to_dict()["identifiants_hors_table"] == [34]


def test_la_profondeur_peut_etre_coupee():
    """
    Le trafic isochrone de la profondeur met QEMU en défaut.

    ``usb_packet_complete_one`` — une assertion sur la file d'endpoint — a fait
    tomber la machine virtuelle le 2026-08-23. La mesure d'angle n'a besoin que
    de la couleur.
    """
    from vtctl.hw.banc import Banc

    b = Banc(simulation=True, avec_main=False, settle=0.0, verrous=False,
             avec_profondeur=False).open()
    try:
        assert b.camera.avec_profondeur is False
    finally:
        b.close()


# ── Surveillance ──────────────────────────────────────────────────────────────

def test_une_alerte_ne_se_journalise_quune_fois():
    """
    Une panne qui dure ne doit pas remplir le journal de la même ligne.

    Sinon on ne voit plus les autres — et c'est justement quand plusieurs
    choses lâchent en même temps qu'on a besoin de les lire.
    """
    from vtctl.api.surveillance import ERREUR, Surveillance

    lignes = []
    s = Surveillance(banc=None, noter=lambda m, n="info": lignes.append((n, m)))
    assert s.lever("x", ERREUR, "la main est muette") is True
    assert s.lever("x", ERREUR, "la main est muette") is False
    assert s.lever("x", ERREUR, "la main est muette") is False
    assert len(lignes) == 1
    assert s.actives()[0]["occurrences"] == 3


def test_la_resolution_est_journalisee():
    """Savoir quand c'est reparti vaut autant que savoir quand ça s'est cassé."""
    from vtctl.api.surveillance import AVERT, Surveillance

    lignes = []
    s = Surveillance(banc=None, noter=lambda m, n="info": lignes.append((n, m)))
    s.lever("y", AVERT, "la lampe ne suit pas")
    assert s.resoudre("y") is True
    assert s.resoudre("y") is False          # déjà résolue
    assert len(lignes) == 2 and "Résolu" in lignes[1][1]
    assert s.actives() == []


def test_les_alertes_sortent_les_plus_graves_en_premier():
    from vtctl.api.surveillance import AVERT, ERREUR, INFO, Surveillance

    s = Surveillance(banc=None, noter=lambda *a, **k: None)
    s.lever("a", INFO, "info")
    s.lever("b", ERREUR, "grave")
    s.lever("c", AVERT, "moyen")
    assert [x["niveau"] for x in s.actives()] == [ERREUR, AVERT, INFO]


def test_la_lampe_inerte_est_detectee():
    """
    Le variateur acquitte la trame quoi qu'il arrive : seule une image le dit.

    C'est la panne qui trompe le plus, parce que tous les signaux logiciels
    disent que tout va bien.
    """
    from vtctl.api.surveillance import Surveillance

    lignes = []
    s = Surveillance(banc=None, noter=lambda m, n="info": lignes.append(m))

    s.juger_lampe(avant=14.7, apres=14.7, pwm_avant=0, pwm_apres=200)
    assert s.actives() and s.actives()[0]["cle"] == "lampe_inerte"

    s.juger_lampe(avant=14.7, apres=63.0, pwm_avant=0, pwm_apres=200)
    assert s.actives() == []

    # Un écart de consigne trop faible ne permet pas de conclure : on se tait
    # plutôt que d'annoncer une panne sur du bruit.
    s.juger_lampe(avant=20.0, apres=20.0, pwm_avant=118, pwm_apres=120)
    assert s.actives() == []


def test_la_camera_deplacee_leve_une_alerte():
    """
    Le signe qui ne trompe pas n'est pas l'absence de marqueur.

    Elle est normale : la moitié des positions sont aveugles. Ce sont des motifs
    **lus de travers** qui signent le déplacement.
    """
    from vtctl.api.surveillance import Surveillance
    from vtctl.hw.angle import Mesure

    s = Surveillance(banc=None, noter=lambda *a, **k: None)

    s.juger_pose(Mesure(None, 0, 40, None, set(), 0.0, 1.0, hors_table={12, 34}))
    assert [a["cle"] for a in s.actives()] == ["camera_deplacee"]

    # Aucun marqueur du tout : c'est l'éclairage, pas la pose — on ne crie pas.
    s2 = Surveillance(banc=None, noter=lambda *a, **k: None)
    s2.juger_pose(Mesure(None, 0, 40, None, set(), 0.0, 1.0, hors_table=set()))
    assert s2.actives() == []

    # Une mesure saine efface l'alerte.
    s.juger_pose(Mesure(90.0, 30, 40, 1.1, {1, 4}, 0.0, 1.0))
    assert s.actives() == []


def test_le_plateau_immobile_nalerte_que_si_on_a_demande():
    """Immobile sans ordre est le cas nominal ; immobile après un ordre, non."""
    from vtctl.api.surveillance import Surveillance

    s = Surveillance(banc=None, noter=lambda *a, **k: None)
    s.juger_plateau(tourne=False, degres=0.2, demande=False)
    assert s.actives() == []
    s.juger_plateau(tourne=False, degres=0.2, demande=True)
    assert [a["cle"] for a in s.actives()] == ["plateau_immobile"]
    s.juger_plateau(tourne=True, degres=48.0, demande=True)
    assert s.actives() == []


def test_reconnexion_de_la_camera_ne_lache_pas_le_jeton():
    """
    Relâcher la ressource pendant la reprise ouvrirait une fenêtre.

    Un autre processus pourrait s'emparer de la caméra, et l'on se retrouverait
    à deux dessus — exactement ce que le gestionnaire empêche.
    """
    from vtctl.hw.banc import Banc

    b = Banc(simulation=True, avec_main=False, settle=0.0, verrous=False).open()
    try:
        avant = b.camera.ouvertures
        r = b.camera.reconnecter()
        assert r["reconnectee"] and b.camera.ouvertures == avant + 1
        assert "camera" in b.mgr.tenues()
        couleur, prof, _t = b.camera.grab_dataset()
        assert couleur is not None and prof is not None
    finally:
        b.close()


def test_reconnexion_du_port_reemet_le_pwm():
    """
    Le firmware redémarre **lampe à fond** quand on rouvre le port.

    Sans réémission, la lampe se retrouve à 255 juste après une reconnexion :
    le blanc des marqueurs sort de la bande de détection et l'angle devient
    introuvable. Une panne qui suit une réparation est la plus trompeuse.
    """
    from vtctl.hw.banc import Banc

    b = Banc(simulation=True, avec_main=False, settle=0.0, verrous=False).open()
    try:
        b.bench.lumiere(60)
        assert b.bench.pwm == 60
        r = b.bench.reconnecter()
        assert r["pwm_reemis"] == 60
        assert b.bench.pwm == 60, "le PWM doit être réémis, pas laissé à 255"
        assert "serie" in b.mgr.tenues()
        # On ne prétend plus savoir si le plateau tourne : ça ne se devine pas.
        assert b.bench.lance is False
    finally:
        b.close()


def test_pas_dalerte_camera_pendant_le_demarrage():
    """
    Crier pendant les deux secondes de démarrage apprend à ignorer le bandeau.

    C'est le pire service qu'on puisse rendre à quelqu'un qui doit s'y fier
    quand ça compte vraiment.
    """
    from vtctl.api.surveillance import Surveillance

    class _Cam:
        ouverte = True
        def derniere_image(self):  # noqa: D102
            return None

    class _Banc:
        camera = _Cam()
        hand = None
        avec_camera, avec_plateau = True, False
        class mgr:  # noqa: D106
            @staticmethod
            def tenues(): return {"camera": "CameraOwner"}

    s = Surveillance(banc=_Banc(), noter=lambda *a, **k: None)
    s._controler()                                   # noqa: SLF001
    assert s.actives() == [], "aucune alerte dans le délai de grâce"

    s._demarre -= 60                                 # noqa: SLF001 — on vieillit
    s._controler()                                   # noqa: SLF001
    assert [a["cle"] for a in s.actives()] == ["camera_muette"]


def test_laplatissement_se_mesure_sur_les_coins_quand_on_decode():
    """
    Les coins d'un carreau décodé disent sa géométrie ; une tache ne fait
    que l'approcher.

    Et l'on mesure les **côtés**, pas la boîte englobante : un carré tourné de
    45° a une boîte englobante carrée, ce qui masquerait tout l'aplatissement.
    """
    import numpy as _np

    from vtctl.hw.pose import _forme_du_quadrilatere

    carre = _np.array([[0., 0.], [40., 0.], [40., 40.], [0., 40.]])
    forme, haut = _forme_du_quadrilatere(carre)
    assert forme == pytest.approx(1.0, abs=0.01) and haut == pytest.approx(40.0)

    # Le même carré tourné de 45° : sa boîte englobante reste carrée, mais ses
    # côtés aussi — la mesure ne doit pas bouger.
    r = _np.sqrt(2) * 20
    tourne = _np.array([[r, 0.], [2 * r, r], [r, 2 * r], [0., r]])
    assert _forme_du_quadrilatere(tourne)[0] == pytest.approx(1.0, abs=0.01)

    # Aplati de moitié : c'est ce que voit une caméra trop rasante.
    plat = _np.array([[0., 0.], [40., 0.], [40., 20.], [0., 20.]])
    assert _forme_du_quadrilatere(plat)[0] == pytest.approx(2.0, abs=0.01)


def test_lordre_des_phases_ne_diverge_pas_entre_les_couches():
    """
    Un même ordre déclaré à trois endroits finit par diverger — et il l'a fait.

    Le protocole passait bien le tactile en premier, mais l'API et le menu de
    l'interface gardaient l'ancien ordre en dur : une session lancée depuis la
    page commençait donc par le balayage visuel, à l'inverse de ce qui était
    demandé. Ce test verrouille la seule source de vérité.
    """
    import pathlib

    from vtctl.protocol import states as S

    assert S.ORDRE_PHASES == ("tactile", "visuelle")

    racine = pathlib.Path(__file__).resolve().parents[1] / "vtctl"

    # L'API ne redéclare pas l'ordre : elle lit ORDRE_PHASES.
    serveur = (racine / "api/server.py").read_text()
    assert "S.ORDRE_PHASES" in serveur
    assert "[PHASE_VISUELLE, PHASE_TACTILE]" not in serveur, \
        "l'API redéclare l'ordre en dur"

    # Le menu de l'interface propose le bon en premier : c'est lui que prend
    # un opérateur qui ne touche à rien.
    page = (racine / "ui/index.html").read_text()
    debut = page.index('<select id="phases">')
    premier = page[debut:debut + 400].split('value="', 1)[1].split('"', 1)[0]
    assert premier == "tactile,visuelle", \
        f"le menu propose « {premier} » en premier"


def test_chaque_image_porte_ses_conditions_dacquisition():
    """
    Une image prise dans de mauvaises conditions est indiscernable d'une bonne.

    Trois choses changent en cours de session et ne se lisent pas dans l'image :
    l'exposition bascule entre la détection et le jeu de données, la lampe met
    plus de deux secondes à s'établir, et le PWM peut avoir été changé à la
    main. Renvoyer aux métadonnées de session ne suffit donc pas.
    """
    import json

    from vtctl.hw.banc import Banc
    from vtctl.protocol.runner import PHASE_VISUELLE, Runner

    b = Banc(simulation=True, avec_main=False, settle=0.0, verrous=False).open()
    try:
        b.asservissement.tolerance = 180.0
        r = Runner(b, config.Reglages(objet="essai", angles=[0.0],
                                      timeout_angle=25.0),
                   "/tmp/vt-conditions", phases=(PHASE_VISUELLE,))
        session = r.demarrer()
        assert r.attendre(timeout=200)

        m = json.loads((session.root / "manifest.json").read_text())
        assert m["images"], "aucune image écrite"
        acq = m["images"][0]["acquisition"]
        assert acq["mode"] == "dataset", "une image du jeu de données, pas ArUco"
        assert acq["exposition_us"] > 0
        assert acq["lampe_etablie"] is True, \
            "une image prise avant établissement est à un éclairement inconnu"
        assert acq["pwm"] is not None

        # La pose caméra est consignée : elle bouge, et rien d'autre ne le dirait.
        meta = json.loads((session.root / "session.json").read_text())
        assert "pose_camera" in meta
        assert "aplatissement" in meta["pose_camera"]
    finally:
        b.close()
