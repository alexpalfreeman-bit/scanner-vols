from unittest.mock import Mock

import pytest

from providers import duffel


def test_import_duffel() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import providers.duffel  # noqa: F401


# ---------------------------------------------------------------- post_resilient (1.4)


def test_post_resilient_429_avec_retry_after_puis_succes(monkeypatch) -> None:
    monkeypatch.setattr(duffel.time, "sleep", lambda _: None)
    monkeypatch.setattr(duffel.random, "uniform", lambda a, b: 0)
    reponse_429 = Mock(status_code=429, headers={"Retry-After": "2"})
    reponse_ok = Mock(status_code=200, headers={})
    session = Mock()
    session.post.side_effect = [reponse_429, reponse_ok]

    resultat = duffel.post_resilient(session, "https://exemple.test", {})

    assert resultat is reponse_ok
    assert session.post.call_count == 2


def test_post_resilient_echecs_consecutifs_leve_exception(monkeypatch) -> None:
    monkeypatch.setattr(duffel.time, "sleep", lambda _: None)
    monkeypatch.setattr(duffel.random, "uniform", lambda a, b: 0)
    session = Mock()
    session.post.return_value = Mock(status_code=503, headers={})

    with pytest.raises(RuntimeError):
        duffel.post_resilient(session, "https://exemple.test", {}, essais=4)

    assert session.post.call_count == 4


def test_post_resilient_404_retour_immediat_sans_retry(monkeypatch) -> None:
    monkeypatch.setattr(duffel.time, "sleep", lambda _: None)
    reponse_404 = Mock(status_code=404, headers={})
    session = Mock()
    session.post.return_value = reponse_404

    resultat = duffel.post_resilient(session, "https://exemple.test", {})

    assert resultat is reponse_404
    assert session.post.call_count == 1


# ---------------------------------------------------------------- extraire_message_erreur (1.3)


def test_extraire_message_erreur_json_valide() -> None:
    r = Mock()
    r.json.return_value = {
        "errors": [{"type": "invalid_request_error", "title": "Bad", "message": "oops"}]
    }

    assert duffel.extraire_message_erreur(r) == "invalid_request_error: Bad: oops"


def test_extraire_message_erreur_corps_html_502() -> None:
    r = Mock()
    r.json.side_effect = ValueError("pas du JSON")
    r.status_code = 502
    r.text = "<html><body>Bad Gateway</body></html>"

    message = duffel.extraire_message_erreur(r)

    assert "502" in message
