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
        environnement="production",
    )
    conn.commit()

    ligne = conn.execute("SELECT horizon_jours, cabine, fournisseur FROM observations").fetchone()
    assert ligne["horizon_jours"] == 10
    # Valeurs par defaut du schema (aucune observation historique n'a jamais
    # utilise autre chose, voir AUDIT.md 2.2).
    assert ligne["cabine"] == "economy"
    assert ligne["fournisseur"] == "duffel"
    conn.close()


# ---------------------------------------------------------------- lire_historique / enregistrer_observation


def test_lire_historique_base_vide_retourne_liste_vide(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    assert storage.lire_historique() == []


def test_enregistrer_observation_puis_lire_historique_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    storage.enregistrer_observation(
        origine="YUL",
        destination="CDG",
        observe_le="2026-01-01T00:00:00+00:00",
        date_depart="2026-06-01",
        date_retour="2026-06-13",
        prix_cents=41019,
        devise="USD",
        compagnie="Test Air",
        escales=0,
        environnement="production",
    )
    lignes = storage.lire_historique()

    assert len(lignes) == 1
    assert lignes[0]["destination"] == "CDG"
    assert lignes[0]["date_retour"] == "2026-06-13"
    assert lignes[0]["prix"] == pytest.approx(410.19)
    assert lignes[0]["devise"] == "USD"


def test_enregistrer_observation_retourne_meme_route_id_deuxieme_appel(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    base = {
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-01",
        "date_retour": None,
        "prix_cents": 50000,
        "devise": "USD",
        "compagnie": "Test Air",
        "escales": 0,
        "environnement": "production",
    }

    id1 = storage.enregistrer_observation(observe_le="2026-01-01T00:00:00+00:00", **base)
    id2 = storage.enregistrer_observation(observe_le="2026-01-02T00:00:00+00:00", **base)

    assert id1 == id2


def test_enregistrer_observation_sans_date_retour(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    storage.enregistrer_observation(
        origine="YUL",
        destination="CDG",
        observe_le="2026-01-01T00:00:00+00:00",
        date_depart="2026-06-01",
        date_retour=None,
        prix_cents=50000,
        devise="USD",
        compagnie="Test Air",
        escales=0,
        environnement="production",
    )
    lignes = storage.lire_historique()

    assert lignes[0]["date_retour"] == ""


def test_enregistrer_observation_deux_observations_meme_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    base = {
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-01",
        "date_retour": None,
        "devise": "USD",
        "compagnie": "Test Air",
        "escales": 0,
        "environnement": "production",
    }

    storage.enregistrer_observation(
        observe_le="2026-01-01T00:00:00+00:00", prix_cents=50000, **base
    )
    storage.enregistrer_observation(
        observe_le="2026-01-02T00:00:00+00:00", prix_cents=48000, **base
    )

    lignes = storage.lire_historique()
    assert [ligne["prix"] for ligne in lignes] == [pytest.approx(500.0), pytest.approx(480.0)]


def test_enregistrer_observation_cree_le_dossier_parent(tmp_path, monkeypatch) -> None:
    chemin = tmp_path / "sous_dossier" / "scanner.db"
    monkeypatch.setattr(storage, "DB_FILE", chemin)

    storage.enregistrer_observation(
        origine="YUL",
        destination="CDG",
        observe_le="2026-01-01T00:00:00+00:00",
        date_depart="2026-06-01",
        date_retour=None,
        prix_cents=50000,
        devise="USD",
        compagnie="Test Air",
        escales=0,
        environnement="production",
    )

    assert chemin.exists()


# ---------------------------------------------------------------- lire_observations


def test_lire_observations_base_vide_retourne_liste_vide(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    assert storage.lire_observations() == []


def test_lire_observations_forme_native_centimes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    route_id = storage.enregistrer_observation(
        origine="YUL",
        destination="CDG",
        observe_le="2026-01-01T00:00:00+00:00",
        date_depart="2026-06-01",
        date_retour=None,
        prix_cents=50000,
        devise="USD",
        compagnie="Test Air",
        escales=0,
        environnement="production",
    )

    lignes = storage.lire_observations()

    assert lignes == [
        {
            "route_id": route_id,
            "devise": "USD",
            "observe_le": "2026-01-01T00:00:00+00:00",
            "date_depart": "2026-06-01",
            "horizon_jours": storage.horizon_jours("2026-01-01T00:00:00+00:00", "2026-06-01"),
            "prix_cents": 50000,
        }
    ]


def test_lire_observations_plusieurs_routes_et_devises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    storage.enregistrer_observation(
        origine="YUL",
        destination="CDG",
        observe_le="2026-01-01T00:00:00+00:00",
        date_depart="2026-06-01",
        date_retour=None,
        prix_cents=50000,
        devise="USD",
        compagnie="Test Air",
        escales=0,
        environnement="production",
    )
    storage.enregistrer_observation(
        origine="YUL",
        destination="LHR",
        observe_le="2026-01-02T00:00:00+00:00",
        date_depart="2026-07-01",
        date_retour=None,
        prix_cents=60000,
        devise="CAD",
        compagnie="Test Air",
        escales=1,
        environnement="production",
    )

    lignes = storage.lire_observations()

    assert len(lignes) == 2
    assert {ligne["prix_cents"] for ligne in lignes} == {50000, 60000}
    assert {ligne["devise"] for ligne in lignes} == {"USD", "CAD"}


# ---------------------------------------------------------------- garde-fou environnement (audit data/scanner.db)


def _observation_environnement(**overrides: object) -> dict:
    base: dict = {
        "origine": "YUL",
        "destination": "CDG",
        "observe_le": "2026-01-01T00:00:00+00:00",
        "date_depart": "2026-06-01",
        "date_retour": None,
        "prix_cents": 50000,
        "devise": "USD",
        "compagnie": "Test Air",
        "escales": 0,
        "environnement": "production",
    }
    base.update(overrides)
    return base


def test_lire_observations_exclut_sandbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    storage.enregistrer_observation(**_observation_environnement(environnement="sandbox"))

    assert storage.lire_observations() == []


def test_lire_observations_exclut_inconnu(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    storage.enregistrer_observation(**_observation_environnement(environnement="inconnu"))

    assert storage.lire_observations() == []


def test_lire_observations_melange_ne_renvoie_que_la_production(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    storage.enregistrer_observation(
        **_observation_environnement(environnement="production", prix_cents=50000)
    )
    storage.enregistrer_observation(
        **_observation_environnement(environnement="sandbox", prix_cents=99999)
    )

    lignes = storage.lire_observations()

    assert len(lignes) == 1
    assert lignes[0]["prix_cents"] == 50000


def test_lire_historique_exclut_sandbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    storage.enregistrer_observation(**_observation_environnement(environnement="sandbox"))

    assert storage.lire_historique() == []


def test_lire_historique_melange_ne_renvoie_que_la_production(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    storage.enregistrer_observation(
        **_observation_environnement(environnement="production", prix_cents=50000)
    )
    storage.enregistrer_observation(
        **_observation_environnement(environnement="inconnu", prix_cents=99999)
    )

    lignes = storage.lire_historique()

    assert len(lignes) == 1
    assert lignes[0]["prix"] == pytest.approx(500.0)


def test_lire_observations_normalise_observe_le_naif_en_utc(tmp_path, monkeypatch) -> None:
    """Regression : les observations anterieures au fix 1.6 (horodatage UTC
    systematique depuis) peuvent avoir un observe_le naif, ex. migrees depuis
    l'ancien data/history.csv. detection.echantillon_comparable compare
    observe_le a un datetime UTC-aware et levait TypeError avant ce fix."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    conn = storage.obtenir_connexion()
    route_id = storage.obtenir_ou_creer_route(conn, "YUL", "CDG")
    storage.inserer_observation(
        conn,
        route_id=route_id,
        observe_le="2026-01-01 14:30",
        date_depart="2026-06-01",
        date_retour=None,
        prix_cents=50000,
        devise="USD",
        compagnie="Test Air",
        escales=0,
        environnement="production",
    )
    conn.commit()
    conn.close()

    lignes = storage.lire_observations()

    assert lignes[0]["observe_le"] == "2026-01-01T14:30:00+00:00"


# ---------------------------------------------------------------- horizon_jours


def test_horizon_jours_calcule_la_difference_en_jours() -> None:
    assert storage.horizon_jours("2026-01-01T00:00:00+00:00", "2026-01-11") == 10


def test_horizon_jours_ignore_heure_de_observe_le() -> None:
    assert storage.horizon_jours("2026-01-01T23:59:59+00:00", "2026-01-11") == 10


# ---------------------------------------------------------------- obtenir_derniere_alerte / enregistrer_alerte


def test_obtenir_derniere_alerte_absente_retourne_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    assert storage.obtenir_derniere_alerte(1, "2026-06-01", "aubaine") is None


def test_enregistrer_puis_obtenir_derniere_alerte(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    storage.enregistrer_alerte(
        route_id=1,
        date_depart="2026-06-01",
        type_alerte="aubaine",
        prix_cents=42000,
        envoyee_le="2026-01-01T00:00:00+00:00",
    )

    assert storage.obtenir_derniere_alerte(1, "2026-06-01", "aubaine") == {
        "prix_cents": 42000,
        "envoyee_le": "2026-01-01T00:00:00+00:00",
    }


def test_enregistrer_alerte_deuxieme_appel_ecrase_la_precedente(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    storage.enregistrer_alerte(
        route_id=1,
        date_depart="2026-06-01",
        type_alerte="aubaine",
        prix_cents=42000,
        envoyee_le="2026-01-01T00:00:00+00:00",
    )
    storage.enregistrer_alerte(
        route_id=1,
        date_depart="2026-06-01",
        type_alerte="aubaine",
        prix_cents=39000,
        envoyee_le="2026-01-05T00:00:00+00:00",
    )

    assert storage.obtenir_derniere_alerte(1, "2026-06-01", "aubaine") == {
        "prix_cents": 39000,
        "envoyee_le": "2026-01-05T00:00:00+00:00",
    }


def test_enregistrer_alerte_types_distincts_lignes_distinctes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    storage.enregistrer_alerte(
        route_id=1,
        date_depart="2026-06-01",
        type_alerte="aubaine",
        prix_cents=42000,
        envoyee_le="2026-01-01T00:00:00+00:00",
    )
    storage.enregistrer_alerte(
        route_id=1,
        date_depart="2026-06-01",
        type_alerte="minimum",
        prix_cents=41000,
        envoyee_le="2026-01-01T00:00:00+00:00",
    )

    alerte_aubaine = storage.obtenir_derniere_alerte(1, "2026-06-01", "aubaine")
    alerte_minimum = storage.obtenir_derniere_alerte(1, "2026-06-01", "minimum")
    assert alerte_aubaine is not None and alerte_aubaine["prix_cents"] == 42000
    assert alerte_minimum is not None and alerte_minimum["prix_cents"] == 41000
