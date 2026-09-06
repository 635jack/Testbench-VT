#!/usr/bin/env python3
"""
resources.py — un propriétaire par ressource, et la preuve qu'il l'est.

Trois ressources du banc sont physiquement exclusives, et chacune a déjà coûté
une session :

* **la D405** n'accepte qu'un ``rs.pipeline`` ; deux prétendants existent dans
  le dépôt (``vt_light.camera.D405`` et ``ArUcoTracker``) ;
* **``/dev/ttyACM0``** porte le variateur *et* le pont infrarouge sur le même
  ESP32 ; ouvrir un second port réinitialise la carte, et le firmware redémarre
  à ``ledcWrite(255)`` — lampe à fond, marqueurs ArUco noyés ;
* **l'interface EtherCAT** ne supporte qu'un maître, y compris un maître
  survivant d'un autre processus.

Deux niveaux de verrou, parce que les deux échecs existent :

1. **En processus** — un jeton par ressource. Un second prétendant est
   *refusé*, jamais mis en file. Une attente silencieuse sur un port série
   ressemble exactement à un banc qui rame.
2. **Entre processus** — ``flock`` sur un fichier. Un verrou ``flock`` meurt
   avec le processus qui le tient : un plantage ne laisse pas de verrou
   fantôme, contrairement à un fichier témoin.

Le contrôle des démons concurrents (:func:`demons_concurrents`) complète le
verrou : ``lhandpro_service`` tient le maître EtherCAT sans jamais poser de
verrou, et sans lui ``connect()`` échoue trente secondes plus tard sur un
message qui ne dit pas pourquoi.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("vtctl.resources")

#: Les trois ressources exclusives du banc.
CAMERA = "camera"
SERIE = "serie"
ETHERCAT = "ethercat"
RESSOURCES = (CAMERA, SERIE, ETHERCAT)


class ResourceBusy(RuntimeError):
    """La ressource est déjà tenue. Le message dit par qui."""


class Token:
    """
    Preuve de possession d'une ressource, libérable une seule fois.

    Sert de gestionnaire de contexte : ``with mgr.acquire(CAMERA, "owner"):``.
    """

    def __init__(self, manager: "ResourceManager", nom: str, proprietaire: str):
        self.nom = nom
        self.proprietaire = proprietaire
        self._mgr = manager
        self._libere = False

    @property
    def actif(self) -> bool:
        return not self._libere

    def release(self) -> None:
        if not self._libere:
            self._libere = True
            self._mgr._liberer(self.nom, self)

    def __enter__(self) -> "Token":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __repr__(self) -> str:
        etat = "actif" if self.actif else "libéré"
        return f"<Token {self.nom} → {self.proprietaire} ({etat})>"


class ResourceManager:
    """
    Le registre des ressources tenues, et le seul endroit qui les distribue.

    Args:
        lock_dir: où poser les verrous inter-processus. ``None`` désactive ce
            second niveau — utile en test, où plusieurs suites tournent en
            parallèle sur la même machine sans matériel à protéger.
    """

    def __init__(self, lock_dir=None):
        self._lock = threading.Lock()
        self._tenus: dict = {}
        self._fichiers: dict = {}
        self._lock_dir = Path(lock_dir) if lock_dir else None

    # ── Acquisition ───────────────────────────────────────────────────────────

    def acquire(self, nom: str, proprietaire: str) -> Token:
        """
        Réclame une ressource pour ``proprietaire``.

        Raises:
            ResourceBusy: si elle est déjà tenue — dans ce processus ou dans un
                autre. Le message nomme le détenteur, parce que « ressource
                occupée » sans plus n'aide personne à 2 h du matin.
        """
        if nom not in RESSOURCES:
            raise ValueError(f"ressource inconnue : {nom!r} (connues : {RESSOURCES})")
        with self._lock:
            deja = self._tenus.get(nom)
            if deja is not None and deja.actif:
                raise ResourceBusy(
                    f"« {nom} » est déjà tenue par « {deja.proprietaire} ». "
                    f"Un seul composant possède chaque ressource matérielle.")
            self._verrou_systeme(nom, proprietaire)
            token = Token(self, nom, proprietaire)
            self._tenus[nom] = token
            log.info("ressource « %s » → %s", nom, proprietaire)
            return token

    def _verrou_systeme(self, nom: str, proprietaire: str) -> None:
        """Pose un ``flock`` non bloquant, s'il y a un dossier de verrous."""
        if self._lock_dir is None:
            return
        import fcntl  # noqa: PLC0415 — absent sur Windows, présent partout ici

        self._lock_dir.mkdir(parents=True, exist_ok=True)
        chemin = self._lock_dir / f"vtctl-{nom}.lock"
        try:
            fh = self._ouvrir_verrou(chemin)
        except PermissionError as e:
            # Le fichier existe et appartient à quelqu'un d'autre — typiquement
            # à root, parce que la main demande les droits root et que le même
            # dossier sert aux deux. Refuser ici serait absurde : on ne sait
            # rien de l'occupation réelle. On le dit, et on continue avec le
            # seul verrou en mémoire.
            log.warning("verrou %s inaccessible (%s) : protection inter-processus "
                        "désactivée pour « %s ». Corriger avec "
                        "« sudo chmod 666 %s ».", chemin, e.strerror, nom, chemin)
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise ResourceBusy(
                f"« {nom} » est tenue par un autre processus (verrou {chemin}). "
                f"Un seul vtctl à la fois peut piloter le banc.") from e
        try:
            fh.truncate(0)
            fh.write(f"{os.getpid()} {proprietaire}\n")
            fh.flush()
        except OSError:
            pass                    # le verrou tient, le contenu n'est qu'indicatif
        self._fichiers[nom] = fh

    @staticmethod
    def _ouvrir_verrou(chemin: Path):
        """
        Ouvre le fichier de verrou, en le laissant accessible aux autres comptes.

        Le banc se pilote tantôt en root — le maître EtherCAT ouvre des sockets
        raw — tantôt sous le compte courant pour la caméra et le plateau. Les
        deux doivent voir le **même** verrou, sans quoi ils se croiraient seuls
        et ouvriraient le même appareil. D'où le mode 666 posé explicitement :
        celui qui crée le fichier ne doit pas en priver l'autre.
        """
        neuf = not chemin.exists()
        fd = os.open(chemin, os.O_RDWR | os.O_CREAT, 0o666)
        if neuf:
            try:
                os.fchmod(fd, 0o666)     # l'umask a pu retirer des droits
            except OSError:
                pass
        return os.fdopen(fd, "r+")

    def _liberer(self, nom: str, token: Token) -> None:
        with self._lock:
            if self._tenus.get(nom) is token:
                del self._tenus[nom]
            fh = self._fichiers.pop(nom, None)
            if fh is not None and not fh.closed:
                # Fermer le descripteur relâche le flock : inutile de le faire
                # explicitement, et le faire séparément ouvrirait une fenêtre
                # où le fichier est déverrouillé mais encore ouvert.
                fh.close()
            log.info("ressource « %s » libérée par %s", nom, token.proprietaire)

    # ── État ──────────────────────────────────────────────────────────────────

    def tenues(self) -> dict:
        """Qui tient quoi, en ce moment."""
        with self._lock:
            return {n: t.proprietaire for n, t in self._tenus.items() if t.actif}

    def libre(self, nom: str) -> bool:
        with self._lock:
            t = self._tenus.get(nom)
            return t is None or not t.actif

    def release_all(self) -> None:
        """Libère tout. Appelé à la clôture, et sur signal."""
        for token in list(self._tenus.values()):
            token.release()


# ── Démons concurrents ────────────────────────────────────────────────────────

#: Motif des processus qui tiennent le maître EtherCAT sans poser de verrou.
#: Les crochets dans ``py3noca[p]`` empêchent ``pgrep`` de se trouver lui-même
#: — piège classique, et qui fait croire à un démon présent en permanence.
MOTIF_DEMONS = r"lhandpro|py3noca[p]"


def demons_concurrents() -> list:
    """
    Les processus qui tiennent le bus EtherCAT en dehors de nous.

    ``lhandpro_service`` et ``lhandpro_web`` survivent volontiers à leur
    utilité : observés le 2026-08-18 tournant depuis deux heures sur une
    interface réseau disparue, à 82 % de CPU invité.

    Returns:
        une liste de ``{"pid": int, "cmd": str}``, vide si tout va bien.
    """
    try:
        sortie = subprocess.run(
            ["pgrep", "-af", MOTIF_DEMONS],
            capture_output=True, text=True, timeout=5, check=False).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []                       # pas de pgrep : on ne peut pas savoir
    trouves = []
    for ligne in sortie.splitlines():
        pid, _, cmd = ligne.partition(" ")
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        if "pgrep" in cmd:
            continue
        trouves.append({"pid": int(pid), "cmd": cmd.strip()})
    return trouves
