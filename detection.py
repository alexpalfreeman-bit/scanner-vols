"""
Detection statistique (bonnes affaires, nouveaux minimums, seuils fixes).
----------------------------------------------------------------------------
Fonctions pures uniquement : aucun reseau, aucun I/O, aucune lecture
d'horloge. Tout est parametre, donc 100% testable (voir CLAUDE.md).
"""

import statistics


def statistiques_destination(
    historique: list[dict], origine: str, destination: str, devise: str, config: dict
) -> dict | None:
    """Statistiques de la destination (toutes dates confondues, puisque la
    date candidate change a chaque run) : minimum, mediane, et tendance
    recente vs ancienne. Filtre aussi par devise : un historique qui
    melangerait USD et CAD (ex. apres un changement de compte Duffel)
    ne doit jamais comparer des montants de devises differentes.
    Retourne None sans aucune observation prealable (dans cette devise)."""
    lignes = sorted(
        (
            ligne
            for ligne in historique
            if ligne["origine"] == origine
            and ligne["destination"] == destination
            and ligne["devise"] == devise
        ),
        key=lambda ligne: ligne["horodatage_utc"],
    )
    prix = [float(ligne["prix"]) for ligne in lignes]
    if not prix:
        return None

    cfg = config.get("detection", {})
    fenetre = cfg.get("fenetre_tendance", 5)
    seuil_variation = cfg.get("variation_tendance_pct", 0.05)

    recents = prix[-fenetre:]
    anciens = prix[-(fenetre * 2) : -fenetre]

    tendance, variation = None, None
    if len(recents) >= 2 and len(anciens) >= 2:
        moy_recent, moy_ancien = statistics.mean(recents), statistics.mean(anciens)
        variation = (moy_recent - moy_ancien) / moy_ancien
        if variation >= seuil_variation:
            tendance = "hausse"
        elif variation <= -seuil_variation:
            tendance = "baisse"
        else:
            tendance = "stable"

    return {
        "n": len(prix),
        "min": min(prix),
        "mediane": statistics.median(prix),
        "tendance": tendance,
        "variation_pct": variation,
    }


def est_nouveau_minimum(
    prix: float, stats: dict | None, echantillon_min: int, marge_minimum_pct: float
) -> bool:
    """Un "nouveau minimum" n'est significatif qu'avec un minimum d'historique
    (sinon la rotation des dates candidates fait qu'un simple changement de
    date ressemble a un nouveau minimum) et une marge sous l'ancien minimum
    (pour ignorer le bruit de quelques pourcents)."""
    if stats is None or stats["n"] < echantillon_min:
        return False
    return prix < float(stats["min"]) * (1 - marge_minimum_pct)


def raison_prix_max(
    prix: float, devise: str, prix_max: float | None, devise_attendue: str
) -> str | None:
    """Compare prix/devise au seuil fixe prix_max, qui est exprime dans
    devise_attendue (config['devise']). Retourne None si aucun seuil n'est
    configure, ou si la devise de l'offre ne correspond pas (mieux vaut
    ignorer la comparaison que comparer des devises differentes en silence)."""
    if prix_max is None or devise != devise_attendue:
        return None
    if prix <= prix_max:
        return f"sous ton seuil fixe de {prix_max} {devise}"
    return None
