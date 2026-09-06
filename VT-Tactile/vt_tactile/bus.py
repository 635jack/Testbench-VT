#!/usr/bin/env python3
"""
bus.py — mise en route de la main, réduite au strict nécessaire.

Le SDK constructeur ne sert qu'à trois choses : monter EtherCAT, réveiller la
main, et basculer ses trames en mode capteur. Le contenu des trames est décodé
par :mod:`vt_tactile.tpdo`, hors du SDK.

Quatre pièges, tous constatés au banc, tous traités ici :

1. **La main n'émet aucune télémétrie tant qu'elle n'a pas été alimentée ET
   homée.** Les trames circulent, en-têtes corrects, charge utile nulle. C'est
   le piège numéro un : on croit à une panne d'alimentation.
2. ``PyLHandProLib`` n'expose pas ``set_tpdo_frame_type`` alors que le symbole C
   existe. On le lie par ctypes.
3. Le chargeur Leadshine déclare des symboles absents de la ``.so`` Linux et
   s'effondre à la construction. On tolère leur absence.
4. Les modules Python du SDK ne sont livrés que sous ``x86_64``, alors que la
   ``.so`` doit correspondre à l'architecture. Sur une VM aarch64 il faut
   piocher dans les deux arborescences.

À exécuter dans la VM, en root : le maître EtherCAT ouvre des sockets raw.
"""
from __future__ import annotations

import ctypes
import logging
import platform
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import hardware as hw

log = logging.getLogger(__name__)

LCN_ECAT = 0
LCM_POSITION = 0
FRAME_TACTILE = 0x40
FRAME_MOTOR = 0x00


class BusError(RuntimeError):
    """La main n'est pas exploitable."""


class _MissingSymbol:
    """Bouchon : accepte restype/argtypes, ne proteste qu'à l'appel."""

    def __init__(self, name: str):
        self._name = name

    def __call__(self, *a, **k):
        raise BusError(f"{self._name} est absent de cette bibliothèque")


class _TolerantCDLL(ctypes.CDLL):
    """CDLL qui consigne les symboles manquants au lieu de s'effondrer."""

    missing: set[str] = set()

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            _TolerantCDLL.missing.add(name)
            return _MissingSymbol(name)


def find_sdk(explicit: str | None = None) -> tuple[str, str]:
    """Localise ``(dossier_python, chemin_so)``, cherchés séparément."""
    arch = "aarch64" if platform.machine() in ("aarch64", "arm64") else "x86_64"
    roots = ([Path(explicit)] if explicit else []) + [
        Path.home() / "Leadshine_SDK_original/sdk_lib",
        Path.home() / "DH116-ISIR/external/DH116_LHandProLib-API-Linux-20251128",
        Path(__file__).resolve().parents[2] / "Leadshine_SDK_original/sdk_lib",
    ]
    so = next((p for r in roots
               if (p := r / arch / "lib/libLHandProLib.so").is_file()), None)
    py = next((p for r in roots for a in (arch, "x86_64", "aarch64", "i386")
               if (p := r / a / "share/LHandProLib/examples/EtherCAT_python").is_dir()
               and (p / "lhandprolib_wrapper.py").is_file()), None)
    if so is None or py is None:
        raise BusError("SDK introuvable. Cherché dans : "
                       + ", ".join(str(r) for r in roots))
    return str(py), str(so)


class Frame(NamedTuple):
    """
    Une trame telle qu'elle est sortie du bus, avec l'instant de sa lecture.

    ``t`` est un ``time.perf_counter`` : c'est l'horloge que
    :meth:`vt_tactile.dataset.Session.rel` ramene sur celle de la session, et
    c'est ce qui permet de recoller une image sur un etat tactile.
    """

    t: float
    data: bytes


#: Plafond du tampon d'enregistrement, en trames. A ~1000 lectures/s et
#: 192 octets, 400 000 trames font 77 Mo et une heure de banc : au-dela on
#: cesse d'accumuler plutot que de remplir la memoire de la VM en silence.
RECORD_MAX_FRAMES = 400_000


class Hand:
    """
    Connexion à la main, orientée lecture de trames.

        with Hand() as hand:
            for raw in hand.frames():
                ...
    """

    def __init__(self, sdk_dir: str | None = None, iface_hint: str = "enx"):
        self._sdk_dir = sdk_dir
        self._iface_hint = iface_hint
        self._lhp = None
        self._master = None
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._latest_tactile: bytes | None = None
        self._latest_motor: bytes | None = None
        self._recording = False
        self._record: list[Frame] = []
        self._record_lock = threading.Lock()
        self._record_dropped = 0
        self.interface = ""
        self.dof: tuple[int, int] = (0, 0)

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def connect(self, iface_index: int | None = None) -> None:
        py_dir, so = find_sdk(self._sdk_dir)
        if py_dir not in sys.path:
            sys.path.insert(0, py_dir)
        try:
            import lhandprolib_loader as loader            # noqa: PLC0415
            from lhandprolib_wrapper import PyLHandProLib  # noqa: PLC0415
            from ethercat_master import EthercatMaster     # noqa: PLC0415
        except ImportError as e:
            raise BusError(f"import du SDK impossible : {e}") from e

        def _load_library(inner, lib_path=None):
            inner._lib = _TolerantCDLL(str(lib_path or inner._find_library()))

        loader.LHandProLibLoader._load_library = _load_library
        loader._global_lhandpro_lib = None

        self._lhp = PyLHandProLib(lib_path=so)
        self._master = EthercatMaster()

        ifaces = self._master.scanNetworkInterfaces()
        if not ifaces:
            raise BusError("aucune interface réseau visible")
        names = [n.decode() if isinstance(n, bytes) else str(n) for n in ifaces]
        def live(name: str) -> bool:
            base = Path(f"/sys/class/net/{name}")
            return (base / "carrier").exists() and \
                (base / "carrier").read_text().strip() == "1"

        if iface_index is None:
            # On ne devine pas l'interface : on la **trouve**, en essayant
            # l'initialisation jusqu'à tomber sur celle qui voit un esclave.
            #
            # Se fier au nom ne suffit pas. L'indice d'une interface bouge
            # quand Docker crée ou détruit un `veth`, et un changement
            # d'adaptateur change le suffixe du nom : on a vu le maître se
            # monter sur `vethe03b61c` et échouer sans que rien n'explique
            # pourquoi. L'ordre d'essai reste raisonné — lien établi d'abord,
            # nom plausible ensuite — mais c'est la présence d'un esclave qui
            # tranche, et elle ne se trompe pas.
            def rang(i: int) -> tuple:
                n = names[i]
                return (0 if live(n) else 1,
                        0 if n.startswith(self._iface_hint) else 1,
                        1 if n.startswith(("veth", "docker", "br-", "lo")) else 0,
                        i)

            essais = sorted(range(len(names)), key=rang)
            for i in essais:
                if self._master.init(i, ifaces):
                    iface_index = i
                    break
                log.debug("Pas d'esclave EtherCAT sur %s", names[i])
            else:
                raise BusError(
                    "aucun esclave EtherCAT trouvé, sur aucune interface : "
                    + ", ".join(f"{n}{'' if live(n) else ' (sans lien)'}"
                                for n in names)
                    + ". Vérifier le câble entre l'adaptateur et la main, et "
                      "que la main est alimentée.")
            self.interface = names[iface_index]
            log.info("Esclave EtherCAT trouvé sur %s", self.interface)
        else:
            self.interface = names[iface_index]
            if not self._master.init(iface_index, ifaces):
                raise BusError(f"init EtherCAT refusée sur {self.interface}")
        if not self._master.start():
            raise BusError("démarrage du maître refusé")
        self._master.run()

        self._lhp.set_send_rpdo_callback(
            lambda data: bool(self._master.setOutputs(data, len(data))))
        self._start_pump()
        self._lhp.initial(LCN_ECAT)
        self.dof = self._lhp.get_dof()
        log.info("Bus monté sur %s, DOF %s", self.interface, self.dof)

    def close(self) -> None:
        self._stop.set()
        if self._pump and self._pump.is_alive():
            self._pump.join(timeout=2.0)
        if self._master is not None:
            # Le maître du SDK laisse sa boucle d'E/S tourner après stop() et
            # lève alors sur l'interface fermée. On la coupe avant.
            setattr(self._master, "running", False)
            thread = getattr(self._master, "thread", None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        for obj, meth in ((self._lhp, "close"), (self._master, "stop")):
            if obj is None:
                continue
            try:
                getattr(obj, meth)()
            except Exception as e:  # noqa: BLE001
                log.warning("%s() imparfait : %s", meth, e)
        self._lhp = self._master = None

    def __enter__(self) -> "Hand":
        self.connect()
        self.wake()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
        self.close()

    # ── Réveil ────────────────────────────────────────────────────────────────

    def wake(self, home_wait: float = 9.0, timeout: float = 10.0,
             require_tactile: bool = True) -> bool:
        """
        Rend la main bavarde : moteurs alimentés, homing, puis trames capteur.

        Les trois sont nécessaires. Sans le homing en particulier, la charge
        utile des trames reste identiquement nulle — mesuré, pas supposé.

        L'ordre compte : ``set_tpdo_frame_type`` est réémis **après** le homing,
        parce que le homing remet la configuration des trames à sa valeur par
        défaut. Le demander avant seulement donne une main qui n'émet que des
        trames moteur, sans le moindre message d'erreur.

        Le homing du moteur 1 ne se termine jamais sur cette main (défaut
        d'actionneur connu). C'est sans effet sur les capteurs.
        """
        self._set_tpdo_frame_type()
        self._lhp.set_control_mode(0, LCM_POSITION)
        self._lhp.set_enable(0, True)
        time.sleep(1.0)
        # Homing moteur par moteur, en sautant ceux qui sont en panne. Le
        # broadcast home_motors(0) lance aussi la flexion du pouce, dont le
        # homing ne se termine jamais : le variateur finit par passer en
        # ALARM code 1 (« out of position ») et la main s'allume en rouge.
        for m in hw.MOTOR_IDS:
            if m in hw.BROKEN_MOTORS:
                log.info("Homing sauté pour %s (panne connue)", hw.MOTOR_NAMES[m])
                continue
            self._lhp.home_motors(m)
        time.sleep(home_wait)

        self._set_tpdo_frame_type()
        # Le tactile et les moteurs sont deux choses distinctes : la main peut
        # refuser de basculer en mode capteur tout en obéissant parfaitement aux
        # consignes de position. Exiger le tactile pour piloter interdisait le
        # pilotage sans raison — on rend donc l'échec facultatif.
        tactile_ok = True
        try:
            # La bascule en mode capteur echoue parfois sans raison visible :
            # le bus est monte, les trames moteur circulent, et pas une seule
            # trame capteur en dix secondes. Reemettre la demande suffit. Ne
            # pas insister coutait une acquisition entiere sur un alea.
            derniere = None
            for essai in range(3):
                try:
                    self._wait_for_tactile(timeout)
                    break
                except BusError as e:
                    derniere = e
                    log.warning("Pas de trame capteur (essai %d/3) — on "
                                "redemande le mode capteur", essai + 1)
                    self._set_tpdo_frame_type()
            else:
                raise derniere
        except BusError:
            if require_tactile:
                raise
            tactile_ok = False
            log.warning("Pas de trame capteur : les moteurs restent pilotables, "
                        "le tactile sera indisponible.")
        faults = self.alarms()
        if faults:
            log.warning("Alarmes après réveil : %s",
                        {hw.MOTOR_NAMES[m]: a for m, a in faults.items()})
        log.info("Main réveillée%s.",
                 ", trames capteur confirmées" if tactile_ok else " (sans tactile)")
        return tactile_ok

    def enable(self) -> None:
        """
        Réaffirme mode position et alimentation, sans homing.

        Le variateur cesse d'exécuter les consignes après un temps d'usage :
        elles sont acceptées, la cible est relue correctement, aucune alarme
        n'est levée, et rien ne bouge. Un ``wake()`` complet rétablit — mesuré le
        2026-08-19 — mais coûte dix secondes de homing. Ceci en est la partie
        bon marché, à réémettre avant chaque consigne.
        """
        self._lhp.set_control_mode(0, LCM_POSITION)
        self._lhp.set_enable(0, True)

    def alarms(self) -> dict[int, int]:
        """Moteurs en défaut, code d'alarme non nul."""
        out = {}
        for m in hw.MOTOR_IDS:
            try:
                code = int(self._lhp.get_now_alarm(m))
            except Exception:  # noqa: BLE001
                continue
            if code:
                out[m] = code
        return out

    def clear_alarms(self) -> dict[int, int]:
        """Acquitte les alarmes et rend celles qui persistent."""
        self._lhp.set_clear_alarm(0)
        time.sleep(1.0)
        return self.alarms()

    def _wait_for_tactile(self, timeout: float) -> None:
        """Vérifie qu'une trame capteur arrive vraiment, plutôt que l'espérer."""
        from .tpdo import is_tactile  # noqa: PLC0415
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.latest()
            if raw is not None and is_tactile(raw):
                return
            time.sleep(0.05)
        seen = (self.latest() or b"\xff")[0]
        raise BusError(
            f"aucune trame capteur après {timeout:.0f} s (dernier type reçu : "
            f"0x{seen:02x}). La main n'a pas basculé en mode capteur.")

    def release(self, force: bool = False) -> None:
        """
        Ouvre la main puis coupe le couple.

        L'ouverture vient d'abord et n'est pas facultative : la main n'est pas
        rétro-entraînable, donc couper le couple sur une main fermée la laisse
        serrée sur l'objet, et plus rien ne la rouvrira sans la réalimenter.

        ``force=True`` coupe malgré un échec d'ouverture — à ne faire qu'après
        avoir dégagé l'objet à la main.
        """
        opened = False
        try:
            opened = self.open_hand()
        except Exception as e:  # noqa: BLE001
            log.warning("Ouverture impossible : %s", e)
        if not opened and not force:
            log.error("La main n'est pas ouverte : le couple est MAINTENU pour "
                      "pouvoir réessayer. Dégagez l'objet, puis rappelez "
                      "release(force=True) ou open_hand().")
            return
        try:
            self._lhp.stop_motors(0)
            self._lhp.set_enable(0, False)
        except Exception as e:  # noqa: BLE001
            log.warning("Coupure du couple imparfaite : %s", e)

    def _set_tpdo_frame_type(self) -> None:
        fn = self._lhp._lib.lhandprolib_set_tpdo_frame_type  # noqa: SLF001
        if isinstance(fn, _MissingSymbol):
            raise BusError("set_tpdo_frame_type absent : tactile inaccessible")
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p]
        if (rc := fn(self._lhp._handle)) != 0:  # noqa: SLF001
            raise BusError(f"set_tpdo_frame_type → {rc}")

    # ── Lecture ───────────────────────────────────────────────────────────────

    def _start_pump(self) -> None:
        def loop():
            time.sleep(0.2)
            size = self._master.getInputSize()
            while not self._stop.is_set():
                raw = self._master.getInputs(size)
                if raw:
                    if self._recording:
                        # Horodate avant toute autre chose : le decodage SDK
                        # qui suit prend du temps, et c'est l'instant de la
                        # lecture qui sert de reference commune aux images.
                        self._capture(time.perf_counter(), raw)
                    with self._lock:
                        self._latest = raw
                        if raw[0] == FRAME_TACTILE:
                            self._latest_tactile = raw
                        elif raw[0] == FRAME_MOTOR:
                            self._latest_motor = raw
                    self._lhp.set_tpdo_data_decode(raw)
                time.sleep(0.001)

        self._pump = threading.Thread(target=loop, daemon=True)
        self._stop.clear()
        self._pump.start()

    def _capture(self, t: float, raw: bytes) -> None:
        with self._record_lock:
            if len(self._record) >= RECORD_MAX_FRAMES:
                self._record_dropped += 1
                return
            self._record.append(Frame(t, bytes(raw)))

    def start_recording(self) -> None:
        """
        Conserve **toutes** les trames lues, des deux types, jusqu'a l'arret.

        Aucun filtrage, aucun sous-echantillonnage, et surtout aucune
        deduplication : deux trames identiques sont deux trames. Au repos la
        charge utile ne bouge pas d'un octet pendant des secondes — dedupliquer
        reviendrait a effacer ces periodes, qui sont precisement la ligne de
        base dont le decodage a besoin.

        C'est ce qui rend une session reinterpretable : les valeurs decodees
        dependent de la table de decoupage, les octets non.
        """
        with self._record_lock:
            self._record = []
            self._record_dropped = 0
        self._recording = True
        log.info("Enregistrement du flux brut demarre")

    def stop_recording(self) -> list["Frame"]:
        """
        Arrete l'enregistrement et rend les trames accumulees.

        Returns:
            la liste des :class:`Frame`, dans l'ordre de lecture. C'est
            directement ce qu'attend
            :meth:`vt_tactile.dataset.Session.save_raw_stream`.
        """
        self._recording = False
        # Laisser la pompe finir l'iteration en cours avant de prendre le
        # tampon, sans quoi la derniere trame peut manquer.
        time.sleep(0.05)
        with self._record_lock:
            frames, self._record = self._record, []
            perdues = self._record_dropped
            self._record_dropped = 0
        if perdues:
            log.warning("Tampon plein : %d trames non enregistrees", perdues)
        if frames:
            span = frames[-1].t - frames[0].t
            log.info("Flux brut : %d trames sur %.1f s (%.0f/s)",
                     len(frames), span, len(frames) / span if span else 0.0)
        return frames

    @property
    def recording(self) -> bool:
        return self._recording

    def latest(self) -> bytes | None:
        """Dernière trame reçue, quel que soit son type."""
        with self._lock:
            return self._latest

    def latest_motor(self) -> bytes | None:
        """Dernière trame d'**état moteur**, brute. Les positions y sont
        telles que le variateur les publie, sans passer par le SDK."""
        with self._lock:
            return self._latest_motor

    def latest_tactile(self) -> bytes | None:
        """
        Dernière trame **capteur**.

        La main alterne trames moteur et trames capteur à parts égales : une
        lecture sur deux de :meth:`latest` tombe sur une trame moteur. C'est ce
        qu'il faut appeler dans une boucle d'acquisition.
        """
        with self._lock:
            return self._latest_tactile

    def frames(self, duration: float | None = None, period: float = 0.005):
        """Générateur de trames distinctes."""
        deadline = None if duration is None else time.time() + duration
        last = None
        while deadline is None or time.time() < deadline:
            raw = self.latest()
            if raw is not None and raw != last:
                last = raw
                yield raw
            time.sleep(period)

    # ── Moteurs ───────────────────────────────────────────────────────────────

    def positions(self, motors=hw.MOTOR_IDS) -> dict[int, int]:
        """
        Position de chaque moteur, en counts, **signee**.

        Lue dans la trame brute et non par le getter du SDK, qui **ecrete les
        valeurs negatives a zero**. Un doigt pousse au-dela de son zero — ce
        qui arrive des qu'un objet le repousse — y apparait alors fige a 0 :
        toute boucle qui juge le mouvement sur cette lecture conclut
        « immobile » pendant que le doigt remonte reellement vers zero. Quatre
        moteurs parfaitement sains sont ainsi passes pour muets une nuit
        entiere, sur trois outils differents.

        Le getter du SDK reste disponible par :meth:`positions_sdk`, et sert
        de repli tant qu'aucune trame moteur n'est arrivee.
        """
        from .tpdo import decode_motor  # noqa: PLC0415
        brut = self.latest_motor()
        if brut is None:
            return self.positions_sdk(motors)
        d = decode_motor(bytes(brut))
        return {m: d["positions"][m] for m in motors}

    def positions_sdk(self, motors=hw.MOTOR_IDS) -> dict[int, int]:
        """Position telle que le SDK la rend — negatifs ecretes a zero."""
        return {m: int(self._lhp.get_now_position(m)) for m in motors}

    def currents(self, motors=hw.MOTOR_IDS) -> dict[int, int]:
        """
        Courant de chaque moteur, en pour mille du couple nominal.

        Lu dans la trame brute, comme :meth:`positions`. Ce n'est pas
        seulement plus direct : c'est **moins d'appels au SDK**. Une boucle de
        fermeture qui interroge six positions et six courants toutes les 60 ms
        en emet plus de huit cents par seconde, pendant que le fil de pompe en
        emet six cents. L'interface web, elle, serialise ses acces et se tait
        pendant les mouvements — et elle pilote la main sans jamais perdre un
        ordre. On supprime donc la sollicitation plutot que d'ajouter un verrou.
        """
        from .tpdo import decode_motor  # noqa: PLC0415
        brut = self.latest_motor()
        if brut is None:
            return self.currents_sdk(motors)
        d = decode_motor(bytes(brut))
        return {m: d["currents"][m] for m in motors}

    def currents_sdk(self, motors=hw.MOTOR_IDS) -> dict[int, int]:
        """Courant tel que le SDK le rend."""
        return {m: int(self._lhp.get_now_current(m)) for m in motors}

    def command(self, targets: dict[int, int], velocity: int,
                max_current: int) -> None:
        """
        Applique une consigne de position et déclenche le mouvement.

        Le courant maximum est réémis à chaque appel : c'est lui qui borne
        physiquement l'effort sur l'objet, on ne le laisse pas à la main d'un
        état antérieur.
        """
        for motor, target in targets.items():
            self._lhp.set_target_position(motor, int(target))
            self._lhp.set_position_velocity(motor, int(velocity))
            self._lhp.set_max_current(motor, int(max_current))
        # ``move_motors`` est déclenché deux fois, espacées d'un cycle TPDO.
        # Le service constructeur fait de même, pour un symptôme voisin : une
        # commande sur deux n'actionnait rien, les cibles restant mémorisées
        # jusqu'au ``move_motors`` suivant. Le second appel est idempotent — il
        # ne fait que relancer le mouvement vers des cibles déjà écrites.
        self._lhp.move_motors(0)
        time.sleep(0.06)
        self._lhp.move_motors(0)

    def relancer(self) -> None:
        """
        Réémet l'ordre de mouvement vers les cibles déjà écrites.

        Le variateur laisse passer des consignes sans les exécuter : la cible
        est mémorisée, ``move_motors`` répond 0, et rien ne bouge. Un second
        appel suffit le plus souvent à démarrer le mouvement — c'est déjà ce que
        fait :meth:`command`. Ceci permet d'insister quand il n'a toujours pas
        démarré au bout d'une seconde, plutôt que de rouvrir tout le bus.
        """
        self._lhp.move_motors(0)

    def open_hand(self, timeout: float = 8.0, tolerance: int = 120,
                  motors=None, step: int = 250, include_pivot: bool = True) -> bool:
        """
        Ramène la main en position ouverte, **par rampe**, et vérifie.

        Une consigne unique ne suffit pas : le variateur ignore une commande
        identique à la précédente, et sur une main qui vient de tenir un objet
        elle peut rester sans effet. Constaté au banc — la main est restée
        fermée sur le cylindre malgré trois ``open_hand`` successifs, jusqu'à
        ce qu'on descende par paliers strictement décroissants.

        Le pivot du pouce est ramené en premier : laissé en opposition, il
        barre la paume et n'est pas une pose de repos.

        Ne touche jamais à l'activation des moteurs — couper le couple sur une
        main fermée la laisse fermée, elle n'est pas rétro-entraînable.
        """
        if motors is None:
            motors = [m for m in hw.FLEXORS if m not in hw.BROKEN_MOTORS]
        if include_pivot:
            motors = [hw.THUMB_PIVOT, *motors]

        deadline = time.time() + timeout
        pos: dict[int, int] = {}
        while time.time() < deadline:
            pos = self.positions(tuple(motors))
            if all(v <= tolerance for v in pos.values()):
                return True
            for m, v in pos.items():
                if v > tolerance:
                    self.command({m: max(0, v - step)},
                                 hw.VELOCITY_OPEN, hw.FULL_CURRENT)
            time.sleep(0.15)

        pos = self.positions(tuple(motors))
        if all(v <= tolerance for v in pos.values()):
            return True
        log.error("Ouverture incomplète après %.0f s : %s", timeout, pos)
        return False

    def collect(self, seconds: float, tactile_only: bool = True) -> list[bytes]:
        """Ramasse les trames pendant la durée demandée. Sert au zéro."""
        from .tpdo import is_tactile  # noqa: PLC0415
        return [f for f in self.frames(duration=seconds)
                if not tactile_only or is_tactile(f)]
