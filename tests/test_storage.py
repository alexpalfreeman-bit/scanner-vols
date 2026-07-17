import sqlite3

import pytest

import storage


def test_import_storage() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import storage  # noqa: F401


def _connexion_test(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    return storage.obtenir_connexion()


# ---------------------------------------------------------------- schema


def test_initialiser_db_est_idempotent(tmp_path, monkeypatch) -> None:
    conn = _connexion_test(tmp_path, monkeypatch)
    storage.initialiser_db(conn)  # deuxieme appel : ne doit pas lever
    conn.close()


# ---------------------------------------------------------------- obtenir_ou_creer_route


def test_obtenir_ou_creer_route_est_idempotent(tmp_path, monkeypatch) -> None:
    conn = _connexion_test(tmp_path, monkeypatch)

    id1 = storage.obtenir_ou_creer_route(conn, "YUL", "CDG")
    id2 = storage.obtenir_ou_creer_route(conn, "YUL", "CDG")

    assert id1 == id2
    conn.close()


def test_obtenir_ou_creer_route_routes_distinctes(tmp_path, monkeypatch) -> None:
    conn = _connexion_test(tmp_path, monkeypatch)

    id_cdg = storage.obtenir_ou_creer_route(conn, "YUL", "CDG")
    id_lhr = storage.obtenir_ou_creer_route(conn, "YUL", "LHR")

    assert id_cdg != id_lhr
    conn.close()


# ---------------------------------------------------------------- inserer_observation


def test_inserer_observation_calcule_horizon_jours(tmp_path, monkeypatch) -> None:
    conn = _connexion_test(tmp_path, monkeypatch)
    route_id = storage.obtenir_ou_creer_route(conn, "YUL", "CDG")

    storage.inserer_observation(
        conn,
        route_id=route_id,
        observe_le="2026-01-01T00:00:00+00:00",
        date_depart="2026-01-11",
        date_retour=None,
        prix_cents=50000,
        devise="USD",
        compagnie="Test Air",
        escales=0,
    )
    conn.commit()

    ligne = conn.execute("SELECT horizon_jours, cabine, fournisseur FROM observations").fetchone()
    assert ligne["horizon_jours"] == 10
    # Valeurs par defaut du schema (aucune observation historique n'a jamais
    # utilise autre chose, voir AUDIT.md 2.2).
    assert ligne["cabine"] == "economy"
    assert ligne["fournisseur"] == "duffel"
    conn.close()


# ---------------------------------------------------------------- lire_historique / ajouter_historique


def test_lire_historique_base_vide_retourne_liste_vide(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    assert storage.lire_historique() == []


def test_ajouter_puis_lire_historique_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    ligne = {
        "horodatage_utc": "2026-01-01T00:00:00+00:00",
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-01",
        "date_retour": "2026-06-13",
        "prix": 410.19,
        "devise": "USD",
        "compagnie": "Test Air",
        "escales": 0,
    }

    storage.ajouter_historique(ligne)
    lignes = storage.lire_historique()

    assert len(lignes) == 1
    assert lignes[0]["destination"] == "CDG"
    assert lignes[0]["date_retour"] == "2026-06-13"
    assert lignes[0]["prix"] == pytest.approx(410.19)
    assert lignes[0]["devise"] == "USD"


def test_ajouter_historique_sans_date_retour(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    ligne = {
        "horodatage_utc": "2026-01-01T00:00:00+00:00",
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-01",
        "date_retour": "",
        "prix": 500.0,
        "devise": "USD",
        "compagnie": "Test Air",
        "escales": 0,
    }

    storage.ajouter_historique(ligne)
    lignes = storage.lire_historique()

    assert lignes[0]["date_retour"] == ""


def test_ajouter_historique_deux_observations_meme_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    base = {
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-01",
        "date_retour": "",
        "devise": "USD",
        "compagnie": "Test Air",
        "escales": 0,
    }

    storage.ajouter_historique(
        {**base, "horodatage_utc": "2026-01-01T00:00:00+00:00", "prix": 500.0}
    )
    storage.ajouter_historique(
        {**base, "horodatage_utc": "2026-01-02T00:00:00+00:00", "prix": 480.0}
    )

    lignes = storage.lire_historique()
    assert [ligne["prix"] for ligne in lignes] == [pytest.approx(500.0), pytest.approx(480.0)]


def test_ajouter_historique_cree_le_dossier_parent(tmp_path, monkeypatch) -> None:
    chemin = tmp_path / "sous_dossier" / "scanner.db"
    monkeypatch.setattr(storage, "DB_FILE", chemin)

    storage.ajouter_historique(
        {
            "horodatage_utc": "2026-01-01T00:00:00+00:00",
            "origine": "YUL",
            "destination": "CDG",
            "date_depart": "2026-06-01",
            "date_retour": "",
            "prix": 500.0,
            "devise": "USD",
            "compagnie": "Test Air",
            "escales": 0,
        }
    )

    assert chemin.exists()
