# VT-Light — éclairage du banc et réglage de la D405

Choix, mesure et validation des réglages photométriques du banc visuo-tactile :
trois niveaux de lumière et un jeu de paramètres caméra figés, destinés à un jeu de
données de reconstruction 3D sur trois matériaux (PLA marbré, PLA argenté, PLA
translucide) sous six angles.

Toutes les valeurs de ce fichier ont été **mesurées sur le banc le 2026-07-30**,
avec le cube en PLA marbré en place. Aucune n'est reprise d'une fiche technique.

---

## Le profil retenu

```
640 x 480 à 30 fps, couleur + profondeur
exposition      2200 us      (auto-exposition COUPÉE)
gain            16           (minimum du capteur)
balance blancs  5180 K       (auto-balance COUPÉE)
anti-scintillement  désactivé
brightness 0 | contrast 50 | gamma 300 | hue 0 | saturation 64 | sharpness 50

niveau haut    PWM 200
niveau moyen   PWM  60       -2,00 diaphragme (vérifié)
niveau bas     PWM  21       -4,01 diaphragme (vérifié)

prise de vue de pose   PWM 35, même exposition — hors jeu de données
```

Le fichier de référence est `results/light_profile.json`, produit par
`tools/30_choose.py` et consommé par `vt_light.profile.LightProfile`. **Ne pas
recopier ces constantes ailleurs** : elles y divergeraient.

---

## Pourquoi ces valeurs, et pas d'autres

### L'exposition doit être la même aux trois niveaux

C'est le piège central. Une exposition ajustée à chaque niveau compense la
variation de lumière, et le jeu de données ne montre plus rien de l'effet cherché.
L'exposition est donc fixée une fois pour toutes, par la contrainte la plus dure :
**ne pas écrêter l'objet au niveau le plus clair**. L'écrêtage est le seul défaut
irréversible — un pixel à 255 a perdu son information, aucun traitement en aval ne
la retrouve.

Mesuré à PWM 200 : la face supérieure du cube reste intacte jusqu'à 3000 us, brûle
à 17,6 % dès 3800 us. La frontière est franche. On retient 2200 us, soit un demi
diaphragme sous la limite, en réserve pour le PLA argenté qui n'est pas encore
monté.

### Le niveau haut n'influe pas sur la qualité du niveau bas

Contre-intuitif mais direct : le signal au niveau bas vaut
`E_bas x t = (E_haut x t) / 2^(2 x pas)`, et le produit `E_haut x t` est déjà
plafonné par l'écrêtage. **Seul l'écart entre niveaux décide** de ce qui reste au
niveau bas. Le choix du PWM haut, lui, ne joue que sur la marge disponible.

### L'écart de 2 diaphragmes est la limite de la lampe, pas un choix arbitraire

Le variateur couvre 4,65 diaphragmes exploitables (PWM 15 à 200). Trois niveaux
espacés de `p` en consomment `2p` : le maximum possible est donc 2,32. On retient
2,0 — valeur ronde, et contraste maximal entre conditions. Les écarts de 1,0 et 1,5
diaphragme ont aussi été mesurés et restent exploitables (`results/30_choose/levels.csv`)
si l'objet suivant demande plus de marge.

### L'espacement se règle en éclairement, jamais en PWM ni en niveaux de pixels

Deux non-linéarités s'ajoutent :

- **Le pipeline couleur applique un gamma de 0,653** (mesuré). Un blanc à 90 ne
  reçoit donc pas la moitié de la lumière d'un blanc à 180.
- **Le variateur sature au-delà de PWM 200** : de 200 à 255 l'éclairement ne gagne
  que 0,12 diaphragme, alors qu'il est quasi linéaire en PWM entre 20 et 200.

Trois PWM régulièrement répartis donneraient donc des éclairements sans aucune
régularité. La méthode employée n'exige pourtant aucun photomètre : **le temps
d'exposition est une référence linéaire gratuite.** Comme `H = E x t`, balayer `t`
à éclairement constant donne la courbe de réponse `v = f(H)` de la caméra ; une
fois inversée, toute mesure `(t, v)` devient un éclairement relatif. Voir
`vt_light/photometry.py`.

Contrôle : la même courbe d'éclairement relevée à deux expositions différentes
concorde à **0,147 diaphragme** près. Et les écarts obtenus valent 0,99 / 1,02 —
1,50 / 1,51 — 2,00 / 2,01 diaphragme pour des consignes de 1,0, 1,5 et 2,0.

Une précaution s'est avérée nécessaire : la courbe de réponse n'est inversible que
de 18,2 à 227,1 en niveau, et le blanc du niveau bas vaut 18,1 — juste en dessous.
Son éclairement n'était donc pas vérifiable à l'exposition du jeu de données. Comme
l'éclairement ne dépend pas de l'exposition, `illuminance_at` rallonge celle-ci le
temps de la seule mesure (4400 us au lieu de 2200), ce qui ramène le niveau dans la
plage. Sans cela, l'écart annoncé pour le niveau bas aurait été supposé et non
mesuré.

### La balance des blancs était le réglage le plus négligé

Le code existant coupe l'auto-balance **sans écrire de valeur** : la caméra reste
sur la dernière trouvée par l'automatisme, différente à chaque démarrage. Les
images d'un même jeu de données n'étaient donc pas comparables colorimétriquement.

Le PLA blanc des marqueurs sert de référence neutre : sous la bonne balance, ses
trois canaux sont égaux. Optimum à **5180 K**, retrouvé à 70 K près à deux niveaux
de lumière indépendants. Le défaut 4600 K laissait 10 à 15 % de dérive
(B/G = 1,12), contre 2 à 5 % au réglage retenu.

### L'exposition doit être un multiple de 200 us

La porteuse PWM du firmware tourne à 5 kHz, soit 200 us de période. Une exposition
qui n'en est pas un multiple entier intègre un nombre fractionnaire de créneaux, et
l'éclairement reçu varie alors d'une image à l'autre selon la phase — jusqu'à
±2,4 % pour un demi-créneau. Tous les outils appliquent
`snap_exposure_to_pwm_period`.

---

## Deux particularités de la D405 qui commandent tout le reste

**Il n'y a qu'un seul capteur** (`Stereo Module`). Exposition, gain et balance des
blancs sont **partagés entre couleur et profondeur** : aucun réglage ne peut viser
l'un sans agir sur l'autre.

**Il n'y a aucun projecteur infrarouge** — pas d'option `Emitter Enabled` ni
`Laser Power`. La stéréo est **passive** : la profondeur est calculée sur la
texture éclairée par la lampe. Baisser la lumière dégrade donc directement la
géométrie, et pas seulement la couleur. C'est ce qui rend l'étude pertinente.

Les deux flux ne sont pas co-registrés : l'écart atteint ~1,8 mm sur la face du
cube. `D405(align_depth=True)` (défaut) aligne la profondeur sur la couleur, sans
quoi un masque tracé sur l'image couleur ne désigne pas les mêmes points en
profondeur.

---

## Le conflit objet / marqueurs, et sa résolution

Deux sujets se disputent la même exposition et ne veulent pas la même chose. La
détection ArUco n'est **pas monotone** en luminance du marqueur — elle est en U :

| blanc du marqueur | 8–15 | 18–50 | 55–75 | 80–140 | 170 | 200 | 254 |
|---|---|---|---|---|---|---|---|
| marqueurs détectés / image | 0,00 | **1,1–1,7** | 0,2–0,9 | **0,00** | 0,40 | 0,96 | 1,00 |

Les deux balayages croisés — exposition à PWM fixe, puis PWM à exposition fixe —
se superposent quand on les trace contre la luminance du marqueur : c'est **elle
seule** qui gouverne la détection, pas le réglage employé pour l'obtenir.

La conséquence est gênante : la bande 80–140, qui correspond à une image « bien
exposée » et où tombe le niveau haut du profil, ne décode **rien**. Or la fenêtre
utilisable ne fait que 1,5 diaphragme de large, alors que les trois niveaux en
couvrent 4. **Aucun jeu de trois niveaux ne peut donc y tenir.**

D'où la quatrième prise de vue, à **PWM 35** — au milieu du plateau de détection et
non à son meilleur point, pour ne pas s'installer au bord d'une falaise. Le plateau
étant immobile, l'angle est le même pour les quatre images : la vérité terrain
angulaire est ainsi **découplée** des réglages du jeu de données. Elle y rend 1,2 à
1,7 marqueur par image selon les séries — la dispersion d'une exécution à l'autre
est du même ordre que les écarts à l'intérieur du plateau, ce qui est précisément la
raison de se placer en son milieu.

> **Limite de fond.** Seuls les identifiants **1 et 6** sortent, quel que soit le
> réglage, et jamais plus de 2 simultanément. La limite est **géométrique** —
> marqueurs de 10 mm vus en incidence rasante — et aucun réglage photométrique ne
> la lèvera. Les leviers sont ailleurs : surélever la caméra au-delà de 50°
> d'élévation, ou passer à des marqueurs de 20 mm.

---

## Ce que les trois niveaux révèlent des trois matériaux

Les trois cubes ont été montés et validés le 2026-07-30, **sans jamais toucher au
profil**. Aucun des trois n'écrête : p99 de 182, 185 et 190 pour un plafond à 255.
Le PLA argenté imprimé est satiné et non miroir — l'écrêtage spéculaire redouté
n'existe pas.

Mesures sur la **face plane dominante** de chaque cube, à surface équivalente
(`tools/60_reanalyse_depth.py`) :

| | marbré | argenté | translucide |
|:--|--:|--:|--:|
| **remplissage** haut (PWM 200) | 100 % | 99,1 % | 100 % |
| moyen (PWM 60) | 100 % | 97,0 % | 99,3 % |
| bas (PWM 21) | 100 % | **69,0 %** | 96,6 % |
| **bruit** haut | 0,15 mm | 0,51 mm | **0,75 mm** |
| moyen | 0,19 mm | 0,60 mm | 0,76 mm |
| bas | 0,24 mm | 0,82 mm | 0,91 mm |

**Les trois matériaux échouent de trois façons différentes, et les niveaux les
séparent.**

- **Marbré** : insensible à la lumière sur les deux métriques. Mat, clair,
  fortement moucheté — le cas le plus favorable pour la stéréo passive.
- **Argenté** : *limité par le remplissage*. Correct à pleine lumière, il
  **s'effondre au niveau bas** — près d'un tiers de la face perd toute mesure. Sa
  seule texture est la fine striation des couches d'impression, qu'il faut
  suffisamment de lumière pour résoudre.
- **Translucide** : *limité par le bruit*. Le pire des trois **à tous les niveaux**,
  0,75 mm même à pleine lumière, et la lumière n'y change presque rien
  (0,75 → 0,91). Signature de la diffusion sous la surface : la stéréo apparie à une
  profondeur *à l'intérieur* du matériau, et le motif de remplissage interne,
  visible par transparence sur les images, ajoute une texture située au mauvais
  endroit. Un biais systématique, que plus de lumière ne corrige pas.

C'est exactement ce qu'on attendait des trois niveaux : ils distinguent un matériau
que la lumière peut sauver (l'argenté) d'un matériau qu'elle ne sauvera pas (le
translucide).

> **Piège de mesure, corrigé.** Un ajustement de plan par moindres carrés sur tout
> le masque ne mesure le bruit que si le masque couvre **une seule face**. Le cube
> translucide est posé sur un sommet et expose deux faces éclairées : l'ajustement
> global y rendait 9 à 11 mm, soit l'angle dièdre entre les faces déguisé en bruit.
> `metrics.depth_stats` isole désormais le plan dominant par RANSAC et publie
> `plane_inlier_frac` — nettement sous 100 %, c'est le signal qu'un masque couvre
> plusieurs faces. La colonne `plane_rms_all_mm` conserve l'ajustement naïf, dont
> l'écart avec la version robuste rend le problème visible.

À l'échelle de la **scène entière** — fond noir compris — le remplissage passe de
54,9 % à 34,0 % entre le niveau haut et le bas sur le marbré. L'effet de la lumière
est donc bien réel partout ; il se concentre sur les zones peu contrastées, où la
stéréo passive manque de motif à apparier. Les deux échelles méritent d'être
conservées dans le jeu de données : l'une dit ce que devient la surface visée,
l'autre tout le reste.

### Aucun angle n'écrête : vérifié sur une révolution complète

L'exposition ayant été fixée sous une seule orientation, `50_specular_sweep.py`
balaie un tour entier en rotation continue — 1025 images, soit 0,4° d'échantillonnage
angulaire. À 2200 us, le flou de filé vaut 0,1° : négligeable, donc inutile de
positionner le plateau, ce qui évite la roue libre de ~15° et la bascule
`START_PAUSE`.

| matériau | p99,9 min → max sur le tour | pixels écrêtés | marge au pire angle |
|:--|--:|--:|--:|
| argenté | 146 → 218 | **0** | 0,23 diaphragme |
| translucide | — → 236 | **4** | 0,11 diaphragme |

Sur l'argenté, le p99,9 varie de 0,9 diaphragme selon l'orientation sans jamais
écrêter ; le pic n'est pas un éclat spéculaire mais la face supérieure captant la
lampe plus frontalement.

Le translucide écrête **4 pixels** à une seule orientation, localisés en
x 316-320 / y 63-65 : le **sommet supérieur du cube**, là où l'impression est
déformée. C'est un défaut de cette pièce, pas une limite du réglage — 4 pixels sur
307 200 n'affectent aucune reconstruction.

> **Ne pas corriger.** Baisser les trois niveaux de 0,3 diaphragme (PWM 200/60/21 →
> 165/51/18) ramènerait la marge à 0,4 mais ferait tomber le rapport signal/bruit du
> niveau bas de 22,5 à ~20,5 sur le translucide, soit la limite d'acceptation. On
> échangerait quatre pixels sur un défaut d'impression contre une dégradation réelle
> et générale.

Lumière parasite mesurée à PWM 0 : **0,78 / 255**. La pièce est noire, le zéro du
variateur est un vrai zéro.

---

## Stabilité entre deux prises de vue

Séquence testée telle qu'elle sera pratiquée : 45 s à PWM 255 — le temps de faire
tourner le plateau et de cadrer — puis bascule au niveau de la prise de vue et
suivi pendant 25 s.

| niveau | images encore sous l'ancien éclairage | dérive sur 25 s |
|:--|--:|--:|
| haut | 0 | −0,18 % |
| moyen | 1 | +0,02 % |
| bas | 1 | +0,74 % |

**On peut laisser la lampe au maximum entre les prises** : il suffit de jeter 3
images après la bascule (`flush`, déjà fait par `LightProfile.apply`). La dérive
thermique de la LED est négligeable — 0,74 % au pire, soit 0,01 diaphragme.

En revanche, la pleine lumière ne convient **pas** à l'asservissement angulaire par
ArUco, qui n'y détecte rien : pour cela il faut redescendre dans la fenêtre
PWM 21–50.

---

## Utilisation

### Depuis la chaîne d'acquisition

```python
from vt_light import Dimmer, D405
from vt_light.profile import LightProfile

profile = LightProfile.load()                      # results/light_profile.json
with Dimmer("/dev/ttyACM0") as dim, D405(profile.camera_settings()) as cam:
    for angle in (0, 60, 120, 180, 240, 300):
        # ... amener le plateau à l'angle, plateau immobile ...

        profile.apply_pose_frame(cam, dim)         # mesure de l'angle réel
        pose_color, _ = cam.grab()

        for level in profile.level_names:          # "haut", "moyen", "bas"
            profile.apply(cam, dim, level)
            color, depth = cam.grab()
            # ... enregistrer ...
```

### Refaire le réglage

Les outils sont numérotés dans l'ordre du protocole. Chacun écrit dans `results/`
ce que le suivant consomme.

| outil | rôle | à relancer quand |
|:--|:--|:--|
| `00_check.py` | état du banc : acquittement du dimmer, réglages relus, sens du PWM, lumière parasite | début de session |
| `05_masks.py` | fige les zones de mesure (blanc des marqueurs, objet) | changement d'objet ou de pose caméra |
| `10_white_balance.py` | balayage 2800–6500 K sur le PLA blanc | changement de lampe |
| `20_response.py` | courbe de réponse caméra puis courbe du variateur | changement de lampe ou de gain |
| `25_markers.py` | taux de détection ArUco contre luminance (diagnostic) | changement de pose caméra |
| `30_choose.py` | **décide** exposition, trois niveaux, prise de vue de pose | après les précédents |
| `40_validate.py` | valide le profil sur l'objet monté | **chaque matériau** |
| `45_stability.py` | dérive après une bascule depuis le maximum | changement de lampe |
| `50_specular_sweep.py` | cherche un écrêtage sur une révolution complète | **chaque matériau** |
| `60_reanalyse_depth.py` | recalcule les métriques de profondeur des matériaux déjà enregistrés, hors banc | après un changement de métrique |

```bash
python3 tools/00_check.py
python3 tools/05_masks.py -w 5180
python3 tools/10_white_balance.py                 # donne la valeur de -w
python3 tools/20_response.py  -w 5180
python3 tools/25_markers.py   -w 5180
python3 tools/30_choose.py    -w 5180
python3 tools/40_validate.py  --object pla_marbre
python3 tools/45_stability.py
python3 tools/50_specular_sweep.py --object pla_marbre
```

Le variateur et le pont infrarouge du plateau sont le **même ESP32 sur le même port
série** : `Dimmer.send_ir()` émet les trames du plateau par la connexion déjà
ouverte. Ouvrir un second port en parallèle échouerait.

> `10_white_balance.py` doit tourner avant `05_masks.py -w`, mais `05_masks.py`
> fournit le masque dont il a besoin. Lancer `05_masks.py` une première fois sans
> `-w` (la valeur par défaut suffit à détecter les marqueurs), puis relancer avec la
> valeur trouvée.

### Où ça tourne

Tout dans la VM `openclaw-vm` : c'est là que vivent `pyrealsense2` et le port série
de l'ESP32. Le Mac n'a pas de roue `pyrealsense2` pour arm64.

```bash
rsync -az --exclude __pycache__ --exclude .git --exclude results \
      VT-Light/ openclaw-vm:~/VT-Light/
ssh openclaw-vm 'cd ~/VT-Light && python3 tools/00_check.py'
rsync -az openclaw-vm:~/VT-Light/results/ VT-Light/results/
```

La D405 et le Feather ESP32-S3 doivent être routés **vers la VM**. Cela se fait en
ligne de commande, sans passer par l'interface d'UTM :

```bash
/Applications/UTM.app/Contents/MacOS/utmctl usb list
/Applications/UTM.app/Contents/MacOS/utmctl usb connect Ubuntu24lts 8086:0B5B
```

---

## Le firmware

`Dimmer_And_IR_control_ESP32/DimOK_IRnotTested/` — sortie PWM sur **A2**, 5 kHz,
8 bits. Le même ESP32 sert de pont infrarouge pour le plateau : il émet donc
spontanément des lignes `RCV ...`, que toute lecture d'acquittement doit ignorer
(`Dimmer._read_ack`).

```
PWM <0-255>\n   ->   ACK PWM <valeur>\r\n
```

L'acquittement est ce qui distingue un problème de liaison série d'un problème
d'alimentation de la lampe. `Dimmer.set_pwm` échoue explicitement plutôt que de
supposer que la commande est passée.

Le firmware démarre à `ledcWrite(255)` : **la lampe est à pleine puissance dès la
mise sous tension**, avant toute commande.

---

## À faire avant de lancer le jeu de données

1. ~~Valider les trois matériaux~~ — **fait le 2026-07-30. Le profil convient aux
   trois sans modification**, et les trois niveaux les discriminent bien.
2. **Le cube translucide est déformé à son sommet supérieur.** C'est là que se
   trouvent les 4 pixels écrêtés, et le défaut fausse aussi sa géométrie de
   référence. À réimprimer si la vérité terrain géométrique doit servir à évaluer
   la reconstruction ; sans importance si seule la qualité relative entre niveaux
   est étudiée.
3. **Recalibrer le centre du plateau** (`Control_Turtable_IR/calibrate_center.py`) :
   il est stocké en pixels et ne vaut que pour la pose caméra courante.
4. Décider si l'on relève la caméra ou si l'on imprime des marqueurs de 20 mm.
   En l'état, la vérité terrain angulaire repose sur 1 à 2 marqueurs, soit environ
   ±2° — voir `Control_Turtable_IR/FONCTIONNEMENT.md`.
