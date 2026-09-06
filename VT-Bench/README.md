# VT-Bench — caractériser le matériel DH116

Repart de zéro : **rien** de ce qui a été conclu ailleurs n'est présupposé ici.
Le seul intrant est le SDK constructeur ré-extrait dans
`../Leadshine_SDK_original/`, utilisé pour monter le bus et pour interroger son
API tactile. Le contenu des trames n'est pas décodé, il est **mesuré**.

C'est un choix de méthode, pas une coquetterie : une caractérisation qui part
d'un mapping supposé ne peut que confirmer ce mapping. Ici la structure de la
charge utile est déduite par autocorrélation du motif d'octets actifs. Si elle
ressort, tant mieux ; si elle ne ressort pas, c'est un résultat.

## Ce que le banc mesure

**Santé du flux** — cadence de lecture, cadence de trames *neuves*, taux de
fraîcheur, intervalle médian et p95, plus longue interruption. C'est là que se
verrait un problème d'hôte : si la main émet régulièrement mais que la VM relit
dix fois la même trame, la cadence de lecture reste haute pendant que le taux de
fraîcheur s'effondre. Les deux chiffres sont séparés exprès.

**Comportement de chaque octet au repos** — moyenne, écart-type, min, max,
nombre de valeurs distinctes, dérive entre le début et la fin de la fenêtre.
Verdict par octet : mort, figé, stable (σ ≤ 1 LSB), bruyant, très bruyant. Un
octet mort et un octet figé sont deux pannes différentes.

**Structure déduite** — périodicité du motif d'octets actifs, avec son score.

**API tactile du SDK** — les cinq lectures (`pressure`, `normal_force`,
`tangential_force`, `force_direction`, `proximity`) pour les onze ids déclarés
dans le manuel, **codes d'erreur conservés**. Un id qui répond toujours la même
valeur et un id qui renvoie toujours une erreur sont deux diagnostics distincts.

**Réponse à un stimulus** — z-score de chaque octet entre repos et sollicitation,
zone par zone. Un octet ne compte comme réactif que s'il bouge nettement plus
que son propre bruit au repos.

## Protocole

Quatre exécutions : (alimentation A, B) × (interface 1, 2). Même batterie à
chaque fois, donc toute différence vient du matériel.

```bash
# dans la VM, en root — le maître EtherCAT ouvre des sockets raw
cd ~/VT-Bench
python3 selftest.py                       # 2 s, sans matériel, à faire une fois

sudo python3 -m bench.run --label alimA-nic1
sudo python3 -m bench.run --label alimA-nic2
sudo python3 -m bench.run --label alimB-nic1
sudo python3 -m bench.run --label alimB-nic2

python3 -m bench.compare runs/*.json
python3 -m bench.compare runs/*.json --bytes   # désaccords octet par octet
```

Une exécution complète prend une dizaine de minutes, dont 60 s de repos
automatique et deux séries guidées. Pour un premier tour rapide, les phases
guidées se sautent :

```bash
sudo python3 -m bench.run --label alimA-nic1 --skip-touch --skip-proximity --rest 30
```

Les phases automatiques (0 à 2) suffisent à comparer les configurations. Les
phases guidées, elles, ne servent qu'une fois la meilleure configuration
retenue — inutile de refaire onze zones quatre fois.

### Déroulé

| phase | durée | contenu | présence requise |
|---:|---:|:--|:--|
| 0 | ~5 s | hôte, lien, esclaves, symboles réellement exportés par la `.so` | non |
| 1 | 5 s | ce que la main émet **avant** `set_tpdo_frame_type` | non |
| 2 | 60 s | repos : bruit, dérive, octets morts, santé du flux, API du SDK | **ne pas toucher** |
| 3 | ~2 min | approche **sans contact** — pouce, index, paume | oui |
| 4 | ~7 min | appui franc, 11 zones dont les 5 pulpes | oui |

Phase 3 : approcher la main à 1-2 cm **sans toucher**. C'est le seul test qui
peut isoler les capteurs de proximité — s'ils remontent quelque chose, des
octets bougeront sans qu'aucune pression ne soit appliquée. Si rien ne bouge
pendant l'approche mais que tout bouge au contact, la proximité n'est pas
exploitable sur ce bus, et ce sera établi plutôt que supposé.

Phase 4 : les pulpes figurent dans la liste **exprès**. On veut constater
qu'elles ne remontent rien, pas le déduire d'une lecture de code.

## Lire les résultats

Chaque exécution écrit `runs/<label>.json` (tout, y compris le détail par octet)
et `runs/<label>.md` (le résumé lisible). `bench.compare` marque d'un `≠` toute
ligne qui diffère entre configurations ; le reste est identique et n'appelle pas
de discussion.

Trois lectures utiles :

- **le nombre d'octets morts change avec l'alimentation** → c'est
  l'alimentation, et le choix est fait ;
- **il ne change pas mais le taux de fraîcheur ou le p95 change avec
  l'interface** → c'est le lien ou l'hôte, pas les capteurs ;
- **rien ne change** → les défauts sont dans la main, et on sait alors qu'il ne
  sert à rien de continuer à chercher du côté du montage.

Pour la question de la VM : regarder `Taux de fraîcheur`, `Intervalle p95` et
`Plus longue coupure`. Une VM qui gêne se voit là, et nulle part ailleurs. Si le
taux de fraîcheur est proche de 1 et le p95 proche de la médiane, l'hôte suit
sans peine et la virtualisation est hors de cause.

## Contenu

| chemin | rôle |
|:--|:--|
| `bench/sdk.py` | accès à la main via le SDK, aucune interprétation des trames |
| `bench/metrics.py` | statistiques sans hypothèse sur le contenu |
| `bench/run.py` | la batterie, une exécution par configuration |
| `bench/compare.py` | mise côte à côte de plusieurs exécutions |
| `selftest.py` | vérifie la chaîne de mesure sur des trames fabriquées |

`bench/sdk.py` localise le SDK tout seul (architecture comprise). Si votre copie
est ailleurs : `--sdk-dir /chemin/vers/sdk_lib`.

## Trois écarts du wrapper Python de Leadshine, corrigés ici

Chacun est capable de rendre la main muette sans message d'erreur, et le banc
les signale au lieu de les subir :

1. `PyLHandProLib` n'expose pas `set_tpdo_frame_type` alors que le symbole C
   existe. Un appel naïf lève `AttributeError` ; enveloppé dans un `try/except`,
   il donne une main silencieuse. On lie le symbole par ctypes.
2. `lhandprolib_loader` déclare des prototypes pour des symboles absents de
   certaines versions de la `.so`. La phase 0 liste ce qui est **réellement**
   exporté.
3. Le manuel v1.4 documente `set_finger_pressure_reset(int sensor_id)` ; la
   `.so` livrée exporte la version sans argument. Le banc sonde les deux et
   rapporte laquelle a répondu — manuel et binaire ne sont pas de la même
   version, c'est bon à savoir avant de faire confiance au manuel.
