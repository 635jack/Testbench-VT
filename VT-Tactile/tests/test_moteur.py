#!/usr/bin/env python3
"""Le décodage de la trame moteur, vérifié sans matériel.

La table de découpage vient de la mesure : deux sessions de palpation
confrontées à leur flux brut. Ces tests la verrouillent, pour qu'une
réécriture du décodeur ne la perde pas en silence — c'est déjà arrivé au
découpage tactile, dont le SDK lit les champs un octet trop tôt.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from vt_tactile.tpdo import (
    MOTOR_ORDRE,
    PAYLOAD_OFFSET,  # noqa: E402
    FRAME_MOTOR, MOTOR_SLOT_BASE, MOTOR_SLOT_SIZE, decode_motor, motor_slot,
)


def trame(positions=None, courants=None, milieu=None):
    """Fabrique une trame moteur de 192 octets."""
    buf = bytearray(192)
    buf[0] = FRAME_MOTOR
    for m in range(1, 7):
        o = motor_slot(m)
        struct.pack_into("<H", buf, o, (positions or {}).get(m, 0))
        struct.pack_into("<H", buf, o + 2, (milieu or {}).get(m, 0))
        struct.pack_into("<H", buf, o + 4, (courants or {}).get(m, 0))
    return bytes(buf)


def test_offsets_mesures():
    """
    Les offsets relevés au banc le 2026-08-26, moteur par moteur.

    Protocole : chaque moteur commandé à une valeur distincte, puis la trame
    confrontée à ``get_now_position`` du SDK. Les six concordent.

        moteur 2  pivot du pouce    consigne 4000  SDK 3999  trame 3999
        moteur 1  flexion du pouce  consigne 3000  SDK 3000  trame 3000
        moteur 6  index             consigne 3500  SDK 3500  trame 3500
        moteur 5  majeur            consigne 2500  SDK 2499  trame 2499
        moteur 4  annulaire         consigne 6000  SDK 6000  trame 6000
        moteur 3  auriculaire       consigne 5000  SDK 4999  trame 4999

    La version précédente donnait 38 et 44 au pouce, extrapolés de la suite
    décroissante observée sur les quatre longs doigts — juste par coïncidence
    pour les moteurs 3 à 6. Ils sont à 2 et 8 : le pouce ouvre les créneaux, il
    ne les ferme pas. Conséquence du mauvais offset : le pivot du pouce a été
    journalisé à plat pendant un mois alors qu'il fonctionnait.
    """
    assert motor_slot(2) == 2       # pivot du pouce
    assert motor_slot(1) == 8       # flexion du pouce
    assert motor_slot(6) == 14      # index
    assert motor_slot(5) == 20      # majeur
    assert motor_slot(4) == 26      # annulaire
    assert motor_slot(3) == 32      # auriculaire


def test_creneaux_contigus_dans_lordre_des_articulations():
    """
    Six créneaux de 6 octets, jointifs, dans l'ordre des articulations.

    Pas dans l'ordre des moteurs : les créneaux suivent l'URDF — ``finger11``,
    ``finger12``, ``finger21``, ``finger31``, ``finger41``, ``finger51`` —
    soit les moteurs 2, 1, 6, 5, 4, 3. Lire l'ordre dans les identifiants a
    coûté deux moteurs de pouce qu'on croyait non alimentés.
    """
    offs = [motor_slot(m) for m in MOTOR_ORDRE]
    assert offs == list(range(MOTOR_SLOT_BASE,
                              MOTOR_SLOT_BASE + 6 * MOTOR_SLOT_SIZE,
                              MOTOR_SLOT_SIZE))
    assert MOTOR_SLOT_BASE == PAYLOAD_OFFSET, \
        "les créneaux moteur commencent avec la charge utile"


def test_le_pouce_nest_pas_confondu_avec_du_remplissage():
    """
    Les deux créneaux du pouce sont **dans** la charge utile moteur.

    L'ancienne formule les plaçait à 38 et 44, là où se trouvent d'autres
    données. ``positions()`` y lisait zéro quoi que fasse le pouce — ce qui
    rendait toute exclusion du pouce irréfutable, faute de mesure capable de
    la contredire.
    """
    for moteur in (1, 2):
        assert 0 <= motor_slot(moteur) < MOTOR_SLOT_BASE + 6 * MOTOR_SLOT_SIZE
    assert motor_slot(2) < motor_slot(1) < motor_slot(6), \
        "le pouce précède les longs doigts"


def test_moteur_hors_bornes_refuse():
    for m in (0, 7, -1):
        with pytest.raises(ValueError):
            motor_slot(m)


def test_positions_et_courants_relus():
    pos = {1: 2, 2: 3242, 3: 8499, 4: 2883, 5: 3796, 6: 4004}
    cur = {1: 190, 2: 46, 3: 264, 4: 802, 5: 802, 6: 625}
    d = decode_motor(trame(pos, cur))
    assert d["positions"] == pos
    assert d["currents"] == cur


def test_valeurs_du_banc_du_2026_08_20():
    """Les positions finales réellement mesurées lors de la palpation."""
    d = decode_motor(trame({3: 8499, 4: 2883, 5: 3796, 6: 4004}))
    assert d["positions"][3] == 8499     # auriculaire, à sa butée
    assert d["positions"][4] == 2883
    assert d["positions"][5] == 3796
    assert d["positions"][6] == 4004


def test_champ_du_milieu_conserve_brut():
    """Non identifié, donc rendu tel quel plutôt que nommé à tort."""
    d = decode_motor(trame(milieu={6: 49, 5: 1234}))
    assert d["raw_mid"][6] == 49
    assert d["raw_mid"][5] == 1234


def test_position_signee():
    """Un doigt poussé au-delà de son zéro rend une position négative.

    C'est le cas qui a coûté le plus cher : le getter du SDK écrête ces
    valeurs à zéro, si bien qu'un doigt qui remonte de −1543 vers zéro passe
    pour immobile pendant toute la remontée.
    """
    d = decode_motor(trame({6: 63993}))          # 63993 - 65536
    assert d["positions"][6] == -1543
    assert d["positions_u16"][6] == 63993


def test_frontiere_du_signe():
    assert decode_motor(trame({6: 32767}))["positions"][6] == 32767
    assert decode_motor(trame({6: 32768}))["positions"][6] == -32768
    assert decode_motor(trame({6: 65535}))["positions"][6] == -1


def test_courant_reste_non_signe():
    """Le courant est un pour-mille : il n'a pas de raison d'être négatif."""
    d = decode_motor(trame(courants={6: 65535}))
    assert d["currents"][6] == 65535


def test_petit_boutisme():
    """Little-endian : l'octet de poids faible vient en premier."""
    buf = bytearray(trame())
    buf[motor_slot(6)] = 0x34
    buf[motor_slot(6) + 1] = 0x12
    assert decode_motor(bytes(buf))["positions"][6] == 0x1234


def test_type_de_trame_verifie():
    """Une trame capteur passée par erreur doit être refusée, pas décodée."""
    buf = bytearray(trame())
    buf[0] = 0x40
    with pytest.raises(ValueError, match="0x40"):
        decode_motor(bytes(buf))


def test_trame_tronquee_refusee():
    with pytest.raises(ValueError, match="trop courte"):
        decode_motor(trame()[:20])
