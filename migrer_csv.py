"""
Migration ponctuelle data/history.csv -> data/scanner.db (AUDIT.md 2.2).
----------------------------------------------------------------------------
A executer une seule fois, sur une base neuve : le script refuse de tourner
si data/scanner.db contient deja des observations, pour ne jamais dupliquer
l'historique (supprime le fichier .db si tu veux vraiment relancer une
migration a blanc). Convertit prix (dollars flottants) en prix_cents
(entiers, regle CLAUDE.md sur l'argent) ; horizon_jours est calcule par
storage.inserer_observation.

Usage : python migrer_csv.py
"""

import csv
import logging
import sys

import storage

logger = logging.getLogger(__name__)


def migrer() -> int:
    if not storage.HISTORY_FILE.exists():
        logger.error("Introuvable : %s", storage.HISTORY_FILE)
        return 1

    conn = storage.obtenir_connexion()
    try:
        deja = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        if deja:
            logger.error(
                "%s contient deja %d observation(s) : migration refusee "
                "(supprime le fichier .db si tu veux relancer une migration a blanc).",
                storage.DB_FILE,
                deja,
            )
            return 1

        with open(storage.HISTORY_FILE, newline="", encoding="utf-8") as f:
            lignes = list(csv.DictReader(f))

        for ligne in lignes:
            route_id = storage.obtenir_ou_creer_route(conn, ligne["origine"], ligne["destination"])
            storage.inserer_observation(
                conn,
                route_id=route_id,
                observe_le=ligne["horodatage_utc"],
                date_depart=ligne["date_depart"],
                date_retour=ligne.get("date_retour") or None,
                prix_cents=round(float(ligne["prix"]) * 100),
                devise=ligne["devise"],
                compagnie=ligne.get("compagnie") or None,
                escales=int(ligne["escales"]) if ligne.get("escales") else None,
                # Le CSV source ne trace pas le token Duffel a l'origine de chaque
                # ligne : 'inconnu' (jamais 'production' par reflexe, cf. audit
                # data/scanner.db) - ces lignes restent donc hors du moteur de
                # detection (storage.lire_historique/lire_observations).
                environnement="inconnu",
            )
        conn.commit()
    except Exception:
        conn.close()
        raise

    conn.close()
    logger.info(
        "%d ligne(s) migree(s) : %s -> %s", len(lignes), storage.HISTORY_FILE, storage.DB_FILE
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(migrer())
