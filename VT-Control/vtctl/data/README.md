# Données embarquées

## `DH116-L000-A1.urdf`

Le modèle cinématique de la main, copié depuis
`RealDH116/src/lhandpro_description/urdf/`.

**Pourquoi une copie plutôt qu'un chemin vers le dépôt voisin.** VT-Control est
déployé seul dans la machine virtuelle ; `RealDH116` ne l'est pas. Un modèle
dont dépend l'acquisition ne peut pas vivre dans un dépôt qui n'est pas là au
moment de s'en servir — et la panne se manifesterait au pire moment, en pleine
session, sous forme de poses simplement absentes.

La copie fige aussi le modèle avec le code qui l'utilise : si la géométrie
change, les sessions passées gardent celle qui les a produites.

Seuls les liaisons, origines et axes sont lus ; les maillages référencés ne sont
pas nécessaires et ne sont pas copiés.
