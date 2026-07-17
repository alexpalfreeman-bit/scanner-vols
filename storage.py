"""
Stockage de l'historique des prix.
-------------------------------------
Interface repository : lire_historique() / ajouter_historique(ligne) restent
stables independamment du backend (CSV pour l'instant, SQLite en Phase 2.2 -
voir AUDIT.md), pour que scanner.py et detection.py n'aient pas a changer
une deuxieme fois.
"""

import csv
from pathlib import Path

BASE_DIR = Path(__file__).parent
HISTORY_FILE = BASE_DIR / "data" / "history.csv"

COLONNES = [
    "horodatage_utc",
    "origine",
    "destination",
    "date_depart",
    "date_retour",
    "prix",
    "devise",
    "compagnie",
    "escales",
]


def lire_historique() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ajouter_historique(ligne: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    nouveau_fichier = not HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLONNES)
        if nouveau_fichier:
            writer.writeheader()
        writer.writerow(ligne)
