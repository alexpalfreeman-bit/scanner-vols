import storage


def test_import_storage() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import storage  # noqa: F401


def test_lire_historique_fichier_absent_retourne_liste_vide(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "HISTORY_FILE", tmp_path / "absent.csv")
    assert storage.lire_historique() == []


def test_ajouter_puis_lire_historique_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "HISTORY_FILE", tmp_path / "history.csv")
    ligne = {
        "horodatage_utc": "2026-01-01T00:00:00+00:00",
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-01",
        "date_retour": "2026-06-13",
        "prix": 500.0,
        "devise": "USD",
        "compagnie": "Test Air",
        "escales": 0,
    }

    storage.ajouter_historique(ligne)
    lignes = storage.lire_historique()

    assert len(lignes) == 1
    assert lignes[0]["destination"] == "CDG"
    assert lignes[0]["prix"] == "500.0"  # lecture CSV : tout ressort en str


def test_ajouter_historique_deux_fois_pas_de_double_entete(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "HISTORY_FILE", tmp_path / "history.csv")
    ligne_cdg = dict.fromkeys(storage.COLONNES, "")
    ligne_cdg["destination"] = "CDG"
    ligne_lhr = {**ligne_cdg, "destination": "LHR"}

    storage.ajouter_historique(ligne_cdg)
    storage.ajouter_historique(ligne_lhr)

    lignes = storage.lire_historique()
    assert [ligne["destination"] for ligne in lignes] == ["CDG", "LHR"]


def test_ajouter_historique_cree_le_dossier_parent(tmp_path, monkeypatch) -> None:
    chemin = tmp_path / "sous_dossier" / "history.csv"
    monkeypatch.setattr(storage, "HISTORY_FILE", chemin)

    storage.ajouter_historique(dict.fromkeys(storage.COLONNES, ""))

    assert chemin.exists()
