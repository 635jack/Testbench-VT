#!/usr/bin/env python3
"""
session.py — le dossier d'une acquisition, et son index.

Format **version 2**. Il étend celui de ``vt_tactile.dataset`` (version 1) sur
trois points, chacun venu d'un défaut constaté :

1. **Un journal append-only.** Le manifeste de la version 1 n'était écrit qu'au
   ``close()`` final : deux sessions sur 39 n'en ont pas. Ici le manifeste est
   un résumé, et ``recover.py`` sait le reconstruire depuis le journal.

2. **Une ancre d'horloge.** La version 1 horodate tout en ``perf_counter``
   relatif, sans jamais l'attacher à une date. Un tel ``t`` n'a de sens que
   dans le processus qui l'a pris : deux sessions ne sont plus comparables.
   On écrit donc une fois le triplet ``(monotone, unix, iso)``.

3. **L'horodatage est pris à l'acquisition**, pas à l'écriture. En version 1,
   ``save_frame`` prenait son ``t`` *après* ``cv2.imwrite`` : le temps
   d'encodage PNG s'ajoutait au délai. Ici l'appelant passe le ``t`` relevé au
   retour de ``grab()``.

Ce qui ne change pas, et ne doit pas changer : le flux brut passe par
``vt_tactile.dataset.write_raw_stream``, unique implémentation de sa
persistance. Deux écritures divergentes du même flux seraient le meilleur moyen
de rendre les sessions incomparables.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .journal import Journal

FORMAT_VERSION = 2

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


class Session:
    """
    Un dossier de session, et tout ce qu'on y écrit.

    L'horloge de référence est ``time.perf_counter`` prise à la création : tous
    les ``t`` du manifeste sont des secondes depuis ce zéro.
    """

    def __init__(self, root: Path, objet: str, meta: "dict | None" = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._t0 = time.perf_counter()
        self.objet = objet
        self.images: list = []
        self.raw: list = []
        self.marks: list = []
        self.captures: list = []
        self.meta = dict(meta or {})
        self.meta.update({
            "format_version": FORMAT_VERSION,
            "objet": {"nom": objet},
            "horloge": self._ancre(),
            "provenance": _provenance(),
        })
        self.journal = Journal(self.root / "journal.jsonl", self._t0)
        self._ecrire_session_json()
        self.journal.ecrire("session_ouverte", objet=objet, racine=str(self.root))

    @classmethod
    def create(cls, base, objet: str, meta: "dict | None" = None) -> "Session":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        nom = "".join(c if c.isalnum() or c in "-_" else "_" for c in objet) or "objet"
        return cls(Path(base) / f"{stamp}_{nom}", objet, meta)

    # ── Horloge ───────────────────────────────────────────────────────────────

    def _ancre(self) -> dict:
        """
        Le triplet qui rend les ``t`` interprétables hors du processus.

        Sans lui, un ``perf_counter`` relatif ne dit ni quand la session a eu
        lieu ni comment la recoller à une autre. C'est écrit une seule fois,
        au tout début, et jamais recalculé.
        """
        unix = time.time()
        return {
            "source": "time.perf_counter",
            "t0_monotonic": round(self._t0, 6),
            "t0_unix": round(unix, 6),
            "t0_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(unix))
                      + _offset_utc(unix),
        }

    @property
    def t(self) -> float:
        """Secondes depuis le début de la session."""
        return round(time.perf_counter() - self._t0, 4)

    def rel(self, t_perf: float) -> float:
        """Ramène un ``time.perf_counter`` sur l'horloge de la session."""
        return round(t_perf - self._t0, 4)

    # ── Écriture ──────────────────────────────────────────────────────────────

    def save_image(self, etape: str, index: int, color, depth,
                   t: "float | None" = None, **extra) -> dict:
        """
        Enregistre une image couleur et sa profondeur.

        Args:
            t: l'instant de la **prise de vue**, sur l'horloge de session. À
                relever au retour de ``grab()`` — pas ici : l'encodage PNG
                coûte des dizaines de millisecondes, et les mettre dans
                l'horodatage revient à décaler l'image de ce délai.

        La profondeur part en ``.npy`` et non en image : c'est de l'entier
        16 bits en unités capteur (dixièmes de millimètre sur la D405), et la
        convertir pour l'affichage détruirait la mesure.
        """
        if cv2 is None:
            raise RuntimeError("cv2 absent : impossible d'écrire les images.")
        d = self.root / etape
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{index:04d}"
        cv2.imwrite(str(d / f"{stem}_color.png"), color)
        if depth is not None:
            np.save(d / f"{stem}_depth.npy", np.asarray(depth))

        entree = {
            "t": self.t if t is None else round(float(t), 4),
            "t_ecriture": self.t,
            "etape": etape,
            "index": index,
            "color": f"{etape}/{stem}_color.png",
            "depth": f"{etape}/{stem}_depth.npy" if depth is not None else None,
        }
        entree.update(extra)
        self.images.append(entree)
        self.journal.ecrire("image", **entree)
        return entree

    def save_raw_stream(self, etape: str, frames) -> dict:
        """
        Enregistre **toutes** les trames reçues, sans filtrage.

        Délègue à ``vt_tactile.dataset.write_raw_stream``, unique
        implémentation de la persistance du flux brut. Aucune déduplication :
        deux trames identiques sont deux trames. Au repos la charge utile ne
        bouge pas d'un octet pendant des secondes — dédupliquer effacerait ces
        périodes, qui sont précisément la ligne de base dont dépend tout
        décodage ultérieur.
        """
        from vt_tactile.dataset import write_raw_stream  # noqa: PLC0415

        idx = len(self.raw)
        entree = write_raw_stream(self.root / etape, f"stream_{idx:02d}",
                                  frames, rel=self.rel)
        if not entree:
            self.journal.ecrire("flux_brut_vide", etape=etape)
            return {}
        entree["data"] = f"{etape}/{Path(entree['data']).name}"
        entree["times"] = f"{etape}/{Path(entree['times']).name}"
        # Surtout pas de clé « kind » ici : le journal en pose déjà une, et la
        # collision fait échouer l'écriture au moment précis où l'on tient le
        # flux brut — la seule pièce qui ne se rejoue pas.
        entree.update({"t": self.t, "etape": etape, "type": "flux_brut"})
        self.raw.append(entree)
        self.journal.ecrire("flux_brut", **entree)
        return entree

    def write_json(self, relpath: str, payload: dict) -> Path:
        """Écrit un petit fichier JSON dans la session, et le journalise."""
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                                default=_json_safe), encoding="utf-8")
        self.journal.ecrire("json", chemin=relpath, contenu=payload)
        return p

    def mark(self, nom: str, **extra) -> dict:
        """
        Pose un repère horodaté.

        Le flux brut est continu et ignore les étapes ; ce sont ces repères qui
        permettent de retrouver après coup où commence la fermeture, où le
        pouce s'est posé, où la main s'est rouverte.
        """
        entree = {"t": self.t, "nom": nom, **extra}
        self.marks.append(entree)
        self.journal.ecrire("mark", **entree)
        return entree

    def capture(self, etape: str, payload: dict) -> dict:
        """Enregistre l'issue d'une capture : validation, statut du pouce, angle."""
        entree = {"t": self.t, "etape": etape, **payload}
        self.captures.append(entree)
        self.write_json(f"{etape}/capture.json", entree)
        return entree

    def valider(self, etape: str, validation: str, par: str = "operateur",
                commentaire: str = "") -> dict:
        """
        Valide ou invalide une capture déjà écrite.

        La validation est **révisable** : elle réécrit ``capture.json`` et
        journalise le changement, de sorte que l'historique reste dans le
        journal même si le fichier ne montre que le dernier état.
        """
        if validation not in ("valide", "invalide", "en_attente"):
            raise ValueError(f"validation inconnue : {validation!r}")
        for c in self.captures:
            if c["etape"] == etape:
                c.update({"validation": validation, "valide_par": par,
                          "valide_t": self.t, "commentaire": commentaire})
                self.write_json(f"{etape}/capture.json", c)
                self.journal.ecrire("validation", etape=etape,
                                    validation=validation, par=par,
                                    commentaire=commentaire)
                return c
        raise KeyError(f"aucune capture à l'étape {etape!r}")

    def note(self, cle: str, valeur) -> None:
        self.meta[cle] = valeur
        self.journal.ecrire("note", cle=cle, valeur=valeur)
        self._ecrire_session_json()

    # ── Clôture ───────────────────────────────────────────────────────────────

    def _ecrire_session_json(self) -> None:
        (self.root / "session.json").write_text(
            json.dumps(self.meta, indent=2, ensure_ascii=False, default=_json_safe),
            encoding="utf-8")

    def manifeste(self, complet: bool = True) -> dict:
        return {
            "meta": self.meta,
            "complet": complet,
            "images": self.images,
            "raw_tpdo": self.raw,
            "marks": self.marks,
            "captures": self.captures,
            "compteurs": {
                "images": len(self.images),
                "blocs_bruts": len(self.raw),
                "trames_brutes": sum(r.get("count", 0) for r in self.raw),
                "captures": len(self.captures),
                "validees": sum(1 for c in self.captures
                                if c.get("validation") == "valide"),
            },
        }

    def close(self, complet: bool = True) -> Path:
        """Écrit le manifeste et ferme le journal. À appeler quoi qu'il arrive."""
        chemin = self.root / "manifest.json"
        chemin.write_text(json.dumps(self.manifeste(complet), indent=2,
                                     ensure_ascii=False, default=_json_safe),
                          encoding="utf-8")
        self.journal.ecrire("session_fermee", complet=complet,
                            **self.manifeste(complet)["compteurs"])
        self.journal.close()
        return chemin


def _provenance() -> dict:
    """
    De quoi savoir **quel logiciel** a écrit cette session, et avec quelle table.

    Deux choses qui se perdent silencieusement :

    **La version du code.** Sans elle, un défaut découvert plus tard ne peut pas
    être rapporté aux sessions qu'il a touchées : on ne sait pas lesquelles ont
    été acquises avant le correctif.

    **La table de découpage des trames.** Les octets bruts sont conservés et
    restent redécodables, mais les valeurs *déjà décodées* dans les images et
    les captures dépendent de cette table — et elle a déjà changé une fois. La
    consigner permet de savoir quel découpage a produit les valeurs écrites, et
    donc de les refaire si le découpage évolue encore.
    """
    infos: dict = {}
    try:
        from .. import __version__  # noqa: PLC0415

        infos["vtctl"] = __version__
    except Exception:  # noqa: BLE001
        pass

    try:
        import subprocess  # noqa: PLC0415

        racine = Path(__file__).resolve().parents[2]
        for nom, depot in (("vt_control", racine / "VT-Control"),
                           ("vt_tactile", racine / "VT-Tactile"),
                           ("control_turntable", racine / "Control_Turtable_IR")):
            if not (depot / ".git").exists():
                continue
            r = subprocess.run(["git", "-C", str(depot), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=5, check=False)
            if r.returncode == 0:
                infos[nom] = r.stdout.strip()
    except Exception:  # noqa: BLE001 — l'absence de git n'empêche pas d'acquérir
        pass

    try:
        from vt_tactile import tpdo  # noqa: PLC0415

        infos["table_tpdo"] = {
            "taille_trame": tpdo.TPDO_SIZE,
            "creneau": tpdo.SLOT_SIZE,
            "offset_charge": tpdo.PAYLOAD_OFFSET,
            "force_normale": dict(tpdo.NF),
            "force_tangentielle": dict(tpdo.TF),
            "proximite": dict(tpdo.PROX),
            "creneau_moteur": {"base": tpdo.MOTOR_SLOT_BASE,
                               "taille": tpdo.MOTOR_SLOT_SIZE},
            "zones": list(tpdo.ZONE_NAMES),
        }
    except Exception:  # noqa: BLE001
        pass
    return infos


def _offset_utc(unix: float) -> str:
    """Décalage local en ``+HH:MM``, pour que la date soit sans ambiguïté."""
    ecart = (time.localtime(unix).tm_gmtoff or 0)
    signe = "+" if ecart >= 0 else "-"
    ecart = abs(ecart)
    return f"{signe}{ecart // 3600:02d}:{(ecart % 3600) // 60:02d}"


def _json_safe(o):
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "tolist"):
        return o.tolist()
    if hasattr(o, "to_dict"):
        return o.to_dict()
    return str(o)
