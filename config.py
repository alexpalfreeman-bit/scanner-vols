"""
Chargement et validation de la configuration (variables d'environnement +
config.yaml). Toute erreur de configuration leve `ErreurConfiguration` avec
un message explicite plutot qu'un KeyError/ValidationError brut.
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings


class ErreurConfiguration(Exception):
    """Levee quand l'environnement ou config.yaml est invalide/incomplet."""


# ---------------------------------------------------------------- environnement


class Env(BaseSettings):
    duffel_access_token: str
    telegram_bot_token: str
    telegram_chat_id: str


def charger_env() -> Env:
    load_dotenv()
    try:
        return Env()
    except ValidationError as e:
        manquantes = ", ".join(sorted({str(err["loc"][0]) for err in e.errors()})).upper()
        raise ErreurConfiguration(
            f"Variables d'environnement manquantes ou invalides : {manquantes}"
        ) from e


# ---------------------------------------------------------------- config.yaml


class Destination(BaseModel):
    code: str = Field(min_length=3, max_length=3)
    prix_max: float | None = None
    direct_seulement: bool = False

    @model_validator(mode="before")
    @classmethod
    def normaliser_entree_bare_string(cls, data: Any) -> Any:
        """Une destination peut etre juste un code IATA (str) ou un objet
        avec prix_max/direct_seulement en plus."""
        if isinstance(data, str):
            return {"code": data}
        return data

    @field_validator("code")
    @classmethod
    def code_iata_majuscules(cls, v: str) -> str:
        if not v.isalpha():
            raise ValueError("le code IATA doit contenir uniquement des lettres")
        return v.upper()


class Sejour(BaseModel):
    duree_nuits: int = Field(gt=0, default=10)
    candidats_semaines: list[int] = Field(default_factory=lambda: [13, 16, 19, 22, 25])


class Detection(BaseModel):
    seuil_bonne_affaire_pct: float = Field(gt=0, lt=1, default=0.15)
    seuil_erreur_prix_pct: float = Field(gt=0, lt=1, default=0.40)
    echantillon_min: int = Field(gt=0, default=5)
    fenetre_tendance: int = Field(gt=0, default=5)
    variation_tendance_pct: float = Field(gt=0, lt=1, default=0.05)


class ConfigApp(BaseModel):
    origine: str = Field(min_length=3, max_length=3)
    devise: str = Field(min_length=3, max_length=3)
    adultes: int = Field(gt=0, default=1)
    classe: str = "economy"
    sejour: Sejour = Field(default_factory=Sejour)
    detection: Detection = Field(default_factory=Detection)
    destinations: list[Destination]

    @field_validator("origine", "devise")
    @classmethod
    def code_majuscules(cls, v: str) -> str:
        return v.upper()


def valider_config(brut: dict) -> dict:
    """Valide `brut` (le dict issu de charger_config()) et retourne un dict
    brut equivalent (pas l'objet pydantic) : le reste de scanner.py continue
    a utiliser config.get(...) sans aucun changement de signature."""
    try:
        return ConfigApp.model_validate(brut).model_dump()
    except ValidationError as e:
        raise ErreurConfiguration(f"config.yaml invalide :\n{e}") from e
