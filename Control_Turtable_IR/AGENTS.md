# Guide Développeur & LLM : Asservissement du Plateau Tournant IR / ArUco

Ce document est destiné aux développeurs et aux assistants IA (LLMs) prenant la suite du projet. Il décrit l'architecture matérielle, le protocole série IR, le pipeline d'estimation de pose par la vision, et l'API Python d'asservissement en position.

---

## 1. Architecture Matérielle & Spécifications

* **Vitesse de liaison série (UART)** : `115200 bauds` (Ne pas modifier à 9600 !).
* **Port série par défaut (macOS)** : `/dev/cu.usbmodem21401` (ou `/dev/ttyUSB0` sous Linux).
* **Moteur & Télécommande IR** :
  * Le plateau tournant est piloté par des trames infrarouges (protocole **NEC**).
  * Trame d'interception (récepteur) : `RCV NEC <addr> <cmd> <repeat_flag> <state>`
  * Trame d'émission (émetteur UART) : `SEND NEC <addr> <cmd> <repeat_flag> <state>`
  * **ATTENTION** : La commande `COMMANDE_START_PAUSE` (`0x19`) agit comme un **bouton à bascule (Toggle Play/Pause)**. Ne l'envoyer que si le plateau est réellement en mouvement (`STATE_MOVING_CW` / `STATE_MOVING_CCW`), sinon cela relancera la rotation !

---

## 2. Pipeline de Vision ArUco (`aruco_tracker.py`)

### Dispositions des Marqueurs ArUco (`DICT_4X4_50`)
Les marqueurs sont disposés tous les $60^\circ$ au bord du plateau :
- **ID 1** : $0.0^\circ$ (Référence)
- **ID 2** : $60.0^\circ$
- **ID 3** : $120.0^\circ$
- **ID 4** : $180.0^\circ$
- **ID 5** : $240.0^\circ$
- **ID 6** : $300.0^\circ$

### Pipeline de filtrage en 5 niveaux (Stabilité sub-degré)
Pour éliminer les bruits de numérisation, les reflets et les déformations perspectives (caméra inclinée) :
1. **Sub-Pixel Refinement** : `CORNER_REFINE_SUBPIX` sur les coins ArUco.
2. **Pondération par Périmètre** : Chaque marqueur $i$ contribue proportionnellement à son périmètre $w_i = \text{cv2.arcLength}$.
3. **Outlier Rejection** : Élimination des marqueurs déviant de plus de $5^\circ$ par rapport à la médiane instantanée.
4. **Filtre Médian Glissant** : Historique `deque(maxlen=5)` pour éliminer les sauts isolés d'un frame.
5. **Lissage EMA Circulaire** : `smooth_angle` amorti ($\alpha = 0.25$).

---

## 3. Machine d'État de Contrôle en Position (`turntable_position_controller.py`)

L'asservissement utilise une stratégie **2-Étapes (Grand Déplacement + Micro-Ajustement Pas-à-Pas)** :

### Étape 1 : Vitesse Minimale & Freinage Anticipé (Predictive Stopping)
- Au démarrage d'une consigne, la trame `COMMANDE_VITESSE_MOINS` est envoyée **3 fois** pour forcer le plateau à sa vitesse minimale ($\sim 12^\circ/\text{s}$).
- La vitesse angulaire instantanée $\omega$ (deg/s) est mesurée en continu.
- L'ordre de freinage `START_PAUSE` est envoyé de manière **anticipée** lorsque :
  $$\text{Écart} \le \max(\text{tolérance}, \omega \times \tau_{\text{latence}})$$
  où $\tau_{\text{latence}} \approx 0.12\text{s}$ (latence IR + inertie du plateau).

### Étape 2 : Micro-Ajustement Pas-à-Pas (Fine Nudge)
- Après l'envoi du `STOP`, le contrôleur entre en état `STATE_COASTING` pendant 500 ms (le temps que le moteur s'immobilise complètement).
- La position réelle au repos est mesurée.
- Si l'écart final reste supérieur à la tolérance (ex: $> 1.0^\circ$), une **micro-impulsion de 120 ms** ($\sim 0.7^\circ$) est envoyée dans la direction manquante.
- **Plafonnement** : Maximum **2 micro-impulsions** par consigne pour garantir l'absence totale d'oscillation.

---

## 4. Guide d'Utilisation dans un projet Python (TÂCHES DE PRICING / PALPAGE / SCAN 3D)

### Exemple d'intégration automatisée :

```python
from turntable_position_controller import TurntablePositionController

# 1. Initialisation (Mode réel sur port série ou Simulation=True)
controller = TurntablePositionController(
    port="/dev/cu.usbmodem21401",  # ou None si simulation
    tolerance_deg=1.0,
    simulation=False
)

# 2. Déplacement bloquant vers l'angle 60°
controller.set_target_angle(60.0)
success = controller.wait_until_reached(timeout=15.0)

if success:
    print(f"Plateau stabilisé à {controller.last_angle:.1f}°")
    # ---> VOS TRAITEMENTS ICI : Prise de photo, numérisation 3D, palpage <---

# 3. Déplacement vers le Marqueur ID 4 (180°)
controller.set_target_marker(4)
controller.wait_until_reached(timeout=15.0)

# 4. Fermeture propre (Ne relance pas le moteur)
controller.close()
```

---

## 5. Fichiers et Scripts Utiles

* `aruco_control.py` : Script d'exécution interactif (`python aruco_control.py -p /dev/cu.usbmodem21401 -t 1.0`).
* `calibrate_aruco.py` : Script de ré-estimation du centre de rotation et des angles relatifs des marqueurs.
* `generate_aruco_markers.py` : Script de génération des fichiers PNG ArUco pour impression.
* `aruco_config.json` : Fichier de configuration contenant le centre et les angles calibrés.
* `config_telecommande.json` : Base de données des 14 trames IR du plateau tournant.
