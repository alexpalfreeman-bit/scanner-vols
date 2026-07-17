from unittest.mock import Mock, patch

import alerting
from config import Env


def test_import_alerting() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import alerting  # noqa: F401


# ---------------------------------------------------------------- formater_alerte (1.5)


def test_formater_alerte_echappe_html_compagnie() -> None:
    route = {"origine": "YUL", "destination": "CDG", "date_depart": "2026-01-01"}
    vol = {"prix": 500.0, "devise": "USD", "compagnie": "A&B <Air>", "escales": 0}

    message = alerting.formater_alerte(route, vol, ["bonne affaire"], None)

    assert "A&amp;B &lt;Air&gt;" in message
    assert "A&B <Air>" not in message


# ---------------------------------------------------------------- envoyer_telegram


@patch("alerting.requests.post")
def test_envoyer_telegram_poste_au_bon_chat(post_mock) -> None:
    post_mock.return_value = Mock(raise_for_status=Mock())
    env = Env(duffel_access_token="x", telegram_bot_token="123:ABC", telegram_chat_id="42")

    alerting.envoyer_telegram("un message", env)

    url_appelee = post_mock.call_args.args[0]
    corps = post_mock.call_args.kwargs["data"]
    assert url_appelee == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert corps == {"chat_id": "42", "text": "un message", "parse_mode": "HTML"}
