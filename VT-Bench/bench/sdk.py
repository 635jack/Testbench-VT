#!/usr/bin/env python3
"""
sdk.py — accès à la main, en n'utilisant que le SDK constructeur.

Volontairement sans aucune hypothèse sur le contenu des trames : ce module
monte le bus, expose les 192 octets bruts tels quels, et relaie les appels de
l'API tactile du SDK. Il ne décode rien. L'interprétation est du ressort de
``metrics.py``, et elle est mesurée, pas supposée.

Trois écarts du wrapper Python livré par Leadshine sont corrigés ici, chacun
capable de rendre la main muette sans message d'erreur :

1. ``PyLHandProLib`` n'expose pas ``set_tpdo_frame_type``, alors que le symbole
   C ``lhandprolib_set_tpdo_frame_type`` existe. On le lie par ctypes.
2. ``lhandprolib_loader`` déclare des prototypes pour des symboles absents de
   certaines versions de la ``.so``. On tolère leur absence.
3. Le manuel v1.4 documente ``set_finger_pressure_reset(int sensor_id)`` ; la
   ``.so`` livrée exporte la version sans argument. On sonde laquelle est là.

À exécuter dans la VM, en root : le maître EtherCAT ouvre des sockets raw.
"""
from __future__ import annotations

import ctypes
import logging
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

TPDO_SIZE = 192
LCN_ECAT = 0

#: Les onze ids déclarés par le SDK (manuel §« Sensor ID (LSS) Enumeration »).
SENSOR_IDS = tuple(range(1, 12))
SENSOR_LABELS = {
    1: "thumb.tip",   2: "thumb.pad",
    3: "index.tip",   4: "index.pad",
    5: "middle.tip",  6: "middle.pad",
    7: "ring.tip",    8: "ring.pad",
    9: "little.tip",  10: "little.pad",
    11: "palm",
}

#: Les cinq lectures tactiles de l'API, telles que nommées dans le manuel.
SENSOR_READS = ("pressure", "normal_force", "tangential_force",
                "force_direction", "proximity")


class BenchError(RuntimeError):
    """La main n'est pas exploitable."""


class _MissingSymbol:
    """
    Bouchon pour un symbole absent de la bibliothèque.

    Accepte qu'on lui pose un ``restype`` et des ``argtypes`` — c'est tout ce
    que fait le chargeur de Leadshine au moment de la déclaration — et ne
    proteste qu'à l'appel, ce qui n'arrive jamais pour les symboles concernés.
    """

    def __init__(self, name: str):
        self._name = name

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)

    def __call__(self, *args, **kwargs):
        raise BenchError(f"{self._name} est absent de cette bibliothèque")


class _TolerantCDLL(ctypes.CDLL):
    """
    CDLL qui ne s'effondre pas sur un symbole manquant.

    Le chargeur livré par Leadshine déclare les prototypes de
    ``lhandprolib_set_send_data_callback`` et ``lhandprolib_set_recv_data_decode``,
    qui **n'existent pas** dans la ``.so`` Linux fournie. La déclaration lève
    donc ``AttributeError`` dans le constructeur, avant que le moindre code
    applicatif ne s'exécute : la bibliothèque est inutilisable telle quelle.

    Plutôt que de modifier le fichier du constructeur — on veut rester sur une
    copie d'origine — on tolère l'absence et on la consigne. Les symboles
    concernés ne sont pas utilisés sur le chemin EtherCAT.
    """

    missing: set[str] = set()

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            _TolerantCDLL.missing.add(name)
            log.warning("Symbole absent, ignoré : %s", name)
            return _MissingSymbol(name)


@dataclass
class Frame:
    """Une trame TPDO brute, horodatée à la réception."""

    t: float
    data: bytes


@dataclass
class LinkInfo:
    """Ce qu'on peut dire du lien et de l'hôte, pour identifier une config."""

    interface: str = ""
    mac: str = ""
    speed_mbps: int | None = None
    driver: str = ""
    host: str = ""
    kernel: str = ""
    machine: str = ""
    virtualised: str = ""
    slaves: list[dict] = field(default_factory=list)
    output_size: int = 0
    input_size: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def probe_host(interface: str) -> LinkInfo:
    """Empreinte de l'hôte et du lien — sert à comparer deux configurations."""
    info = LinkInfo(
        interface=interface,
        host=platform.node(),
        kernel=platform.release(),
        machine=platform.machine(),
    )
    base = f"/sys/class/net/{interface}"
    info.mac = _read(f"{base}/address")
    speed = _read(f"{base}/speed")
    info.speed_mbps = int(speed) if speed.lstrip("-").isdigit() else None
    driver = Path(f"{base}/device/driver").resolve().name if Path(base).exists() else ""
    info.driver = driver
    # systemd-detect-virt répond « none » sur métal nu ; utile pour la question VM
    try:
        info.virtualised = subprocess.run(
            ["systemd-detect-virt"], capture_output=True, text=True, timeout=2
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        info.virtualised = "unknown"
    return info


class Hand:
    """
    Connexion à la main : bus EtherCAT + bibliothèque constructeur.

    Deux vues cohabitent, jamais mélangées :

    * ``frames`` — les 192 octets bruts, sans interprétation ;
    * ``read_sdk_sensors()`` — ce que l'API tactile du SDK répond, codes
      d'erreur compris.

    Comparer les deux est tout l'intérêt du banc.
    """

    def __init__(self, sdk_dir: str | None = None, lib_path: str | None = None):
        self._sdk_dir = sdk_dir
        self._lib_path = lib_path
        self._lhp = None
        self._master = None
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._recording: list[Frame] | None = None
        self.link = LinkInfo()
        self.capabilities: dict[str, bool] = {}
        self.missing_symbols: list[str] = []
        self.dof: tuple[int, int] = (0, 0)

    # ── Chargement du SDK ─────────────────────────────────────────────────────

    @staticmethod
    def find_sdk(explicit: str | None = None) -> tuple[str, str]:
        """
        Localise les modules Python et la ``.so`` du SDK.

        Les deux sont cherchés **séparément** : Leadshine ne livre les modules
        Python que sous ``x86_64``, alors que la bibliothèque native doit
        correspondre à l'architecture de l'hôte. Sur la VM aarch64, il faut donc
        le Python de ``x86_64`` et la ``.so`` de ``aarch64``. Les exiger sous la
        même arborescence ne marche que sur PC.

        Retourne ``(dossier_python, chemin_so)``.
        """
        arch = "aarch64" if platform.machine() in ("aarch64", "arm64") else "x86_64"
        roots = []
        if explicit:
            roots.append(Path(explicit))
        roots += [
            Path.home() / "Leadshine_SDK_original/sdk_lib",
            Path.home() / "DH116-ISIR/external/DH116_LHandProLib-API-Linux-20251128",
            Path(__file__).resolve().parents[2] / "Leadshine_SDK_original/sdk_lib",
        ]

        so = next(
            (p for root in roots
             if (p := root / arch / "lib/libLHandProLib.so").is_file()), None)
        py = next(
            (p for root in roots for a in (arch, "x86_64", "aarch64", "i386")
             if (p := root / a / "share/LHandProLib/examples/EtherCAT_python"
                 ).is_dir() and (p / "lhandprolib_wrapper.py").is_file()), None)

        if so is None:
            raise BenchError(
                f"libLHandProLib.so introuvable pour {arch}. Cherché dans : "
                + ", ".join(str(r) for r in roots))
        if py is None:
            raise BenchError(
                "modules Python du SDK introuvables. Cherché dans : "
                + ", ".join(str(r) for r in roots))
        return str(py), str(so)

    def _import(self):
        py_dir, so = self.find_sdk(self._sdk_dir)
        self._lib_path = self._lib_path or so
        if py_dir not in sys.path:
            sys.path.insert(0, py_dir)
        log.info("SDK Python : %s", py_dir)
        log.info("SDK natif  : %s", self._lib_path)
        try:
            import lhandprolib_loader as loader           # noqa: PLC0415
            from lhandprolib_wrapper import PyLHandProLib  # noqa: PLC0415
            from ethercat_master import EthercatMaster     # noqa: PLC0415
        except ImportError as e:
            raise BenchError(f"import du SDK impossible : {e}") from e

        # On remplace le chargement de la bibliothèque par une version tolérante
        # aux symboles absents, sans toucher au fichier du constructeur.
        def _load_library(inner_self, lib_path=None):
            path = Path(lib_path) if lib_path else inner_self._find_library()
            if not path.exists():
                raise BenchError(f"bibliothèque introuvable : {path}")
            inner_self._lib = _TolerantCDLL(str(path))

        loader.LHandProLibLoader._load_library = _load_library
        loader._global_lhandpro_lib = None  # le singleton peut garder un échec
        return PyLHandProLib, EthercatMaster

    def _probe_capabilities(self) -> None:
        """Quels symboles cette ``.so`` expose-t-elle réellement ?"""
        lib = self._lhp._lib  # noqa: SLF001
        wanted = [
            "lhandprolib_set_tpdo_frame_type",
            "lhandprolib_get_finger_pressure",
            "lhandprolib_get_finger_proximity",
            "lhandprolib_get_finger_sensor_pos",
            "lhandprolib_set_finger_pressure_reset",
            "lhandprolib_get_finger_normal_force",
            "lhandprolib_get_finger_tangential_force",
            "lhandprolib_get_finger_force_direction",
        ]
        for name in wanted:
            try:
                sym = getattr(lib, name)
            except AttributeError:
                sym = None
            # avec _TolerantCDLL, un symbole absent revient sous forme de bouchon
            present = sym is not None and not isinstance(sym, _MissingSymbol)
            self.capabilities[name] = present
            if not present:
                log.warning("Symbole absent de la bibliothèque : %s", name)

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def connect(self, iface_index: int | None = None, iface_hint: str = "enx") -> None:
        PyLHandProLib, EthercatMaster = self._import()
        _TolerantCDLL.missing.clear()
        self._lhp = PyLHandProLib(lib_path=self._lib_path)
        self._probe_capabilities()
        self.missing_symbols = sorted(_TolerantCDLL.missing)
        self._master = EthercatMaster()

        ifaces = self._master.scanNetworkInterfaces()
        if not ifaces:
            raise BenchError("aucune interface réseau visible")
        names = [n.decode() if isinstance(n, bytes) else str(n) for n in ifaces]
        if iface_index is None:
            iface_index = next(
                (i for i, n in enumerate(names) if n.startswith(iface_hint)),
                len(names) - 1,
            )
        chosen = names[iface_index]
        log.info("Interfaces vues : %s — choisie : %s", names, chosen)

        if not self._master.init(iface_index, ifaces):
            raise BenchError(f"init EtherCAT refusée sur {chosen}")
        if not self._master.start():
            raise BenchError("démarrage du maître refusé")
        self._master.run()

        self.link = probe_host(chosen)
        self.link.input_size = self._master.getInputSize()
        self.link.output_size = self._master.getOutputSize()
        self.link.slaves = [
            {"name": getattr(s, "name", "?"),
             "man": hex(getattr(s, "man", 0)),
             "id": hex(getattr(s, "id", 0)),
             "input_bytes": len(getattr(s, "input", b"")),
             "output_bytes": len(getattr(s, "output", b""))}
            for s in getattr(self._master, "slaves", [])
        ]

        self._lhp.set_send_rpdo_callback(self._send_rpdo)
        self._start_pump()
        self._lhp.initial(LCN_ECAT)
        total, active = self._lhp.get_dof()
        log.info("DOF : %d au total, %d actifs", total, active)
        self.dof = (total, active)

    def close(self) -> None:
        self._stop.set()
        if self._pump and self._pump.is_alive():
            self._pump.join(timeout=2.0)
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
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Pompe TPDO ────────────────────────────────────────────────────────────

    def _send_rpdo(self, data: bytes) -> bool:
        return bool(self._master and self._master.setOutputs(data, len(data)))

    def _start_pump(self) -> None:
        """
        Boucle de réception dédiée.

        On horodate **chaque** lecture, doublons compris : c'est la seule façon
        de distinguer « la main n'émet pas » de « l'hôte ne lit pas assez vite »,
        et donc de répondre à la question de la VM.
        """
        def loop():
            time.sleep(0.2)
            size = self._master.getInputSize()
            while not self._stop.is_set():
                raw = self._master.getInputs(size)
                if raw:
                    fr = Frame(time.perf_counter(), raw)
                    with self._lock:
                        self._latest = fr
                        if self._recording is not None:
                            self._recording.append(fr)
                    if self._lhp is not None:
                        self._lhp.set_tpdo_data_decode(raw)
                time.sleep(0.001)

        self._pump = threading.Thread(target=loop, daemon=True)
        self._stop.clear()
        self._pump.start()

    # ── Configuration des trames ──────────────────────────────────────────────

    def enable_tactile(self) -> None:
        """
        Bascule la main en émission de trames capteur.

        Sans cet appel, le SDK ne reçoit jamais de données tactiles. Il n'est
        pas exposé par le wrapper Python : on lie le symbole C directement.
        """
        if not self.capabilities.get("lhandprolib_set_tpdo_frame_type"):
            raise BenchError("cette bibliothèque n'expose pas set_tpdo_frame_type")
        fn = self._lhp._lib.lhandprolib_set_tpdo_frame_type  # noqa: SLF001
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p]
        rc = fn(self._lhp._handle)  # noqa: SLF001
        if rc != 0:
            raise BenchError(f"set_tpdo_frame_type → {rc}")
        log.info("Trames capteur demandées.")

    def enable_motors(self, home: bool = False, home_wait: float = 8.0) -> dict:
        """
        Alimente les moteurs, éventuellement avec homing.

        Le banc tactile ne le fait pas par défaut : mesurer des capteurs ne
        devrait pas demander d'énergiser des actionneurs. On le rend possible
        parce que c'est une hypothèse à tester — la carte capteurs pourrait
        n'échantillonner qu'une fois la main réveillée.

        Attention : la main n'est pas rétro-entraînable. Une fois les moteurs
        alimentés, les doigts se figent dans leur pose courante.
        """
        out: dict = {"home": home}
        self._lhp.set_control_mode(0, 0)     # LCM_POSITION
        self._lhp.set_enable(0, True)
        time.sleep(1.0)
        out["enable_readback"] = {m: self._lhp.get_enable(m) for m in range(1, 7)}
        if home:
            self._lhp.home_motors(0)
            time.sleep(home_wait)
            out["status_after_home"] = {m: self._lhp.get_now_status(m)
                                        for m in range(1, 7)}
        log.info("Moteurs alimentés : %s", out)
        return out

    def disable_motors(self) -> None:
        """Coupe le couple. À appeler avant de partir."""
        try:
            self._lhp.stop_motors(0)
            self._lhp.set_enable(0, False)
        except Exception as e:  # noqa: BLE001
            log.warning("Coupure des moteurs imparfaite : %s", e)

    def send_raw_rpdo(self, payload: bytes) -> bool:
        """Écrit une trame RPDO brute. Sert à explorer la configuration."""
        size = self._master.getOutputSize()
        if len(payload) > size:
            raise BenchError(f"charge utile trop longue ({len(payload)} > {size})")
        return bool(self._master.setOutputs(payload + bytes(size - len(payload)), size))

    def reset_pressure_reference(self) -> int | None:
        """
        Remet à zéro la référence de pression.

        Le manuel v1.4 la documente avec un ``sensor_id``, la ``.so`` livrée
        l'exporte sans. On essaie les deux et on rapporte laquelle a répondu.
        """
        if not self.capabilities.get("lhandprolib_set_finger_pressure_reset"):
            return None
        fn = self._lhp._lib.lhandprolib_set_finger_pressure_reset  # noqa: SLF001
        fn.restype = ctypes.c_int
        for argtypes, args in (([ctypes.c_void_p], ()),
                               ([ctypes.c_void_p, ctypes.c_int], (0,))):
            try:
                fn.argtypes = argtypes
                return int(fn(self._lhp._handle, *args))  # noqa: SLF001
            except Exception:  # noqa: BLE001, S112
                continue
        return None

    # ── Lecture ───────────────────────────────────────────────────────────────

    @property
    def lhp(self):
        return self._lhp

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def start_recording(self) -> None:
        """Ouvre une fenêtre d'enregistrement, sans bloquer l'appelant."""
        with self._lock:
            self._recording = []

    def stop_recording(self) -> list[Frame]:
        """Ferme la fenêtre et rend les trames captées."""
        with self._lock:
            out, self._recording = self._recording, None
        return out or []

    def record(self, seconds: float) -> list[Frame]:
        """Enregistre toutes les lectures pendant la durée demandée."""
        self.start_recording()
        time.sleep(seconds)
        return self.stop_recording()

    def read_sdk_sensors(self) -> dict[int, dict]:
        """
        Interroge l'API tactile du SDK pour les onze ids déclarés.

        Les codes d'erreur sont conservés tels quels : un id qui échoue
        systématiquement est un résultat, pas un incident à masquer.
        """
        out: dict[int, dict] = {}
        lhp = self._lhp
        for sid in SENSOR_IDS:
            entry: dict = {"label": SENSOR_LABELS[sid]}
            for name in SENSOR_READS:
                getter = getattr(lhp, f"get_finger_{name}", None)
                if getter is None:
                    entry[name] = {"error": "absent du wrapper"}
                    continue
                try:
                    value = getter(sid)
                    entry[name] = {"value": value}
                except Exception as e:  # noqa: BLE001
                    entry[name] = {"error_code": getattr(e, "error_code", None),
                                   "error": str(e)[:120]}
            try:
                x, y = lhp.get_finger_sensor_pos(sid)
                entry["sensor_pos_count"] = len(x)
            except Exception as e:  # noqa: BLE001
                entry["sensor_pos_count"] = None
                entry["sensor_pos_error"] = getattr(e, "error_code", str(e)[:60])
            out[sid] = entry
        return out
