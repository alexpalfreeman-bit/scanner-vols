"""
Stockage de l'historique des prix (SQLite).
-----------------------------------------------
Interface repository, deux familles de lecture/ecriture cote a cote (Phase
2.3/2.4 cablage, AUDIT.md) :
- lire_historique() : forme dollars flottants (dict), alimente le moteur
  Phase 1 conserve (detection.statistiques_destination) ;
- lire_observations()/enregistrer_observation() : forme native de la table
  observations (prix_cents entiers, regle CLAUDE.md sur l'argent), alimente
  le nouveau moteur (detection.echantillon_comparable) ;
- obtenir_derniere_alerte()/enregistrer_alerte() : dedup/cooldown (table
  alertes) pour detection.doit_alerter, cote alerting.envoyer_alerte.

Garde-fou sandbox/production (audit data/scanner.db, cf. Journal) : chaque
observation porte sa colonne `environnement` (valeur calculee par
providers.duffel.environnement_duffel a partir du prefixe du token, jamais
devinee ici). lire_historique()/lire_observations() ne renvoient JAMAIS que
les observations `environnement = 'production'` : les runs sandbox/inconnus
continuent de s'inserer normalement (utile pour tester en local) mais ne
peuvent jamais atteindre le moteur de detection statistique.
"""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "data" / "scanner.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routes (
  id          INTEGER PRIMARY KEY,
  origine     TEXT NOT NULL,
  destination TEXT NOT NULL,
  UNIQUE (origine, destination)
);

CREATE TABLE IF NOT EXISTS observations (
  id            INTEGER PRIMARY KEY,
  route_id      INTEGER NOT NULL REFERENCES routes(id),
  observe_le    TEXT NOT NULL,
  date_depart   TEXT NOT NULL,
  date_retour   TEXT,
  horizon_jours INTEGER NOT NULL,
  prix_cents    INTEGER NOT NULL,
  devise        TEXT NOT NULL,
  compagnie     TEXT,
  escales       INTEGER,
  cabine        TEXT NOT NULL DEFAULT 'economy',
  fournisseur   TEXT NOT NULL DEFAULT 'duffel',
  environnement TEXT NOT NULL DEFAULT 'inconnu'
);
CREATE INDEX IF NOT EXISTS idx_obs_lookup ON observations (route_id, date_depart, observe_le);

CREATE TABLE IF NOT EXISTS stats_routes (
  route_id        INTEGER NOT NULL,
  mois_depart     TEXT NOT NULL,
  tranche_horizon INTEGER NOT NULL,
  devise          TEXT NOT NULL,
  n               INTEGER NOT NULL,
  p05_cents       INTEGER,
  mediane_cents   INTEGER,
  mad_cents       INTEGER,
  maj_le          TEXT NOT NULL,
  PRIMARY KEY (route_id, mois_depart, tranche_horizon, devise)
);

CREATE TABLE IF NOT EXISTS alertes (
  id          INTEGER PRIMARY KEY,
  route_id    INTEGER NOT NULL,
  date_depart TEXT NOT NULL,
  type        TEXT NOT NULL,
  prix_cents  INTEGER NOT NULL,
  envoyee_le  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup ON alertes (route_id, date_depart, type);
"""
# stats_routes et alertes sont creees ici mais ne sont pas encore peuplees :
# leur logique appartient au futur moteur de detection (AUDIT.md 2.3/2.5).


def initialiser_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def obtenir_connexion() -> sqlite3.Connection:
    """Ouvre une connexion SQLite pret a l'emploi (schema garanti present,
    cles etrangeres activees, lignes accessibles par nom de colonne)."""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    initialiser_db(conn)
    return conn


def obtenir_ou_creer_route(conn: sqlite3.Connection, origine: str, destination: str) -> int:
    conn.execute(
        "INSERT INTO routes (origine, destination) VALUES (?, ?) "
        "ON CONFLICT (origine, destination) DO NOTHING",
        (origine, destination),
    )
    ligne = conn.execute(
        "SELECT id FROM routes WHERE origine = ? AND destination = ?",
        (origine, destination),
    ).fetchone()
    return int(ligne["id"])


def horizon_jours(observe_le: str, date_depart: str) -> int:
    jour_observation = datetime.fromisoformat(observe_le).date()
    return (date.fromisoformat(date_depart) - jour_observation).days


def inserer_observation(
    conn: sqlite3.Connection,
    *,
    route_id: int,
    observe_le: str,
    date_depart: str,
    date_retour: str | None,
    prix_cents: int,
    devise: str,
    compagnie: str | None,
    escales: int | None,
    environnement: str,
) -> None:
    """N'effectue pas le commit : a la charge de l'appelant (permet d'inserer
    plusieurs observations dans une seule transaction).

    environnement (pas de defaut : parametre obligatoire, comme prix_cents)
    doit venir de providers.duffel.environnement_duffel - jamais devine ni
    laisse a 'production' par reflexe, exactement le bug constate a l'audit
    (donnees sandbox committees sans etiquette, contaminant la detection)."""
    conn.execute(
        "INSERT INTO observations "
        "(route_id, observe_le, date_depart, date_retour, horizon_jours, "
        " prix_cents, devise, compagnie, escales, environnement) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            route_id,
            observe_le,
            date_depart,
            date_retour,
            horizon_jours(observe_le, date_depart),
            prix_cents,
            devise,
            compagnie,
            escales,
            environnement,
        ),
    )


def lire_historique() -> list[dict]:
    conn = obtenir_connexion()
    try:
        lignes = conn.execute(
            "SELECT r.origine, r.destination, o.observe_le, o.date_depart, o.date_retour, "
            "       o.prix_cents, o.devise, o.compagnie, o.escales "
            "FROM observations o JOIN routes r ON r.id = o.route_id "
            "WHERE o.environnement = 'production' "
            "ORDER BY o.observe_le"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "horodatage_utc": ligne["observe_le"],
            "origine": ligne["origine"],
            "destination": ligne["destination"],
            "date_depart": ligne["date_depart"],
            "date_retour": ligne["date_retour"] or "",
            "prix": ligne["prix_cents"] / 100,
            "devise": ligne["devise"],
            "compagnie": ligne["compagnie"],
            "escales": ligne["escales"],
        }
        for ligne in lignes
    ]


def enregistrer_observation(
    *,
    origine: str,
    destination: str,
    observe_le: str,
    date_depart: str,
    date_retour: str | None,
    prix_cents: int,
    devise: str,
    compagnie: str | None,
    escales: int | None,
    environnement: str,
) -> int:
    """Cree-ou-recupere la route puis insere l'observation (centimes natifs,
    aucun aller-retour par les dollars flottants) ; retourne route_id, dont
    le moteur de detection (echantillon_comparable) et la dedup d'alertes
    (obtenir_derniere_alerte/enregistrer_alerte) ont besoin en aval.

    environnement : voir inserer_observation - obligatoire, jamais devine."""
    conn = obtenir_connexion()
    try:
        route_id = obtenir_ou_creer_route(conn, origine, destination)
        inserer_observation(
            conn,
            route_id=route_id,
            observe_le=observe_le,
            date_depart=date_depart,
            date_retour=date_retour,
            prix_cents=prix_cents,
            devise=devise,
            compagnie=compagnie,
            escales=escales,
            environnement=environnement,
        )
        conn.commit()
        return route_id
    finally:
        conn.close()


def _observe_le_normalise(valeur: str) -> str:
    """Normalise un observe_le naif (observations anterieures au fix 1.6,
    horodatage UTC systematique depuis - certaines lignes migrees depuis
    l'ancien data/history.csv n'ont pas de fuseau) en UTC explicite : la
    convention CLAUDE.md est que tout timestamp du projet represente deja
    l'UTC, un timestamp naif n'a donc jamais represente une autre zone, seul
    le marqueur de fuseau manque. Necessaire ici (pas dans lire_historique,
    qui ne fait que trier ces chaines) : echantillon_comparable est le
    premier appelant a parser observe_le en datetime pour le comparer a un
    datetime UTC-aware, ce qui leve TypeError sur un naif non normalise."""
    dt = datetime.fromisoformat(valeur)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def lire_observations() -> list[dict]:
    """Lecture native (prix_cents entiers, sans jointure) : alimente
    detection.echantillon_comparable, qui filtre lui-meme par route_id/
    devise/recence sur la liste complete (meme pattern que lire_historique,
    un seul fetch avant la boucle des routes - voir scanner.py). Ne renvoie
    que les observations `environnement = 'production'` (voir docstring du
    module)."""
    conn = obtenir_connexion()
    try:
        lignes = conn.execute(
            "SELECT route_id, devise, observe_le, date_depart, horizon_jours, prix_cents "
            "FROM observations WHERE environnement = 'production'"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "route_id": ligne["route_id"],
            "devise": ligne["devise"],
            "observe_le": _observe_le_normalise(ligne["observe_le"]),
            "date_depart": ligne["date_depart"],
            "horizon_jours": ligne["horizon_jours"],
            "prix_cents": ligne["prix_cents"],
        }
        for ligne in lignes
    ]


def obtenir_derniere_alerte(route_id: int, date_depart: str, type_alerte: str) -> dict | None:
    """Ligne alertes correspondant a (route_id, date_depart, type) - au plus
    une, grace a idx_dedup. Alimente alerte_precedente de detection.doit_alerter."""
    conn = obtenir_connexion()
    try:
        ligne = conn.execute(
            "SELECT prix_cents, envoyee_le FROM alertes "
            "WHERE route_id = ? AND date_depart = ? AND type = ?",
            (route_id, date_depart, type_alerte),
        ).fetchone()
    finally:
        conn.close()
    return {"prix_cents": ligne["prix_cents"], "envoyee_le": ligne["envoyee_le"]} if ligne else None


def enregistrer_alerte(
    *, route_id: int, date_depart: str, type_alerte: str, prix_cents: int, envoyee_le: str
) -> None:
    """Upsert (pas juste insert) : une realerte legitime apres cooldown doit
    ecraser la ligne existante pour que le prochain cooldown reparte de la
    nouvelle valeur, d'ou DO UPDATE (pas DO NOTHING comme routes)."""
    conn = obtenir_connexion()
    try:
        conn.execute(
            "INSERT INTO alertes (route_id, date_depart, type, prix_cents, envoyee_le) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (route_id, date_depart, type) "
            "DO UPDATE SET prix_cents = excluded.prix_cents, envoyee_le = excluded.envoyee_le",
            (route_id, date_depart, type_alerte, prix_cents, envoyee_le),
        )
        conn.commit()
    finally:
        conn.close()
