# AUDIT.md — Plan d'exécution de l'audit (source de vérité)

> **Contrat d'exécution pour Claude Code**
> 1. Exécuter les phases **dans l'ordre** (0 → 1 → 2). Ne pas entamer une phase
>    tant que la précédente ne satisfait pas la « definition of done » de CLAUDE.md.
> 2. Cocher chaque case `[ ]` → `[x]` au fur et à mesure, dans ce fichier.
> 3. En cas d'ambiguïté : choisir l'option la plus simple qui respecte les
>    conventions, et documenter la décision dans le Journal (bas de fichier).
> 4. La Phase 3 est un backlog : **ne pas l'exécuter** sans demande explicite.
> 5. Les extraits de code ci-dessous sont des implémentations de référence :
>    les adapter au contexte, pas les coller aveuglément.

---

## Phase 0 — Fondations de l'outillage

- [x] Créer la branche `refactor-audit` depuis `main`.
- [x] Créer `pyproject.toml` : métadonnées du projet, `requires-python = ">=3.12"`,
      dépendances épinglées aux dernières versions stables (requests, PyYAML,
      python-dotenv, pydantic, pydantic-settings) et extra `[dev]`
      (pytest, ruff, mypy, pre-commit, types-requests, types-PyYAML).
      Supprimer `requirements.txt` une fois l'équivalence vérifiée.
- [x] Configurer `ruff` (lint + format) et `mypy` dans `pyproject.toml`.
- [x] Ajouter `.pre-commit-config.yaml` : ruff, ruff-format, gitleaks (anti-secrets).
- [x] Créer `.github/workflows/ci.yml` : sur push/PR → ruff → mypy → pytest.
- [x] Durcir `.github/workflows/scan.yml` :
      - bloc `concurrency:` (groupe unique, `cancel-in-progress: false`) ;
      - `timeout-minutes: 15` sur le job ;
      - `git pull --rebase` avant le `git push` de l'historique ;
      - politique de sortie : échec du run seulement si TOUTES les routes ont
        échoué ; sinon succès + résumé des erreurs dans le job summary.

**Critères d'acceptation** : `pip install -e ".[dev]"` fonctionne ; `ruff`, `mypy`,
`pytest` s'exécutent (même avec 0 test) ; CI verte sur la branche.

---

## Phase 1 — Corrections de bugs (chaque fix = au moins 1 test)

### 1.1 Bug devise (CRITIQUE) ✅
**Problème** : `config.yaml` déclare `devise: CAD` mais la clé n'est jamais lue.
Duffel renvoie la devise du compte (USD dans l'historique actuel). Conséquences :
`prix_max` (pensé en CAD) est comparé à des montants USD, et
`statistiques_destination()` mélangerait des devises différentes sans erreur.
**Correctif attendu** :
- lire `config["devise"]` et vérifier à chaque réponse API que
  `total_currency` correspond ; sinon, logger un avertissement explicite et
  exclure l'observation des comparaisons de seuil ;
- filtrer toutes les statistiques historiques par devise ;
- documenter dans le README que la devise effective dépend du compte Duffel.
**Test** : un historique mêlant USD et CAD → les stats n'utilisent que la devise
demandée ; un `prix_max` n'est comparé qu'à un prix de même devise.

### 1.2 Alerte « nouveau minimum » sans garde ✅
**Problème** : l'alerte se déclenche dès que `prix < min historique`, sans taille
d'échantillon minimale ni marge → spam pendant les premières semaines (la rotation
des dates rend les premiers runs pleins de « nouveaux minimums »).
**Correctif** : ne déclencher que si `n >= detection.echantillon_min` ET
`prix < min * 0.97` (marge de 3 %, configurable `marge_minimum_pct`).
**Test** : n=3 → pas d'alerte même si prix plus bas ; n=6 et prix à 99 % du min
→ pas d'alerte ; n=6 et prix à 95 % du min → alerte.

### 1.3 `r.json()` dans la branche d'erreur ✅
**Problème** : si l'API renvoie une erreur non-JSON (page HTML d'un 502),
`r.json()` lève `JSONDecodeError` et masque l'erreur HTTP réelle.
**Correctif** : envelopper le parsing d'erreur ; en cas d'échec, remonter
`HTTP {status_code}` + les 200 premiers caractères du corps.
**Test** : réponse 502 avec corps HTML → message d'erreur contenant « 502 ».

### 1.4 Aucun retry / gestion du rate limiting ✅
**Problème** : une erreur transitoire (timeout, 429, 5xx) = un point de donnée
perdu ; `time.sleep(0.25)` fixe n'exploite pas `Retry-After`.
**Correctif** : implémentation de référence à intégrer (module `providers/`) :

```python
import random, time, requests

RETRIABLES = {429, 500, 502, 503, 504}

def post_resilient(session: requests.Session, url: str, corps: dict,
                   essais: int = 4) -> requests.Response:
    derniere_erreur: Exception = RuntimeError("aucune tentative")
    for tentative in range(essais):
        attente = (2 ** tentative) + random.uniform(0, 1)  # 1-2s, 2-3s, 4-5s…
        try:
            r = session.post(url, json=corps, timeout=30)
            if r.status_code not in RETRIABLES:
                return r  # succès OU erreur non transitoire (4xx) traitée en amont
            attente = max(attente, float(r.headers.get("Retry-After", 0)))
            derniere_erreur = RuntimeError(f"HTTP {r.status_code} sur {url}")
        except requests.RequestException as e:  # timeout, DNS, connexion
            derniere_erreur = e
        if tentative < essais - 1:
            time.sleep(attente)
    raise derniere_erreur
```
**Tests** (réseau mocké) : 429 avec `Retry-After` → attente respectée puis succès ;
4 échecs consécutifs → exception remontée ; 404 → retour immédiat sans retry.

### 1.5 Injection HTML dans Telegram ✅
**Problème** : `parse_mode=HTML` sans échappement des champs dynamiques
(compagnie, etc.) → un `&` ou `<` casse le message et l'alerte est perdue.
**Correctif** : `html.escape()` sur toute valeur interpolée dans le message.
**Test** : compagnie `"A&B <Air>"` → message contenant `A&amp;B &lt;Air&gt;`.

### 1.6 Horodatage ✅
**Problème** : timestamp calculé une seule fois par run, précision à la minute.
**Correctif** : un timestamp ISO 8601 UTC à la seconde **par observation**.
**Test** : deux observations consécutives ont des timestamps distincts et parsables.

### 1.7 Remplacer `print()` par `logging` ✅
Logger structuré (module standard `logging`) : niveau INFO pour le déroulé,
WARNING/ERROR pour les anomalies. Sortie lisible dans GitHub Actions.

### 1.8 Validation de la configuration au démarrage ✅
`os.environ["X"]` crashe en `KeyError` cryptique. Introduire `config.py` avec
`pydantic-settings` : les 3 variables d'env requises + le chargement/validation
de `config.yaml` (codes IATA à 3 lettres, seuils dans ]0,1[, etc.).
Message d'erreur clair listant ce qui manque.
**Test** : env incomplet → erreur explicite nommant la variable manquante.

### 1.9 Offre la moins chère côté serveur ✅
Vérifier dans la doc Duffel v2 si la liste d'offres embarquée dans la réponse
`offer_requests` peut être tronquée. Si oui, utiliser
`GET /air/offers?offer_request_id=…&sort=total_amount&limit=1`.
Documenter la conclusion dans le code.

**Critères d'acceptation Phase 1** : tous les tests ci-dessus existent et passent ;
le comportement du scanner en fonctionnement nominal est inchangé (mêmes alertes
sur données saines), seuls les cas d'erreur/bord sont corrigés.

---

## Phase 2 — Architecture, stockage, détection

### 2.1 Découpage en modules (layout plat, sans `src/`)
Décision : on garde le layout plat choisi en Phase 0 (voir Journal) — pas de
dossier `src/`, pas de package `scanner_vols/`. Éclater `scanner.py` en
modules au niveau racine du dépôt (`providers/`, `storage.py`, `detection.py`,
`alerting.py`, aux côtés de `config.py` déjà en place depuis 1.8), décrits
dans CLAUDE.md. `scanner.py` devient l'orchestrateur mince et reste le point
d'entrée : `python scanner.py` (inchangé, `scan.yml` n'a pas à changer sur ce
point). Refactor pur, sans changement de comportement, couvert par les tests
de Phase 1 (déplacés vers les fichiers de test correspondant à chaque module).

### 2.2 Stockage SQLite (SQL portable vers Postgres)
Créer `storage.py` (stdlib `sqlite3`, pattern repository, transactions) avec ce schéma :

```sql
CREATE TABLE routes (
  id          INTEGER PRIMARY KEY,
  origine     TEXT NOT NULL,
  destination TEXT NOT NULL,
  UNIQUE (origine, destination)
);

CREATE TABLE observations (
  id            INTEGER PRIMARY KEY,
  route_id      INTEGER NOT NULL REFERENCES routes(id),
  observe_le    TEXT NOT NULL,        -- ISO 8601 UTC, à la seconde
  date_depart   TEXT NOT NULL,
  date_retour   TEXT,
  horizon_jours INTEGER NOT NULL,     -- date_depart - observe_le (en jours)
  prix_cents    INTEGER NOT NULL,     -- jamais de float pour l'argent
  devise        TEXT NOT NULL,
  compagnie     TEXT,
  escales       INTEGER,
  cabine        TEXT NOT NULL DEFAULT 'economy',
  fournisseur   TEXT NOT NULL DEFAULT 'duffel'
);
CREATE INDEX idx_obs_lookup ON observations (route_id, date_depart, observe_le);

CREATE TABLE stats_routes (           -- pré-agrégé : la détection lit ici, O(1)
  route_id        INTEGER NOT NULL,
  mois_depart     TEXT NOT NULL,      -- '01'..'12' : capture la saisonnalité
  tranche_horizon INTEGER NOT NULL,   -- 0 = 0-45 j, 1 = 46-90 j, 2 = 91-180 j, 3 = 180+
  devise          TEXT NOT NULL,
  n               INTEGER NOT NULL,
  p05_cents       INTEGER,
  mediane_cents   INTEGER,
  mad_cents       INTEGER,
  maj_le          TEXT NOT NULL,
  PRIMARY KEY (route_id, mois_depart, tranche_horizon, devise)
);

CREATE TABLE alertes (
  id          INTEGER PRIMARY KEY,
  route_id    INTEGER NOT NULL,
  date_depart TEXT NOT NULL,
  type        TEXT NOT NULL,          -- 'aubaine' | 'erreur_prix' | 'minimum'
  prix_cents  INTEGER NOT NULL,
  envoyee_le  TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_dedup ON alertes (route_id, date_depart, type);
```

- [x] Script `python migrer_csv.py` (layout plat, pas de package `scanner_vols` —
      voir 2.1) : `data/history.csv` → `data/scanner.db` (conversion en cents,
      calcul d'`horizon_jours`). Exécuté pour de vrai sur l'historique réel.
- [x] Test de migration : même nombre de lignes, sommes de contrôle identiques
      (`tests/test_migrer_csv.py`, + validation manuelle sur les 185 lignes
      réelles, voir Journal).
- [ ] `stats_routes` rafraîchie incrémentalement après chaque scan (uniquement
      les segments touchés par les nouvelles observations). **Reporté à la
      Phase 2.3** (décision validée avec l'utilisateur) : le bucketing
      (`mois_depart`/`tranche_horizon`) et les agrégats appartiennent au futur
      moteur de détection, qu'on ne touche pas en 2.2. La table existe
      (schéma créé par `storage.initialiser_db`) mais reste vide.
- [x] Interim assumé : committer `data/scanner.db` depuis le workflow comme
      aujourd'hui le CSV (GitHub Actions n'a pas de disque persistant).
      Le passage à un Postgres géré est en Phase 3. Garder le SQL portable.
- [x] Une fois la migration validée : retirer `data/history.csv` du dépôt.
      Confirmation explicite obtenue de l'utilisateur avant suppression
      (garde-fou CLAUDE.md).

### 2.3 Moteur de détection (`detection.py`, fonctions pures)
Remplacer la médiane toutes-dates-confondues (qui ignore la saisonnalité et
mélange les horizons de réservation) par :

**a) Échantillon comparable** — même route, même mois de départ, horizon similaire,
même devise, 18 derniers mois :

```sql
SELECT prix_cents FROM observations
WHERE route_id = :route
  AND strftime('%m', date_depart) = :mois_depart
  AND horizon_jours BETWEEN :h - 21 AND :h + 21
  AND devise = :devise
  AND observe_le >= datetime('now', '-18 months')
```

Repli hiérarchique si n < 8 : (route+mois+horizon) → (route+mois) → (route).
Tracer le niveau de repli utilisé dans le résultat.

**b) Score robuste** :

```python
from statistics import median

def z_modifie(prix: float, echantillon: list[float]) -> float | None:
    """Z-score modifié (Iglewicz-Hoaglin), insensible aux valeurs aberrantes."""
    if len(echantillon) < 8:
        return None
    med = median(echantillon)
    mad = median(abs(p - med) for p in echantillon)
    if mad == 0:
        return None
    return 0.6745 * (prix - med) / mad

def classifier(prix: float, echantillon: list[float]) -> str:
    z = z_modifie(prix, echantillon)
    if z is None:   return "donnees_insuffisantes"
    if z <= -3.5:   return "candidat_erreur_prix"
    if z <= -2.0:   return "bonne_affaire"
    return "normal"
```

**c) Corroboration des candidats « erreur de prix »** (derrière un flag de config,
car chaque vérification consomme des requêtes) :
1. re-requête immédiate de la même route → le prix se re-cote-t-il ?
2. sonde de 2 dates voisines (±3 jours) → l'anomalie est-elle localisée ?
3. plancher absolu par région (configurable, ex. transatlantique A/R < 25 000 cents) ;
4. vitesse de chute : comparer au dernier prix observé < 48 h si disponible.
Verdict `erreur_prix` seulement si (1) ET au moins un autre signal ; sinon
rétrograder en `bonne_affaire`.

**d) Déduplication et cooldown** : avant envoi, consulter la table `alertes` —
pas de réalerte sur le même (route, date_depart, type) avant 72 h, sauf si le
nouveau prix est < 90 % du prix déjà alerté.

**e) Conserver** les seuils fixes `prix_max` par destination (comportement actuel),
comparés dans la bonne devise (cf. 1.1).

- [x] Tests exhaustifs de `detection.py` : n<8 → insuffisant ; MAD=0 → insuffisant ;
      outlier net → candidat_erreur_prix ; -25 % → bonne_affaire ; repli
      hiérarchique ; cooldown ; corroboration (chaque combinaison de signaux).

### 2.4 Couche fournisseurs
- [x] `providers/base.py` :

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class Offre:
    prix_cents: int
    devise: str
    compagnie: str
    escales: int
    fournisseur: str

class FournisseurVols(Protocol):
    nom: str
    def meilleure_offre(self, route: "Route") -> Offre | None: ...
```

- [x] `providers/duffel.py` : le code actuel, refactoré derrière ce contrat,
      avec `post_resilient` (1.4) et **validation Pydantic de la réponse**
      (un changement de schéma côté Duffel doit lever une erreur explicite,
      jamais produire des données silencieusement fausses).
- [x] Circuit breaker simple par fournisseur : après 5 échecs consécutifs,
      suspendre le fournisseur pour le reste du run et le signaler dans le digest.
- [x] Route canari (ex. YUL→JFK) vérifiée à chaque run + compteur de réponses
      « 0 offres » ; taux anormal → avertissement dans le digest technique.
- [x] Fixture `tests/fixtures/duffel_offer_request.json` : réponse réaliste
      construite d'après le parsing actuel, utilisée par les tests du provider.

### 2.5 Alerting
- [ ] `alerting.py` : formatage (échappement HTML systématique), envoi Telegram
      avec retry léger, et **digest technique** en fin de run (nombre de routes OK /
      en erreur, fournisseurs suspendus) envoyé sur Telegram seulement si erreurs.

**Critères d'acceptation Phase 2** : le scan complet tourne en local sur SQLite ;
`scan.yml` fonctionne avec la nouvelle commande ; couverture de tests de
`detection.py` ≈ 100 % ; aucune régression sur les alertes `prix_max`.

---

## Phase 3 — Backlog produit (NE PAS exécuter sans demande explicite)

- Postgres géré (Neon/Supabase/RDS) via `DATABASE_URL`, partitionnement mensuel
  d'`observations`, archivage Parquet au-delà de 24 mois.
- Passage de GitHub Actions à un worker/scheduler dédié (Render déjà connu) ;
  fréquence adaptative : scan quotidien global + scan horaire des routes
  volatiles ou ayant produit un candidat récent.
- Second fournisseur réel (Amadeus Self-Service) + corroboration croisée ;
  prior de démarrage à froid via l'API de percentiles historiques d'Amadeus.
- Sentry (erreurs), monitoring uptime, budget de coût API/jour avec coupure.
- API FastAPI + comptes utilisateurs + préférences de routes/seuils par user.
- Stripe freemium : gratuit = top deals avec délai 24 h ; payant = alertes
  instantanées + routes personnalisées.
- Conformité Loi 25 (Québec) dès les comptes utilisateurs : politique de
  confidentialité, registre des données, CGU (mention explicite : les error
  fares peuvent être annulées par les compagnies).
- Monétisation immédiate : liens d'affiliation (Travelpayouts/Kiwi) dans les alertes.

---

## Journal (rempli par Claude Code au fil des phases)

### Phase 0 (2026-07-16)

- Branche `refactor-audit` créée depuis `master` (ce dépôt n'a pas de branche
  `main` — partout où l'audit dit "main", lire "master"). Un clone imbriqué
  redondant (`scanner-vols/scanner-vols/`, même remote, branche
  `refactor-audit` sans commit propre) a été supprimé avant de démarrer.
- `pyproject.toml` en layout plat (`scanner.py`/`config.py` via
  `tool.setuptools.py-modules`, pas de `src/` — réservé à la Phase 2).
  Dépendances épinglées en exact, versions vérifiées via `pip freeze` (déps
  existantes) et `pip index versions` (nouvelles) au moment de l'implémentation
  plutôt que devinées.
- mypy configuré "raisonnablement strict" (pas `strict = true`) : le code
  existant utilise des génériques nus partout, activer `strict` aurait exigé
  une réécriture mécanique sans rapport avec les 9 bugs. Plugin
  `pydantic.mypy` ajouté (nécessaire : sans lui, mypy signale à tort le
  constructeur sans argument de `BaseSettings` comme invalide).
- Pas d'outil de lockfile ajouté (pip-tools/uv/poetry) : projet petit, peu de
  dépendances transitives, pins `==` exacts jugés suffisants pour l'instant.
- Deux bugs réels découverts en testant (pas dans le scope d'AUDIT.md, corrigés
  car bloquants) :
  1. Un nouveau fichier de workflow ajouté dans le commit racine de la branche
     n'est pas indexé par GitHub Actions tant qu'un push "normal" ultérieur ne
     le retouche pas (déjà rencontré plus tôt dans l'historique de ce projet).
  2. `git pull --rebase origin master` (codé en dur) rebasait la branche
     courante contre `master` au lieu de sa propre branche distante — cassé
     dès qu'on teste `scan.yml` via `workflow_dispatch` sur une branche autre
     que `master`. Corrigé en `git pull --rebase` (sans argument), qui
     s'appuie sur le suivi de branche déjà configuré par `actions/checkout`
     (même mécanisme que le `git push` existant).
- 4 écarts ruff + 2 erreurs mypy préexistants dans `scanner.py` corrigés au
  passage (variable ambiguë, alias `datetime.UTC`, retour `Any` non typé,
  garde de type sur `stdout`/`stderr.reconfigure`) pour que l'outillage parte
  propre.
- Vérifié : `pip install -e ".[dev]"` fonctionne ; `ruff check`/`ruff format
  --check`/`mypy`/`pytest -q` passent tous ; CI verte sur GitHub
  (`refactor-audit`) ; `scan.yml` durci déclenché via `workflow_dispatch` sur
  `refactor-audit` termine avec succès (après correctif du rebase).

### Phase 1 (2026-07-16 → 2026-07-17)

Ordre d'implémentation : 1.8 → 1.1 → 1.3 → 1.4 → 1.2 → 1.5 → 1.6 → 1.7 → 1.9
(validation de config en fondation d'abord, logging en dernier car il
retouche tous les points d'appel de `main()`).

- **1.8** : `config.py` — `Env` (pydantic-settings) pour les 3 variables
  d'environnement, `ConfigApp`/`Sejour`/`Detection`/`Destination` (pydantic
  `BaseModel`) pour `config.yaml`. `valider_config()` retourne un dict (pas
  l'objet pydantic) : aucune signature existante ailleurs dans `scanner.py`
  n'a changé.
- **1.1 (critique)** : `statistiques_destination()` filtre aussi par devise ;
  `raison_prix_max()` ignore (avec avertissement loggé) un seuil fixe dont la
  devise ne correspond pas à l'offre. Conséquence observée en conditions
  réelles : `MAD`/`CUN` (seuils pensés en CAD) sont maintenant
  avertis-et-ignorés puisque ce compte Duffel renvoie du USD — comportement
  correct, pas une régression.
- **1.2** : `est_nouveau_minimum()` ajoute une garde `echantillon_min` +
  `marge_minimum_pct` (nouvelle clé `config.yaml`, défaut 3 %) — corrige le
  bruit observé en production (16/35 destinations alertées le 2ᵉ jour à cause
  de la rotation des dates).
- **1.3** / **1.4** : `extraire_message_erreur()` (repli propre sur corps non
  JSON) et `post_resilient()` (retry/backoff exponentiel + jitter sur
  429/5xx, respecte `Retry-After`).
- **1.5** : `formater_alerte()` passe tout champ dynamique par `html.escape()`.
- **1.6** : `horodatage_maintenant()` appelée par observation (ISO 8601 UTC,
  précision seconde), plus une seule fois par run.
- **1.7** : `print()` → `logging` (INFO déroulé nominal, WARNING anomalies
  bénignes, ERROR échecs).
- **1.9** : vérifié dans la doc Duffel v2 en direct — `return_offers=true`
  embarque bien "all the offers returned by the airlines" (pas de troncature
  documentée) ; la séparation `return_offers=false` + `GET /air/offers` est
  recommandée par Duffel pour la pagination/tri côté serveur, pas pour la
  fiabilité. Pas de changement de code (doublerait les appels API sans
  problème observé) ; conclusion documentée en docstring, candidat Phase 2
  (`providers/duffel.py`) si la taille de payload devient un jour un
  problème réel.
- Vérification finale : suite `pytest -q` complète verte (27 tests) après
  chaque commit ; un run local complet (`python scanner.py`, 37 destinations)
  confirme le comportement nominal inchangé (aucune erreur, logs
  structurés lisibles) et montre exactement l'avertissement devise attendu
  sur `MAD`/`CUN` — la seule différence de comportement sur données saines,
  et c'est la correction du bug 1.1, pas une régression.

### Phase 2.1 — décision de layout (2026-07-16)

- Confirmation explicite (demande utilisateur) : le layout plat retenu en
  Phase 0 est définitif, pas une étape transitoire. La Phase 2.1 ne migre
  donc pas vers `src/scanner_vols/` comme le décrivait initialement
  CLAUDE.md — `AUDIT.md` §2.1 et l'« Architecture cible » de `CLAUDE.md` ont
  été corrigées en conséquence (modules au niveau racine, `scanner.py` reste
  le point d'entrée `python scanner.py`, pas de `python -m scanner_vols`).
- Corrigé au passage : `CLAUDE.md` référençait encore `mypy src/` (Commandes
  + Definition of done), une commande qui échoue aujourd'hui (`src/`
  n'existe pas). La configuration réelle (`[tool.mypy].files` dans
  `pyproject.toml`, invoquée en pratique par `mypy` sans argument, cf.
  `ci.yml`) était déjà la source de vérité effective depuis la Phase 0 ;
  `CLAUDE.md` ne faisait que ne pas encore le refléter.
- Implémentation de 2.1 et 2.2 pas encore commencée : plan proposé à
  l'utilisateur pour validation avant d'écrire du code.

### Phase 2.1 — découpage en modules (2026-07-16)

- Réalisé en 6 commits atomiques (un par module + un dernier pour le
  câblage) : `providers/duffel.py`, `storage.py` (backend CSV),
  `detection.py`, `alerting.py`, `charger_config` déplacé dans
  `config.py`, puis `scanner.py` réduit à l'orchestration.
- Aucun changement de comportement : tests de Phase 1 déplacés vers les
  fichiers de test dédiés à chaque module. Les tests de `main()` n'ont pas
  eu à changer leurs cibles `@patch("scanner.X")` : `scanner.py` continue
  d'importer ces noms dans son propre espace de noms.
- Ajouts mineurs, dans l'esprit du déplacement mais hors périmètre strict :
  tests de round-trip pour `storage.py` et un test pour `envoyer_telegram`
  (aucun test direct n'existait avant sur ces fonctions).
- Vérifié : `ruff check`, `ruff format --check`, `mypy` et `pytest -q`
  (36 tests) verts ; poussé sur `refactor-audit`.

### Phase 2.2 — stockage SQLite (2026-07-16)

- `storage.py` bascule sur SQLite (schéma exact d'AUDIT.md, `CREATE TABLE
  IF NOT EXISTS` pour rester idempotent). `lire_historique()` /
  `ajouter_historique(ligne)` gardent la même signature et la même forme
  de dict (prix en dollars flottants) qu'avec le backend CSV de 2.1 :
  `scanner.py` et `detection.py` n'ont pas eu à rechanger.
- Trois décisions validées avec l'utilisateur avant l'implémentation :
  1. `stats_routes`/`alertes` : schéma créé maintenant, peuplement reporté
     à la Phase 2.3 (bucketing et agrégats appartiennent au futur moteur
     de détection, pas encore réécrit) ;
  2. argent (règle CLAUDE.md, jamais de float) : conversion appliquée
     uniquement à la frontière de `storage.py` — `scanner.py`/
     `detection.py`/`alerting.py` restent en dollars flottants jusqu'à la
     réécriture du moteur de détection en 2.3, pour éviter de toucher deux
     fois les mêmes signatures ;
  3. `providers/base.py` (Protocol) : pas créé en 2.1, réservé à la
     Phase 2.4 (qui prévoit aussi la validation Pydantic de la réponse).
- `migrer_csv.py` testé sur données synthétiques mélangeant l'ancien
  format d'horodatage (pré-1.6, "AAAA-MM-JJ HH:MM") et le nouveau (ISO
  avec fuseau) — les deux réellement présents dans `data/history.csv` —
  puis exécuté pour de vrai sur les 185 observations réelles. Validation
  indépendante du script (pas juste "les tests passent") : 185 lignes CSV
  = 185 observations en base, somme de contrôle `prix_cents` identique
  par devise (USD : 10 453 427 des deux côtés — tout l'historique actuel
  est en USD, cohérent avec le bug 1.1). Le script refuse de tourner si
  `data/scanner.db` contient déjà des observations (anti double-migration).
- `scan.yml` committe désormais `data/scanner.db` au lieu de
  `data/history.csv`.
- `data/history.csv` retiré du dépôt seulement après confirmation
  explicite de l'utilisateur (garde-fou CLAUDE.md), une fois la migration
  déjà validée par le test ET par l'exécution réelle ci-dessus. Le
  garde-fou correspondant dans CLAUDE.md a été mis à jour en conséquence
  (fait, pas juste "à ne pas faire tant que"). README.md : les mentions de
  `data/history.csv` corrigées en `data/scanner.db` (3 endroits).
- Vérifié : `ruff check`, `ruff format --check`, `mypy` et `pytest -q`
  tous verts ; poussé sur `refactor-audit`.

### Phase 2.3 — moteur de détection (2026-07-17)

Périmètre de cette session, donné explicitement par l'utilisateur : les
fonctions pures de `detection.py` + `tests/test_detection.py` uniquement,
« UNIQUEMENT » — pas `storage.py`, `scanner.py`, `config.py`/`config.yaml`,
`providers/`. Plan présenté et validé avant implémentation (y compris un
arbitrage explicite de l'utilisateur, voir plus bas). Conception
pressure-testée via un agent Plan dédié avant validation.

- **Nouveau moteur ajouté à côté de l'ancien**, pas en remplacement :
  `statistiques_destination`, `est_nouveau_minimum`, `raison_prix_max`
  (Phase 1) restent intouchées et continuent d'être ce que `scanner.py`
  appelle réellement. Le nouveau moteur (`echantillon_comparable`,
  `z_score_modifie`, `classifier`, `corroborer_erreur_prix`, `doit_alerter`,
  helpers associés) n'est **pas câblé** — c'est un contrat testé
  uniquement avec des fixtures synthétiques, comme convenu. Câblage
  (adapter `storage.py` pour exposer `route_id`/`horizon_jours`/
  `prix_cents` nativement, remplacer les appels dans `scanner.py`, mapper
  le vocabulaire `classifier()` ↔ `alertes.type`, brancher un flag de
  config pour la corroboration, peupler `stats_routes`/`alertes`) reste
  entièrement à faire — prochaine session.
- **Argent en centimes entiers** dans tout le nouveau moteur (règle
  CLAUDE.md), ce qui referme la dérogation actée au Journal de la Phase 2.2
  (dollars flottants « jusqu'à la réécriture du moteur de détection en
  2.3 »). Contrat de données déconnecté de la forme actuelle de
  `storage.lire_historique()` : les nouvelles fonctions attendent des
  `dict` miroir des lignes SQL (`route_id`, `horizon_jours`, `prix_cents`,
  `devise`, `observe_le`, `date_depart`), pas les dicts dollars-flottants
  d'aujourd'hui. Décision assumée : plutôt que de faire coller le nouveau
  moteur à une forme de stockage qui doit de toute façon changer au
  câblage, il définit son propre contrat correct dès maintenant.
- **Repli hiérarchique** (`echantillon_comparable`) : route+mois+horizon →
  route+mois → route, arrêt au premier niveau avec n ≥ 8, sinon retombe sur
  `route` avec son n réel (jamais `None`). Fenêtre de 18 mois calculée par
  arithmétique calendaire exacte (`calendar.monthrange`, stdlib) plutôt
  qu'une approximation en jours — pas de dépendance ajoutée
  (`python-dateutil` n'est pas dans le projet).
- **Score robuste** (`z_score_modifie`/`classifier`) : portage direct de la
  référence AUDIT (Iglewicz-Hoaglin, seuils -3.5/-2.0) en centimes entiers,
  seuils/`n_min` paramétrables.
- **Corroboration** (`corroborer_erreur_prix`) : signal 1 (re-requête)
  obligatoire, verdict `erreur_prix` seulement s'il est confirmé **et**
  qu'au moins un des signaux 2/3/4 corrobore. Retourne un
  `ResultatCorroboration` (verdict + chaque signal individuel) plutôt
  qu'une simple string, pour la traçabilité future (logs/message d'alerte).
  **Arbitrage utilisateur explicite** sur le signal 2 (dates voisines ±3
  jours) : l'énoncé d'AUDIT.md (« l'anomalie est-elle localisée ? ») était
  ambigu entre deux lectures opposées ; l'utilisateur a tranché pour
  « voisines aussi anormalement basses = signal confirmant » (une vraie
  erreur tarifaire touche typiquement une plage de dates, pas un jour
  isolé) plutôt que la lecture littérale inverse (voisines à prix normal
  = anomalie confirmée isolée à cette date).
- **Déduplication/cooldown** (`doit_alerter`) : `alerte_precedente` est
  singulière (`dict | None`), pas une liste — l'index `UNIQUE
  idx_dedup (route_id, date_depart, type)` de la table `alertes` garantit
  qu'il ne peut jamais en exister plus d'une pour une clé donnée. Pas de
  garde de cohérence entre `alerte_precedente` et la clé demandée
  (route_id/date_depart/type_alerte) : ce sont des paramètres
  documentaires (la signature décrit la clé) mais l'appelant est réputé
  avoir déjà filtré correctement (ex. clause SQL `WHERE`), cohérent avec
  la consigne CLAUDE.md de ne pas valider l'invalidable.
- **Un seul garde-fou ajouté** : `maintenant` doit être timezone-aware
  (`ValueError` explicite sinon) dans `echantillon_comparable` et
  `doit_alerter` — frontière pur/impur (résultat d'une lecture d'horloge
  faite par l'appelant), pas un « ça ne peut pas arriver » interne.
- **Types `Literal`** aux frontières de vocabulaire (`Classification`,
  `VerdictCorroboration`, `TypeAlerte`) plutôt que `str` générique.
  `TypeAlerte = Literal["aubaine", "erreur_prix", "minimum"]` rend visible
  statiquement que le vocabulaire de `classifier()`
  (`candidat_erreur_prix`/`bonne_affaire`/`normal`/`donnees_insuffisantes`)
  et celui de la colonne `alertes.type` sont deux choses différentes — le
  mapping entre les deux se fera au câblage, pas dans `detection.py`.
- **Écarts connus, documentés mais non corrigés cette session** (aucun
  n'est une régression — tous préexistants ou explicitement hors périmètre) :
  la table `alertes` n'a pas de colonne `devise` (le cooldown suppose donc
  la même devise entre deux alertes successives — vrai en pratique
  aujourd'hui, un seul compte Duffel) ; le bucketing `tranche_horizon` (4
  tranches fixes) de `stats_routes` ne correspond pas à la fenêtre glissante
  ±21 jours utilisée ici (peuplement de `stats_routes` reste un
  non-objectif, la case correspondante de la Phase 2.2 reste décochée) ;
  `cabine`/`escales`/`fournisseur` non filtrés dans l'échantillon comparable
  (sans risque tant que `config.yaml` n'a qu'une seule classe/un seul
  fournisseur) ; `n_min` (8) existe indépendamment dans
  `echantillon_comparable` et `z_score_modifie`/`classifier` — couplés par
  convention documentée, pas par le code, un test fige le comportement
  dégradé (pas cassé) si jamais désaccordés ; `seuil_chute_pct` (0.40, signal
  4 de corroboration) n'a pas de valeur donnée par AUDIT.md contrairement
  aux seuils -3.5/-2.0 — défaut assumé, repris de l'ancien
  `seuil_erreur_prix_pct`.
- Tests : 76 tests neufs dans `tests/test_detection.py` (85 au total dans ce
  fichier avec les 9 déjà là, 121 pour la suite complète), toutes fixtures
  synthétiques à échantillons contrôlés — aucun accès à la vraie base,
  conformément à la consigne. Une seule utilisation de
  `pytest.mark.parametrize` dans tout le projet (matrice des 2³
  combinaisons de signaux de corroboration), volontairement ciblée plutôt
  que généralisée au reste du fichier de tests. Couverture vérifiée avec
  `coverage` installé de façon éphémère dans le venv (jamais ajouté à
  `pyproject.toml`) : 100 % lignes/branches sur tout le nouveau moteur
  (aucune ligne/branche manquante à partir de la section Phase 2.3 du
  fichier) ; les seules lignes non couvertes du fichier appartiennent au
  code Phase 1 préexistant et non touché (`statistiques_destination`/
  `raison_prix_max`), déjà le cas avant cette session — pas une régression,
  hors périmètre.
- Vérifié : `ruff check`, `ruff format --check`, `mypy` et `pytest -q`
  tous verts (121 tests) ; poussé sur `refactor-audit`. Comportement
  nominal du scanner inchangé (aucune fonction Phase 1 modifiée, rien de
  câblé côté `scanner.py`).

### Phase 2.4 — couche fournisseurs (2026-07-17)

Périmètre de cette session, donné explicitement par l'utilisateur :
uniquement `providers/` (`base.py` + `duffel.py`), avec le câblage de
`scanner.py` limité à la stricte mesure imposée par le changement de
contrat — pas de câblage du moteur de détection Phase 2.3, pas de Phase 2.5
(digest technique). Plan pressure-testé via un agent Plan dédié avant
implémentation (même pratique qu'en Phase 2.3).

- `providers/base.py` (nouveau) : `Offre`/`FournisseurVols` repris quasiment
  tels quels d'AUDIT.md, avec deux ajouts décidés et documentés dans le code
  plutôt que dans ce Journal seul : `route: "Route"` devient un alias
  `type Route = dict[str, Any]` (PEP 695) plutôt qu'un `TypedDict` —
  `generer_candidats()`/`formater_alerte()` restent inchangés (`dict[Any,
  Any]` est assignable à `dict[str, Any]`), aucun ripple hors périmètre ;
  `ResumeFournisseur` + la hiérarchie `ErreurFournisseur`/
  `ErreurValidationReponse`/`ErreurFournisseurSuspendu` sont ajoutés à côté
  du contrat minimal demandé — vocabulaire partagé pour un futur 2ᵉ
  fournisseur (Amadeus, Phase 3 backlog), coût nul aujourd'hui. Le Protocol
  lui-même reste minimal (juste `nom` + `meilleure_offre`) : circuit
  breaker/canari/résumé sont des détails de `FournisseurDuffel`, pas du
  contrat — généraliser le Protocol avant qu'un 2ᵉ fournisseur réel en ait
  besoin serait spéculatif.
- `providers/duffel.py` : `chercher_meilleur_vol` (fonction libre) remplacé
  par `FournisseurDuffel` (classe, état mutable scope au run), qui implémente
  `FournisseurVols`. La réponse Duffel est validée par des modèles Pydantic
  privés (`_ReponseOffreRequest` → `_DonneesReponseOffres` → `_OffreDuffel`
  → `_TrajetDuffel` → `_SegmentDuffel`) avant toute extraction ;
  `extra="ignore"` explicite (pas le défaut implicite silencieux) pour
  documenter qu'on tolère le bruit du payload réel et qu'on ne valide que ce
  qu'on consomme. `Field(min_length=1)` appliqué aux deux niveaux `slices`
  ET `segments` : une slice à 0 segment aurait sinon silencieusement donné
  `escales = max(escales, -1)` → `0`, exactement la donnée fausse
  silencieuse qu'AUDIT.md interdit — trouvé pendant le pressure-test, pas
  dans la première version du plan. `total_amount` en `Decimal` (pas
  `float`) : conversion en centimes via `to_integral_value(ROUND_HALF_UP)`,
  conforme à la règle argent de CLAUDE.md et élimine au passage l'aller-
  retour IEEE-754 du `float(total_amount)` actuel. `post_resilient`/
  `extraire_message_erreur`/`creer_session_duffel` inchangés (signature et
  comportement) : les 5 tests déjà présents passent tels quels.
- Circuit breaker : 5 échecs consécutifs → suspension pour le run
  (`ErreurFournisseurSuspendu` levée immédiatement ensuite, sans appel
  réseau). `_appeler()` enveloppe systématiquement toute source d'échec
  (réseau, HTTP non-transitoire, schéma invalide) dans la famille
  `ErreurFournisseur`, ce qui permet à `meilleure_offre()` de catcher
  précisément ce type plutôt qu'`Exception` large — un vrai bug de
  programmation continue de remonter bruyamment (toujours rattrapé par le
  `except Exception` déjà présent dans `scanner.py::main()`, donc aucune
  régression sur la politique de sortie) sans polluer le bookkeeping du
  breaker.
- Route canari : origine de `config.yaml` → `JFK` (constante), aller simple,
  décalage fixe +4 semaines (indépendant de `sejour.candidats_semaines` pour
  rester autonome). Passe par `meilleure_offre()`, donc partage le même
  compteur d'échecs consécutifs que les routes normales — lecture retenue de
  « 5 échecs consécutifs » : une seule chronologie de tentatives par run, le
  canari en étant le premier maillon. Contrairement à `meilleure_offre()`,
  `verifier_canari()` ne propage jamais d'exception (`except Exception`
  large, volontaire et documenté dans le code) : c'est une sonde
  explicitement infaillible, pensée pour un appel sans `try`/`except` côté
  `scanner.py`.
- `resume()` : retourne un `ResumeFournisseur` (nom, appels_reussis,
  appels_zero_offres, echecs_total, suspendu). `echecs_total` vient d'un
  compteur cumulé séparé (`_echecs_cumules`, jamais remis à zéro), distinct
  du compteur consécutif du breaker (remis à zéro à chaque succès) : un vrai
  bug trouvé pendant le pressure-test (le compteur consécutif seul aurait
  donné une valeur trompeuse en fin de run après un succès intercalé). Seuil
  d'avertissement sur le taux de réponses « 0 offre » : 50 %, minimum 5
  appels réussis — non spécifié par AUDIT.md, point de départ conservateur
  assumé comme `seuil_chute_pct` en Phase 2.3 ; `resume()` logge le taux réel
  en INFO à chaque run (pas seulement au franchissement du seuil) pour
  accumuler l'historique empirique qui permettrait de le recalibrer plus
  tard. Le digest technique Telegram lui-même reste Phase 2.5, hors
  périmètre : l'avertissement aujourd'hui est un log structuré (visible dans
  les logs GitHub Actions), seul filet de sécurité réel en l'absence du
  digest.
- `scanner.py` : câblage minimal imposé par le changement de contrat —
  `FournisseurDuffel` remplace `creer_session_duffel`/`chercher_meilleur_vol`
  (supprimée, plus aucun appelant, pas de shim de compatibilité), un appel à
  `verifier_canari()` avant la boucle de destinations et à `resume()` après,
  et un shim local `_vol_depuis_offre()` qui reconvertit l'`Offre` (centimes)
  en dict dollars-flottants pour le reste de `main()` (stats Phase 1,
  `storage`, `alerting`) — le câblage du moteur de détection Phase 2.3 reste
  non fait cette session (voir Journal Phase 2.3), le shim est délibérément
  temporaire et documenté comme tel dans le code, à supprimer à ce câblage.
  Rien d'autre dans `main()` n'a changé (stats, raisons d'alerte,
  `ecrire_resume_github`, politique de sortie).
- Fixture `tests/fixtures/duffel_offer_request.json` : réponse réaliste à 3
  offres — la moins chère a 1 escale et doit être sélectionnée malgré une
  offre à 0 escale plus chère et une autre à escales égales mais plus chère
  (vérifie que c'est le prix, pas le nombre d'escales, qui pilote la
  sélection) — avec du bruit de payload Duffel réel non consommé (`id`,
  `expires_at`, `owner`, `base_amount`, `tax_amount`, `aircraft`, etc.) pour
  prouver que `extra="ignore"` tolère un vrai payload.
- Séquencement des commits ajusté en cours de route par rapport au plan
  initial : supprimer `chercher_meilleur_vol` cassait l'import de
  `scanner.py`, donc la collecte pytest de tout `tests/test_scanner.py` (pas
  seulement les 3 tests de `main()`) tant que `scanner.py` n'était pas
  recâblé dans le même commit. Le premier commit regroupe donc
  `providers/base.py` + refactor `duffel.py` + câblage minimal `scanner.py`
  (au lieu de les séparer comme esquissé dans le plan), pour que chaque
  commit reste vert indépendamment (`ruff`/`mypy`/`pytest` complet, pas
  seulement les fichiers touchés). Circuit breaker, canari, `resume()`
  restent chacun des commits additifs séparés comme prévu au plan.
- `tests/test_scanner.py` : les 3 tests de `main()` perdent chacun un
  paramètre positionnel (9 → 8 `@patch` : `creer_session_duffel` disparaît,
  `chercher_meilleur_vol` + `creer_session_duffel` fusionnent en un seul
  patch de la classe `FournisseurDuffel`) — piège identifié pendant le
  pressure-test, pas juste un renommage de mock. `_vol_sans_alerte()` (dict
  dollars) devient `_offre_sans_alerte()` (vraie instance `Offre` en
  centimes), puisque le shim de `scanner.py` s'exécute pour de vrai sur la
  valeur retournée par le mock. Un test neuf verrouille le câblage
  (`verifier_canari`/`resume` appelés exactement une fois par `main()`).
- Tests : 34 tests dans `tests/test_providers.py` (28 nouveaux : validation/
  sélection, repli compagnie, `offers` absent/null/vide, schéma invalide,
  HTTP non-ok, arrondi centimes, circuit breaker, canari, `resume` — 6 déjà
  présents intacts) ; 9 dans `tests/test_scanner.py` (1 nouveau). Aucun accès
  réseau réel : session injectée via le paramètre keyword-only
  `FournisseurDuffel(..., session=...)`, et `post_resilient` monkeypatché
  pour les scénarios d'échecs répétés (évite de dépendre du retry interne de
  `post_resilient`, déjà testé séparément par ailleurs).
- Vérifié : `ruff check`, `ruff format --check`, `mypy` et `pytest -q` tous
  verts (150 tests, 121 + 29) après chaque commit ; poussé sur
  `refactor-audit`. Comportement nominal inchangé côté détection/alerting/
  storage (aucune fonction Phase 1 modifiée) : seuls le fetch Duffel et son
  câblage dans `main()` ont changé.