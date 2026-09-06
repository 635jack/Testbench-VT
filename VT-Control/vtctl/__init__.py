"""
VT-Control — supervision et orchestration de l'acquisition visuo-tactile.

N'embarque aucun pilote : les briques existantes de ``VT-Tactile``,
``VT-Light`` et ``Control_Turtable_IR`` sont importées telles quelles, et ce
dépôt n'ajoute que ce qui leur manquait — un propriétaire unique par ressource
matérielle, une machine à états explicite, un journal qui survit au plantage,
et une interface.
"""

__version__ = "0.1.0"
