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
