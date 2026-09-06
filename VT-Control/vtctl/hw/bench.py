#!/usr/bin/env python3
"""
bench.py — le seul propriétaire du port série : lampe et plateau.

**Le variateur de lumière et le pont infrarouge sont la même carte** sur
``/dev/ttyACM0``. Ouvrir un second port la réinitialise, et le firmware
redémarre à ``ledcWrite(255)`` : lampe à fond, blanc des marqueurs hors de la
bande de détection, asservissement aveugle. ``Control_Turtable_IR/turntable.py``
fait exactement cela — il n'est donc importé nulle part ici.

Le ``Dimmer`` garde le port, et les trames du plateau passent par ``send_ir``
via ``vt_tactile.plateau.IRviaDimmer``.

Trois comportements de la télécommande, qu'on ne peut pas déduire du code :

* **``START_PAUSE`` est une bascule.** La même trame démarre ou arrête. Un
  ordre d'arrêt de trop **relance** le plateau. On ne l'envoie donc que si
  l'image montre encore du mouvement.
* **``VITESSE_MOINS`` démarre le plateau**, dans le dernier sens utilisé. Ce
  n'est pas qu'un réglage : réduire la vitesse avant d'avoir fixé le sens lance
  le plateau à contresens une fois sur deux.
* **La roue libre vaut ~15°** après l'ordre d'arrêt. C'est l'inertie du
  plateau, pas une latence de transmission : toute impulsion, même de 120 ms,
  déplace d'au moins autant. Les micro-ajustements sont illusoires.
"""
from __future__ import annotations

import logging
import time

from .. import config
from .resources import SERIE, ResourceManager

log = logging.getLogger("vtctl.bench")


class BenchOwner:
    """
    Détient le port série. Sert la lampe et le plateau, jamais deux ports.

    Args:
        manager: le gestionnaire de ressources.
        fake: un ``FakeDimmer``. Quand il est fourni, aucun matériel n'est
            touché et ``pyserial`` n'est pas importé.
        settle: temps d'établissement de la lampe. Le défaut est celui du banc.
    """

    def __init__(self, manager: ResourceManager, fake=None,
                 settle: float = config.SETTLE_LAMPE, port: "str | None" = None):
        self._mgr = manager
        self._token = None
        self.dim = None
        self.fake = fake
        self.settle = settle
        self._port = port
        self.ir = None
        self._pwm_demande = None
        #: Instant de la dernière consigne de lampe. Le variateur acquitte
        #: immédiatement mais la lampe met plus de deux secondes à s'établir :
        #: ``Dimmer.pwm`` rend la valeur **acquittée**, jamais la lumière. Sans
        #: cet instant, rien ne distingue « commandé » de « établi », et c'est
        #: cette confusion qui a produit des tableaux entiers de mesures fausses
        #: le 2026-08-20.
        self._t_consigne = 0.0
        #: Avons-nous ordonné un départ dont l'arrêt n'a pas encore été envoyé ?
        #: C'est une **connaissance positive**, et elle vaut mieux qu'une mesure
        #: ArUco aveugle : quand on sait avoir lancé le plateau, on sait qu'il
        #: faut une bascule pour l'arrêter, même si l'on ne voit plus rien.
        self.lance = False

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def open(self) -> "BenchOwner":
        if self.dim is not None:
            return self
        self._token = self._mgr.acquire(SERIE, "BenchOwner")
        try:
            if self.fake is not None:
                self.dim = self.fake
                self.dim.connect()
            else:
                from vt_light.dimmer import Dimmer  # noqa: PLC0415

                self.dim = Dimmer(port=self._port)
                self.dim.connect()
            from vt_tactile.plateau import IRviaDimmer  # noqa: PLC0415

            self.ir = IRviaDimmer(self.dim, config.CONFIG_TELECOMMANDE)
            log.info("port série ouvert : %s", self.dim.port)
        except Exception:
            self._token.release()
            self._token = None
            self.dim = None
            raise
        return self

    def close(self) -> None:
        """
        Éteint la lampe puis rend le port.

        La lampe reste allumée si on se contente de fermer le port : le
        firmware ne remet rien à zéro, et le banc reste éclairé jusqu'à la
        prochaine mise sous tension.
        """
        if self.dim is not None:
            try:
                self.dim.set_pwm(0)
            except Exception:  # noqa: BLE001
                log.warning("extinction de la lampe en échec", exc_info=True)
            try:
                self.dim.close()
            except Exception:  # noqa: BLE001
                pass
            self.dim = None
            self.ir = None
        if self._token is not None:
            self._token.release()
            self._token = None

    def __enter__(self) -> "BenchOwner":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def ouvert(self) -> bool:
        return self.dim is not None

    # ── Lampe ─────────────────────────────────────────────────────────────────

    def lumiere(self, pwm: int, attendre: bool = True) -> int:
        """
        Règle la lampe et **attend qu'elle s'établisse**.

        Mesuré le 2026-08-20 : un saut de PWM met plus de deux secondes à se
        stabiliser. Les 0,9 s employées au début ont produit des tableaux
        entiers de mesures fausses — c'est la cause n°1 des conclusions
        erronées de cette journée-là.

        Args:
            attendre: ne le mettre à faux que pour enchaîner plusieurs réglages
                avant une seule attente.
        """
        self._exige_ouvert()
        pwm = max(0, min(255, int(pwm)))
        if self._pwm_demande == pwm and attendre:
            return pwm                    # déjà établie : ne pas repayer 3,5 s
        self.dim.set_pwm(pwm)
        self._pwm_demande = pwm
        self._t_consigne = time.monotonic()
        if attendre and self.settle > 0:
            time.sleep(self.settle)
        return pwm

    def reconnecter(self) -> dict:
        """
        Ferme et rouvre le port série — **et réémet le PWM**.

        Ce dernier point n'est pas un détail : rouvrir le port réinitialise
        l'ESP32, et le firmware redémarre à ``ledcWrite(255)``. Sans réémission,
        la lampe se retrouve à fond juste après une reconnexion, ce qui noie le
        blanc des marqueurs et rend l'angle introuvable — une panne qui suit une
        réparation, donc particulièrement trompeuse.

        Le jeton n'est pas relâché : un autre processus qui ouvrirait le port
        pendant ce temps réinitialiserait la carte à son tour.
        """
        self._exige_ouvert()
        pwm = self._pwm_demande
        try:
            self.dim.close()
        except Exception:  # noqa: BLE001
            log.warning("fermeture du port en échec pendant la reconnexion",
                        exc_info=True)
        self.dim = None
        self.ir = None
        time.sleep(1.5)
        if self.fake is not None:
            self.dim = self.fake
            self.dim.connect()
        else:
            from vt_light.dimmer import Dimmer  # noqa: PLC0415

            self.dim = Dimmer(port=self._port)
            self.dim.connect()
        from vt_tactile.plateau import IRviaDimmer  # noqa: PLC0415

        self.ir = IRviaDimmer(self.dim, config.CONFIG_TELECOMMANDE)
        # Le plateau a pu être laissé en rotation : on ne sait plus, donc on
        # n'affirme rien. Ce drapeau ne se devine pas, il se constate.
        self.lance = False
        self._pwm_demande = None
        if pwm is not None:
            self.lumiere(pwm)
        log.info("port série reconnecté sur %s, PWM réémis à %s", self.dim.port, pwm)
        return {"reconnecte": True, "port": self.dim.port, "pwm_reemis": pwm}

    def niveau(self, nom: str, profil, attendre: bool = True) -> int:
        """Règle la lampe à un niveau nommé du profil VT-Light."""
        return self.lumiere(profil.pwm_of(nom), attendre=attendre)

    def lumiere_aruco(self, pwm: "int | None" = None) -> int:
        """
        Le point de fonctionnement où les marqueurs se décodent le mieux.

        Réglable : c'est une propriété **du banc**, pas du logiciel. Le défaut
        (PWM 120) suppose la lampe en état ; elle ne l'est pas toujours.
        """
        return self.lumiere(config.PWM_ARUCO if pwm is None else pwm)

    @property
    def pwm(self) -> "int | None":
        """
        La dernière valeur **acquittée** par le variateur.

        Ce n'est pas une mesure de lumière : l'ESP32 acquitte la trame, il ne
        photographie rien. Pour savoir si la lampe éclaire vraiment, il faut
        regarder une image — c'est ce que fait le contrôle du selftest.
        """
        return None if self.dim is None else self.dim.pwm

    @property
    def reste_a_etablir(self) -> float:
        """Secondes restantes avant que la lampe ait atteint sa consigne."""
        if self.dim is None or not self._t_consigne or self.settle <= 0:
            return 0.0
        return max(0.0, self.settle - (time.monotonic() - self._t_consigne))

    @property
    def etablie(self) -> bool:
        return self.reste_a_etablir <= 0.0

    # ── Plateau ───────────────────────────────────────────────────────────────

    def demarrer(self, horaire: bool = True, lent: bool = True) -> None:
        """
        Lance le plateau : **le sens d'abord, la vitesse ensuite**.

        L'ordre n'est pas indifférent. ``VITESSE_MOINS`` démarre le plateau dans
        le dernier sens utilisé ; l'émettre avant d'avoir fixé le sens envoie
        donc le plateau dans la direction de la consigne précédente — une fois
        sur deux la mauvaise.
        """
        self._exige_ouvert()
        (self.ir.rotation_droite if horaire else self.ir.rotation_gauche)()
        self.lance = True
        time.sleep(0.3)
        if lent:
            for _ in range(3):            # trois décréments suffisent : ~13 °/s
                self.ir.vitesse_moins()
                time.sleep(0.12)

    def bascule(self) -> None:
        """
        Envoie ``START_PAUSE``. **À n'appeler que si le plateau tourne.**

        Sur un plateau immobile, cette trame le **relance**. C'est le piège qui
        a fait tourner le banc pendant une heure de mesures sans qu'on le sache.
        Passer par :meth:`Banc.arreter_plateau`, qui décide avant d'envoyer.
        """
        self._exige_ouvert()
        self.ir.start_pause()
        self.lance = not self.lance

    def arret_certain(self, roue_libre: float = 3.5) -> bool:
        """
        Arrête un plateau **dont on sait qu'on l'a lancé**, sans rien mesurer.

        Écrit après un échec sur le banc réel : le contrôle du plateau lançait
        la rotation, puis l'arrêt s'en remettait à la mesure ArUco — laquelle
        était aveugle ce jour-là, faute d'éclairage. ``arreter()`` répondait
        donc « immobile », n'envoyait aucune bascule, et **laissait le plateau
        tourner**. Or nous savions parfaitement l'avoir lancé.

        La règle : une connaissance positive prime sur une mesure absente. On
        ne devine jamais qu'il tourne, mais on n'oublie pas qu'on l'a démarré.

        Returns:
            ``True`` si une bascule a été envoyée.
        """
        self._exige_ouvert()
        if not self.lance:
            return False
        log.info("arrêt du plateau : nous l'avons lancé, la bascule est certaine")
        self.bascule()
        time.sleep(roue_libre)             # roue libre : ~15°, 1,2 à 3,2 s
        return True

    def _exige_ouvert(self) -> None:
        if self.dim is None:
            raise RuntimeError("port série fermé : appeler open() d'abord")

    # ── État ──────────────────────────────────────────────────────────────────

    def infos(self) -> dict:
        return {
            "ouvert": self.ouvert,
            "simule": self.fake is not None,
            "port": None if self.dim is None else self.dim.port,
            "pwm": self.pwm,
            "pwm_demande": self._pwm_demande,
            "settle_s": self.settle,
            "etablie": self.etablie,
            "reste_a_etablir_s": round(self.reste_a_etablir, 1),
        }
