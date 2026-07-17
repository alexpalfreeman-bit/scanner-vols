from unittest.mock import Mock, patch

import scanner


def test_import_scanner() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import scanner  # noqa: F401


# ---------------------------------------------------------------- ecrire_resume_github


def test_ecrire_resume_github_ecrit_le_resume(tmp_path, monkeypatch) -> None:
    chemin = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(chemin))

    scanner.ecrire_resume_github([("YUL→CDG (2026-01-01)", "ok : 500 USD")])

    contenu = chemin.read_text(encoding="utf-8")
    assert "YUL→CDG" in contenu
    assert "ok : 500 USD" in contenu


def test_ecrire_resume_github_noop_sans_variable_environnement(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    scanner.ecrire_resume_github([("x", "y")])  # ne doit pas lever


# ---------------------------------------------------------------- devise (1.1)


def _ligne_historique(devise: str, prix: float, horodatage: str) -> dict:
    return {
        "horodatage_utc": horodatage,
        "origine": "YUL",
        "destination": "CDG",
        "prix": prix,
        "devise": devise,
    }


def test_statistiques_destination_filtre_par_devise() -> None:
    historique = [
        _ligne_historique("CAD", 700, "2026-01-01T00:00:00+00:00"),
        _ligne_historique("USD", 500, "2026-01-02T00:00:00+00:00"),
        _ligne_historique("USD", 510, "2026-01-03T00:00:00+00:00"),
    ]

    stats = scanner.statistiques_destination(historique, "YUL", "CDG", "USD", {})

    assert stats is not None
    assert stats["n"] == 2
    assert stats["min"] == 500


def test_raison_prix_max_meme_devise_sous_seuil() -> None:
    assert scanner.raison_prix_max(400, "CAD", 500, "CAD") == "sous ton seuil fixe de 500 CAD"


def test_raison_prix_max_devise_differente_retourne_none() -> None:
    assert scanner.raison_prix_max(400, "USD", 500, "CAD") is None


def test_raison_prix_max_aucun_seuil_configure_retourne_none() -> None:
    assert scanner.raison_prix_max(400, "CAD", None, "CAD") is None


# ---------------------------------------------------------------- extraire_message_erreur (1.3)


def test_extraire_message_erreur_json_valide() -> None:
    r = Mock()
    r.json.return_value = {
        "errors": [{"type": "invalid_request_error", "title": "Bad", "message": "oops"}]
    }

    assert scanner.extraire_message_erreur(r) == "invalid_request_error: Bad: oops"


def test_extraire_message_erreur_corps_html_502() -> None:
    r = Mock()
    r.json.side_effect = ValueError("pas du JSON")
    r.status_code = 502
    r.text = "<html><body>Bad Gateway</body></html>"

    message = scanner.extraire_message_erreur(r)

    assert "502" in message


# ---------------------------------------------------------------- main() : politique de sortie


def _config_minimal(nb_destinations: int) -> dict:
    """Config minimale mais valide vis-à-vis de config.valider_config (codes
    IATA à 3 lettres, devise présente) : main() valide réellement charger_config()."""
    return {
        "origine": "YUL",
        "devise": "USD",
        "destinations": [{"code": chr(65 + i) * 3} for i in range(nb_destinations)],
    }


def _vol_sans_alerte() -> dict:
    return {"prix": 500.0, "devise": "USD", "compagnie": "Test Air", "escales": 0}


@patch("scanner.ecrire_resume_github")
@patch("scanner.ajouter_historique")
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.chercher_meilleur_vol")
@patch("scanner.generer_candidats")
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.creer_session_duffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_reussit_si_au_moins_une_route_reussit(
    charger_env_mock,
    charger_config_mock,
    creer_session_mock,
    lire_historique_mock,
    generer_candidats_mock,
    chercher_vol_mock,
    stats_mock,
    ajouter_historique_mock,
    resume_mock,
) -> None:
    charger_config_mock.return_value = _config_minimal(3)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": f"D{i}", "date_depart": "2026-01-01"} for i in range(3)
    ]
    # 2 routes en erreur API, 1 route réussit sans déclencher d'alerte.
    chercher_vol_mock.side_effect = [RuntimeError("boom"), RuntimeError("boom"), _vol_sans_alerte()]

    assert scanner.main() == 0


@patch("scanner.ecrire_resume_github")
@patch("scanner.ajouter_historique")
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.chercher_meilleur_vol")
@patch("scanner.generer_candidats")
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.creer_session_duffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_echoue_si_toutes_les_routes_echouent(
    charger_env_mock,
    charger_config_mock,
    creer_session_mock,
    lire_historique_mock,
    generer_candidats_mock,
    chercher_vol_mock,
    stats_mock,
    ajouter_historique_mock,
    resume_mock,
) -> None:
    charger_config_mock.return_value = _config_minimal(3)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": f"D{i}", "date_depart": "2026-01-01"} for i in range(3)
    ]
    chercher_vol_mock.side_effect = RuntimeError("boom")

    assert scanner.main() == 1
