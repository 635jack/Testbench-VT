# VT-Tactile — lire les capteurs tactiles de la DH116

Bibliothèque minimale pour l'acquisition visuo-tactile : deux modules, un outil.
Le SDK constructeur ne sert qu'à monter le bus et réveiller la main ; le contenu
des trames est décodé ici, avec une table **mesurée sur la main** et non lue
dans le binaire du SDK.

## Voir la pression en direct

```bash
ssh openclaw-vm
cd ~/VT-Tactile && sudo python3 -m tools.live
```

Réveille la main, prend le zéro, puis affiche les onze zones en continu :

```
Pression tactile DH116    92.4 trames/s   zéro fait

zone         canaux     max   force N  force T    dir   prox
────────────────────────────────────────────────────────────
thumb.tip    ▁▃█▅▁     0.87     13.70     2.10    142   1.00
thumb.pad    ▁▁▁▁      0.00      0.00     0.00      —   0.00
...
palm         ··········▃█▅▁···   0.41
```

`Entrée` ou `r` refait le zéro, `q` quitte. Les points `·` de la paume sont ceux
qui n'ont jamais répondu au balayage du 2026-07-31 ; `--all-palm` les affiche
quand même.

## Refermer la main sur un objet

```bash
sudo python3 -m tools.envelop --object "cylindre" --seat 150
```

Réveille la main, l'ouvre, prend le zéro **objet déjà posé**, puis referme les
quatre doigts pilotables en arrêtant chacun dès qu'il touche. Trajectoire
complète — positions, courants, pression des onze zones à chaque pas — dans
`grasps/<horodatage>.json`.

Trois détecteurs de contact : tactile (seuil de pression), courant (au-dessus du
courant libre **mesuré en début de fermeture**, pas d'un seuil figé), et calage.
Trois garde-fous : plafond de couple à 400 ‰, position bornée, et ouverture
rejouée dans tous les cas de sortie, exception comprise.

`--seat N` fait avancer le doigt de N counts après le premier frôlement, pour
asseoir la prise. `--dry-run` s'arrête avant la fermeture.

Le homing par diffusion (`home_motors(0)`) met la main en défaut — voyant
rouge, ALARM « out of position ». `wake()` home donc moteur par moteur.

Aucun moteur n'est exclu d'office. `hw.BROKEN_MOTORS` est vide et sert à en
écarter un au vol s'il lâche en campagne.

## Utiliser dans une boucle d'acquisition

```python
from vt_tactile.bus import Hand
from vt_tactile.tpdo import TactileReader

with Hand() as hand:                        # connecte ET réveille
    reader = TactileReader()
    reader.zero(hand.collect(2.0))          # main au repos, rien en contact

    while acquisition_en_cours:
        raw = hand.latest_tactile()
        if raw is None:
            continue
        state = reader.decode(raw)
        state["index.tip"].pressure_max     # 0.0 à 1.0
        state["index.tip"].normal_force     # unité constructeur, u16/100
        state.in_contact                    # ['index.tip', 'palm']
        enregistrer(state.to_dict())        # sérialisable tel quel
```

Onze zones : `thumb.tip`, `thumb.pad`, … `little.pad`, `palm`.

## Trois choses à savoir avant de s'en servir

**La main n'émet rien tant qu'elle n'est pas réveillée.** Alimentation des
moteurs **puis homing** : sans ça les trames circulent avec des en-têtes
corrects et une charge utile identiquement nulle. C'est le piège numéro un, on
croit à une panne d'alimentation. `Hand.wake()` s'en charge, réémet la demande
de trames capteur **après** le homing — qui remet la configuration à zéro — et
vérifie qu'une trame capteur arrive vraiment au lieu de l'espérer.

**Le zéro n'est pas optionnel.** Les lignes de base vont de ~80 à ~180 selon le
doigt : sans remise à zéro par canal, les zones ne sont pas comparables entre
elles. Reprenez-le si la main a été mise hors tension ou a beaucoup chauffé.

**Les capteurs saturent vite.** Un appui ferme met les canaux à 255, soit 1.0.
Pour exploiter la dynamique, visez des contacts légers à modérés.

## Ce que la table de découpage doit à la mesure

Établie le 2026-07-31 en sollicitant séparément le bout et la pulpe de chaque
doigt (`../VT-Bench`, run `complet2`). Appuyer sur le bout de l'index déplace les
offsets 1-4, 10-11, 14-15, 18-19, 22 ; sur la pulpe, 5-8, 12-13, 16-17, 20-21, 23.
Séparation parfaite.

| offset dans le créneau | contenu |
|:--|:--|
| 0-4 / 5-8 | pression bout / pulpe |
| 10-11 / 12-13 | force normale bout / pulpe, u16 LE ÷100 |
| 14-15 / 16-17 | force tangentielle bout / pulpe |
| 18-19 / 20-21 | direction bout / pulpe, `0xFFFF` = pas de direction |
| 22 / 23 | proximité bout / pulpe |

Créneaux de 30 octets aux offsets 2, 32, 62, 92, 122, 152 — pouce, index,
majeur, annulaire, auriculaire, paume. La paume est différente : 26 points de
pression, rien d'autre.

**Le SDK lit ces champs un octet trop tôt.** D'où sa proximité constante à 1,00
(il lit un octet figé à `0xFF`) et sa force normale valant 256 fois l'octet de
poids faible de la vraie valeur. Il applique en plus un filtre de plausibilité
aux seuls capteurs 3, 5 et 7 — index, majeur, annulaire — qui **jette la mise à
jour complète** sans rien signaler. C'est toute l'explication du symptôme
historique « seuls le pouce et le petit doigt donnent des infos consistantes ».
Le matériel, lui, répond sur les onze zones.

`tests/test_tpdo.py` verrouille cette table : le test casse si quelqu'un
réintroduit le découpage du SDK.

## État du matériel, mesuré

- **Dix zones de doigt : excellentes.** Pleine échelle, diaphonie quasi nulle.
- **Paume : partielle.** 14 points sur 26 répondent, gain plus faible ; le
  quadrant thénar est muet. Voir `PALM_SILENT`.
- **Proximité : inexploitable** à distance. L'octet ne bouge qu'au contact.
- **Bus : sain.** ~900 lectures/s, 100 % de trames neuves, p95 à 1,16 ms. La VM
  n'est pas un facteur.

## Contenu

| chemin | rôle |
|:--|:--|
| `vt_tactile/tpdo.py` | décodeur et remise à zéro, aucune dépendance constructeur |
| `vt_tactile/bus.py` | montage EtherCAT, réveil, flux de trames |
| `tools/live.py` | affichage temps réel des onze zones |
| `tests/test_tpdo.py` | tests sans matériel |

Pour caractériser le matériel (bruit, dérive, canaux morts, comparaison entre
alimentations ou interfaces), voir `../VT-Bench`.

À exécuter dans la VM, en `sudo` — le maître EtherCAT ouvre des sockets raw — et
un seul processus à la fois peut tenir le bus.
