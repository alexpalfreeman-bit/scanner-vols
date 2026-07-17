"""
Fournisseur Duffel (API v2, HTTP direct)
------------------------------------------
Note technique : on appelle l'API Duffel directement en HTTP (pas via le
package PyPI "duffel-api", qui n'a pas ete mis a jour pour l'API v2 de Duffel
et fait planter silencieusement le parsing des offres).
"""

import random
import time

import requests

from config import Env

DUFFEL_API_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

RETRIABLES = {429, 500, 502, 503, 504}


def creer_session_duffel(env: Env) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {env.duffel_access_token}",
            "Duffel-Version": DUFFEL_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    return session


def post_resilient(
    session: requests.Session, url: str, corps: dict, essais: int = 4
) -> requests.Response:
    """POST avec retry/backoff exponentiel (+ jitter) sur timeout, erreurs
    reseau, ou codes HTTP transitoires (429/5xx) ; respecte Retry-After s'il
    est present. Une erreur non transitoire (4xx hors 429) est retournee
    immediatement, sans retry."""
    derniere_erreur: Exception = RuntimeError("aucune tentative")
    for tentative in range(essais):
        attente = (2**tentative) + random.uniform(0, 1)
        try:
            r = session.post(url, json=corps, timeout=30)
            if r.status_code not in RETRIABLES:
                return r
            attente = max(attente, float(r.headers.get("Retry-After", 0)))
            derniere_erreur = RuntimeError(f"HTTP {r.status_code} sur {url}")
        except requests.RequestException as e:
            derniere_erreur = e
        if tentative < essais - 1:
            time.sleep(attente)
    raise derniere_erreur


def extraire_message_erreur(r: requests.Response) -> str:
    """Message d'erreur lisible a partir d'une reponse HTTP en echec. Si le
    corps n'est pas du JSON exploitable (ex. page HTML d'un 502), on retombe
    sur le code HTTP + le debut du corps plutot que de laisser JSONDecodeError
    masquer l'erreur reelle."""
    try:
        erreur = (r.json().get("errors") or [{}])[0]
        return f"{erreur.get('type')}: {erreur.get('title')}: {erreur.get('message')}"
    except (ValueError, KeyError, AttributeError, IndexError, TypeError):
        return f"HTTP {r.status_code} : {r.text[:200]}"


def chercher_meilleur_vol(session: requests.Session, route: dict, config: dict) -> dict | None:
    """Retourne l'offre la moins chere pour la route, ou None si rien trouve.

    Note (audit 1.9) : la doc Duffel v2 confirme que `return_offers=true`
    embarque "all the offers returned by the airlines" dans la reponse de
    creation - pas de troncature documentee. Duffel recommande plutot
    `return_offers=false` + `GET /air/offers?...&sort=total_amount&limit=1`
    pour profiter de la pagination/tri/filtre cote serveur sur les routes a
    fort volume d'offres, pas pour corriger un probleme de fiabilite. Vu le
    volume actuel (une poignee d'offres par route), le tri cote client via
    min() reste correct et evite de doubler le nombre d'appels API par
    route. A revisiter si la taille des reponses devient un probleme reel
    (Phase 2.4 : validation Pydantic de la reponse + circuit breaker)."""
    # Une "slice" = un trajet (aller). Un aller-retour = deux slices.
    slices = [
        {
            "origin": route["origine"],
            "destination": route["destination"],
            "departure_date": str(route["date_depart"]),
        }
    ]
    if route.get("date_retour"):
        slices.append(
            {
                "origin": route["destination"],
                "destination": route["origine"],
                "departure_date": str(route["date_retour"]),
            }
        )

    passengers = [{"type": "adult"} for _ in range(config.get("adultes", 1))]

    body = {
        "data": {
            "cabin_class": config.get("classe", "economy"),
            "passengers": passengers,
            "slices": slices,
            "max_connections": 0 if route.get("direct_seulement") else 1,
        }
    }
    r = post_resilient(session, f"{DUFFEL_API_URL}/air/offer_requests?return_offers=true", body)
    if not r.ok:
        raise RuntimeError(extraire_message_erreur(r))

    offres = r.json()["data"].get("offers") or []
    if not offres:
        return None

    meilleure = min(offres, key=lambda o: float(o["total_amount"]))

    # Nombre d'escales = segments - 1, additionne sur toutes les slices (max par slice)
    escales = 0
    for sl in meilleure["slices"]:
        escales = max(escales, len(sl["segments"]) - 1)
    # Nom complet de la compagnie operante du premier segment (exige par la reglementation US)
    premier_segment = meilleure["slices"][0]["segments"][0]
    compagnie = (premier_segment.get("operating_carrier") or {}).get("name") or (
        premier_segment.get("marketing_carrier") or {}
    ).get("name", "?")

    return {
        "prix": round(float(meilleure["total_amount"]), 2),
        "devise": meilleure["total_currency"],
        "compagnie": compagnie,
        "escales": escales,
    }
