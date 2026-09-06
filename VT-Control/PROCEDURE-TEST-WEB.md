# Procédure de test de l'interface VT-Control

À donner à un agent qui navigue sur la page. Chaque étape dit **quoi faire**,
**ce qu'on doit voir**, et **ce qui compte comme échec**. Un écart entre
l'attendu et l'observé se rapporte tel quel : ne pas essayer de le corriger, ne
pas relancer pour voir si ça passe la deuxième fois.

## Avant de commencer

Lancer le banc **simulé** — aucun matériel n'est touché, et tous les défauts
mesurés du banc réel sont rejoués :

```bash
cd ~/VT-Control
python3 -m vtctl --simulation --rapide --sans-verrous --out /tmp/vt-essai \
        serve --port 8099
```

Ouvrir <http://127.0.0.1:8099>. Depuis un autre poste :
`ssh -N -L 8099:127.0.0.1:8099 openclaw-vm`.

> Le mode simulation est **le mode de test**. La consigne « tester sans le
> matériel » n'est pas une dégradation : le simulateur fait passer ses trames
> par le décodeur tactile réel, son image par le détecteur ArUco réel, et ses
> commandes moteur par le même `Backend` que le banc.

---

## 1. La page se charge et le flux vit

| Faire | Attendu |
|:--|:--|
| Ouvrir la page | Titre « VT-Control — acquisition visuo-tactile » |
| Regarder les pastilles en haut à droite | « flux », « caméra », « main » **vertes**. Le badge « simulation » est visible. |
| Lire le badge de droite | `serie · camera · ethercat` — les trois ressources sont tenues |
| Attendre 5 s | L'heure et les valeurs bougent : le flux SSE est vivant |

**Échec** : une pastille rouge, un badge de ressource vide, ou une page figée.

---

## 2. Les images s'affichent

| Faire | Attendu |
|:--|:--|
| Regarder le panneau « Images » | Deux vignettes : couleur (scène grise, un cube sur un plateau) et profondeur (fausses couleurs) |
| Attendre 3 s | L'horodatage `t = … s` en haut à droite du panneau **augmente** |

**Échec** : une vignette noire ou cassée, ou un horodatage qui ne bouge pas.

**À ne pas prendre pour un défaut** : l'image de profondeur est bruitée et
irrégulière. C'est une stéréo passive : c'est normal.

---

## 3. La lampe et le plateau répondent

| Faire | Attendu |
|:--|:--|
| Déplacer le curseur « lampe » à ~200, relâcher | L'image couleur **s'éclaircit** en quelques secondes |
| Le remettre à ~120 | L'image s'assombrit |
| Cliquer « Mesurer l'angle » | Un message apparaît en bas, et le grand nombre du panneau « Plateau tournant » affiche un angle en degrés |
| Lire la ligne sous l'angle | Elle nomme les carreaux vus, ex. `carreaux 1, 4` |

**Échec** : l'angle reste `—` **et** la ligne ne dit pas pourquoi.

**À ne pas prendre pour un défaut** : un angle `—` accompagné de « aucun
marqueur décodé ». C'est un résultat légitime — la moitié des images seulement
rend un marqueur, et un angle inventé serait pire qu'un angle absent.

---

## 4. Le plateau se positionne, et s'arrête

| Faire | Attendu |
|:--|:--|
| Saisir `90` dans « aller à », cliquer « Positionner » | Après 20 à 40 s, un message donne la consigne, la mesure et l'écart |
| Vérifier l'écart annoncé | Inférieur à ~5° |
| Cliquer « Arrêter le plateau » | Message « Plateau arrêté. » |
| Cliquer « Arrêter le plateau » **une seconde fois** | Message « Plateau arrêté. » de nouveau — **et l'image ne doit pas se mettre à défiler** |

**Échec de la dernière ligne** : si le second clic met le plateau en rotation,
c'est le défaut le plus grave possible ici. `START_PAUSE` est une bascule, et
une de trop relance le plateau ; un plateau qui tourne sans qu'on le sache
invalide silencieusement toutes les captures suivantes.

---

## 5. La main répond, et le tactile aussi

| Faire | Attendu |
|:--|:--|
| Regarder le panneau « Capteurs tactiles » | Un schéma de main, neuf barres nommées, un âge de trame en haut à droite (quelques dizaines de ms) |
| Regarder les valeurs **sans rien toucher** | Toutes proches de **0,000**. Le zéro est pris automatiquement au réveil de la main. |
| Cliquer « Zéro tactile » | Message « zéro fait sur N trames », N de l'ordre de la trentaine par seconde demandée |
| Panneau « Main » : bouger le curseur du **Majeur** vers ~5000, relâcher | La ligne « pos » du majeur monte progressivement vers la consigne |
| Pendant la montée, regarder les barres tactiles | « Majeur Tip » monte quand le doigt rencontre l'objet |
| Cliquer « Ouvrir la main » | Toutes les positions retournent vers 0 |

**Échec** : aucune zone tactile ne réagit alors qu'un doigt s'est arrêté à
courant élevé. **Échec aussi** si les neuf zones affichent 0,3 à 0,7 au repos —
cela veut dire que le zéro n'a pas été pris, et les valeurs sont alors les lignes
de base brutes, pas des pressions.

**À ne pas prendre pour un défaut, ce sont des pannes connues et voulues :**

- le curseur **Pouce (flexion)** est grisé et marqué « en panne ». C'est le
- la barre **Annulaire Pad** reste à 0,000 quoi qu'il arrive. Cette zone est
  morte, mesuré ;
- la position affichée **ne bouge pas pendant** le déplacement, puis saute à
  l'arrivée. La télémétrie n'est pas interrogée pendant le mouvement : les
  lectures du SDK annulent la trame de commande ;
- l'**auriculaire** peut aller jusqu'à sa butée sans rien toucher. L'objet
  n'est pas sur son trajet.

---

## 6. Une session complète — le cœur du test

Configurer en haut de la page : objet `essai_agent`, angles `2`, pas `180`,
phases **tactile seule**, case « exiger le pouce stable » **cochée**. Cliquer
**Démarrer la session**.

> L'ordre par défaut est **tactile puis visuel**. Si vous choisissez les deux
> phases, la saisie passe en premier.

### 6a. Le bandeau d'état suit le protocole

L'état en gros caractères doit traverser, dans cet ordre :

```
PRETE → POSITIONNEMENT → STABILISATION → MAIN_AU_DEPART
      → POUCE_EN_ATTENTE → ARME → FERMETURE → SAISIE
      → CAPTURE_TACTILE → RELACHE → VALIDATION → PRETE
```

Il n'est pas nécessaire de tous les voir — certains durent moins d'une seconde.
Ce qui compte : **l'ordre n'est jamais inversé**, et l'état ne revient jamais en
arrière sauf par `RECUPERATION`.

Pendant le positionnement, les champs de configuration sont **grisés** et
« Démarrer » est désactivé.

### 6b. Le refus qui compte le plus

Quand l'état passe à **POUCE_EN_ATTENTE**, un encadré orange apparaît.

| Faire | Attendu |
|:--|:--|
| Cliquer « Contourner le critère… » | Une invite demande un motif |
| **Laisser vide** et valider | Message d'erreur « motif obligatoire ». **Rien ne se ferme.** |
| Recliquer « Contourner le critère… », saisir `le pouce n'atteint pas la face avant` | La fermeture part : l'état passe à ARME puis FERMETURE |

**Échec** : si un contournement **sans motif** laisse la fermeture partir. C'est
la seule chose que ce protocole existe pour empêcher. Contourner est légitime —
le pivot du pouce n'amène pas le pouce en opposition sur tous les objets — mais
contourner *en silence* ne l'est pas.

### 6c. La validation ne fait pas avancer

Quand l'état passe à **VALIDATION**, un encadré bleu apparaît avec quatre
boutons répartis sur deux lignes : *valider et…*, *invalider et…*.

| Faire | Attendu |
|:--|:--|
| Cliquer **« Invalider et refaire cet angle »** | Le protocole **reste sur le même angle** et recommence : le compteur en haut passe à « essai 2 » |
| Au second passage, cliquer **« Valider et passer à l'angle suivant »** | Là seulement le protocole avance |

**Échec** : si « refaire cet angle » passe quand même au suivant. C'est le point
de ce mécanisme : une prise ratée se refait, on n'avance que sur ordre.

Vérifier sur le disque que **les deux essais coexistent** :
`tactile/angle_00/essai_00/` et `essai_01/`, chacun avec son `capture.json`.
Le premier porte `"validation": "invalide"`, le second `"valide"`.

### 6d. Le carreau du pouce

Dans l'encadré orange de placement du pouce, une pastille dit si le carreau
ArUco n° 7 est vu. Cliquer **« Le chercher »** force une recherche immédiate.

C'est une **information**, pas une condition : la fermeture part que le carreau
soit vu ou non. **Échec** seulement si la pastille reste grise en permanence,
sans jamais dire ni « vu » ni « hors champ ».

### 6e. L'opposition du pouce

Toujours dans l'encadré orange : un curseur *opposition* et un bouton
*Appliquer*. Le déplacer et appliquer doit faire bouger le pivot du pouce, et le
message de retour donne la nouvelle position **et** la pression du pouce.

Il démarre à mi-course (3000 counts) au début de chaque capture.

### 6f. Sauter un angle

Au **POUCE_EN_ATTENTE** d'un angle, cliquer **« Sauter cet angle »**.

Attendu : le protocole passe à l'angle suivant, et la pastille de l'angle sauté
est **rouge**.

**Échec** : si sauter un angle arrête toute la session. Un angle perdu n'en perd
pas d'autres.

---

## 6bis. Les onglets d'images

Le panneau d'images porte trois onglets.

| Onglet | Attendu |
|:--|:--|
| **En direct** | Couleur et profondeur, horodatage qui avance |
| **Capture en cours** | Après une saisie : une carte avec l'étape, l'angle mesuré, le statut du pouce, les contacts, et les vignettes des images écrites |
| **Jeu de données** | Une liste déroulante des sessions ; « Ouvrir » affiche toutes les captures de la session choisie avec leurs vignettes |

Cliquer une vignette doit l'ouvrir en grand dans un nouvel onglet.

**Échec** : des vignettes cassées, ou l'onglet « Jeu de données » vide alors que
des sessions existent sur le disque.

---

## 7. Ce qui a été écrit

| Faire | Attendu |
|:--|:--|
| Panneau « Sessions », cliquer « Rafraîchir » | La session `…_essai_agent` apparaît en tête, avec un nombre d'images et « manifeste » en vert |

Vérifier ensuite sur le disque (`/tmp/vt-essai/…_essai_agent/`) :

- `session.json` contient un bloc `horloge` avec `t0_unix` **et** `t0_iso` ;
- `tactile/angle_00/pouce.json` porte `"statut": "contourne"` **et** le motif
  qui a été saisi ;
- `tactile/angle_00/angle.json` porte un `mesure_deg` (ou `null`) et un
  `images_exploitables` ;
- `tactile/angle_00/capture.json` porte `"validation": "valide"` et le
  commentaire ;
- `brut/stream_00_data.npy` existe et fait plusieurs centaines de kilooctets.

**Échec** : `manifest.json` absent après une session terminée normalement, ou
`pouce.json` sans motif alors qu'un contournement a eu lieu.

---

## 8. La reprise après plantage

| Faire | Attendu |
|:--|:--|
| Démarrer une nouvelle session (angles `2`, phases **visuelle**) | Elle démarre |
| Pendant qu'elle tourne, **tuer le serveur** (`Ctrl-C` deux fois, ou `kill -9`) | Le processus meurt |
| Relancer le serveur, cliquer « Rafraîchir » dans « Sessions » | La session interrompue apparaît **sans** manifeste, avec un bouton « récupérer » |
| Cliquer « récupérer » | Message « manifeste reconstruit : N images », et la ligne passe à « manifeste » |

**Échec** : si la session interrompue est illisible, ou si « récupérer » ne
produit rien. C'est le défaut que ce dépôt corrige : sur les 39 sessions de
l'outil précédent, deux n'ont pas d'index et 218 Mo de données sont orphelines.

---

## 9. L'exclusivité des ressources

| Faire | Attendu |
|:--|:--|
| Le serveur tournant, ouvrir un second terminal et lancer `python3 -m vtctl --simulation selftest` | Il **refuse** de démarrer, avec un message nommant la ressource et son détenteur |

**Échec** : si le second processus démarre quand même. Une seule instance
possède chaque ressource matérielle ; deux outils qui se marchent dessus, c'est
le problème que ce dépôt existe pour supprimer.

> En simulation lancée avec `--sans-verrous`, ce contrôle est désactivé
> exprès pour permettre les tests parallèles : refaire l'essai **sans** cette
> option.

---

## 10. Vérifier le banc, sans acquisition

| Faire | Attendu |
|:--|:--|
| Cliquer « Vérifier le banc » | Message « vérification en cours », puis après ~20 s « banc : tout répond » |
| Lire le journal en bas de page | Une ligne par contrôle : port série, lampe, caméra, ArUco, plateau, EtherCAT |

---

## Ce qu'il faut rapporter

Pour chaque étape : **fait / pas fait**, et pour tout écart, la citation exacte
du message affiché. Les captures d'écran des encadrés de décision (§6b, §6c)
sont utiles.

Deux points méritent une mention explicite dans le compte rendu, quel que soit
le résultat :

1. **le contournement sans motif a-t-il été refusé** (§6b) ;
2. **le second clic sur « Arrêter le plateau » a-t-il relancé le plateau** (§4).

Ce sont les deux défauts qui, s'ils passaient, corrompraient le jeu de données
sans que rien ne le signale.
