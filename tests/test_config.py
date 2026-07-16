import pytest

import config


def test_import_config() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import config  # noqa: F401


def test_charger_env_variable_manquante_leve_erreur_configuration(monkeypatch) -> None:
    # Neutralise load_dotenv() : sinon le vrai .env du dépôt (non trackè par
    # git, mais présent en local/CI) reremplirait la variable qu'on supprime
    # ci-dessous, et le scénario "variable manquante" ne serait plus testé.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    with pytest.raises(config.ErreurConfiguration, match="DUFFEL_ACCESS_TOKEN"):
        config.charger_env()


def test_valider_config_code_iata_invalide_leve_erreur() -> None:
    brut = {
        "origine": "YUL",
        "devise": "CAD",
        "destinations": [{"code": "TROPLONG"}],
    }
    with pytest.raises(config.ErreurConfiguration):
        config.valider_config(brut)


def test_valider_config_seuil_hors_bornes_leve_erreur() -> None:
    brut = {
        "origine": "YUL",
        "devise": "CAD",
        "detection": {"seuil_bonne_affaire_pct": 1.5},
        "destinations": [{"code": "CDG"}],
    }
    with pytest.raises(config.ErreurConfiguration):
        config.valider_config(brut)


def test_valider_config_entree_destination_bare_string_normalisee() -> None:
    brut = {
        "origine": "YUL",
        "devise": "CAD",
        "destinations": ["cdg"],
    }
    resultat = config.valider_config(brut)
    assert resultat["destinations"] == [
        {"code": "CDG", "prix_max": None, "direct_seulement": False}
    ]
