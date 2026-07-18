import logging
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import scanner
import storage
from providers.base import Offre, ResumeFournisseur


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


# ---------------------------------------------------------------- horodatage_maintenant (1.6)


def test_horodatage_maintenant_precision_seconde_et_parsable(monkeypatch) -> None:
    dt = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
    horloge = Mock()
    horloge.now.return_value = dt
    monkeypatch.setattr(scanner, "datetime", horloge)

    resultat = scanner.horodatage_maintenant()

    assert datetime.fromisoformat(resultat) == dt


def test_horodatage_maintenant_appelle_horloge_a_chaque_fois(monkeypatch) -> None:
    dt1 = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
    dt2 = datetime(2026, 1, 1, 12, 0, 2, tzinfo=UTC)
    horloge = Mock()
    horloge.now.side_effect = [dt1, dt2]
    monkeypatch.setattr(scanner, "datetime", horloge)

    t1 = scanner.horodatage_maintenant()
    t2 = scanner.horodatage_maintenant()

    assert t1 != t2
    assert horloge.now.call_count == 2


# ---------------------------------------------------------------- main() : politique de sortie


def _config_minimal(nb_destinations: int) -> dict:
    """Config minimale mais valide vis-à-vis de config.valider_config (codes
    IATA à 3 lettres, devise présente) : main() valide réellement charger_config()."""
    return {
        "origine": "YUL",
        "devise": "USD",
        "destinations": [{"code": chr(65 + i) * 3} for i in range(nb_destinations)],
    }


def _offre_sans_alerte() -> Offre:
    return Offre(
        prix_cents=50_000, devise="USD", compagnie="Test Air", escales=0, fournisseur="duffel"
    )


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_reussit_si_au_moins_une_route_reussit(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    envoyer_digest_mock,
) -> None:
    charger_config_mock.return_value = _config_minimal(3)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": f"D{i}", "date_depart": "2026-01-01"} for i in range(3)
    ]
    # 2 routes en erreur API, 1 route réussit sans déclencher d'alerte.
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = [
        RuntimeError("boom"),
        RuntimeError("boom"),
        _offre_sans_alerte(),
    ]

    assert scanner.main() == 0


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_echoue_si_toutes_les_routes_echouent(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    envoyer_digest_mock,
) -> None:
    charger_config_mock.return_value = _config_minimal(3)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": f"D{i}", "date_depart": "2026-01-01"} for i in range(3)
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = RuntimeError("boom")

    assert scanner.main() == 1


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_appelle_verifier_canari_et_resume_une_fois(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    envoyer_digest_mock,
) -> None:
    charger_config_mock.return_value = _config_minimal(2)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": f"D{i}", "date_depart": "2026-01-01"} for i in range(2)
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = [
        _offre_sans_alerte(),
        _offre_sans_alerte(),
    ]

    scanner.main()

    fournisseur_classe_mock.return_value.verifier_canari.assert_called_once()
    fournisseur_classe_mock.return_value.resume.assert_called_once()


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_appelle_envoyer_digest_avec_le_resume_du_fournisseur(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    envoyer_digest_mock,
) -> None:
    """Verifie le contenu transmis a envoyer_digest (pas juste qu'un digest
    part) : un bug qui passerait resumes=[] par erreur resterait invisible
    si on ne verifiait que digest_necessaire/formater_digest isolement (deja
    testes a fond dans test_alerting.py)."""
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test123"
    charger_config_mock.return_value = _config_minimal(1)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": "AAA", "date_depart": "2026-01-01"}
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_sans_alerte()
    resume_attendu = ResumeFournisseur(
        nom="duffel", appels_reussis=1, appels_zero_offres=0, echecs_total=0, suspendu=False
    )
    fournisseur_classe_mock.return_value.resume.return_value = resume_attendu

    scanner.main()

    envoyer_digest_mock.assert_called_once()
    args = envoyer_digest_mock.call_args.args
    assert args[1] == [resume_attendu]
    assert envoyer_digest_mock.call_args.kwargs["budget_corroboration_epuise"] is False


# ---------------------------------------------------------------- logging (1.7)


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_erreur_api_est_loggee_en_error(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    envoyer_digest_mock,
    caplog,
) -> None:
    charger_config_mock.return_value = _config_minimal(1)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": "AAA", "date_depart": "2026-01-01"}
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="scanner"):
        scanner.main()

    assert "boom" in caplog.text
    assert "ERROR" in caplog.text


# ---------------------------------------------------------------- environnement Duffel (audit data/scanner.db)


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_logge_avertissement_si_token_sandbox(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    envoyer_digest_mock,
    caplog,
) -> None:
    charger_env_mock.return_value.duffel_access_token = "duffel_test_abc123"
    charger_config_mock.return_value = _config_minimal(1)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": "AAA", "date_depart": "2026-01-01"}
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_sans_alerte()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        scanner.main()

    assert "sandbox" in caplog.text
    assert "WARNING" in caplog.text


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_pas_davertissement_si_token_production(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    envoyer_digest_mock,
    caplog,
) -> None:
    charger_env_mock.return_value.duffel_access_token = "duffel_live_abc123"
    charger_config_mock.return_value = _config_minimal(1)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": "AAA", "date_depart": "2026-01-01"}
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_sans_alerte()

    with caplog.at_level(logging.INFO, logger="scanner"):
        scanner.main()

    assert "environnement Duffel detecte : production" in caplog.text
    assert "WARNING" not in caplog.text


@patch("scanner.envoyer_digest")
@patch("scanner.ecrire_resume_github")
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations", return_value=[])
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
@patch("scanner.enregistrer_observation", return_value=1)
def test_main_transmet_environnement_a_enregistrer_observation(
    enregistrer_observation_mock,
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    ecrire_resume_mock,
    envoyer_digest_mock,
) -> None:
    charger_env_mock.return_value.duffel_access_token = "duffel_test_abc123"
    charger_config_mock.return_value = _config_minimal(1)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": "AAA", "date_depart": "2026-01-01"}
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_sans_alerte()

    scanner.main()

    assert enregistrer_observation_mock.call_args.kwargs["environnement"] == "sandbox"


# ---------------------------------------------------------------- _tenter_alerte : garde-fou environnement


def _params_tenter_alerte(**overrides: object) -> dict:
    base: dict = {
        "route": {"origine": "YUL", "destination": "CDG", "date_depart": "2026-09-01"},
        "vol": {"prix": 350.0, "devise": "USD", "compagnie": "Test Air", "escales": 0},
        "stats": None,
        "route_id": 1,
        "type_alerte": "seuil",
        "raisons": ["sous le seuil"],
        "prix_cents": 35_000,
        "maintenant": datetime(2026, 7, 18, tzinfo=UTC),
        "env": Mock(),
        "environnement": "production",
    }
    base.update(overrides)
    return base


@patch("scanner.envoyer_alerte")
@patch("scanner.obtenir_derniere_alerte")
def test_tenter_alerte_sandbox_ne_verifie_ni_envoie_rien(
    obtenir_derniere_alerte_mock, envoyer_alerte_mock
) -> None:
    resultat = scanner._tenter_alerte(**_params_tenter_alerte(environnement="sandbox"))

    assert resultat == (False, None)
    obtenir_derniere_alerte_mock.assert_not_called()
    envoyer_alerte_mock.assert_not_called()


@patch("scanner.envoyer_alerte")
@patch("scanner.obtenir_derniere_alerte")
def test_tenter_alerte_inconnu_ne_verifie_ni_envoie_rien(
    obtenir_derniere_alerte_mock, envoyer_alerte_mock
) -> None:
    resultat = scanner._tenter_alerte(**_params_tenter_alerte(environnement="inconnu"))

    assert resultat == (False, None)
    obtenir_derniere_alerte_mock.assert_not_called()
    envoyer_alerte_mock.assert_not_called()


@patch("scanner.envoyer_alerte")
@patch("scanner.obtenir_derniere_alerte", return_value=None)
def test_tenter_alerte_production_fonctionne_normalement(
    obtenir_derniere_alerte_mock, envoyer_alerte_mock
) -> None:
    envoyer_alerte_mock.return_value = True

    resultat = scanner._tenter_alerte(**_params_tenter_alerte(environnement="production"))

    assert resultat == (True, None)
    obtenir_derniere_alerte_mock.assert_called_once()
    envoyer_alerte_mock.assert_called_once()


def test_tenter_alerte_sandbox_logge_la_suppression(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="scanner"):
        scanner._tenter_alerte(
            **_params_tenter_alerte(environnement="sandbox", type_alerte="minimum")
        )

    assert "minimum" in caplog.text
    assert "sandbox" in caplog.text


# ---------------------------------------------------------------- classification z-score (Session A/B)


@patch("scanner.envoyer_digest")
@patch("scanner.envoyer_alerte")
@patch("scanner.obtenir_derniere_alerte", return_value=None)
@patch("scanner.ecrire_resume_github")
@patch("scanner.enregistrer_observation", return_value=1)
@patch("scanner.statistiques_destination", return_value=None)
@patch("scanner.generer_candidats")
@patch("scanner.lire_observations")
@patch("scanner.lire_historique", return_value=[])
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_main_logge_la_classification_z_score(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    lire_historique_mock,
    lire_observations_mock,
    generer_candidats_mock,
    stats_mock,
    enregistrer_observation_mock,
    resume_mock,
    obtenir_derniere_alerte_mock,
    envoyer_alerte_mock,
    envoyer_digest_mock,
    caplog,
) -> None:
    """Preuve que le nouveau moteur (echantillon_comparable -> classifier)
    calcule reellement la classification et la loggue. Le cablage complet
    (classification -> envoyer_alerte, AUDIT.md Session B) est verifie de
    bout en bout par les tests dedies plus bas (section "cablage go-live") ;
    ici, envoyer_alerte/envoyer_digest sont mockes pour rester hermetique et
    ce test se concentre uniquement sur la ligne de log."""
    charger_config_mock.return_value = _config_minimal(1)
    generer_candidats_mock.return_value = [
        {"origine": "YUL", "destination": "AAA", "date_depart": "2026-01-01"}
    ]
    # 8 observations comparables (route_id=1, meme mois que la candidate,
    # devise USD) avec une vraie dispersion (MAD non nul) : l'offre a 20000
    # cents est nettement sous ce groupe -> candidat_erreur_prix attendu.
    lire_observations_mock.return_value = [
        {
            "route_id": 1,
            "devise": "USD",
            "observe_le": "2026-01-01T00:00:00+00:00",
            "date_depart": "2026-01-01",
            "horizon_jours": 0,
            "prix_cents": prix,
        }
        for prix in (40_000, 45_000, 48_000, 49_000, 51_000, 52_000, 55_000, 60_000)
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = Offre(
        prix_cents=20_000, devise="USD", compagnie="Test Air", escales=0, fournisseur="duffel"
    )

    with caplog.at_level(logging.INFO, logger="scanner"):
        scanner.main()

    assert "classification z-score = candidat_erreur_prix" in caplog.text


# ---------------------------------------------------------------- _route_avec_date_depart (Session B)


def test_route_avec_date_depart_translate_aussi_le_retour() -> None:
    route = {
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-15",
        "date_retour": "2026-06-27",
    }

    resultat = scanner._route_avec_date_depart(route, "2026-06-18")

    assert resultat["date_depart"] == "2026-06-18"
    assert resultat["date_retour"] == "2026-06-30"  # meme delta (+3 jours)


def test_route_avec_date_depart_aller_simple_ne_fabrique_pas_de_retour() -> None:
    route = {"origine": "YUL", "destination": "CDG", "date_depart": "2026-06-15"}

    resultat = scanner._route_avec_date_depart(route, "2026-06-12")

    assert resultat["date_depart"] == "2026-06-12"
    assert "date_retour" not in resultat


def test_route_avec_date_depart_ne_mute_pas_la_route_originale() -> None:
    route = {"origine": "YUL", "destination": "CDG", "date_depart": "2026-06-15"}

    scanner._route_avec_date_depart(route, "2026-06-12")

    assert route["date_depart"] == "2026-06-15"


# ---------------------------------------------------------------- sonder_corroboration (Session B)


def _route_corrob() -> dict:
    return {"origine": "YUL", "destination": "CDG", "date_depart": "2026-06-15"}


def _offre_corrob(prix_cents: int, devise: str = "USD") -> Offre:
    return Offre(
        prix_cents=prix_cents, devise=devise, compagnie="Test Air", escales=0, fournisseur="duffel"
    )


def test_sonder_corroboration_budget_zero_aucun_appel() -> None:
    fournisseur = Mock()

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 0)

    fournisseur.meilleure_offre.assert_not_called()
    assert signaux.prix_requete_immediate_cents is None
    assert signaux.prix_dates_voisines_cents == ()
    assert budget == 0


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_budget_un_seul_signal_1_uniquement() -> None:
    fournisseur = Mock()
    fournisseur.meilleure_offre.return_value = _offre_corrob(19_000)

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 1)

    assert fournisseur.meilleure_offre.call_count == 1
    assert signaux.prix_requete_immediate_cents == 19_000
    assert signaux.prix_dates_voisines_cents == ()
    assert budget == 0


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_budget_deux_une_seule_voisine_sondee() -> None:
    """Le minimum reel pour qu'un verdict erreur_prix devienne atteignable
    cette session : signal 2 ne demande qu'UNE seule date voisine confirmante,
    pas les deux (cf. detection.corroborer_erreur_prix / test_detection.py)."""
    fournisseur = Mock()
    fournisseur.meilleure_offre.return_value = _offre_corrob(19_000)

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 2)

    assert fournisseur.meilleure_offre.call_count == 2
    assert len(signaux.prix_dates_voisines_cents) == 1
    assert budget == 0


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_budget_suffisant_deux_voisines_sondees() -> None:
    fournisseur = Mock()
    fournisseur.meilleure_offre.return_value = _offre_corrob(19_000)

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 3)

    assert fournisseur.meilleure_offre.call_count == 3
    assert len(signaux.prix_dates_voisines_cents) == 2
    assert budget == 0


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_budget_large_pas_consomme_au_dela_du_necessaire() -> None:
    fournisseur = Mock()
    fournisseur.meilleure_offre.return_value = _offre_corrob(19_000)

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 10)

    assert fournisseur.meilleure_offre.call_count == 3  # 1 + 2, jamais plus
    assert budget == 7


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_signal_1_exception_traitee_comme_absente() -> None:
    fournisseur = Mock()
    fournisseur.meilleure_offre.side_effect = RuntimeError("boom")

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 3)

    assert signaux.prix_requete_immediate_cents is None
    assert signaux.prix_dates_voisines_cents == ()  # les 2 sondes suivantes echouent aussi
    assert budget == 0  # decompte quand meme


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_signal_2_exception_ne_bloque_pas_lautre_voisine() -> None:
    fournisseur = Mock()
    fournisseur.meilleure_offre.side_effect = [
        _offre_corrob(19_000),  # signal 1 : ok
        RuntimeError("boom"),  # signal 2, date -3j : echoue
        _offre_corrob(19_500),  # signal 2, date +3j : ok
    ]

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 3)

    assert signaux.prix_requete_immediate_cents == 19_000
    assert signaux.prix_dates_voisines_cents == (19_500,)
    assert budget == 0


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_offre_none_traitee_comme_absente() -> None:
    """0 offre (pas une exception) : distinct de l'echec reseau, mais meme
    traitement - signal absent, pas de crash."""
    fournisseur = Mock()
    fournisseur.meilleure_offre.return_value = None

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 3)

    assert signaux.prix_requete_immediate_cents is None
    assert signaux.prix_dates_voisines_cents == ()
    assert budget == 0


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_devise_differente_exclue() -> None:
    fournisseur = Mock()
    fournisseur.meilleure_offre.return_value = _offre_corrob(19_000, devise="EUR")

    signaux, budget = scanner.sonder_corroboration(fournisseur, _route_corrob(), "USD", 3)

    assert signaux.prix_requete_immediate_cents is None
    assert signaux.prix_dates_voisines_cents == ()


@patch("scanner.time.sleep", new=lambda *_: None)
def test_sonder_corroboration_translate_les_dates_voisines() -> None:
    fournisseur = Mock()
    fournisseur.meilleure_offre.return_value = _offre_corrob(19_000)
    route = {**_route_corrob(), "date_retour": "2026-06-27"}

    scanner.sonder_corroboration(fournisseur, route, "USD", 3)

    routes_appelees = [appel.args[0] for appel in fournisseur.meilleure_offre.call_args_list]
    assert routes_appelees[0] == route  # signal 1 : re-requete la route candidate telle quelle
    assert routes_appelees[1]["date_depart"] == "2026-06-12"
    assert routes_appelees[1]["date_retour"] == "2026-06-24"
    assert routes_appelees[2]["date_depart"] == "2026-06-18"
    assert routes_appelees[2]["date_retour"] == "2026-06-30"


# ---------------------------------------------------------------- cablage go-live end-to-end (Session B)
#
# Base SQLite temporaire REELLE (via storage.DB_FILE monkeypatche, isolation
# deja garantie en filet par tests/conftest.py), peuplee avec l'echantillon
# deja verifie par calcul dans tests/test_detection.py (median=47000,
# MAD=3000) : sur la vraie base de prod, le chemin z-score reste muet
# (donnees_insuffisantes partout, n=5-6 < 8) - ces tests sont donc la seule
# preuve que le chemin aubaine/erreur_prix/seuil/minimum fonctionne
# reellement de bout en bout (classifier -> envoyer_alerte -> persistance).
# Seule la frontiere reseau (alerting.envoyer_telegram) est mockee.

_ECHANTILLON_CONNU_CENTS = (40_000, 44_000, 44_000, 46_000, 48_000, 50_000, 50_000, 54_000)
# median=47000, MAD=3000 : 35_000 cents -> bonne_affaire (z ~ -2.70) ;
# 18_800 cents -> candidat_erreur_prix (z ~ -6.34, meme valeur que
# tests/test_detection.py::_PRIX_CANDIDAT).


def _peupler_echantillon(
    origine: str, destination: str, date_depart: str, devise: str = "USD"
) -> int:
    """Peuple 8 observations comparables (meme route/devise, observe_le
    recent - donc dans la fenetre de 18 mois) pour que
    echantillon_comparable/classifier et statistiques_destination tournent
    pour de vrai (pas donnees_insuffisantes/aucune observation). Retourne
    route_id (idempotent : cree la route au premier appel, la reutilise
    ensuite)."""
    route_id = 0
    observe_le = datetime.now(UTC).isoformat(timespec="seconds")
    for prix_cents in _ECHANTILLON_CONNU_CENTS:
        route_id = storage.enregistrer_observation(
            origine=origine,
            destination=destination,
            observe_le=observe_le,
            date_depart=date_depart,
            date_retour=None,
            prix_cents=prix_cents,
            devise=devise,
            compagnie="Test Air",
            escales=0,
            environnement="production",
        )
    return route_id


def _config_e2e(**detection_overrides: object) -> dict:
    return {
        "origine": "YUL",
        "devise": "USD",
        "destinations": [{"code": "CDG"}, {"code": "LHR"}],
        "detection": detection_overrides,
    }


def _route_e2e(**overrides: object) -> dict:
    base: dict = {"origine": "YUL", "destination": "CDG", "date_depart": "2026-09-01"}
    base.update(overrides)
    return base


def _offre_e2e(prix_cents: int, devise: str = "USD") -> Offre:
    return Offre(
        prix_cents=prix_cents, devise=devise, compagnie="Test Air", escales=0, fournisseur="duffel"
    )


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_trois_types_alerte_independants_envoyes_et_persistes(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    """Base peuplee, corroboration desactivee (defaut) : un seul prix qui
    declenche a la fois seuil/minimum/aubaine, chacun envoye ET persiste
    independamment - preuve de bout en bout des 3 messages independants avec
    leur propre dedup (AUDIT.md, Journal Session A/B)."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test_e2e"
    route_id = _peupler_echantillon("YUL", "CDG", "2026-01-01")

    charger_config_mock.return_value = _config_e2e()
    generer_candidats_mock.return_value = [_route_e2e(prix_max=360)]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_e2e(35_000)

    assert scanner.main() == 0
    assert telegram_mock.call_count == 3

    for type_alerte in ("seuil", "minimum", "aubaine"):
        alerte = storage.obtenir_derniere_alerte(route_id, "2026-09-01", type_alerte)
        assert alerte is not None, f"alerte {type_alerte} non persistee"
        assert alerte["prix_cents"] == 35_000


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_deuxieme_run_rapproche_meme_prix_ne_realerte_pas(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test_e2e"
    _peupler_echantillon("YUL", "CDG", "2026-01-01")

    charger_config_mock.return_value = _config_e2e()
    generer_candidats_mock.return_value = [_route_e2e(prix_max=360)]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_e2e(35_000)

    scanner.main()
    assert telegram_mock.call_count == 3

    scanner.main()  # meme prix, quelques millisecondes plus tard : dans le cooldown de 72h

    assert telegram_mock.call_count == 3  # aucun nouvel envoi


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_candidat_erreur_prix_corroboration_desactivee_downgrade_aubaine(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test_e2e"
    route_id = _peupler_echantillon("YUL", "CDG", "2026-01-01")

    charger_config_mock.return_value = _config_e2e(corroboration_activee=False)
    generer_candidats_mock.return_value = [_route_e2e()]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_e2e(18_800)

    scanner.main()

    assert fournisseur_classe_mock.return_value.meilleure_offre.call_count == 1  # aucune re-requete
    assert storage.obtenir_derniere_alerte(route_id, "2026-09-01", "erreur_prix") is None
    alerte = storage.obtenir_derniere_alerte(route_id, "2026-09-01", "aubaine")
    assert alerte is not None
    assert alerte["prix_cents"] == 18_800


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_candidat_erreur_prix_corroboration_activee_confirmee_bonus(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    """Budget large, signaux 1 et 2 tous confirmants (memes valeurs que
    tests/test_detection.py::_signaux_avec) : verdict erreur_prix persiste."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test_e2e"
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    route_id = _peupler_echantillon("YUL", "CDG", "2026-01-01")

    charger_config_mock.return_value = _config_e2e(
        corroboration_activee=True, corroboration_max_requetes_par_run=15
    )
    generer_candidats_mock.return_value = [_route_e2e()]
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = [
        _offre_e2e(18_800),  # fetch original
        _offre_e2e(19_000),  # signal 1 : re-requete
        _offre_e2e(19_500),  # signal 2 : voisine -3j
        _offre_e2e(19_500),  # signal 2 : voisine +3j
    ]

    scanner.main()

    assert fournisseur_classe_mock.return_value.meilleure_offre.call_count == 4
    alerte = storage.obtenir_derniere_alerte(route_id, "2026-09-01", "erreur_prix")
    assert alerte is not None
    assert alerte["prix_cents"] == 18_800
    assert storage.obtenir_derniere_alerte(route_id, "2026-09-01", "aubaine") is None


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_corroboration_activee_budget_insuffisant_downgrade_aubaine(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    """budget=1 : seul le signal 1 est tente (le signal 2 exigerait un 2e
    budget) - or signal_1 seul ne confirme jamais (AUDIT.md 2.3c), le
    verdict retrograde donc systematiquement en aubaine."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test_e2e"
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    route_id = _peupler_echantillon("YUL", "CDG", "2026-01-01")

    charger_config_mock.return_value = _config_e2e(
        corroboration_activee=True, corroboration_max_requetes_par_run=1
    )
    generer_candidats_mock.return_value = [_route_e2e()]
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = [
        _offre_e2e(18_800),  # fetch original
        _offre_e2e(19_000),  # signal 1 : re-requete (confirmant, mais seul)
    ]

    scanner.main()

    assert fournisseur_classe_mock.return_value.meilleure_offre.call_count == 2
    assert storage.obtenir_derniere_alerte(route_id, "2026-09-01", "erreur_prix") is None
    assert storage.obtenir_derniere_alerte(route_id, "2026-09-01", "aubaine") is not None


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_budget_corroboration_partage_entre_routes_et_signale_au_digest(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    """Le plafond est partage au niveau du run, pas par route : la 1re route
    consomme tout le budget (3), la 2e n'en a plus du tout - et le run le
    signale (WARNING + budget_corroboration_epuise transmis au digest),
    AUDIT.md Session B (ajustement utilisateur)."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test_e2e"
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    route_id_cdg = _peupler_echantillon("YUL", "CDG", "2026-01-01")
    route_id_lhr = _peupler_echantillon("YUL", "LHR", "2026-01-01")

    charger_config_mock.return_value = _config_e2e(
        corroboration_activee=True, corroboration_max_requetes_par_run=3
    )
    generer_candidats_mock.return_value = [
        _route_e2e(destination="CDG"),
        _route_e2e(destination="LHR"),
    ]
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = [
        _offre_e2e(18_800),  # CDG : fetch original
        _offre_e2e(19_000),  # CDG : signal 1
        _offre_e2e(19_500),  # CDG : signal 2 voisine -3j
        _offre_e2e(19_500),  # CDG : signal 2 voisine +3j (budget CDG epuise : 3/3)
        _offre_e2e(18_800),  # LHR : fetch original (budget corroboration a 0 pour LHR)
    ]

    with patch("scanner.envoyer_digest") as envoyer_digest_mock:
        scanner.main()

    assert fournisseur_classe_mock.return_value.meilleure_offre.call_count == 5

    assert storage.obtenir_derniere_alerte(route_id_cdg, "2026-09-01", "erreur_prix") is not None
    assert storage.obtenir_derniere_alerte(route_id_lhr, "2026-09-01", "erreur_prix") is None
    assert storage.obtenir_derniere_alerte(route_id_lhr, "2026-09-01", "aubaine") is not None

    assert envoyer_digest_mock.call_args.kwargs["budget_corroboration_epuise"] is True


@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_telegram_echoue_aucune_alerte_persistee(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    tmp_path,
    monkeypatch,
) -> None:
    """Le cas explicitement demande, verifie de bout en bout (vraie base,
    vrai pipeline classifier -> envoyer_alerte, seul Telegram est mocke) : si
    Telegram echoue, rien n'est ecrit dans la table alertes - sinon le
    cooldown de 72h supprimerait une alerte legitime jamais recue."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_live_test_e2e"
    route_id = _peupler_echantillon("YUL", "CDG", "2026-01-01")

    charger_config_mock.return_value = _config_e2e()
    generer_candidats_mock.return_value = [_route_e2e(prix_max=360)]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_e2e(35_000)

    with patch("alerting.envoyer_telegram", side_effect=RuntimeError("boom")):
        resultat = scanner.main()

    assert resultat == 1  # la seule route du run echoue (3 alertes, 3 echecs Telegram)
    for type_alerte in ("seuil", "minimum", "aubaine"):
        assert storage.obtenir_derniere_alerte(route_id, "2026-09-01", type_alerte) is None


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_sandbox_ne_declenche_aucune_alerte_meme_si_tout_declencherait(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    """Faille corrigee (audit data/scanner.db, Journal) : prix_max et
    est_nouveau_minimum ne dependent pas de l'historique filtre, donc le
    garde-fou storage.py seul ne suffisait pas - meme scenario que
    test_e2e_trois_types_alerte_independants_envoyes_et_persistes (les 3
    types declencheraient normalement), mais avec un token sandbox : aucun
    appel Telegram, aucune persistance."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_test_sandbox123"
    route_id = _peupler_echantillon("YUL", "CDG", "2026-01-01")

    charger_config_mock.return_value = _config_e2e()
    generer_candidats_mock.return_value = [_route_e2e(prix_max=360)]
    fournisseur_classe_mock.return_value.meilleure_offre.return_value = _offre_e2e(35_000)

    resultat = scanner.main()

    telegram_mock.assert_not_called()
    assert resultat == 0
    for type_alerte in ("seuil", "minimum", "aubaine"):
        assert storage.obtenir_derniere_alerte(route_id, "2026-09-01", type_alerte) is None


@patch("alerting.envoyer_telegram")
@patch("scanner.ecrire_resume_github")
@patch("scanner.generer_candidats")
@patch("scanner.FournisseurDuffel")
@patch("scanner.charger_config")
@patch("scanner.charger_env")
def test_e2e_sandbox_ne_envoie_pas_le_digest_meme_avec_erreur(
    charger_env_mock,
    charger_config_mock,
    fournisseur_classe_mock,
    generer_candidats_mock,
    ecrire_resume_mock,
    telegram_mock,
    tmp_path,
    monkeypatch,
) -> None:
    """Le digest technique est lui aussi suppime hors environnement
    production, meme quand digest_necessaire serait vrai (route en erreur)."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    charger_env_mock.return_value.duffel_access_token = "duffel_test_sandbox123"

    charger_config_mock.return_value = _config_e2e()
    generer_candidats_mock.return_value = [_route_e2e()]
    fournisseur_classe_mock.return_value.meilleure_offre.side_effect = RuntimeError("boom")

    scanner.main()

    telegram_mock.assert_not_called()
