#!/usr/bin/env python3
"""
Pilotage du variateur de lumière du banc (sortie PWM du Feather ESP32-S3).

Le même ESP32 sert de pont infrarouge pour le plateau tournant : il émet donc
spontanément des lignes ``RCV ...`` dès qu'il reçoit une trame IR. Toute lecture
d'acquittement doit les ignorer.

Protocole série, 115200 bauds :

    PWM <0-255>\\n   ->   ACK PWM <valeur>\\r\\n
    SEND <proto> <addr> <cmd> <bits>\\n  ->  ACK <proto> ...   (plateau, non utilisé ici)
"""
import glob
import logging
import time

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None

logger = logging.getLogger(__name__)

#: Fréquence de la porteuse PWM du firmware (``PWM_FREQ`` dans le .ino).
PWM_FREQ_HZ = 5000

#: Période de la porteuse, en microsecondes. Une exposition qui n'est pas un
#: multiple entier de cette période intègre un nombre non entier de créneaux :
#: l'éclairement reçu varie alors d'une image à l'autre. Voir
#: :func:`snap_exposure_to_pwm_period`.
PWM_PERIOD_US = 1_000_000 // PWM_FREQ_HZ  # 200 us


def snap_exposure_to_pwm_period(exposure_us):
    """
    Arrondit une exposition au multiple de la période PWM le plus proche.

    À 5 kHz, une exposition de 4000 us intègre exactement 20 créneaux : le
    rapport cyclique vu par le capteur est celui commandé, quelle que soit la
    phase. Une exposition de 4100 us intègre 20,5 créneaux et l'éclairement
    intégré oscille de +-2,4 % selon la phase entre l'obturateur et le PWM.
    """
    n = max(1, round(exposure_us / PWM_PERIOD_US))
    return n * PWM_PERIOD_US


class Dimmer:
    """
    Variateur de lumière. Utilisable en gestionnaire de contexte :

        with Dimmer("/dev/ttyACM0") as d:
            d.set_pwm(128)
    """

    PWM_MIN = 0
    PWM_MAX = 255

    def __init__(self, port=None, baudrate=115200, timeout=1.0,
                 settle_sec=2.0, simulation=False):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.settle_sec = settle_sec
        self.simulation = simulation or serial is None
        self.ser = None
        self._pwm = None
        if not self.simulation:
            self.connect()

    # ------------------------------------------------------------------ liaison

    def connect(self):
        candidates = [self.port] if self.port else []
        candidates += sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
                             + glob.glob("/dev/cu.usbmodem*"))
        for port in [c for c in candidates if c]:
            try:
                self.ser = serial.Serial(port, self.baudrate, timeout=self.timeout)
                self.port = port
                break
            except Exception as exc:
                logger.debug("Port %s indisponible : %s", port, exc)
        if self.ser is None:
            raise RuntimeError(f"Aucun port série ouvrable parmi {candidates}")

        # L'ESP32-S3 en USB CDC natif peut redémarrer à l'ouverture du port :
        # on laisse passer le boot et le "READY IR_BRIDGE" avant tout dialogue.
        time.sleep(self.settle_sec)
        self.ser.reset_input_buffer()
        logger.info("Dimmer connecté sur %s", self.port)

    def close(self):
        if self.ser is not None and self.ser.is_open:
            self.ser.close()
            logger.info("Port dimmer fermé.")
        self.ser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ commande

    @property
    def pwm(self):
        """Dernière valeur PWM acquittée, ou ``None`` si aucune commande émise."""
        return self._pwm

    def set_pwm(self, value, retries=2):
        """
        Impose le rapport cyclique et **attend l'acquittement**.

        L'acquittement est ce qui distingue un problème de liaison série d'un
        problème d'alimentation de la lampe : sans lui on ne sait pas si la
        commande est partie.

        Returns:
            La valeur acquittée par l'ESP32 (bornée 0-255 par le firmware).
        Raises:
            RuntimeError si aucun ``ACK PWM`` n'arrive.
        """
        value = int(max(self.PWM_MIN, min(self.PWM_MAX, value)))
        if self.simulation:
            self._pwm = value
            return value

        for attempt in range(retries + 1):
            self.ser.reset_input_buffer()
            self.ser.write(f"PWM {value}\n".encode())
            self.ser.flush()
            ack = self._read_ack("ACK PWM")
            if ack is not None:
                acked = int(ack.split()[-1])
                if acked != value:
                    logger.warning("PWM %d acquitté à %d (bornage firmware)", value, acked)
                self._pwm = acked
                return acked
            logger.warning("Pas d'ACK PWM pour %d (tentative %d)", value, attempt + 1)
        raise RuntimeError(f"Le dimmer n'acquitte pas PWM {value}")

    def _read_ack(self, prefix, deadline_sec=1.5):
        """Lit des lignes jusqu'à trouver ``prefix``, en ignorant les trames IR."""
        t0 = time.time()
        while time.time() - t0 < deadline_sec:
            line = self.ser.readline().decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith(prefix):
                return line
            # "RCV ..." = trame infrarouge captée, sans rapport avec le PWM.
            logger.debug("Ligne série ignorée : %s", line)
        return None

    def off(self):
        return self.set_pwm(0)

    def full(self):
        return self.set_pwm(255)

    def ramp_to(self, value, step=8, delay=0.02):
        """
        Rejoint ``value`` par paliers. Sans intérêt optique — le LED suit le PWM
        en quelques microsecondes — mais évite les appels de courant brutaux sur
        l'alimentation partagée avec la main DH116.
        """
        start = self._pwm if self._pwm is not None else value
        direction = 1 if value >= start else -1
        for v in range(start, value, direction * max(1, step)):
            self.set_pwm(v)
            time.sleep(delay)
        return self.set_pwm(value)
