"""
Stockage de l'historique des prix (SQLite).
-----------------------------------------------
Interface repository : lire_historique() / ajouter_historique(ligne) gardent
la meme forme (dict, prix en dollars flottants) qu'avec l'ancien backend CSV
(Phase 2.1), pour que scanner.py et detection.py n'aient pas a changer. La
conversion vers/depuis les centimes entiers (regle CLAUDE.md sur l'argent)
se fait uniquement a cette frontiere : le reste du pipeline reste en dollars
flottants jusqu'a la reecriture du moteur de detection (Phase 2.3).
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "data" / "scanner.db"

# Conserve uniquement pour migrer_csv.py (migration ponctuelle, voir AUDIT.md 2.2).
HISTORY_FILE = BASE_DIR / "data" / "history.csv"

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
  fournisseur   TEXT NOT NULL DEFAULT 'duffel'
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


def _horizon_jours(observe_le: str, date_depart: str) -> int:
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
) -> None:
    """N'effectue pas le commit : a la charge de l'appelant (permet d'inserer
    plusieurs observations dans une seule transaction, ex. migrer_csv.py)."""
    conn.execute(
        "INSERT INTO observations "
        "(route_id, observe_le, date_depart, date_retour, horizon_jours, "
        " prix_cents, devise, compagnie, escales) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            route_id,
            observe_le,
            date_depart,
            date_retour,
            _horizon_jours(observe_le, date_depart),
            prix_cents,
            devise,
            compagnie,
            escales,
        ),
    )


def lire_historique() -> list[dict]:
    conn = obtenir_connexion()
    try:
        lignes = conn.execute(
            "SELECT r.origine, r.destination, o.observe_le, o.date_depart, o.date_retour, "
            "       o.prix_cents, o.devise, o.compagnie, o.escales "
            "FROM observations o JOIN routes r ON r.id = o.route_id "
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


def ajouter_historique(ligne: dict) -> None:
    conn = obtenir_connexion()
    try:
        route_id = obtenir_ou_creer_route(conn, ligne["origine"], ligne["destination"])
        inserer_observation(
            conn,
            route_id=route_id,
            observe_le=ligne["horodatage_utc"],
            date_depart=ligne["date_depart"],
            date_retour=ligne.get("date_retour") or None,
            prix_cents=round(float(ligne["prix"]) * 100),
            devise=ligne["devise"],
            compagnie=ligne.get("compagnie"),
            escales=ligne.get("escales"),
        )
        conn.commit()
    finally:
        conn.close()
