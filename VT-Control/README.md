# VT-Control — supervision et orchestration du banc visuo-tactile

Un seul outil pour tout le banc : caméra, main DH116, plateau tournant et
lampe. En ligne de commande **et** en interface web, avec les mêmes fonctions
des deux côtés.

**Aucun pilote n'est réécrit ici.** VT-Control importe `VT-Tactile`, `VT-Light`
et `Control_Turtable_IR` tels quels, et n'ajoute que ce qui leur manquait : un
propriétaire unique par ressource matérielle, une machine à états explicite, un
journal qui survit au plantage, et une interface.

```bash
sudo python3 -m vtctl serve                     # interface web
sudo python3 -m vtctl session --objet cube      # le même protocole, en console
python3 -m vtctl --simulation --rapide serve    # tout le banc simulé, sans matériel
```

---

## Ce que ça règle

Le dépôt contient plusieurs outils qui pilotent les mêmes appareils, et rien
n'empêchait deux d'entre eux de se marcher dessus. Trois ressources sont
physiquement exclusives, et chacune a déjà coûté une session :

| Ressource | Ce qui casse quand deux processus s'en servent |
|:--|:--|
| **D405** | Un seul `rs.pipeline` par appareil. `vt_light.camera.D405` et `ArUcoTracker` le réclament tous les deux. |
| **`/dev/ttyACM0`** | Le variateur de lumière et le pont infrarouge sont **le même ESP32**. Ouvrir un second port réinitialise la carte, et le firmware redémarre **lampe à fond** : les marqueurs ArUco sont alors noyés. |
| **EtherCAT** | Un maître à la fois, y compris un maître survivant d'un autre processus (`lhandpro_service` tient l'interface sans poser de verrou). |

VT-Control donne à chacune **un propriétaire unique**, pour toute la durée de la
session. Un second prétendant est *refusé* avec un message qui nomme le premier,
jamais mis en attente silencieuse — une attente silencieuse sur un port série
ressemble exactement à un banc qui rame.

Un `flock` complète le verrou en mémoire : il meurt avec le processus qui le
tient, donc un plantage ne laisse pas de verrou fantôme.

### Un seul pipeline caméra

Auparavant, le tracker ArUco ouvrait sa propre caméra, et il fallait l'ouvrir
puis la refermer autour de **chaque** positionnement — deux établissements de
lampe par angle, soit sept secondes, et un état de lissage remis à zéro sans que
rien ne le signale.

Ici la caméra est ouverte une fois, et c'est l'**exposition** qui bascule :
600 µs pour lire les marqueurs, celle du profil VT-Light pour le jeu de données.
C'est possible parce que `detect_markers` et `estimate_turntable_angle` prennent
une image *en argument* : ils n'ouvrent aucune caméra.

---

## Le protocole

Deux phases, une machine à états explicite, chaque transition journalisée.

**Phase tactile d'abord.** L'opérateur amène le pouce au contact avec le curseur
d'opposition ; dès que le contact est franc et stable, ou qu'il choisit de passer
outre, les autres doigts se referment.

**Phase visuelle ensuite** — l'objet seul, aux mêmes angles, sans la main. Elle
n'a besoin ni du bus EtherCAT, ni des droits root, ni de l'opérateur.

Cet ordre est délibéré. L'inverse paraît naturel — photographier l'objet intact
avant d'y toucher — mais il fait dépendre la partie longue et fragile, la
saisie, d'un balayage déjà consommé. En commençant par le tactile, un abandon en
cours de session laisse au moins les saisies faites, et les vues de l'objet seul
se reprennent quand on veut.

```
POSITIONNEMENT → STABILISATION → MAIN_AU_DEPART → POUCE_EN_ATTENTE
                                                        │
                        ┌───────────────┬───────────────┤
                   satisfait       contourné       non requis
                        └───────────────┴───────────────┘
                                        ↓
    ARME → FERMETURE → SAISIE → CAPTURE → RELACHE → VALIDATION
```

Depuis n'importe quel état, une défaillance mène à `ERREUR` puis
`RECUPERATION`, d'où la session **repart** : un angle perdu n'en perd pas
d'autres.

### Valider ne fait pas avancer

Une prise ratée — la main a glissé, l'objet a bougé, aucun doigt n'a touché — se
refait **sur le même angle**. L'opérateur valide ou invalide, puis choisit
séparément : *refaire cet angle* ou *passer au suivant*. On n'avance que sur cet
ordre-là.

Chaque reprise écrit son propre essai à côté du précédent
(`tactile/angle_00/essai_00/`, `essai_01/`, …). Écraser priverait de la
comparaison qui dit *pourquoi* le premier était raté.

### Le critère « pouce stable » a trois issues, toutes légitimes

`satisfait`, `contourne`, `non_requis`. Le pivot du pouce n'amène pas le pouce
en opposition sur tous les objets — mesuré le 2026-08-20, il reste 7 à 24 mm à
droite de la face avant du cylindre — donc **contourner est un cas courant, pas
une avarie**.

Ce qui est refusé, c'est le contournement *silencieux* : `contourne` exige un
motif, et le refus est aussi bien dans la machine à états que dans l'interface.
Le statut, le motif, les seuils et la pression au déclenchement partent dans
`pouce.json`, à côté de la capture.

### Le carreau du pouce est une information, pas une condition

Le marqueur ArUco n° 7 est collé sur le pouce. L'interface dit s'il est dans le
champ pendant que l'opérateur place le pouce, et la capture enregistre la
réponse. Mais il ne conditionne rien : le pouce peut parfaitement toucher l'objet
hors du champ, et refuser la saisie pour ça serait absurde. Ce qu'on y gagne,
c'est de savoir **avant** de fermer que l'image de la prise ne montrera pas ce
qui touche.

---

## Ce qui est enregistré

```
sessions/20260821-131500_cube_pla_gris/
  session.json        identité, matériel, réglages, ancre d'horloge
  journal.jsonl       append-only, fsync à chaque évènement
  manifest.json       index — reconstructible depuis le journal
  visuel/angle_00/    angle.json · capture.json · 0000_color.png · 0000_depth.npy
  tactile/angle_00/   angle.json · pouce.json · capture.json · baseline.json
                      00_avant/ · 01_saisi/ · 02_relache/
  brut/               stream_00_data.npy  (N, 192) uint8
                      stream_00_t.npy     (N,)     float64
```

**Une horloge unique**, `time.perf_counter`, ancrée une fois à l'heure murale.
Sans cette ancre — c'est ce qui manquait au format 1 — un `t` relatif n'a de
sens que dans le processus qui l'a pris, et deux sessions ne sont plus
comparables.

**L'horodatage est pris à la prise de vue**, pas à l'écriture. En version 1 il
l'était *après* `cv2.imwrite`, si bien que le temps d'encodage PNG s'ajoutait au
délai réel.

**Les trames brutes ne sont jamais filtrées**, sous-échantillonnées ni
dédupliquées. Deux trames identiques sont deux trames : au repos la charge utile
ne bouge pas d'un octet pendant des secondes, et ces périodes sont précisément
la ligne de base dont dépend tout décodage ultérieur.

**L'angle enregistré est l'angle mesuré**, jamais l'angle commandé — et `null`
est un résultat légitime. Les marqueurs font 10 mm et la caméra est à 29,5°
d'élévation : un angle inventé serait pire qu'un angle absent.

### Une session tuée reste exploitable

Le manifeste n'est qu'un résumé ; le journal, lui, est écrit et `fsync`é
évènement par évènement. Sur les 39 sessions produites par l'outil précédent,
**deux n'ont pas de manifeste** — 218 Mo d'images et de trames sans index.

```bash
python3 -m vtctl recover --lister          # qu'est-ce qui manque ?
python3 -m vtctl recover 20260821-125907_cube_pla_gris
```

Le manifeste reconstruit porte `complet: false` et un bloc `recuperation` qui
dit d'où il vient. Il ne se fait pas passer pour un manifeste normal.

---

## Commandes

| | |
|:--|:--|
| `vtctl selftest` | Le banc répond-il ? Un contrôle par ressource, aucune acquisition. Code de sortie exploitable. |
| `vtctl serve` | Interface web et API locale, port 8090. |
| `vtctl session` | Le protocole complet en console, avec les mêmes décisions que l'interface. |
| `vtctl pose` | **Aider à replacer la caméra.** Dit si les carreaux sont lisibles, et sinon quoi corriger. |
| `vtctl angle` | Mesurer, positionner ou calibrer le plateau. |
| `vtctl main` | Pilotage direct de la main : ouvrir, zéro tactile, position, pivot du pouce. |
| `vtctl recover` | Reconstruire un manifeste depuis le journal. |

Options utiles sur toutes : `--simulation`, `--sans-main` (dispense des droits
root), `--exposition`, `--pwm-aruco` / `--expo-aruco` / `--gain-aruco`.

L'interface tourne **dans la VM**, seule machine qui voit le matériel :

```bash
ssh -N -L 8090:127.0.0.1:8090 openclaw-vm     # puis http://127.0.0.1:8090
```

---

## La caméra bouge, et rien ne le signale

Le centre du plateau est stocké **en pixels**. Une caméra déplacée de quelques
centimètres rend donc tous les angles faux, sans qu'aucune alarme ne se
déclenche — et la panne se déguise en autre chose :

| Ce qu'on observe | Ce que c'est vraiment |
|:--|:--|
| identifiants aberrants (12, 17, 26, 34 au lieu de 1-6) | motif lu de travers : incidence trop rasante |
| aucun marqueur, à **toutes** les expositions | caméra trop basse ou trop loin |
| angles qui sautent de dizaines de degrés | carreaux mal décodés, pas un défaut d'asservissement |

```bash
python3 -m vtctl pose --suivre 120         # bougez la caméra, la ligne suit
python3 -m vtctl pose --image /tmp/p.png   # une image annotée, à regarder
```

La mesure qui décide est **l'aplatissement** : un carreau *est un carré*, et le
rapport largeur/hauteur de sa boîte englobante donne directement l'incidence.
Elle a le mérite de fonctionner **quand rien ne se décode** — c'est-à-dire
précisément dans le cas où tous les autres indicateurs sont à zéro et où l'on ne
sait plus si le problème vient de la lumière, du cadrage ou de la pose.

Relevé le 2026-08-23, caméra trop basse : `forme 1.59:1`, aucun carreau
décodable entre 2200 et 60000 µs. Viser moins de `1.35:1`, puis
`python3 -m vtctl angle --calibrer` avant d'acquérir.

---

## Le mode simulation

`--simulation` monte un banc complet en mémoire. Ce n'est pas une maquette :
il rejoue les défauts **mesurés** du matériel, et fait passer ses données par
les vrais décodeurs.

- La main n'émet rien tant qu'elle n'a pas reçu `set_enable` **puis**
  `home_motors` — en-têtes corrects, charge utile identiquement nulle.
- Les créneaux moteur de la trame suivent l'ordre des articulations de l'URDF,
  pas les numéros de moteur : 2, 1, 6, 5, 4, 3. Les deux du pouce ouvrent la
  charge utile. Les lire dans l'ordre des identifiants rend zéro sur le pouce
  quoi qu'il fasse. Le pivot du pouce a ainsi été journalisé à plat pendant un
  mois alors qu'il fonctionnait, et l'exclusion de la flexion — justifiée sur
  la main précédente — a survécu sans qu'on puisse la réfuter.
- `move_motors` n'est pas toujours pris : un envoi isolé rate une fois sur deux.
- La position est un **u16 signé** : un doigt repoussé sous son zéro rend 63993
  pour −1543.
- `START_PAUSE` est une bascule, et `VITESSE_MOINS` démarre le plateau.
- La lampe met plus de deux secondes à s'établir, et démarre à PWM 255.
- `ring.pad` est mort ; la paume ne répond que sur 14 points sur 26.
- Les marqueurs ArUco sont de **vrais** marqueurs `DICT_4X4_50`, en couleurs
  inversées comme ceux du banc, et ne se décodent qu'une image sur deux.

Un simulateur qui ne reproduit que le cas nominal valide un matériel qui
n'existe pas.

---

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -m "not lent"   # 74 tests, 24 s
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q                 # + 6 sessions complètes, 7 min
```

Aucun ne demande de matériel : ils tournent sur un poste de travail sans
`pyrealsense2` ni `pyserial`.

| Fichier | Ce qu'il verrouille |
|:--|:--|
| `test_machine.py` | Le protocole lui-même : transitions légales, refus des autres, les trois issues du pouce, et le refus d'un contournement sans motif. |
| `test_store.py` | Le journal survit à une ligne tronquée ; les octets relus sont les octets écrits ; la profondeur reste `uint16` ; l'ancre d'horloge permet de dater. |
| `test_resources.py` | Un second prétendant est refusé, pas mis en file ; le verrou meurt avec son processus. |
| `test_banc_simule.py` | Les trames simulées passent le **vrai** décodeur ; l'angle passe le **vrai** détecteur ; les défauts du matériel sont bien rejoués. |
| `test_runner.py` (`-m lent`) | Le protocole complet, de la session ouverte au manifeste : les deux phases, les trois issues du pouce, le saut d'un angle, l'arrêt propre. |

Pour l'interface, voir [`PROCEDURE-TEST-WEB.md`](PROCEDURE-TEST-WEB.md) — une
procédure pas à pas, écrite pour être suivie sans connaître le code.

---

## Ce qui est hors périmètre

Odométrie de la main, interprétation fine des capteurs tactiles, mesure des
courants moteur, modèles de fusion. Le MVP acquiert des **données brutes
correctement horodatées et associées à un angle** ; l'interprétation viendra
sur des données déjà acquises.

Nuance sur les courants : ils restent présents dans les trames moteur brutes, et
sont conservés intégralement. Leur seul usage dans le code est un garde-fou —
arrêter un doigt, jamais mesurer quoi que ce soit. Un doigt immobile **à courant
élevé** n'est pas un ordre perdu mais un obstacle mécanique : le relancer, c'est
forcer dessus.

---

## Dépendances

Python 3.10+, `numpy`, `opencv-python`. Sur le banc, en plus :
`pyrealsense2`, `pyserial`, et le SDK constructeur `LHandProLib` là où
`vt_tactile.bus` sait le trouver. L'interface web n'ajoute rien : serveur HTTP
de la bibliothèque standard, Server-Sent Events, page autonome.

Les droits root ne sont nécessaires que pour la main — le maître EtherCAT ouvre
des sockets raw.
