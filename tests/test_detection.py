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
