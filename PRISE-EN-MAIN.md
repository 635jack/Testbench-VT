# Prise en main du banc visuo-tactile

Vous recevez le matériel **en pièces**. Ce document va du carton à un jeu de
données complet. Il suppose que la robotique et l'apprentissage vous sont
familiers : il n'explique donc pas ce qu'est EtherCAT ou un marqueur ArUco, mais
il détaille tout ce qui est **propre à ce banc** et qu'aucune documentation
générale ne dira.

Les cinq modules ont chacun leur README, plus détaillé que celui-ci sur son
domaine. Ce document les articule et donne l'ordre des opérations.

---

## 1. Ce que vous recevez

| Pièce | Rôle | Sans elle |
|:--|:--|:--|
| Main **DH116** gauche | 6 moteurs, 11 articulations, 11 zones tactiles | rien du tactile |
| Adaptateur USB↔Ethernet | porte l'EtherCAT vers la main | rien du tactile |
| Caméra **RealSense D405** | couleur + profondeur de la saisie | rien du visuel |
| Webcam **C920** | angle du plateau par marqueurs | angle dégradé, voir §6 |
| Plateau tournant + télécommande IR | présente l'objet sous plusieurs angles | un seul angle |
| **ESP32** (Feather) | variateur de lampe + émetteur IR | ni lumière ni plateau |
| Bandeau LED + caisson | éclairage maîtrisé | mesures non reproductibles |
| Primitives imprimées | objets de référence, dimensions connues | pas de vérité terrain |

**Le SDK Leadshine n'est pas dans le dépôt** — il vous est livré avec la main.
Sans lui, tout marche sauf le pilotage des moteurs.

---

## 2. Remontage

### L'ordre qui compte

La main **n'est pas rétro-entraînable** : fermée, elle le reste jusqu'à ce qu'on
lui dise de s'ouvrir. Deux conséquences pour le montage.

1. **Alimentez la main avant de brancher l'EtherCAT.** L'ordre inverse marche,
   mais vous perdez le repère de diagnostic le plus utile : une porteuse à
   100 Mb/s sur l'interface `enx*` **prouve que la main est sous tension**,
   puisqu'une porteuse suppose le PHY d'en face allumé. Sans ce repère, « pas de
   porteuse » devient ambigu entre câble et alimentation.
2. **Ne coupez jamais l'alimentation main fermée.** Elle resterait serrée sur
   l'objet jusqu'à la remise sous tension.

### Placement de la caméra de profondeur

C'est le réglage le plus délicat, et celui que nous n'avons jamais tout à fait
réussi. Deux exigences se contredisent :

- **pour la saisie**, la caméra doit voir la main et l'objet de près ;
- **pour l'angle du plateau**, elle doit voir les carreaux ArUco assez grands et
  assez de face.

Nous avons tourné à **29,5° d'élévation**, alors que `FONCTIONNEMENT.md`
recommande **au moins 50°**. À 29,5°, quatre carreaux tombaient à 8-13 px de haut
et les deux plus proches étaient coupés par le bord bas de l'image : **zéro
détection sur 40**, quel que soit l'éclairage.

> **Montez la caméra plus haut que nous.** Visez un aplatissement de carreau
> inférieur à 1,35 — `python3 -m vtctl pose --suivre` l'affiche en direct et vous
> dit s'il faut la relever. C'est cinq minutes au montage, contre des semaines
> d'angle plateau inutilisable.

La webcam d'angle (§6) lève en grande partie cette contradiction : si vous
l'installez, la D405 peut être placée pour la seule saisie.

### Placement de la main

Les bouts de doigts doivent atteindre l'objet posé sur le plateau. Mesuré chez
nous : bouts à **178-189 mm** du repère de la main, objet de **125 à 171 mm**,
soit 7 à 18 mm de marge. Avec 54 mm de trop, aucune zone tactile ne répondait et
tous les doigts s'arrêtaient sur le courant — un banc qui semble marcher et
n'enregistre rien.

**Vérifiez cette marge avant tout le reste** : une saisie sans contact tactile
est indiscernable d'une saisie réussie dans les journaux, sauf à lire les
pressions.

### Le concentrateur USB — et pourquoi cette règle peut ne pas vous concerner

Notre documentation insiste : *brancher la D405 via le concentrateur, jamais en
direct, sinon aucune profondeur*. **C'est un artefact de notre hôte, pas de la
caméra.** Le banc tournait dans une machine virtuelle sur un Mac, et c'est la
redirection USB d'UTM qui ne soutient pas les transferts isochrones du flux de
profondeur en SuperSpeed.

**Sur un Linux natif, cette contrainte disparaît** — branchez la D405 comme vous
voulez. Plusieurs autres pièges documentés sont du même ordre : plafond de
périphériques partagés, trames MJPEG tronquées, gigue sur le lien EtherCAT. Ils
appartiennent à la virtualisation, pas au banc.

---

## 3. Mise en route logicielle

```bash
git clone https://github.com/635jack/Testbench-VT.git
cd Testbench-VT
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$PWD/Leadshine_SDK_original/sdk_lib/x86_64/share/LHandProLib/examples/EtherCAT_python:$PYTHONPATH"
sudo setcap cap_net_raw,cap_net_admin+eip "$(readlink -f .venv/bin/python3)"
```

Puis, **dans cet ordre** :

```bash
cd VT-Control
python3 -m vtctl doctor      # l'environnement est-il complet ?
python3 -m vtctl selftest    # le banc répond-il ? aucune acquisition
```

`doctor` ne touche à rien et dit quoi faire pour chaque manque. `selftest`
exerce chaque pièce — port série, lampe, caméra, marqueurs, plateau, trames
EtherCAT, mouvement d'un doigt.

**Ne passez pas à la suite tant que `selftest` n'est pas vert.** Chacun de ses
contrôles correspond à une panne qui, plus loin, se manifeste par un symptôme
qui n'a rien à voir avec sa cause.

---

## 4. Les quatre étalonnages, tous obligatoires

Aucune valeur du dépôt ne vaut pour votre exemplaire. Les réemployer donne des
résultats faux **sans qu'aucune erreur ne soit levée** — c'est le mode de panne
le plus coûteux de ce banc.

### 4.1 Centre du plateau, en pixels

Il ne vaut que pour la résolution **et** la pose de caméra où il a été estimé.
Quelques centimètres de déplacement le décalent de dizaines de pixels.

```bash
python3 calibrate_center.py -p /dev/ttyACM0 -d 25   # depuis Control_Turtable_IR
```

### 4.2 Angles des carreaux ArUco

**Les carreaux ne sont pas régulièrement répartis.** Les nôtres forment trois
paires serrées espacées de 120°, et non six marqueurs tous les 60° comme un
README l'affirmait :

    {1: 0,0  2: 121,7  3: 234,6  4: 345,9  5: 119,3  6: 240,1}

Une table fausse fait **sauter l'angle annoncé selon le carreau visible**. Chez
nous, l'erreur était d'un cran entier et donnait un asservissement en apparence
capricieux — excellent quand deux mesures tombaient sur le même carreau, absurde
dès qu'il changeait. Elle a été la cause première de tous les échecs de
positionnement, pendant des semaines.

**Le test qui tranche** : chaque carreau donne angle plateau = angle apparent
moins angle sur table. Si la table est juste, les six s'accordent. Si deux
carreaux adjacents se contredisent, elle est fausse.

### 4.3 Profil photométrique

Réponse du variateur et de la caméra. Voir `VT-Light/README.md`, qui est le plus
complet des cinq. Deux pièges qui ont produit des tableaux entiers de valeurs
fausses :

- la lampe met **plus de 2 s** à s'établir après un saut de PWM — attendez 3,5 s ;
- l'exposition doit être un **multiple de 200 µs**, période du variateur.

### 4.4 Facteur counts/radian de la main

```bash
python3 -m vtctl cinematique --etalonner
```

> **Attention à ce que cet étalonnage mesure vraiment.** Il compare les counts de
> la trame brute aux degrés rendus par `get_now_angle`. Or le SDK rend exactement
> `counts / 125` : on remesure donc une constante logicielle, pas la mécanique. Le
> facteur sort toujours à 7161,972 avec une dispersion nulle, et la dérive
> rapportée sera toujours de 0 %. Ce n'est **pas** une validation de la
> cinématique. Une vraie mesure demande une référence externe au SDK — voir §8.

---

## 5. Première saisie

Posez une primitive sur le plateau, puis :

```bash
python3 -m vtctl serve    # interface sur http://127.0.0.1:8090
```

L'interface guide la séquence : pouce en opposition, fermeture, capture,
relâchement. Le pivot du pouce se règle à la main — c'est lui qui décide s'il y a
préhension, et il ne vient pas en opposition sur tous les objets.

**Ce qu'il faut regarder dans le résultat**, et qui ne saute pas aux yeux : le
motif d'arrêt de chaque doigt. « Arrêt sur le tactile » signifie un contact
mesuré ; « arrêt sur le courant » signifie que le doigt a buté sans rien sentir.
Une saisie où tous les doigts s'arrêtent sur le courant est mécaniquement réussie
et **scientifiquement vide**.

---

## 6. La webcam d'angle

Sur notre banc, les carreaux du plateau étaient indéchiffrables depuis la D405.
Une C920 en vue plongeante lève trois compromis d'un coup — cadrage, exposition,
et lumière.

**Elle ne demande aucun éclairage particulier.** Son auto-exposition compense : de
PWM 21 à 255 la luminance reste plate. Ce qui varie n'est pas la clarté mais le
**reflet spéculaire** sur les pastilles, et le meilleur score est obtenu lampe
éteinte. Le chemin de rotation n'a donc plus à toucher à la lampe, ni à attendre
ses 3,5 s d'établissement, deux fois par angle.

Réglages retenus : **1280×720**, autofocus coupé, auto-exposition coupée,
exposition 78. Le champ est identique à celui du 1080p — c'est un
sous-échantillonnage, pas un recadrage.

**Le nœud v4l2 se cherche par nom, jamais par numéro.** `/dev/video0` est la
RealSense dès qu'elle est branchée : elle expose six nœuds, la webcam vient
après. Une numérotation qui dépend de l'ordre de branchement se trompe en
silence. `vtctl.hw.webcam.trouver_peripherique()` fait cette résolution.

### Le décodage ne peut pas être celui d'OpenCV

Nos pastilles sont imprimées **sans cadre noir** : le motif flotte au milieu du
blanc. OpenCV trouve très bien les six quadrilatères, mais les rejette au
**contrôle de bordure**, avant même de regarder la charge utile. Relus à la main
sur les candidats *rejetés*, cinq carreaux sur six se lisaient déjà juste, à 0-2
bits près.

D'où le décodeur de `vtctl/hw/webcam.py` : quadrilatères par OpenCV, grille 4×4
lue directement, bordure ignorée, appariement sur les seuls identifiants du
plateau. Résultat : les six carreaux retrouvés, médiane de 4 par image, là où les
réglages d'origine n'en rendaient aucun.

**Tolérer deux bits faux est sûr**, et c'est démontré : la distance de Hamming
minimale entre les six carreaux vaut 6, rotations comprises, et 8 entre un
carreau et ses propres rotations. C'est la restriction aux six identifiants qui
achète cette marge — sur les cinquante du dictionnaire elle tombe à 4.

> **Si vous réimprimez les carreaux**, mettez un cadre noir dans le motif et des
> coins carrés. Tout ce paragraphe devient alors inutile et le décodeur standard
> suffit.

---

## 7. Acquisition d'un jeu de données

Le protocole enchaîne deux phases aux mêmes angles : **visuelle** (l'objet seul,
vues non occultées) puis **tactile** (l'opérateur amène le pouce au contact, les
autres doigts se referment).

La totalité des trames EtherCAT est conservée pendant la phase tactile, **sans
filtrage**. C'est délibéré : les valeurs décodées dépendent de la table de
découpage, les octets non — et cette table a déjà changé une fois. Une session
enregistrée reste donc réinterprétable après correction d'un défaut de décodage.
Nous avons ainsi récupéré rétroactivement un mois de données du pouce.

### Un danger à connaître avant de lancer

**Le plateau démarre même sans angle connu**, c'est voulu et documenté. Mais si
aucun carreau ne décode, il part à l'aveugle, ne lit jamais d'angle, ne freine
jamais, et tourne le `timeout_angle` entier. Pire, `tourne()` répond « immobile »
faute de mesure, donc `arreter()` renvoie vrai **sans envoyer d'ordre d'arrêt** —
et le protocole enchaîne la saisie pendant que le plateau tourne encore.

> **Ne lancez pas le protocole tant que `vtctl angle` ne rend pas de mesure.**
> Vérifiez l'immobilité autrement : deux images à 4 s d'écart, p99 de la
> différence sous 12 sur la bande basse.

---

## 8. Ce qui reste ouvert

Par honnêteté, et pour que vous ne cherchiez pas ce qui n'existe pas.

**La cinématique n'est pas validée métriquement.** Le facteur counts/radian vient
du SDK et l'étalonnage est circulaire (§4.4). Une saisie de cylindre Ø55 donne un
écartement pouce-doigts de 59,3 mm avec le facteur SDK et 52,9 mm avec
l'hypothèse par défaut : les deux sont plausibles, parce que ces distances
relient des origines de maillons et non des surfaces, et que l'épaisseur du doigt
(~17 mm) dépasse l'écart entre les deux hypothèses.

**Le recalage pulpe ↔ carreau du pouce n'est pas fait.** C'est ce qui manque pour
chaîner main → caméra. Deux voies : le pied à coulisse sur le maillon distal, ou
une série de contacts du pouce sur une plaque plane de géométrie connue. La
seconde a été conçue mais jamais exécutée. Attention : la fenêtre de visibilité
du carreau est étroite — flexion ≥ 3000 et pivot ≥ 4000 — ce qui plafonne le
balayage à 9,6° et rend le facteur d'échelle non identifiable. **Déplacer le
carreau, ou en coller un second sur une autre face**, est le changement physique
au meilleur rapport effort/résultat.

**Les alarmes de la main ne sont pas remontées.** `Backend._alarms` est
initialisé vide et jamais réassigné : le champ `alarms` de l'API est mort. Le SDK
expose pourtant `get_now_alarm` et `get_now_status`, et surtout **le bit de
défaut est déjà dans la trame** — 27 octets d'état à l'offset 164, que
`decode_motor_frame` extrait sans que personne les lise. Une saisie que nous
avions annoncée réussie avait trois moteurs en défaut, dont un pendant 3 s.
C'est gratuit à corriger et ça rend toutes les mesures suivantes fiables.

**`ouvrir_main` échoue souvent** — « ouverture incomplète après 8 s », doigts
laissés vers 490 counts. Ça se résorbe seul, mais il faudrait qu'elle insiste et
vérifie plutôt que de rendre la main sur un échec.

---

## 9. Catalogue des pannes

| Symptôme | Cause probable | Geste |
|:--|:--|:--|
| `aucun esclave EtherCAT trouvé`, porteuse absente | main hors tension, ou interface éteinte | `sudo ip link set enx… up` ; puis l'alimentation |
| Esclave vu mais `Invalid mailbox configuration`, `Sync manager watchdog`, `PacketError` | état résiduel du variateur | **coupure franche** de l'alimentation main, plusieurs secondes |
| Consigne acceptée, cible relue, **rien ne bouge**, aucune alarme | le variateur a cessé d'exécuter | reprendre le bus à zéro (`reconnecter_main`) |
| `move_motors` sans effet une fois sur deux | défaut connu du variateur | passer par `aller_a`, qui réémet. **Jamais sur un doigt déjà bloqué** |
| Doigt immobile à courant faible | il est peut-être **sous son zéro** | lire la trame brute : la position est un u16 **signé**, le SDK écrête à 0 |
| Angle plateau capricieux, saute d'un cran | table des angles fausse | §4.2 |
| Plateau qui tourne sans qu'on le sache | `COMMANDE_START_PAUSE` est une **bascule** | ne renvoyer la bascule que si un mouvement est confirmé |
| Aucun marqueur décodé, image pourtant belle | l'image bien exposée est la **pire** pour les marqueurs | raccourcir l'exposition, pas l'allonger |
| `Frame didn't arrive within 5000` | contention USB (virtualisation) | reconnecter la caméra ; sur Linux natif, ne devrait pas survenir |
| Le tactile ne répond nulle part | la main n'émet rien avant `set_enable` **puis** `home_motors` | homer **moteur par moteur** — `home_motors(0)` met en ALARM |

---

## 10. Ce qui nous a coûté le plus cher

Si vous ne lisez qu'un paragraphe, que ce soit celui-ci.

**Les pannes les plus coûteuses n'ont jamais été des pannes.** Elles ont été des
mesures fausses que rien ne signalait : une table d'angles décalée d'un cran, un
décodage de trame lu 36 octets trop loin qui a fait passer un moteur sain pour
mort pendant un mois, un getter du SDK qui écrête les négatifs à zéro et
transforme un doigt qui bouge en doigt bloqué, une lampe qu'on interrogeait avant
qu'elle ne soit stable.

Aucune ne levait d'exception. Toutes rendaient des chiffres plausibles.

D'où la règle qui vaut pour tout ce banc : **confronter deux sources
indépendantes avant de conclure**. La trame brute contre le SDK. Le carreau
proche contre le carreau lointain. L'objet de dimensions connues contre le modèle.
C'est ce qui a tranché chaque fois, en quelques minutes, ce que des jours de
raisonnement sur une seule source n'avaient pas résolu.
