# Bibliothèque de Contrôle du Plateau Tournant 3-en-1 (IR / Série) & Asservissement ArUco

Cette bibliothèque permet de piloter et d'intercepter les commandes d'un plateau tournant d'exposition ("3 in 1 Rotating Display Stand") via une liaison série UART et d'effectuer un **asservissement en position fermée par vision 2D/3D** (avec la caméra **Intel RealSense D405** et des marqueurs **ArUco**).

---

## Installation & Dépendances

Installer les dépendances requises :

```bash
pip install pyserial opencv-python numpy
```

*(Optionnel pour RealSense D405 physique)* :
```bash
pip install pyrealsense2
```

---

## Structure du Projet

- [turntable.py](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/turntable.py) : La bibliothèque principale contenant la classe de contrôle IR/série `TurntableController`.
- [aruco_tracker.py](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/aruco_tracker.py) : Module de capture vidéo RealSense D405 / Webcam / Simulation et d'estimation d'angle absolu par marqueurs ArUco.
- [turntable_position_controller.py](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/turntable_position_controller.py) : Contrôleur en position à boucle fermée asservissant la table vers un angle ou un marqueur.
- [aruco_control.py](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/aruco_control.py) : Script d'exécution interactif principal avec affichage vidéo temps réel.
- [generate_aruco_markers.py](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/generate_aruco_markers.py) : Générateur d'images PNG imprimables des marqueurs ArUco (IDs 1 à 6).
- [calibrate.py](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/calibrate.py) : Script d'apprentissage des touches IR de la télécommande physique.
- [example.py](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/example.py) : Exemple d'utilisation simple des commandes IR.

---

## Contrôle en Position ArUco (RealSense D405)

### Dispositions des Marqueurs ArUco (`DICT_4X4_50`)
Les marqueurs sont placés tous les **60°** au bord du plateau tournant :
- **ID 1** : Référence $0^\circ$ (ou $360^\circ$)
- **ID 2** : $60^\circ$
- **ID 3** : $120^\circ$
- **ID 4** : $180^\circ$
- **ID 5** : $240^\circ$
- **ID 6** : $300^\circ$

### 1. Génération des marqueurs à imprimer
Pour générer les marqueurs ArUco au format PNG prêts à imprimer :
```bash
python3 generate_aruco_markers.py
```
Les fichiers sont créés dans le dossier `aruco_markers/`.

### 2. Lancement du contrôle interactif

- **Avec la caméra RealSense D405 et le port série du plateau** :
  ```bash
  python3 aruco_control.py --port /dev/ttyUSB0
  ```

- **Mode Simulation synthétique (sans matériel)** :
  ```bash
  python3 aruco_control.py --simulation
  ```

### Commandes Clavier dans l'application :
- **`1` à `6`** : Envoyer la table vers le marqueur ArUco ID 1 à 6 (ex: `1` $\rightarrow 0^\circ$, `3` $\rightarrow 120^\circ$).
- **`a`** : Saisir un angle personnalisé en degrés dans la console (ex: `90.0`, `270.0`).
- **`ESPACE` / `s`** : Arrêter le mouvement.
- **`c`** : Calibrer le centre de la table par clic sur l'image vidéo.
- **`q` / `ESC`** : Quitter.

---

## Documentation pour les Développeurs & LLM

Une documentation technique détaillée décrivant les spécifications matérielles, le protocole série IR, le pipeline de filtrage 5-niveaux et la machine d'état à 2-étapes est disponible dans [AGENTS.md](file:///Users/kojack/Documents/ISIR-Stage/Repo/Control_Turtable_IR/AGENTS.md).

---

## Utilisation dans votre code Python (API Reutilisable)

```python
from turntable_position_controller import TurntablePositionController

# Initialiser le contrôleur de position (mode réel ou simulation)
controller = TurntablePositionController(port="/dev/cu.usbmodem21401", tolerance_deg=1.0)

# Déplacement bloquant vers un angle ou un marqueur (ex: Marqueur ID 3 -> 120°)
controller.set_target_marker(3) # ou controller.set_target_angle(120.0)
success = controller.wait_until_reached(timeout=15.0)

if success:
    print("Plateau stabilisé et prêt pour la prise de vue ou le palpage !")

# Fermeture propre (libère les ports sans relancer la rotation)
controller.close()
```

