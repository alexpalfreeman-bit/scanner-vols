"""
Fournisseur Duffel (API v2, HTTP direct)
------------------------------------------
Note technique : on appelle l'API Duffel directement en HTTP (pas via le
package PyPI "duffel-api", qui n'a pas ete mis a jour pour l'API v2 de Duffel
et fait planter silencieusement le parsing des offres).

FournisseurDuffel implemente le contrat FournisseurVols (providers/base.py) :
la reponse Duffel est validee via des modeles Pydantic prives avant toute
extraction (AUDIT.md 2.4) - un changement de schema cote Duffel leve
ErreurValidationReponse au lieu de produire silencieusement une Offre fausse.
"""

import logging
import random
import time
from decimal import ROUND_HALF_UP, Decimal

import requests
from pydantic import BaseModel, ConfigDict, Field

from config import Env
from providers.base import (
    ErreurFournisseur,
    ErreurFournisseurSuspendu,
    ErreurValidationReponse,
    Offre,
    Route,
)

logger = logging.getLogger(__name__)

DUFFEL_API_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

RETRIABLES = {429, 500, 502, 503, 504}
ECHECS_CONSECUTIFS_MAX = 5


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


# ---------------------------------------------------------------- validation Pydantic (2.4)


class _ModeleDuffel(BaseModel):
    """Base commune : tout champ de la reponse Duffel qu'on ne modelise pas
    ici est ignore (comportement par defaut de Pydantic), pas rejete - on ne
    valide que ce qu'on consomme reellement. Explicite via ConfigDict plutot
    que laisse implicite, pour qu'un futur relecteur ne se demande pas si
    extra="forbid" a ete oublie."""

    model_config = ConfigDict(extra="ignore")


class _CompagnieDuffel(_ModeleDuffel):
    name: str


class _SegmentDuffel(_ModeleDuffel):
    operating_carrier: _CompagnieDuffel | None = None
    marketing_carrier: _CompagnieDuffel | None = None


class _TrajetDuffel(_ModeleDuffel):
    # min_length=1 : une slice sans segment ferait silencieusement calculer
    # escales = -1 en aval (max(escales, len([])-1)) - exactement le genre de
    # donnee fausse silencieuse qu'AUDIT.md demande d'empecher.
    segments: list[_SegmentDuffel] = Field(min_length=1)


class _OffreDuffel(_ModeleDuffel):
    # Decimal, pas float : Duffel renvoie une string decimale ("532.10"),
    # Pydantic la parse nativement sans passer par l'arithmetique flottante
    # (regle CLAUDE.md : jamais de float pour un montant).
    total_amount: Decimal
    total_currency: str = Field(min_length=3, max_length=3)
    slices: list[_TrajetDuffel] = Field(min_length=1)


class _DonneesReponseOffres(_ModeleDuffel):
    # list | None (pas Field(default_factory=list)) : tolere aussi bien une
    # cle "offers" absente qu'une valeur explicitement null, comme le fait
    # le code actuel avec `or []`.
    offers: list[_OffreDuffel] | None = None


class _ReponseOffreRequest(_ModeleDuffel):
    data: _DonneesReponseOffres


def _centimes_depuis_montant(montant: Decimal) -> int:
    return int((montant * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _compagnie_depuis_segment(segment: _SegmentDuffel) -> str:
    """Nom complet de la compagnie operante (exige par la reglementation US),
    repli sur la compagnie commerciale si absente, puis "?" si aucune des
    deux n'est renseignee."""
    if segment.operating_carrier is not None:
        return segment.operating_carrier.name
    if segment.marketing_carrier is not None:
        return segment.marketing_carrier.name
    return "?"


# ---------------------------------------------------------------- FournisseurDuffel


class FournisseurDuffel:
    """Fournisseur Duffel : implemente FournisseurVols (providers/base.py)."""

    nom = "duffel"

    def __init__(self, env: Env, config: dict, *, session: requests.Session | None = None) -> None:
        self._session = session or creer_session_duffel(env)
        self._config = config
        self._echecs_consecutifs = 0  # remis a 0 a chaque succes -> pilote le circuit breaker
        self._echecs_cumules = 0  # jamais remis a 0 -> pilote resume().echecs_total
        self._suspendu = False

    def meilleure_offre(self, route: Route) -> Offre | None:
        """Circuit breaker (AUDIT.md 2.4) : suspend le fournisseur pour le
        reste du run apres ECHECS_CONSECUTIFS_MAX echecs d'affilee. Une fois
        suspendu, aucun appel reseau n'est tente : leve immediatement."""
        if self._suspendu:
            raise ErreurFournisseurSuspendu(
                f"{self.nom} suspendu apres {ECHECS_CONSECUTIFS_MAX} echecs consecutifs"
            )
        try:
            offre = self._appeler(route)
        except ErreurFournisseur:
            self._echecs_consecutifs += 1
            self._echecs_cumules += 1
            if self._echecs_consecutifs >= ECHECS_CONSECUTIFS_MAX:
                self._suspendu = True
                logger.warning(
                    "%s : suspendu pour le reste du run (%d echecs consecutifs)",
                    self.nom,
                    self._echecs_consecutifs,
                )
            raise
        self._echecs_consecutifs = 0
        return offre

    def _appeler(self, route: Route) -> Offre | None:
        """Note (audit 1.9) : la doc Duffel v2 confirme que
        `return_offers=true` embarque "all the offers returned by the
        airlines" dans la reponse de creation - pas de troncature
        documentee. Le tri cote client via min() reste donc correct."""
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

        passengers = [{"type": "adult"} for _ in range(self._config.get("adultes", 1))]
        body = {
            "data": {
                "cabin_class": self._config.get("classe", "economy"),
                "passengers": passengers,
                "slices": slices,
                "max_connections": 0 if route.get("direct_seulement") else 1,
            }
        }

        try:
            r = post_resilient(
                self._session, f"{DUFFEL_API_URL}/air/offer_requests?return_offers=true", body
            )
        except (requests.RequestException, RuntimeError) as e:
            raise ErreurFournisseur(str(e)) from e
        if not r.ok:
            raise ErreurFournisseur(extraire_message_erreur(r))

        try:
            reponse = _ReponseOffreRequest.model_validate(r.json())
        except ValueError as e:
            # ValueError capture a la fois JSONDecodeError (corps non-JSON,
            # herite de ValueError) et pydantic.ValidationError (idem) : un
            # changement de schema Duffel leve donc toujours une erreur
            # explicite ici, jamais une extraction silencieusement fausse.
            raise ErreurValidationReponse(
                f"reponse Duffel invalide (schema inattendu) : {e}"
            ) from e

        offres = reponse.data.offers or []
        if not offres:
            return None

        meilleure = min(offres, key=lambda o: o.total_amount)
        escales = max(len(sl.segments) - 1 for sl in meilleure.slices)
        premier_segment = meilleure.slices[0].segments[0]

        return Offre(
            prix_cents=_centimes_depuis_montant(meilleure.total_amount),
            devise=meilleure.total_currency,
            compagnie=_compagnie_depuis_segment(premier_segment),
            escales=escales,
            fournisseur=self.nom,
        )
