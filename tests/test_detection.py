from datetime import UTC, datetime

import pytest

import detection


def test_import_detection() -> None:
    """Smoke test : le module s'importe sans erreur de syntaxe ni au chargement."""
    import detection  # noqa: F401


# ---------------------------------------------------------------- statistiques_destination (1.1)


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

    stats = detection.statistiques_destination(historique, "YUL", "CDG", "USD", {})

    assert stats is not None
    assert stats["n"] == 2
    assert stats["min"] == 500


# ---------------------------------------------------------------- raison_prix_max (1.1)


def test_raison_prix_max_meme_devise_sous_seuil() -> None:
    assert detection.raison_prix_max(400, "CAD", 500, "CAD") == "sous ton seuil fixe de 500 CAD"


def test_raison_prix_max_devise_differente_retourne_none() -> None:
    assert detection.raison_prix_max(400, "USD", 500, "CAD") is None


def test_raison_prix_max_aucun_seuil_configure_retourne_none() -> None:
    assert detection.raison_prix_max(400, "CAD", None, "CAD") is None


# ---------------------------------------------------------------- est_nouveau_minimum (1.2)


def _stats(n: int, minimum: float) -> dict:
    return {"n": n, "min": minimum, "mediane": minimum, "tendance": None, "variation_pct": None}


def test_est_nouveau_minimum_echantillon_insuffisant() -> None:
    stats = _stats(n=3, minimum=500)
    assert (
        detection.est_nouveau_minimum(400, stats, echantillon_min=5, marge_minimum_pct=0.03)
        is False
    )


def test_est_nouveau_minimum_dans_la_marge_pas_alerte() -> None:
    stats = _stats(n=6, minimum=500)
    prix = 500 * 0.99  # 99% du minimum, dans la marge de 3%
    assert (
        detection.est_nouveau_minimum(prix, stats, echantillon_min=5, marge_minimum_pct=0.03)
        is False
    )


def test_est_nouveau_minimum_sous_la_marge_alerte() -> None:
    stats = _stats(n=6, minimum=500)
    prix = 500 * 0.95  # 95% du minimum, sous la marge de 3%
    assert (
        detection.est_nouveau_minimum(prix, stats, echantillon_min=5, marge_minimum_pct=0.03)
        is True
    )


def test_est_nouveau_minimum_stats_absentes() -> None:
    assert (
        detection.est_nouveau_minimum(400, None, echantillon_min=5, marge_minimum_pct=0.03) is False
    )


# ==================================================================
# Phase 2.3 : nouveau moteur de detection
# ==================================================================

# ---------------------------------------------------------------- _decaler_mois (2.3.a)


def test_decaler_mois_cas_simple() -> None:
    ref = datetime(2026, 7, 15, tzinfo=UTC)
    assert detection._decaler_mois(ref, 3) == datetime(2026, 4, 15, tzinfo=UTC)


def test_decaler_mois_franchissement_annee() -> None:
    ref = datetime(2026, 7, 1, tzinfo=UTC)
    assert detection._decaler_mois(ref, 18) == datetime(2025, 1, 1, tzinfo=UTC)


def test_decaler_mois_clampe_jour() -> None:
    ref = datetime(2026, 8, 31, tzinfo=UTC)
    assert detection._decaler_mois(ref, 6) == datetime(2026, 2, 28, tzinfo=UTC)


def test_decaler_mois_bissextile() -> None:
    ref = datetime(2028, 3, 31, tzinfo=UTC)
    assert detection._decaler_mois(ref, 1) == datetime(2028, 2, 29, tzinfo=UTC)


# ---------------------------------------------------------------- echantillon_comparable (2.3.a)

MAINTENANT = datetime(2026, 7, 1, tzinfo=UTC)


def _observation(
    *,
    route_id: int = 1,
    devise: str = "USD",
    observe_le: str = "2026-06-01T00:00:00+00:00",
    date_depart: str = "2026-09-01",
    horizon_jours: int = 100,
    prix_cents: int = 50_000,
) -> dict:
    return {
        "route_id": route_id,
        "devise": devise,
        "observe_le": observe_le,
        "date_depart": date_depart,
        "horizon_jours": horizon_jours,
        "prix_cents": prix_cents,
    }


def _echantillon(observations: list[dict], **overrides: object) -> detection.EchantillonComparable:
    parametres: dict = {
        "route_id": 1,
        "devise": "USD",
        "date_depart_candidat": "2026-09-01",
        "horizon_jours_candidat": 100,
        "maintenant": MAINTENANT,
        **overrides,
    }
    return detection.echantillon_comparable(observations, **parametres)


def test_echantillon_comparable_vide() -> None:
    resultat = _echantillon([])
    assert resultat.niveau == "route"
    assert resultat.n == 0
    assert resultat.prix_cents == ()


def test_echantillon_comparable_niveau_le_plus_specifique_sans_elargir() -> None:
    # 8 observations au niveau le plus fin ET 8 autres qui ne correspondent
    # qu'au niveau route (mois different) : le niveau le plus specifique doit
    # etre retenu, pas juste "un niveau qui marche".
    fines = [_observation(prix_cents=40_000 + i) for i in range(8)]
    larges = [_observation(date_depart="2026-01-01", prix_cents=90_000 + i) for i in range(8)]

    resultat = _echantillon(fines + larges)

    assert resultat.niveau == "route_mois_horizon"
    assert resultat.n == 8
    assert set(resultat.prix_cents) == {40_000 + i for i in range(8)}


def test_echantillon_comparable_repli_vers_route_mois() -> None:
    # 7 dans la fenetre d'horizon (n=7 < 8 au niveau le plus fin) + 3 hors
    # fenetre horizon mais meme mois (n=10 >= 8 au niveau route_mois).
    dans_fenetre = [_observation(horizon_jours=100, prix_cents=40_000 + i) for i in range(7)]
    hors_fenetre = [_observation(horizon_jours=200, prix_cents=70_000 + i) for i in range(3)]

    resultat = _echantillon(dans_fenetre + hors_fenetre)

    assert resultat.niveau == "route_mois"
    assert resultat.n == 10


def test_echantillon_comparable_repli_vers_route() -> None:
    # 3 observations au bon mois (n=3 < 8 aux 2 niveaux fins) + 5 d'un autre
    # mois (portent le total au niveau route a 8).
    meme_mois = [_observation(prix_cents=40_000 + i) for i in range(3)]
    autre_mois = [_observation(date_depart="2026-01-01", prix_cents=70_000 + i) for i in range(5)]

    resultat = _echantillon(meme_mois + autre_mois)

    assert resultat.niveau == "route"
    assert resultat.n == 8


def test_echantillon_comparable_insuffisant_partout_retourne_route() -> None:
    obs = [_observation(prix_cents=40_000 + i) for i in range(3)]

    resultat = _echantillon(obs)

    assert resultat.niveau == "route"
    assert resultat.n == 3


def test_echantillon_comparable_n_min_frontiere_8_accepte() -> None:
    obs = [_observation(prix_cents=40_000 + i) for i in range(8)]

    resultat = _echantillon(obs)

    assert resultat.niveau == "route_mois_horizon"
    assert resultat.n == 8


def test_echantillon_comparable_n_min_frontiere_7_replie() -> None:
    obs = [_observation(prix_cents=40_000 + i) for i in range(7)]

    resultat = _echantillon(obs)

    assert resultat.niveau == "route"
    assert resultat.n == 7


def test_echantillon_comparable_horizon_borne_inferieure_exclue() -> None:
    # candidat h=100, fenetre +-21 -> [79, 121] ; 78 est juste hors bornes.
    resultat = _echantillon([_observation(horizon_jours=78)], n_min=1)
    assert resultat.niveau == "route_mois"


def test_echantillon_comparable_horizon_borne_inferieure_incluse() -> None:
    resultat = _echantillon([_observation(horizon_jours=79)], n_min=1)
    assert resultat.niveau == "route_mois_horizon"


def test_echantillon_comparable_horizon_borne_superieure_incluse() -> None:
    resultat = _echantillon([_observation(horizon_jours=121)], n_min=1)
    assert resultat.niveau == "route_mois_horizon"


def test_echantillon_comparable_horizon_borne_superieure_exclue() -> None:
    resultat = _echantillon([_observation(horizon_jours=122)], n_min=1)
    assert resultat.niveau == "route_mois"


def test_echantillon_comparable_mois_different_exclu_sauf_au_niveau_route() -> None:
    resultat = _echantillon([_observation(date_depart="2026-01-01")], n_min=1)
    assert resultat.niveau == "route"
    assert resultat.n == 1


def test_echantillon_comparable_devise_differente_exclue_partout() -> None:
    resultat = _echantillon([_observation(devise="CAD")], n_min=1)
    assert resultat.niveau == "route"
    assert resultat.n == 0


def test_echantillon_comparable_route_id_different_exclu() -> None:
    resultat = _echantillon([_observation(route_id=2)], n_min=1)
    assert resultat.n == 0


def test_echantillon_comparable_frontiere_18_mois_incluse() -> None:
    # seuil = _decaler_mois(MAINTENANT, 18) = 2025-01-01T00:00:00+00:00 exactement.
    resultat = _echantillon([_observation(observe_le="2025-01-01T00:00:00+00:00")], n_min=1)
    assert resultat.n == 1


def test_echantillon_comparable_frontiere_18_mois_exclue() -> None:
    resultat = _echantillon([_observation(observe_le="2024-12-31T23:59:59+00:00")], n_min=1)
    assert resultat.n == 0


def test_echantillon_comparable_override_n_min() -> None:
    obs = [_observation(prix_cents=40_000 + i) for i in range(3)]

    resultat = _echantillon(obs, n_min=3)

    assert resultat.niveau == "route_mois_horizon"
    assert resultat.n == 3


def test_echantillon_comparable_override_fenetre_horizon_jours() -> None:
    # h=150 est hors de la fenetre par defaut (+-21) mais dans une fenetre elargie.
    resultat = _echantillon([_observation(horizon_jours=150)], n_min=1, fenetre_horizon_jours=60)
    assert resultat.niveau == "route_mois_horizon"


def test_echantillon_comparable_override_fenetre_historique_mois() -> None:
    # exclue par la fenetre par defaut (18 mois, seuil 2025-01-01), incluse a 36 mois.
    resultat = _echantillon(
        [_observation(observe_le="2024-01-01T00:00:00+00:00")], n_min=1, fenetre_historique_mois=36
    )
    assert resultat.n == 1


def test_echantillon_comparable_maintenant_naif_leve_erreur() -> None:
    with pytest.raises(ValueError):
        _echantillon([], maintenant=datetime(2026, 7, 1))


# ---------------------------------------------------------------- z_score_modifie (2.3.b)

# median=45.0, MAD=20.0 (calcule a la main, voir plan).
_ECHANTILLON_MAD_CONNU = [10, 20, 30, 40, 50, 60, 70, 80]


def test_z_score_modifie_echantillon_insuffisant() -> None:
    assert detection.z_score_modifie(500, [100] * 7) is None


def test_z_score_modifie_n_min_frontiere_8_calcule() -> None:
    assert detection.z_score_modifie(45, _ECHANTILLON_MAD_CONNU) is not None


def test_z_score_modifie_mad_nulle_retourne_none() -> None:
    assert detection.z_score_modifie(999, [500] * 8) is None


def test_z_score_modifie_ordre_sans_effet() -> None:
    trie = sorted(_ECHANTILLON_MAD_CONNU)
    melange = [40, 10, 80, 20, 70, 30, 60, 50]
    assert detection.z_score_modifie(10, trie) == detection.z_score_modifie(10, melange)


def test_z_score_modifie_avec_doublons() -> None:
    # median=75.0, MAD=25.0 (calcule a la main) : le median/MAD gerent les
    # doublons sans traitement special, mais autant le verifier explicitement.
    echantillon = [50, 50, 50, 50, 100, 100, 100, 100]
    assert detection.z_score_modifie(50, echantillon) == pytest.approx(-0.6745)


def test_z_score_modifie_valeur_calculee_signe_negatif() -> None:
    z = detection.z_score_modifie(10, _ECHANTILLON_MAD_CONNU)
    assert z == pytest.approx(-1.180375)


def test_z_score_modifie_valeur_calculee_signe_positif() -> None:
    z = detection.z_score_modifie(90, _ECHANTILLON_MAD_CONNU)
    assert z == pytest.approx(1.517625)


def test_z_score_modifie_prix_egal_mediane_z_zero() -> None:
    assert detection.z_score_modifie(45, _ECHANTILLON_MAD_CONNU) == pytest.approx(0.0)


# ---------------------------------------------------------------- classifier (2.3.b)


def test_classifier_donnees_insuffisantes() -> None:
    assert detection.classifier(500, [100] * 3) == "donnees_insuffisantes"


def test_classifier_frontiere_erreur_prix_exacte(monkeypatch) -> None:
    monkeypatch.setattr(detection, "z_score_modifie", lambda *a, **k: -3.5)
    assert detection.classifier(1, [1] * 8) == "candidat_erreur_prix"


def test_classifier_juste_au_dessus_frontiere_erreur_prix(monkeypatch) -> None:
    monkeypatch.setattr(detection, "z_score_modifie", lambda *a, **k: -3.4999)
    assert detection.classifier(1, [1] * 8) == "bonne_affaire"


def test_classifier_frontiere_bonne_affaire_exacte(monkeypatch) -> None:
    monkeypatch.setattr(detection, "z_score_modifie", lambda *a, **k: -2.0)
    assert detection.classifier(1, [1] * 8) == "bonne_affaire"


def test_classifier_juste_au_dessus_frontiere_bonne_affaire(monkeypatch) -> None:
    monkeypatch.setattr(detection, "z_score_modifie", lambda *a, **k: -1.9999)
    assert detection.classifier(1, [1] * 8) == "normal"


def test_classifier_z_none_donnees_insuffisantes(monkeypatch) -> None:
    monkeypatch.setattr(detection, "z_score_modifie", lambda *a, **k: None)
    assert detection.classifier(1, [1] * 8) == "donnees_insuffisantes"


def test_classifier_moins_25_pourcent_bonne_affaire() -> None:
    # median=47000, MAD=3000 (verifie par calcul). -25% vs la mediane -> z ~ -2.64.
    echantillon = [40000, 44000, 44000, 46000, 48000, 50000, 50000, 54000]
    assert detection.classifier(35250, echantillon) == "bonne_affaire"


def test_classifier_outlier_net_candidat_erreur_prix() -> None:
    # Meme echantillon, -60% vs la mediane -> z ~ -6.34.
    echantillon = [40000, 44000, 44000, 46000, 48000, 50000, 50000, 54000]
    assert detection.classifier(18800, echantillon) == "candidat_erreur_prix"


def test_classifier_override_seuil_affaire(monkeypatch) -> None:
    monkeypatch.setattr(detection, "z_score_modifie", lambda *a, **k: -1.0)
    assert detection.classifier(1, [1] * 8) == "normal"
    assert detection.classifier(1, [1] * 8, seuil_affaire_z=-0.5) == "bonne_affaire"


def test_classifier_override_seuil_erreur(monkeypatch) -> None:
    monkeypatch.setattr(detection, "z_score_modifie", lambda *a, **k: -2.5)
    assert detection.classifier(1, [1] * 8) == "bonne_affaire"
    assert detection.classifier(1, [1] * 8, seuil_erreur_z=-2.0) == "candidat_erreur_prix"


def test_classifier_n_min_est_transmis_a_z_score_modifie() -> None:
    # n_min ici et dans echantillon_comparable ne sont couples que par
    # convention (decision documentee) : un n_min desaccorde reste correct,
    # juste degrade (repli qui n'atteint plus le seuil que classifier exige).
    assert detection.classifier(10, _ECHANTILLON_MAD_CONNU, n_min=9) == "donnees_insuffisantes"


# ---------------------------------------------------------------- corroborer_erreur_prix (2.3.c)

# median=47000, MAD=3000 (meme echantillon que les tests classifier ci-dessus).
_ECHANTILLON_CORROBORATION = [40000, 44000, 44000, 46000, 48000, 50000, 50000, 54000]
_PRIX_CANDIDAT = 18800  # classifier(...) == "candidat_erreur_prix" (z ~ -6.34)


def _signaux_avec(
    *, voisines: bool = False, plancher: bool = False, chute: bool = False
) -> detection.SignauxCorroboration:
    """Signal 1 (re-requete) toujours confirmant ; chaque autre signal est
    soit force a une valeur confirmante, soit a une valeur neutre/absente."""
    return detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000,
        prix_dates_voisines_cents=(19500, 19500) if voisines else (47000, 47000),
        plancher_absolu_cents=20000 if plancher else None,
        dernier_prix_cents=40000 if chute else None,
        dernier_prix_age_heures=24.0 if chute else None,
    )


def test_corroborer_erreur_prix_signal_1_absent_meme_avec_2_3_4() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=None,
        prix_dates_voisines_cents=(19500, 19500),
        plancher_absolu_cents=20000,
        dernier_prix_cents=40000,
        dernier_prix_age_heures=24.0,
    )

    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)

    assert resultat.verdict == "bonne_affaire"
    assert resultat.signal_reconfirme is False


def test_corroborer_erreur_prix_reconfirmation_normale_domine() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=47000,  # reclassifie "normal" (== mediane)
        prix_dates_voisines_cents=(19500, 19500),
        plancher_absolu_cents=20000,
        dernier_prix_cents=40000,
        dernier_prix_age_heures=24.0,
    )

    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)

    assert resultat.verdict == "bonne_affaire"
    assert resultat.signal_reconfirme is False


@pytest.mark.parametrize(
    ("voisines", "plancher", "chute", "verdict_attendu"),
    [
        (False, False, False, "bonne_affaire"),  # signal 1 seul : insuffisant (AUDIT.md 2.3c)
        (True, False, False, "erreur_prix"),
        (False, True, False, "erreur_prix"),
        (False, False, True, "erreur_prix"),
        (True, True, False, "erreur_prix"),
        (True, False, True, "erreur_prix"),
        (False, True, True, "erreur_prix"),
        (True, True, True, "erreur_prix"),
    ],
)
def test_corroborer_erreur_prix_matrice_signaux(voisines, plancher, chute, verdict_attendu) -> None:
    signaux = _signaux_avec(voisines=voisines, plancher=plancher, chute=chute)

    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)

    assert resultat.verdict == verdict_attendu
    assert resultat.signal_reconfirme is True
    assert resultat.signal_voisines_aussi_basses is voisines
    assert resultat.signal_plancher_absolu is plancher
    assert resultat.signal_vitesse_chute is chute


def test_corroborer_erreur_prix_aucune_date_voisine_sondee() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, prix_dates_voisines_cents=()
    )
    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_voisines_aussi_basses is False


def test_corroborer_erreur_prix_une_seule_date_voisine_sondee_basse() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, prix_dates_voisines_cents=(19500,)
    )
    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_voisines_aussi_basses is True


def test_corroborer_erreur_prix_deux_dates_voisines_une_normale_une_basse() -> None:
    # "all" : une seule voisine a prix normal suffit a invalider le signal.
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, prix_dates_voisines_cents=(19500, 47000)
    )
    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_voisines_aussi_basses is False


def test_corroborer_erreur_prix_plancher_absolu_none() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, plancher_absolu_cents=None
    )
    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_plancher_absolu is False


def test_corroborer_erreur_prix_plancher_absolu_egalite_stricte() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, plancher_absolu_cents=_PRIX_CANDIDAT
    )
    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_plancher_absolu is False


def test_corroborer_erreur_prix_chute_un_seul_champ_fourni() -> None:
    sans_age = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, dernier_prix_cents=40000, dernier_prix_age_heures=None
    )
    sans_prix = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, dernier_prix_cents=None, dernier_prix_age_heures=24.0
    )
    for signaux in (sans_age, sans_prix):
        resultat = detection.corroborer_erreur_prix(
            _PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux
        )
        assert resultat.signal_vitesse_chute is False


def test_corroborer_erreur_prix_chute_age_frontiere_48h_exclue() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, dernier_prix_cents=40000, dernier_prix_age_heures=48.0
    )
    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_vitesse_chute is False


def test_corroborer_erreur_prix_chute_age_juste_sous_48h_incluse() -> None:
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, dernier_prix_cents=40000, dernier_prix_age_heures=47.99
    )
    resultat = detection.corroborer_erreur_prix(_PRIX_CANDIDAT, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_vitesse_chute is True


def test_corroborer_erreur_prix_chute_exactement_au_seuil_exclue() -> None:
    # seuil_chute_pct=0.40 (defaut), dernier_prix=40000 -> plancher 24000 exactement.
    signaux = detection.SignauxCorroboration(
        prix_requete_immediate_cents=19000, dernier_prix_cents=40000, dernier_prix_age_heures=24.0
    )
    resultat = detection.corroborer_erreur_prix(24_000, _ECHANTILLON_CORROBORATION, signaux)
    assert resultat.signal_vitesse_chute is False


# ---------------------------------------------------------------- dates_voisines_a_sonder (2.3.c)


def test_dates_voisines_a_sonder_defaut() -> None:
    assert detection.dates_voisines_a_sonder("2026-06-15") == ("2026-06-12", "2026-06-18")


def test_dates_voisines_a_sonder_ecart_personnalise() -> None:
    assert detection.dates_voisines_a_sonder("2026-06-15", ecart_jours=7) == (
        "2026-06-08",
        "2026-06-22",
    )


def test_dates_voisines_a_sonder_franchissement_mois_annee() -> None:
    assert detection.dates_voisines_a_sonder("2026-01-01") == ("2025-12-29", "2026-01-04")


# ---------------------------------------------------------------- doit_alerter (2.3.d)

MAINTENANT_ALERTE = datetime(2026, 7, 3, tzinfo=UTC)


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


def _doit_alerter(alerte_precedente: dict | None, **overrides: object) -> bool:
    parametres: dict = {
        "route_id": 1,
        "date_depart": "2026-09-01",
        "type_alerte": "erreur_prix",
        "prix_cents": 45_000,
        "alerte_precedente": alerte_precedente,
        "maintenant": MAINTENANT_ALERTE,
        **overrides,
    }
    return detection.doit_alerter(**parametres)


def test_doit_alerter_aucune_alerte_precedente() -> None:
    assert _doit_alerter(None) is True


def test_doit_alerter_dans_cooldown_prix_pas_assez_bas_pas_de_realerte() -> None:
    precedente = _alerte_precedente(prix_cents=50_000)
    assert _doit_alerter(precedente, prix_cents=48_000) is False  # 96% de l'ancien prix


def test_doit_alerter_dans_cooldown_prix_nettement_plus_bas_realerte() -> None:
    precedente = _alerte_precedente(prix_cents=50_000)
    assert _doit_alerter(precedente, prix_cents=40_000) is True  # 80% de l'ancien prix


def test_doit_alerter_frontiere_exacte_90_pourcent_pas_de_realerte() -> None:
    precedente = _alerte_precedente(prix_cents=100_000)
    assert _doit_alerter(precedente, prix_cents=90_000) is False  # pas strictement sous 90%


def test_doit_alerter_juste_sous_90_pourcent_realerte() -> None:
    precedente = _alerte_precedente(prix_cents=100_000)
    assert _doit_alerter(precedente, prix_cents=89_999) is True


def test_doit_alerter_age_exactement_72h_realerte() -> None:
    # MAINTENANT_ALERTE (2026-07-03T00:00:00) - envoyee_le -> age 72h exactement.
    precedente = _alerte_precedente(prix_cents=40_000, envoyee_le="2026-06-30T00:00:00+00:00")
    assert (
        _doit_alerter(precedente, prix_cents=45_000) is True
    )  # prix plus haut : seul l'age compte


def test_doit_alerter_juste_sous_72h_sans_baisse_pas_de_realerte() -> None:
    precedente = _alerte_precedente(prix_cents=40_000, envoyee_le="2026-06-30T00:00:01+00:00")
    assert _doit_alerter(precedente, prix_cents=45_000) is False


def test_doit_alerter_largement_au_dela_du_cooldown_realerte_quel_que_soit_le_prix() -> None:
    precedente = _alerte_precedente(prix_cents=1, envoyee_le="2020-01-01T00:00:00+00:00")
    assert _doit_alerter(precedente, prix_cents=999_999) is True


def test_doit_alerter_override_cooldown_heures() -> None:
    # age = 24h : dans le cooldown par defaut (72h), au-dela avec cooldown_heures=12.
    precedente = _alerte_precedente(prix_cents=40_000, envoyee_le="2026-07-02T00:00:00+00:00")
    assert _doit_alerter(precedente, prix_cents=45_000) is False
    assert _doit_alerter(precedente, prix_cents=45_000, cooldown_heures=12.0) is True


def test_doit_alerter_override_ratio_reduction_min() -> None:
    precedente = _alerte_precedente(prix_cents=50_000)
    # 48000 = 96% de 50000 : pas de realerte au seuil par defaut (90%),
    # mais oui avec un seuil plus permissif (97%).
    assert _doit_alerter(precedente, prix_cents=48_000, ratio_reduction_min=0.90) is False
    assert _doit_alerter(precedente, prix_cents=48_000, ratio_reduction_min=0.97) is True


def test_doit_alerter_maintenant_naif_leve_erreur() -> None:
    with pytest.raises(ValueError):
        _doit_alerter(None, maintenant=datetime(2026, 7, 3))
