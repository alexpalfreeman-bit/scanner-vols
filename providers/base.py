"""
Contrat de la couche fournisseurs (AUDIT.md Phase 2.4).
-----------------------------------------------------------
`Offre`/`FournisseurVols` sont le contrat minimal demande par AUDIT.md.
`ResumeFournisseur` et la hierarchie d'erreurs vivent aussi ici (pas dans
duffel.py) : ce sont des concepts de la couche fournisseurs, pas des details
Duffel - un futur 2e fournisseur (Amadeus, Phase 3 backlog) devra lever les
memes erreurs pour qu'un futur code amont (digest technique, Phase 2.5) les
traite sans isinstance par fournisseur.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# Alias nomme (pas TypedDict) : generer_candidats() dans scanner.py continue
# de retourner des dict bruts sans aucun changement de signature - un
# TypedDict ferait de la verification structurelle et deborderait le refactor
# dans scanner.py/alerting.py, hors scope de la Phase 2.4.
type Route = dict[str, Any]


@dataclass(frozen=True)
class Offre:
    prix_cents: int
    devise: str
    compagnie: str
    escales: int
    fournisseur: str


class FournisseurVols(Protocol):
    nom: str

    def meilleure_offre(self, route: Route) -> Offre | None: ...


@dataclass(frozen=True)
class ResumeFournisseur:
    """Snapshot de fin de run pour un fournisseur (consomme par le futur
    digest technique, Phase 2.5 - non cablee cette session)."""

    nom: str
    appels_reussis: int
    appels_zero_offres: int
    echecs_total: int
    suspendu: bool


class ErreurFournisseur(RuntimeError):
    """Erreur lors de l'appel a un fournisseur (reseau, HTTP, ou schema)."""


class ErreurValidationReponse(ErreurFournisseur):
    """La reponse du fournisseur ne correspond pas au schema attendu (ex.
    changement d'API cote fournisseur) : ne jamais construire une Offre a
    partir d'une reponse qui a echoue cette validation."""


class ErreurFournisseurSuspendu(ErreurFournisseur):
    """Circuit breaker ouvert : trop d'echecs consecutifs, fournisseur
    suspendu pour le reste du run."""
