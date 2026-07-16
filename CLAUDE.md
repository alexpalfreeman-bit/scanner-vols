# CLAUDE.md — scanner-vols

## Contexte du projet
Scanner de prix de vols (origine YUL) : interroge l'API Duffel v2 en HTTP direct,
historise les prix, et envoie des alertes Telegram quand un prix est une aubaine,
un nouveau minimum, ou une possible erreur tarifaire. Objectif : amener le projet
à un niveau de production professionnel (concurrent des services d'alertes de vols).

Un audit complet a été réalisé. **`AUDIT.md` est la source de vérité des tâches** :
exécuter les phases dans l'ordre, cocher les cases au fur et à mesure.

## Conventions
- Code, commentaires, docstrings et messages de commit en **français** (cohérent avec l'existant).
- Python 3.12. Lint/format : `ruff`. Typage : `mypy` (mode strict raisonnable sur `src/`).
- L'argent est TOUJOURS en centimes entiers (`prix_cents: int`) accompagné d'une
  devise explicite. Jamais de `float` pour un montant.
- `detection.py` ne contient que des **fonctions pures** : aucun réseau, aucun I/O,
  aucune lecture d'horloge. Tout est paramètre → 100 % testable.
- Toute correction de bug s'accompagne d'un test qui échouait avant le fix.
- Dépendances épinglées dans `pyproject.toml` (+ lockfile). Jamais de `>=` seul.
- Timestamps : ISO 8601 UTC, précision à la seconde, un timestamp par observation.

## Commandes
- Installer (dev) : `pip install -e ".[dev]"`
- Tests : `pytest -q`
- Lint : `ruff check . && ruff format --check .`
- Types : `mypy src/`
- Lancer le scan : `python -m scanner_vols` (avant refactor : `python scanner.py`)

## Garde-fous (IMPORTANT)
- Ne JAMAIS lire, afficher, committer ou modifier `.env`. Aucun secret en dur,
  nulle part (code, tests, fixtures, messages de commit).
- Ne JAMAIS lancer de vraies requêtes réseau (Duffel, Telegram) depuis les tests :
  tout passe par des mocks/fixtures locales.
- Ne pas supprimer `data/history.csv` tant que la migration SQLite n'est pas
  validée par un test de migration (mêmes totaux, mêmes lignes).
- Pas de nouvelle dépendance sans justification dans le message de commit.
- Travailler sur la branche `refactor-audit`. Ne jamais committer sur `main`.
- Un commit atomique par bloc logique, message en français expliquant le *pourquoi*.

## Architecture cible (détail en Phase 2 d'AUDIT.md)
```
src/scanner_vols/
├── config.py        # pydantic-settings : validation env + config au démarrage
├── providers/       # base.py (Protocol), duffel.py (+ amadeus.py plus tard)
├── storage.py       # SQLite (SQL portable vers Postgres), pattern repository
├── detection.py     # fonctions pures : z-score MAD, classification, corroboration
├── alerting.py      # Telegram : échappement HTML, déduplication, digest erreurs
└── cli.py           # point d'entrée + __main__.py
tests/               # test_detection.py, test_storage.py, fixtures/
```

## Definition of done (pour CHAQUE phase)
1. `ruff check .`, `mypy src/` et `pytest -q` passent tous, sans avertissement ignoré.
2. Toutes les cases de la phase sont cochées dans `AUDIT.md`.
3. Les commits sont atomiques et poussés sur `refactor-audit`.
4. Un court résumé de ce qui a été fait + décisions prises est ajouté en bas
   d'`AUDIT.md` (section Journal).