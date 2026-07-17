import logging
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest
import requests

import alerting
from config import Env
from providers.base import ResumeFournisseur


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


# ---------------------------------------------------------------- envoyer_alerte (2.5, gate 2.3.d)

MAINTENANT_ENVOI = datetime(2026, 7, 3, tzinfo=UTC)


def _alerte_precedente(
    *,
    route_id: int = 1,
    date_depart: str = "2026-09-01",
    type_alerte: str = "erreur_prix",
    prix_cents: int = 50_000,
    envoyee_le: str = "2026-07-01T00:00:00+00:00",
) -> dict:
    return {
        "route_id": route_id,
        "date_depart": date_depart,
        "type": type_alerte,
        "prix_cents": prix_cents,
        "envoyee_le": envoyee_le,
    }


def _parametres_envoyer_alerte(**overrides: object) -> dict:
    base: dict = {
        "route": _route(date_depart="2026-09-01"),
        "vol": _vol(),
        "raisons": ["bonne affaire"],
        "stats": None,
        "route_id": 1,
        "type_alerte": "erreur_prix",
        "prix_cents": 45_000,
        "alerte_precedente": None,
        "maintenant": MAINTENANT_ENVOI,
        "env": _env(),
    }
    base.update(overrides)
    return base


@patch("alerting.envoyer_telegram")
def test_envoyer_alerte_aucune_alerte_precedente_envoie(telegram_mock) -> None:
    resultat = alerting.envoyer_alerte(**_parametres_envoyer_alerte(alerte_precedente=None))

    assert resultat is True
    telegram_mock.assert_called_once()


@patch("alerting.envoyer_telegram")
def test_envoyer_alerte_dans_cooldown_prix_pas_assez_bas_supprime(telegram_mock) -> None:
    precedente = _alerte_precedente(prix_cents=50_000)  # envoyee il y a 48h < cooldown 72h

    resultat = alerting.envoyer_alerte(
        **_parametres_envoyer_alerte(alerte_precedente=precedente, prix_cents=48_000)
    )  # 96% de l'ancien prix : pas assez bas pour casser le cooldown

    assert resultat is False
    telegram_mock.assert_not_called()


@patch("alerting.envoyer_telegram")
def test_envoyer_alerte_dans_cooldown_prix_nettement_plus_bas_envoie(telegram_mock) -> None:
    precedente = _alerte_precedente(prix_cents=50_000)

    resultat = alerting.envoyer_alerte(
        **_parametres_envoyer_alerte(alerte_precedente=precedente, prix_cents=40_000)
    )  # 80% de l'ancien prix : casse le cooldown

    assert resultat is True
    telegram_mock.assert_called_once()


@patch("alerting.envoyer_telegram")
@patch("alerting.doit_alerter")
def test_envoyer_alerte_derive_date_depart_depuis_route(doit_alerter_mock, telegram_mock) -> None:
    doit_alerter_mock.return_value = True
    route = _route(date_depart="2026-11-20")

    alerting.envoyer_alerte(**_parametres_envoyer_alerte(route=route))

    assert doit_alerter_mock.call_args.kwargs["date_depart"] == "2026-11-20"


def test_envoyer_alerte_propage_exception_horloge_naive() -> None:
    with pytest.raises(ValueError):
        alerting.envoyer_alerte(**_parametres_envoyer_alerte(maintenant=datetime(2026, 7, 3)))


@patch("alerting.envoyer_telegram")
def test_envoyer_alerte_propage_exception_telegram(telegram_mock) -> None:
    telegram_mock.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        alerting.envoyer_alerte(**_parametres_envoyer_alerte())


@patch("alerting.envoyer_telegram")
def test_envoyer_alerte_log_info_quand_supprimee(telegram_mock, caplog) -> None:
    precedente = _alerte_precedente(prix_cents=50_000)

    with caplog.at_level(logging.INFO, logger="alerting"):
        alerting.envoyer_alerte(
            **_parametres_envoyer_alerte(alerte_precedente=precedente, prix_cents=48_000)
        )

    assert "alerte supprimee" in caplog.text


# ---------------------------------------------------------------- digest technique (2.5)


def _resume(**overrides: object) -> ResumeFournisseur:
    base: dict = {
        "nom": "duffel",
        "appels_reussis": 10,
        "appels_zero_offres": 2,
        "echecs_total": 0,
        "suspendu": False,
    }
    base.update(overrides)
    return ResumeFournisseur(**base)


# --- digest_necessaire


def test_digest_necessaire_run_sain_pas_de_digest() -> None:
    resultats = [("YUL→CDG (2026-09-01)", "ok : 500 USD")]
    resumes = [_resume(appels_reussis=10, appels_zero_offres=2)]  # 20%, sous le seuil

    assert alerting.digest_necessaire(resultats, resumes) is False


def test_digest_necessaire_route_en_erreur() -> None:
    resultats = [("YUL→CUN (2026-08-01)", "ERREUR API : HTTP 502")]

    assert alerting.digest_necessaire(resultats, []) is True


def test_digest_necessaire_fournisseur_suspendu() -> None:
    resumes = [_resume(suspendu=True)]

    assert alerting.digest_necessaire([], resumes) is True


def test_digest_necessaire_taux_zero_offre_anormal() -> None:
    resumes = [_resume(appels_reussis=10, appels_zero_offres=6)]  # 60%

    assert alerting.digest_necessaire([], resumes) is True


def test_digest_necessaire_taux_zero_offre_sous_le_seuil() -> None:
    resumes = [_resume(appels_reussis=10, appels_zero_offres=4)]  # 40%

    assert alerting.digest_necessaire([], resumes) is False


def test_digest_necessaire_frontiere_exacte_seuil() -> None:
    resumes = [_resume(appels_reussis=10, appels_zero_offres=5)]  # exactement 50%

    assert alerting.digest_necessaire([], resumes) is False  # strictement superieur requis


def test_digest_necessaire_taux_eleve_mais_trop_peu_dappels() -> None:
    resumes = [_resume(appels_reussis=4, appels_zero_offres=4)]  # 100%, sous le minimum (5)

    assert alerting.digest_necessaire([], resumes) is False


def test_digest_necessaire_appels_reussis_zero_pas_de_crash() -> None:
    resumes = [_resume(appels_reussis=0, appels_zero_offres=0)]

    assert alerting.digest_necessaire([], resumes) is False


def test_digest_necessaire_entrees_vides() -> None:
    assert alerting.digest_necessaire([], []) is False


# --- formater_digest


def test_formater_digest_compte_ok_et_erreur() -> None:
    resultats = [
        ("YUL→CDG (2026-09-01)", "ok : 500 USD"),
        ("YUL→CUN (2026-08-01)", "ERREUR API : HTTP 502"),
        ("YUL→NRT (2026-10-01)", "alerte envoyée : 800 USD"),
    ]

    message = alerting.formater_digest(resultats, [])

    assert "2/3 routes OK, 1 en erreur" in message


def test_formater_digest_echappe_details_erreur_html_brut() -> None:
    resultats = [("YUL→CUN (2026-08-01)", "ERREUR API : HTTP 502 : <html><body>oops</body></html>")]

    message = alerting.formater_digest(resultats, [])

    assert "<html>" not in message
    assert "&lt;html&gt;" in message


def test_formater_digest_echappe_nom_fournisseur() -> None:
    resumes = [_resume(nom="duf&fel")]

    message = alerting.formater_digest([], resumes)

    assert "duf&amp;fel" in message
    assert "duf&fel" not in message


def test_formater_digest_mentionne_suspendu() -> None:
    resumes = [_resume(nom="duffel", suspendu=True)]

    message = alerting.formater_digest([], resumes)

    assert "SUSPENDU" in message


def test_formater_digest_plafonne_routes_affichees() -> None:
    resultats = [(f"YUL→XX{i} (2026-09-01)", "ERREUR API : boom") for i in range(25)]

    message = alerting.formater_digest(resultats, [])

    assert message.count("ERREUR API") == alerting.MAX_ROUTES_ERREUR_AFFICHEES
    assert "5 autre(s)" in message


def test_formater_digest_tronque_detail_trop_long() -> None:
    detail = "ERREUR" + "x" * 300
    resultats = [("YUL→CUN (2026-08-01)", detail)]

    message = alerting.formater_digest(resultats, [])

    assert detail[: alerting.MAX_LONGUEUR_DETAIL_ERREUR] in message
    assert detail[: alerting.MAX_LONGUEUR_DETAIL_ERREUR + 1] not in message


def test_formater_digest_reste_sous_la_limite_telegram() -> None:
    resultats = [(f"YUL→XX{i} (2026-09-01)", "ERREUR API : " + "x" * 300) for i in range(50)]
    resumes = [_resume(nom="duffel", suspendu=True), _resume(nom="amadeus")]

    message = alerting.formater_digest(resultats, resumes)

    assert len(message) < 4096


def test_formater_digest_ne_liste_pas_les_routes_ok() -> None:
    resultats = [
        ("YUL→CDG (2026-09-01)", "ok : 500 USD"),
        ("YUL→CUN (2026-08-01)", "ERREUR API : boom"),
    ]

    message = alerting.formater_digest(resultats, [])

    assert "YUL→CDG" not in message
    assert "YUL→CUN" in message


def test_formater_digest_entrees_vides_pas_de_crash() -> None:
    message = alerting.formater_digest([], [])

    assert "0/0 routes OK, 0 en erreur" in message


# --- envoyer_digest


@patch("alerting.envoyer_telegram")
def test_envoyer_digest_necessaire_envoie_et_retourne_true(telegram_mock) -> None:
    resultats = [("YUL→CUN (2026-08-01)", "ERREUR API : boom")]

    resultat = alerting.envoyer_digest(resultats, [], _env())

    assert resultat is True
    telegram_mock.assert_called_once()


@patch("alerting.envoyer_telegram")
def test_envoyer_digest_non_necessaire_ne_envoie_pas_et_retourne_false(telegram_mock) -> None:
    resultats = [("YUL→CDG (2026-09-01)", "ok : 500 USD")]

    resultat = alerting.envoyer_digest(resultats, [], _env())

    assert resultat is False
    telegram_mock.assert_not_called()
