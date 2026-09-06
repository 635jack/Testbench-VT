# Testbench-VT — banc d'acquisition visuo-tactile

Un banc qui enregistre, pour un même objet et au même instant, **ce que voit une
caméra de profondeur** et **ce que sent une main robotique** en le saisissant.
Il produit des paires image–toucher exploitables pour la reconstruction 3D.

Construit à l'ISIR (Sorbonne Université / CNRS, équipe ASIMOV) pendant un stage
de M2, avec une contrainte de bout en bout : **du matériel bon marché**.

---

## Le matériel

| Élément | Modèle | Rôle |
|:--|:--|:--|
| Main | Leadshine **DH116**, gauche | 6 moteurs, 11 articulations, 11 zones tactiles |
| Caméra de profondeur | Intel RealSense **D405** | images couleur + profondeur de la saisie |
| Caméra d'angle | Logitech **C920** | mesure l'angle du plateau, par marqueurs ArUco |
| Plateau | tourne-disque piloté en infrarouge | présente l'objet sous plusieurs angles |
| Éclairage | bandeau LED + variateur sur ESP32 | éclairage maîtrisé, profil photométrique mesuré |
| Liaison main | adaptateur USB↔Ethernet | EtherCAT, sockets brutes, **root requis** |

La main n'est **pas rétro-entraînable** : une main fermée le reste jusqu'à ce
qu'on lui dise de s'ouvrir. Tout ce qui la referme dans ce dépôt est borné dans
le temps et rouvre d'office.

---

## Les modules

    Testbench-VT/
    ├── VT-Control/            vtctl : interface web, protocole, cinématique
    ├── VT-Tactile/            bus EtherCAT, décodage des trames, enveloppement
    ├── VT-Bench/              essais de bout en bout
    ├── VT-Light/              profil photométrique, étalonnage caméra
    ├── Control_Turtable_IR/   plateau et détection ArUco
    └── banc.sh                orchestration (machine virtuelle, macOS)

**La disposition compte.** `vtctl` retrouve ses voisins par chemin relatif
(`vtctl.config.install_paths`) : les dossiers doivent rester frères. Un seul
clone suffit, ne les déplacez pas.

---

## Installation

### Linux, en natif — recommandé

C'est le chemin le plus simple et le plus fiable. Tout tourne directement, sans
redirection USB ni couche intermédiaire.

```bash
git clone https://github.com/635jack/Testbench-VT.git
cd Testbench-VT
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

### Le SDK de la main — à obtenir séparément

**Il n'est pas dans ce dépôt** : c'est un logiciel du constructeur, livré avec la
main, et sa redistribution ne nous appartient pas. Sans lui, tout fonctionne sauf
le pilotage de la main.

Il est fourni compilé pour **i386, x86_64 et aarch64** — Intel comme ARM
conviennent. Placez-le à côté des modules et pointez-y `PYTHONPATH` :

```bash
export PYTHONPATH="$PWD/Leadshine_SDK_original/sdk_lib/x86_64/share/LHandProLib/examples/EtherCAT_python:$PYTHONPATH"
```

`vtctl doctor` vous dira s'il est trouvé.

L'URDF de la main, lui, **est** embarqué dans `VT-Control/vtctl/data/` : une copie
plutôt qu'un chemin, pour que le modèle reste figé avec le code qui l'utilise et
qu'une géométrie modifiée ne réécrive pas les sessions passées.

Le maître EtherCAT ouvre des sockets brutes, donc il faut les privilèges :

```bash
sudo setcap cap_net_raw,cap_net_admin+eip "$(readlink -f .venv/bin/python3)"
```

à défaut de quoi il faut lancer sous `sudo`.

### macOS ou Windows — par machine virtuelle

`pyrealsense2` n'a pas de roue macOS arm64 et les sockets brutes veulent Linux :
le banc tourne alors dans une VM Linux, le matériel USB lui étant redirigé.
`banc.sh` orchestre ce cas. C'est **nettement moins confortable** — redirection
USB capricieuse, plafond de périphériques partagés, gigue sur le lien temps
réel. À ne choisir que si l'hôte ne peut pas être un Linux.

---

## Premiers pas

```bash
python3 -m vtctl doctor      # l'environnement est-il complet ?
python3 -m vtctl selftest    # le banc répond-il ? aucune acquisition
python3 -m vtctl serve       # interface web sur http://127.0.0.1:8090
```

`doctor` dit ce qui manque et comment y remédier ; `selftest` exerce chaque
pièce — port série, lampe, caméra, marqueurs, plateau, trames EtherCAT,
mouvement d'un doigt — sans rien enregistrer.

---

## À refaire sur votre exemplaire

Rien de ce qui suit ne se transporte d'un banc à l'autre. Ce sont des mesures,
pas des constantes, et les employer telles quelles donne des résultats faux
**sans que rien ne le signale** :

- **centre du plateau, en pixels** — il ne vaut que pour la résolution *et* la
  pose de caméra où il a été estimé ; quelques centimètres de déplacement le
  décalent de dizaines de pixels ;
- **angles des carreaux ArUco** — ils ne sont pas régulièrement répartis, et une
  table fausse fait sauter l'angle annoncé selon le carreau visible ;
- **profil photométrique** — réponse du variateur et de la caméra ;
- **facteur counts/radian** de la main.

---

## Licence et citation

Le SDK Leadshine appartient à son constructeur et **n'est pas redistribué ici**.
L'URDF de la main est embarqué sous `VT-Control/vtctl/data/`, tel que fourni.
Le reste est publié pour accompagner un mémoire de master ; si ce banc vous sert,
une mention fait plaisir.
