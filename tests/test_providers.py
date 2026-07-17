import json
import logging
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from config import Env
from providers import duffel
from providers.base import (
    ErreurFournisseur,
    ErreurFournisseurSuspendu,
    ErreurValidationReponse,
    Offre,
)

FIXTURES = Path(__file__).parent / "fixtures"


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


# ---------------------------------------------------------------- FournisseurDuffel (2.4)


def _env() -> Env:
    return Env(duffel_access_token="duffel_test_x", telegram_bot_token="tg_x", telegram_chat_id="1")


def _fournisseur(session: Mock, config: dict | None = None) -> duffel.FournisseurDuffel:
    return duffel.FournisseurDuffel(
        _env(), config or {"adultes": 1, "classe": "economy"}, session=session
    )


def _route() -> dict:
    return {
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-11-15",
        "date_retour": "2026-11-27",
    }


def _reponse_mock(corps: dict, status_code: int = 200) -> Mock:
    r = Mock(status_code=status_code, ok=200 <= status_code < 300)
    r.json.return_value = corps
    return r


def _reponse_une_offre(total_amount: str, segment: dict, *, currency: str = "USD") -> dict:
    return {
        "data": {
            "offers": [
                {
                    "total_amount": total_amount,
                    "total_currency": currency,
                    "slices": [{"segments": [segment]}],
                }
            ]
        }
    }


def test_fournisseur_duffel_expose_nom() -> None:
    assert duffel.FournisseurDuffel.nom == "duffel"


def test_meilleure_offre_valide_et_selectionne_la_moins_chere() -> None:
    fixture = json.loads((FIXTURES / "duffel_offer_request.json").read_text(encoding="utf-8"))
    session = Mock()
    session.post.return_value = _reponse_mock(fixture)
    fournisseur = _fournisseur(session)

    offre = fournisseur.meilleure_offre(_route())

    assert offre == Offre(
        prix_cents=41230, devise="USD", compagnie="United Airlines", escales=1, fournisseur="duffel"
    )


def test_compagnie_repli_marketing_carrier_si_operating_absent() -> None:
    session = Mock()
    segment = {"marketing_carrier": {"name": "Air Transat"}}
    session.post.return_value = _reponse_mock(_reponse_une_offre("300.00", segment))
    fournisseur = _fournisseur(session)

    offre = fournisseur.meilleure_offre(_route())

    assert offre is not None
    assert offre.compagnie == "Air Transat"


def test_compagnie_repli_point_interrogation_si_les_deux_absents() -> None:
    session = Mock()
    session.post.return_value = _reponse_mock(_reponse_une_offre("300.00", {}))
    fournisseur = _fournisseur(session)

    offre = fournisseur.meilleure_offre(_route())

    assert offre is not None
    assert offre.compagnie == "?"


def test_offers_absent_retourne_none() -> None:
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {}})
    fournisseur = _fournisseur(session)

    assert fournisseur.meilleure_offre(_route()) is None


def test_offers_null_explicite_retourne_none() -> None:
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": None}})
    fournisseur = _fournisseur(session)

    assert fournisseur.meilleure_offre(_route()) is None


def test_offers_liste_vide_retourne_none() -> None:
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": []}})
    fournisseur = _fournisseur(session)

    assert fournisseur.meilleure_offre(_route()) is None


def test_total_amount_manquant_leve_erreur_validation_reponse() -> None:
    offre_invalide = {
        "total_currency": "USD",
        "slices": [{"segments": [{"marketing_carrier": {"name": "X"}}]}],
    }
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": [offre_invalide]}})
    fournisseur = _fournisseur(session)

    with pytest.raises(ErreurValidationReponse):
        fournisseur.meilleure_offre(_route())


def test_segments_vides_leve_erreur_validation_reponse() -> None:
    offre_invalide = {
        "total_amount": "300.00",
        "total_currency": "USD",
        "slices": [{"segments": []}],
    }
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": [offre_invalide]}})
    fournisseur = _fournisseur(session)

    with pytest.raises(ErreurValidationReponse):
        fournisseur.meilleure_offre(_route())


def test_corps_non_json_leve_erreur_validation_reponse() -> None:
    r = Mock(status_code=200, ok=True)
    r.json.side_effect = ValueError("pas du JSON")
    session = Mock()
    session.post.return_value = r
    fournisseur = _fournisseur(session)

    with pytest.raises(ErreurValidationReponse):
        fournisseur.meilleure_offre(_route())


def test_http_non_ok_leve_erreur_fournisseur() -> None:
    r = Mock(status_code=400, ok=False)
    r.json.return_value = {
        "errors": [{"type": "invalid_request_error", "title": "Bad", "message": "oops"}]
    }
    session = Mock()
    session.post.return_value = r
    fournisseur = _fournisseur(session)

    with pytest.raises(ErreurFournisseur, match="oops"):
        fournisseur.meilleure_offre(_route())


@pytest.mark.parametrize(
    ("montant", "attendu"),
    [
        (Decimal("532.10"), 53210),
        (Decimal("412.3"), 41230),
        (Decimal("532.105"), 53211),  # ROUND_HALF_UP
        (Decimal("0.00"), 0),
    ],
)
def test_centimes_depuis_montant(montant: Decimal, attendu: int) -> None:
    assert duffel._centimes_depuis_montant(montant) == attendu


# ---------------------------------------------------------------- circuit breaker (2.4)


def test_circuit_breaker_suspend_apres_5_echecs_consecutifs(monkeypatch) -> None:
    monkeypatch.setattr(duffel, "post_resilient", Mock(side_effect=RuntimeError("boom")))
    fournisseur = _fournisseur(Mock())

    for _ in range(5):
        with pytest.raises(ErreurFournisseur):
            fournisseur.meilleure_offre(_route())

    assert fournisseur._suspendu is True


def test_circuit_breaker_sous_le_seuil_ne_suspend_pas(monkeypatch) -> None:
    monkeypatch.setattr(duffel, "post_resilient", Mock(side_effect=RuntimeError("boom")))
    fournisseur = _fournisseur(Mock())

    for _ in range(4):
        with pytest.raises(ErreurFournisseur):
            fournisseur.meilleure_offre(_route())

    assert fournisseur._suspendu is False


def test_circuit_breaker_aucun_appel_reseau_une_fois_suspendu(monkeypatch) -> None:
    post_resilient_mock = Mock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(duffel, "post_resilient", post_resilient_mock)
    fournisseur = _fournisseur(Mock())
    for _ in range(5):
        with pytest.raises(ErreurFournisseur):
            fournisseur.meilleure_offre(_route())
    appels_avant = post_resilient_mock.call_count

    with pytest.raises(ErreurFournisseurSuspendu):
        fournisseur.meilleure_offre(_route())

    assert post_resilient_mock.call_count == appels_avant


def test_circuit_breaker_reset_apres_succes_intercale(monkeypatch) -> None:
    fixture = json.loads((FIXTURES / "duffel_offer_request.json").read_text(encoding="utf-8"))
    post_resilient_mock = Mock(
        side_effect=[
            RuntimeError("boom"),
            RuntimeError("boom"),
            RuntimeError("boom"),
            RuntimeError("boom"),
            _reponse_mock(fixture),
            RuntimeError("boom"),
            RuntimeError("boom"),
            RuntimeError("boom"),
            RuntimeError("boom"),
        ]
    )
    monkeypatch.setattr(duffel, "post_resilient", post_resilient_mock)
    fournisseur = _fournisseur(Mock())

    for _ in range(4):
        with pytest.raises(ErreurFournisseur):
            fournisseur.meilleure_offre(_route())
    fournisseur.meilleure_offre(_route())  # succes -> reset le compteur consecutif
    for _ in range(4):
        with pytest.raises(ErreurFournisseur):
            fournisseur.meilleure_offre(_route())

    assert fournisseur._suspendu is False


# ---------------------------------------------------------------- verifier_canari (2.4)


def _config_canari() -> dict:
    return {"origine": "YUL", "adultes": 1, "classe": "economy"}


def test_verifier_canari_succes_retourne_true() -> None:
    fixture = json.loads((FIXTURES / "duffel_offer_request.json").read_text(encoding="utf-8"))
    session = Mock()
    session.post.return_value = _reponse_mock(fixture)
    fournisseur = _fournisseur(session, _config_canari())

    assert fournisseur.verifier_canari() is True


def test_verifier_canari_exception_retourne_false_sans_propager(monkeypatch) -> None:
    monkeypatch.setattr(duffel, "post_resilient", Mock(side_effect=RuntimeError("boom")))
    fournisseur = _fournisseur(Mock(), _config_canari())

    assert fournisseur.verifier_canari() is False


def test_verifier_canari_zero_offre_retourne_false() -> None:
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": []}})
    fournisseur = _fournisseur(session, _config_canari())

    assert fournisseur.verifier_canari() is False


def test_verifier_canari_construit_une_route_aller_simple_vers_jfk() -> None:
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": []}})
    fournisseur = _fournisseur(session, _config_canari())

    fournisseur.verifier_canari()

    corps = session.post.call_args.kwargs["json"]
    slices = corps["data"]["slices"]
    assert len(slices) == 1
    assert slices[0]["origin"] == "YUL"
    assert slices[0]["destination"] == "JFK"


def test_verifier_canari_partage_le_compteur_dechecs_avec_les_routes(monkeypatch) -> None:
    monkeypatch.setattr(duffel, "post_resilient", Mock(side_effect=RuntimeError("boom")))
    fournisseur = _fournisseur(Mock(), _config_canari())

    fournisseur.verifier_canari()  # 1er echec de la chronologie
    for _ in range(4):  # 4 echecs de plus -> 5 au total
        with pytest.raises(ErreurFournisseur):
            fournisseur.meilleure_offre(_route())

    assert fournisseur._suspendu is True


# ---------------------------------------------------------------- resume (2.4)


def test_resume_compte_correctement_y_compris_echecs_total_apres_reset(monkeypatch) -> None:
    fixture = json.loads((FIXTURES / "duffel_offer_request.json").read_text(encoding="utf-8"))
    post_resilient_mock = Mock(
        side_effect=[
            RuntimeError("boom"),
            RuntimeError("boom"),
            RuntimeError("boom"),
            _reponse_mock(fixture),  # succes -> reset le compteur consecutif
            RuntimeError("boom"),
        ]
    )
    monkeypatch.setattr(duffel, "post_resilient", post_resilient_mock)
    fournisseur = _fournisseur(Mock())

    for _ in range(3):
        with pytest.raises(ErreurFournisseur):
            fournisseur.meilleure_offre(_route())
    fournisseur.meilleure_offre(_route())
    with pytest.raises(ErreurFournisseur):
        fournisseur.meilleure_offre(_route())

    resume = fournisseur.resume()

    assert resume.echecs_total == 4  # 3 + 1, malgre le reset du compteur consecutif au succes
    assert resume.appels_reussis == 1
    assert resume.appels_zero_offres == 0
    assert resume.suspendu is False
    assert resume.nom == "duffel"


def test_resume_avertit_si_taux_zero_offres_anormal(caplog) -> None:
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": []}})
    fournisseur = _fournisseur(session)

    for _ in range(6):
        fournisseur.meilleure_offre(_route())

    with caplog.at_level(logging.WARNING, logger="providers.duffel"):
        resume = fournisseur.resume()

    assert resume.appels_zero_offres == 6
    assert "anormalement eleve" in caplog.text


def test_resume_pas_davertissement_sous_le_seuil(caplog) -> None:
    fixture = json.loads((FIXTURES / "duffel_offer_request.json").read_text(encoding="utf-8"))
    session = Mock()
    session.post.side_effect = [_reponse_mock({"data": {"offers": []}})] * 2 + [
        _reponse_mock(fixture)
    ] * 8
    fournisseur = _fournisseur(session)

    for _ in range(10):
        fournisseur.meilleure_offre(_route())

    with caplog.at_level(logging.WARNING, logger="providers.duffel"):
        fournisseur.resume()

    assert "anormalement eleve" not in caplog.text


def test_resume_pas_davertissement_si_trop_peu_dappels(caplog) -> None:
    session = Mock()
    session.post.return_value = _reponse_mock({"data": {"offers": []}})
    fournisseur = _fournisseur(session)

    for _ in range(4):  # sous APPELS_REUSSIS_MIN_POUR_AVERTISSEMENT
        fournisseur.meilleure_offre(_route())

    with caplog.at_level(logging.WARNING, logger="providers.duffel"):
        resume = fournisseur.resume()

    assert resume.appels_zero_offres == 4
    assert "anormalement eleve" not in caplog.text
