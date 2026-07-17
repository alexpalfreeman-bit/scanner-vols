from unittest.mock import Mock, patch

import pytest
import requests

import alerting
from config import Env


def _env() -> Env:
    return Env(duffel_access_token="x", telegram_bot_token="123:ABC", telegram_chat_id="42")


def test_import_alerting() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import alerting  # noqa: F401


# ---------------------------------------------------------------- formater_alerte (1.5 / 2.5)


def _route(**overrides: object) -> dict:
    base: dict = {"origine": "YUL", "destination": "CDG", "date_depart": "2026-01-01"}
    base.update(overrides)
    return base


def _vol(**overrides: object) -> dict:
    base: dict = {"prix": 500.0, "devise": "USD", "compagnie": "Air Test", "escales": 0}
    base.update(overrides)
    return base


def test_formater_alerte_echappe_html_compagnie() -> None:
    route = {"origine": "YUL", "destination": "CDG", "date_depart": "2026-01-01"}
    vol = {"prix": 500.0, "devise": "USD", "compagnie": "A&B <Air>", "escales": 0}

    message = alerting.formater_alerte(route, vol, ["bonne affaire"], None)

    assert "A&amp;B &lt;Air&gt;" in message
    assert "A&B <Air>" not in message


def test_formater_alerte_echappe_html_devise() -> None:
    """Regression : vol['devise'] n'etait pas echappe avant Phase 2.5."""
    message = alerting.formater_alerte(_route(), _vol(devise="C&D"), ["bonne affaire"], None)

    assert "C&amp;D" in message
    assert "C&D" not in message


def test_formater_alerte_echappe_html_date_retour() -> None:
    route = _route(date_retour="2026-01-15 <script>")

    message = alerting.formater_alerte(route, _vol(), ["bonne affaire"], None)

    assert "2026-01-15 &lt;script&gt;" in message
    assert "<script>" not in message


def test_formater_alerte_vol_direct() -> None:
    message = alerting.formater_alerte(_route(), _vol(escales=0), ["bonne affaire"], None)
    assert "direct" in message


def test_formater_alerte_avec_escales() -> None:
    message = alerting.formater_alerte(_route(), _vol(escales=2), ["bonne affaire"], None)
    assert "2 escale(s)" in message


def test_formater_alerte_aller_simple() -> None:
    message = alerting.formater_alerte(_route(), _vol(), ["bonne affaire"], None)
    assert "(aller simple)" in message


def test_formater_alerte_aller_retour() -> None:
    route = _route(date_retour="2026-01-15")
    message = alerting.formater_alerte(route, _vol(), ["bonne affaire"], None)
    assert "→ retour 2026-01-15" in message
    assert "(aller simple)" not in message


def test_formater_alerte_stats_absent() -> None:
    message = alerting.formater_alerte(_route(), _vol(), ["bonne affaire"], None)
    assert "Première observation pour cette destination" in message


def test_formater_alerte_stats_sans_tendance() -> None:
    stats = {"n": 2, "min": 400.0, "mediane": 450.0, "tendance": None, "variation_pct": None}
    message = alerting.formater_alerte(_route(), _vol(), ["bonne affaire"], stats)
    assert "2 observation(s) — pas encore assez pour une tendance" in message


def test_formater_alerte_tendance_hausse() -> None:
    stats = {"n": 10, "min": 400.0, "mediane": 450.0, "tendance": "hausse", "variation_pct": 0.08}
    message = alerting.formater_alerte(_route(), _vol(), ["bonne affaire"], stats)
    assert "📈 Tendance hausse (+8%)" in message


def test_formater_alerte_tendance_baisse() -> None:
    stats = {
        "n": 10,
        "min": 400.0,
        "mediane": 450.0,
        "tendance": "baisse",
        "variation_pct": -0.12,
    }
    message = alerting.formater_alerte(_route(), _vol(), ["bonne affaire"], stats)
    assert "📉 Tendance baisse (-12%)" in message


def test_formater_alerte_tendance_stable() -> None:
    stats = {"n": 10, "min": 400.0, "mediane": 450.0, "tendance": "stable", "variation_pct": 0.01}
    message = alerting.formater_alerte(_route(), _vol(), ["bonne affaire"], stats)
    assert "➡️ Tendance stable (+1%)" in message


# ---------------------------------------------------------------- envoyer_telegram (retry, 2.5)


@patch("alerting.requests.post")
def test_envoyer_telegram_poste_au_bon_chat(post_mock) -> None:
    post_mock.return_value = Mock(status_code=200, raise_for_status=Mock())

    alerting.envoyer_telegram("un message", _env())

    url_appelee = post_mock.call_args.args[0]
    corps = post_mock.call_args.kwargs["data"]
    assert url_appelee == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert corps == {"chat_id": "42", "text": "un message", "parse_mode": "HTML"}


@patch("alerting.requests.post")
def test_envoyer_telegram_retry_reseau_puis_succes(post_mock, monkeypatch) -> None:
    monkeypatch.setattr(alerting.time, "sleep", lambda _: None)
    monkeypatch.setattr(alerting.random, "uniform", lambda a, b: 0)
    post_mock.side_effect = [
        requests.ConnectionError("boom"),
        Mock(status_code=200, raise_for_status=Mock()),
    ]

    alerting.envoyer_telegram("un message", _env())

    assert post_mock.call_count == 2


@patch("alerting.requests.post")
def test_envoyer_telegram_retry_429_puis_succes(post_mock, monkeypatch) -> None:
    monkeypatch.setattr(alerting.time, "sleep", lambda _: None)
    monkeypatch.setattr(alerting.random, "uniform", lambda a, b: 0)
    post_mock.side_effect = [
        Mock(status_code=429, headers={}),
        Mock(status_code=200, raise_for_status=Mock()),
    ]

    alerting.envoyer_telegram("un message", _env())

    assert post_mock.call_count == 2


@patch("alerting.requests.post")
def test_envoyer_telegram_echecs_consecutifs_leve_exception(post_mock, monkeypatch) -> None:
    monkeypatch.setattr(alerting.time, "sleep", lambda _: None)
    monkeypatch.setattr(alerting.random, "uniform", lambda a, b: 0)
    post_mock.side_effect = requests.ConnectionError("boom")

    with pytest.raises(requests.ConnectionError):
        alerting.envoyer_telegram("un message", _env(), essais=2)

    assert post_mock.call_count == 2


@patch("alerting.requests.post")
def test_envoyer_telegram_4xx_non_transitoire_pas_de_retry(post_mock, monkeypatch) -> None:
    """Le test cle du bloc : prouve que le HTTPError d'un 4xx non-transitoire
    n'est pas capture par le except qui gere les retries (sinon un 401
    permanent serait retente essais fois pour rien)."""
    monkeypatch.setattr(alerting.time, "sleep", lambda _: None)
    reponse = Mock(status_code=400)
    reponse.raise_for_status.side_effect = requests.HTTPError("400 bad request")
    post_mock.return_value = reponse

    with pytest.raises(requests.HTTPError):
        alerting.envoyer_telegram("un message", _env())

    assert post_mock.call_count == 1
