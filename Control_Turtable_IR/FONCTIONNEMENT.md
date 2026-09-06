# Fonctionnement du banc — caméra et plateau tournant

Tout ce qui suit a été **mesuré sur le banc**, pas déduit de la documentation.
Les valeurs datent des 27 au 30 juillet 2026.

---

## 1. Caméra RealSense D405

### Mode de fonctionnement retenu

**640 × 480 à 30 images par seconde, couleur + profondeur.** C'est le mode
dans lequel l'ensemble fonctionne : 29,5 fps réels mesurés, détection ArUco
stable, asservissement opérationnel.

`depth_scale = 1e-4`, donc **l'unité de profondeur est le dixième de
millimètre**, pas le millimètre. Piège classique en aval.

### Le branchement USB détermine tout

| Branchement | Vitesse négociée | Couleur + profondeur |
|:--|:--|:--|
| Direct sur le Mac | USB 3.2, 5000 Mbps | **aucune image** |
| Via concentrateur | USB 2.1, 480 Mbps | **30 fps, stable** |

Contre-intuitif mais vérifié : en SuperSpeed, la caméra énumère
parfaitement, le noyau charge `uvcvideo` sans erreur, la **couleur seule**
fonctionne (20 images sur 20) — mais le flux de **profondeur** s'étrangle à
1,3 fps, et la combinaison des deux ne produit rien du tout.

En cause : la redirection USB d'UTM (`usbredir`) ne soutient pas les
transferts isochrones volumineux du flux de profondeur à cette vitesse. Le
réglage « USB 3.0 (XHCI) » d'UTM concerne le contrôleur émulé, pas la
capacité du canal de redirection.

> **Laisser la caméra branchée via le concentrateur.** Le passage direct ne
> serait exploitable qu'avec `-device usb-host` au lieu de la redirection, ce
> qui se heurte au pilote UVC de macOS qui retient l'interface.

### L'exposition doit être imposée

`ArUcoTracker.DEFAULT_EXPOSURE = 4000`.

Sans ça, **aucun marqueur n'est détecté**. L'auto-exposition se règle sur
l'objet clair posé sur le plateau, sature le PLA blanc des marqueurs et noie
leur motif noir. Mesuré en 640 × 480 :

| Exposition | Pixels saturés | Marqueurs détectés |
|:--|--:|--:|
| automatique | 7,1 % | **0** |
| 1500 à 6000 | 0 à 6 % | **2** |
| 9000 | 6,9 % | 1 |

Mettre `exposure=None` rétablit l'automatisme si le montage change.

### Les intrinsèques dépendent du mode

Les modes de la D405 **n'ont pas le même champ de vision** : 78,6° en
640 × 480 contre 88,6° en 1280 × 720. Les intrinsèques et le centre du
plateau ne sont donc **pas transposables** d'une résolution à l'autre.
Une calibration existe pour le 1280 × 720 dans `VT-Calib/results/` ; il en
faudra une pour le 640 × 480 si on a besoin de mesures métriques.

---

## 2. Marqueurs ArUco du plateau

Six marqueurs `DICT_4X4_50`, IDs 1 à 6, tous les 60°, **imprimés en PLA** et
en **couleurs inversées** — d'où `invert_colors: true` dans la configuration.
Leurs bords arrondis par l'impression dégradent le décodage.

Ils font **10 mm de côté**, soit 1,67 mm par cellule pour une grille 4 × 4
avec bordure. C'est le plancher du décodeur ArUco, qui a besoin de 4 à 5
pixels par cellule. La détection est donc structurellement marginale : on en
voit 2 à 4 selon la pose caméra, jamais 6 simultanément.

Des marqueurs de 20 mm doubleraient les pixels par cellule et rendraient la
détection confortable à n'importe quel angle. C'est le seul vrai levier.

### Effet de la pose caméra

Ce qui compte n'est pas la taille apparente mais le **rapport d'aspect** : un
marqueur vu en incidence rasante est écrasé et devient indécodable.

| Élévation de la caméra | Rapport d'aspect | Marqueurs |
|--:|--:|--:|
| 15° | 0,54 | 0 à 1 |
| 30° | — | 2 |
| 47 à 57° | 0,86 | 3 à 4 |

Viser un rapport d'aspect **supérieur à 0,85**, ce qui demande une élévation
d'au moins 50°.

---

## 3. Commande du plateau

Émetteur infrarouge NEC piloté par un Feather ESP32-S3 en série, 115200
bauds, `/dev/ttyACM0`. L'ESP32 **acquitte** chaque trame (`ACK NEC …`), ce qui
permet de distinguer un problème série d'un problème de portée IR.

### Trois comportements à connaître

**`START_PAUSE` est une bascule.** La même trame démarre ou arrête. Un ordre
d'arrêt de trop **relance** le plateau. Ne jamais en renvoyer un sans avoir
laissé la roue libre se terminer.

**`VITESSE_MOINS` démarre le plateau**, dans la dernière direction utilisée.
Ce n'est pas seulement un réglage de vitesse. Conséquence : réduire la vitesse
avant d'avoir fixé le sens lance le plateau à contresens — c'est ce qui rend
la **première consigne de chaque série imprécise**.

**La vitesse persiste** une fois abaissée, mais il faut la réarmer à chaque
consigne dans la pratique.

### Constantes physiques mesurées

| | |
|:--|--:|
| Vitesse maximale | 46 °/s |
| Vitesse minimale (3 décréments suffisent) | ~13 °/s |
| Roue libre après l'ordre d'arrêt | **~15°**, soit 1,2 à 3,2 s |

La roue libre est la contrainte dure : **toute impulsion, même de 120 ms,
déplace d'au moins 15°**. Les micro-ajustements sont donc illusoires — ils
échangent un dépassement contre un autre.

`braking_latency_sec = 0.45` a été réglé par encadrement sur cette base
(0,12 s donnait +9° de dépassement systématique, 0,8 s donnait −11°).

---

## 4. Le centre du plateau doit être recalibré à chaque session

Le centre est stocké **en pixels** : il ne vaut que pour la résolution **et**
la pose caméra où il a été estimé. Une caméra déplacée de quelques
centimètres le décale de dizaines de pixels, et tous les angles sont faux.

```bash
python3 calibrate_center.py -p /dev/ttyACM0 -d 25
```

L'outil fait tourner le plateau 25 secondes et ajuste une **ellipse** sur la
trajectoire de chaque marqueur. Résidu typique : **0,2 pixel**, les six
marqueurs s'accordant à 0,5 px près. Le rapport des axes de l'ellipse fournit
en prime une mesure indépendante de l'élévation de la caméra.

> L'ajustement doit être **elliptique**, pas circulaire : une trajectoire
> circulaire vue de biais se projette en ellipse. Ajuster un cercle laisse
> 13 à 17 px de résidu contre 0,2.

La détection automatique intégrée à `ArUcoTracker.auto_estimate_center` ne
suffit pas ici : elle travaille sur **une seule image** et exige soit une
paire de marqueurs opposés, soit trois marqueurs visibles **simultanément**.
Avec deux marqueurs adjacents, elle échoue. La méthode par trajectoire, elle,
accumule les positions au fil de la rotation et n'a besoin que d'un marqueur.

---

## 5. Précision réellement atteignable

**Enregistrer l'angle mesuré, pas l'angle commandé.** C'est la conclusion
pratique : la caméra donne la vérité terrain, la consigne n'est qu'une
intention.

En régime établi, l'écart à la consigne est de l'ordre de **2 à 5°**, avec
des essais à moins de 1°. La première consigne d'une série est en revanche
systématiquement fausse, de 20 à 35°, pour la raison expliquée plus haut.
Un contournement simple : faire une **première consigne à vide** en début de
session et ignorer son résultat.

### La limite de fond

Le bruit de la mesure d'angle dépend directement du nombre de marqueurs vus :

| Marqueurs détectés | Écart-type de l'angle |
|--:|--:|
| 4 | **0,004°** |
| 2 adjacents | **2,2°** |

La vitesse angulaire étant la **dérivée** de l'angle, ce bruit s'y amplifie :
avec deux marqueurs, la vitesse mesurée peut annoncer 43 °/s sur un plateau
immobile. Or l'anticipation de freinage vaut `vitesse × latence` — elle est
donc pilotée par ce bruit, ce qui explique que le **même code** donne parfois
1° d'écart et parfois 30°.

Deux leviers si l'on veut aller plus loin, par ordre d'efficacité :

1. **Détecter plus de marqueurs** — élévation caméra plus forte, ou marqueurs
   de 20 mm. C'est le levier dominant.
2. **Lisser la vitesse** par régression sur une demi-seconde au lieu de la
   calculer entre deux images consécutives. Diviserait le bruit par quatre
   environ, sans rien changer au montage.
