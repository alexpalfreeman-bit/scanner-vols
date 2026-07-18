"""
Detection statistique (bonnes affaires, nouveaux minimums, seuils fixes).
----------------------------------------------------------------------------
Fonctions pures uniquement : aucun reseau, aucun I/O, aucune lecture
d'horloge. Tout est parametre, donc 100% testable (voir CLAUDE.md).

Deux moteurs coexistent temporairement (voir AUDIT.md Phase 2.3) :
- Phase 1 (ci-dessous) : encore cable dans scanner.py, prix en dollars
  flottants.
- Phase 2.3 (plus bas) : nouveau moteur (echantillon comparable avec repli
  hierarchique, z-score MAD, corroboration, cooldown), pas encore cable.
  Son contrat de donnees (dict miroir des lignes SQL des tables
  observations/alertes, prix_cents entiers) est deliberement decouple de
  la forme actuelle de storage.lire_historique() (dollars flottants) : le
  cablage (adapter storage.py, scanner.py, config.py) est un travail futur
  hors scope de cette phase.
"""

import calendar
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal


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


# ============================================================================
# Phase 2.3 : nouveau moteur de detection (echantillon comparable, z-score
# MAD, corroboration, cooldown). Voir le docstring du module.
# ============================================================================

NiveauEchantillon = Literal["route_mois_horizon", "route_mois", "route"]
Classification = Literal["candidat_erreur_prix", "bonne_affaire", "normal", "donnees_insuffisantes"]
VerdictCorroboration = Literal["erreur_prix", "bonne_affaire"]
TypeAlerte = Literal["aubaine", "erreur_prix", "minimum", "seuil"]


# ---------------------------------------------------------------- echantillon_comparable (2.3.a)


def _decaler_mois(reference: datetime, mois: int) -> datetime:
    """reference moins `mois` mois calendaires (arithmetique exacte, pas une
    approximation en jours - pas de dependance dateutil dans le projet).
    Clampe le jour si le mois cible est plus court (ex. 31 aout - 6 mois ->
    28 ou 29 fevrier)."""
    total = reference.month - 1 - mois
    annee = reference.year + total // 12
    mois_cible = total % 12 + 1
    dernier_jour = calendar.monthrange(annee, mois_cible)[1]
    return reference.replace(year=annee, month=mois_cible, day=min(reference.day, dernier_jour))


@dataclass(frozen=True)
class EchantillonComparable:
    """Resultat du repli hierarchique : `niveau` trace jusqu'ou il a fallu
    elargir pour reunir des observations comparables."""

    niveau: NiveauEchantillon
    prix_cents: tuple[int, ...]

    @property
    def n(self) -> int:
        return len(self.prix_cents)


def echantillon_comparable(
    observations: Sequence[dict],
    *,
    route_id: int,
    devise: str,
    date_depart_candidat: str,
    horizon_jours_candidat: int,
    maintenant: datetime,
    fenetre_horizon_jours: int = 21,
    fenetre_historique_mois: int = 18,
    n_min: int = 8,
) -> EchantillonComparable:
    """Echantillon de prix comparables au candidat (meme route, meme devise,
    observes dans les `fenetre_historique_mois` derniers mois), avec repli
    hierarchique route+mois+horizon -> route+mois -> route : s'arrete au
    premier niveau avec n >= n_min, ou retombe sur "route" avec son n reel
    si aucun niveau ne l'atteint (jamais None - c'est z_score_modifie qui
    decide de l'insuffisance, cf. section suivante).

    `observations` : dicts au format des lignes de la table `observations`
    (cles route_id, devise, observe_le, date_depart, horizon_jours,
    prix_cents - voir storage.py). `maintenant` doit etre timezone-aware
    (UTC, comme tous les timestamps du projet, voir CLAUDE.md)."""
    if maintenant.tzinfo is None:
        raise ValueError("maintenant doit etre timezone-aware (UTC)")

    seuil_recence = _decaler_mois(maintenant, fenetre_historique_mois)
    mois_candidat = date.fromisoformat(date_depart_candidat).month

    base = [
        o
        for o in observations
        if o["route_id"] == route_id
        and o["devise"] == devise
        and datetime.fromisoformat(o["observe_le"]) >= seuil_recence
    ]
    memes_mois = [o for o in base if date.fromisoformat(o["date_depart"]).month == mois_candidat]
    memes_mois_horizon = [
        o
        for o in memes_mois
        if horizon_jours_candidat - fenetre_horizon_jours
        <= o["horizon_jours"]
        <= horizon_jours_candidat + fenetre_horizon_jours
    ]

    niveaux: list[tuple[NiveauEchantillon, list[dict]]] = [
        ("route_mois_horizon", memes_mois_horizon),
        ("route_mois", memes_mois),
        ("route", base),
    ]
    for niveau, lignes in niveaux:
        if len(lignes) >= n_min:
            return EchantillonComparable(niveau, tuple(o["prix_cents"] for o in lignes))
    return EchantillonComparable("route", tuple(o["prix_cents"] for o in base))


# ---------------------------------------------------------------- z_score_modifie / classifier (2.3.b)


def z_score_modifie(
    prix_cents: int, echantillon_cents: Sequence[int], *, n_min: int = 8
) -> float | None:
    """Z-score modifie (Iglewicz-Hoaglin, base sur la MAD plutot que l'ecart-
    type) : insensible aux valeurs aberrantes, contrairement a un z-score
    classique. None si l'echantillon est trop petit ou si la MAD est nulle
    (tous les prix identiques - un z-score n'aurait alors aucun sens)."""
    if len(echantillon_cents) < n_min:
        return None
    med = statistics.median(echantillon_cents)
    mad = statistics.median(abs(p - med) for p in echantillon_cents)
    if mad == 0:
        return None
    return 0.6745 * (prix_cents - med) / mad


def classifier(
    prix_cents: int,
    echantillon_cents: Sequence[int],
    *,
    n_min: int = 8,
    seuil_erreur_z: float = -3.5,
    seuil_affaire_z: float = -2.0,
) -> Classification:
    """Classifie prix_cents par rapport a l'echantillon comparable via son
    z-score modifie. Seuils par defaut : AUDIT.md 2.3b."""
    z = z_score_modifie(prix_cents, echantillon_cents, n_min=n_min)
    if z is None:
        return "donnees_insuffisantes"
    if z <= seuil_erreur_z:
        return "candidat_erreur_prix"
    if z <= seuil_affaire_z:
        return "bonne_affaire"
    return "normal"


# ---------------------------------------------------------------- corroboration erreur_prix (2.3.c)


@dataclass(frozen=True)
class SignauxCorroboration:
    """Signaux deja mesures ailleurs (aucun appel reseau ici - voir le
    docstring du module) pour corroborer un candidat erreur_prix. Un champ
    absent (None, ou tuple vide pour les dates voisines) signifie que la
    sonde correspondante n'a pas ete faite."""

    prix_requete_immediate_cents: int | None = None
    prix_dates_voisines_cents: tuple[int, ...] = ()
    plancher_absolu_cents: int | None = None
    dernier_prix_cents: int | None = None
    dernier_prix_age_heures: float | None = None


@dataclass(frozen=True)
class ResultatCorroboration:
    """Verdict final + chaque signal individuel (tracabilite pour les logs
    et le futur message d'alerte, cf. plan)."""

    verdict: VerdictCorroboration
    signal_reconfirme: bool
    signal_voisines_aussi_basses: bool
    signal_plancher_absolu: bool
    signal_vitesse_chute: bool


def corroborer_erreur_prix(
    prix_cents: int,
    echantillon_cents: Sequence[int],
    signaux: SignauxCorroboration,
    *,
    n_min: int = 8,
    seuil_erreur_z: float = -3.5,
    seuil_affaire_z: float = -2.0,
    seuil_chute_pct: float = 0.40,
    fenetre_dernier_prix_heures: float = 48.0,
) -> ResultatCorroboration:
    """Verdict erreur_prix seulement si le signal 1 (re-requete immediate)
    confirme ET qu'au moins un des signaux 2/3/4 corrobore (AUDIT.md 2.3c) ;
    sinon retrograde en bonne_affaire. Signal 2 (dates voisines +/- 3 jours) :
    des voisines aussi anormalement basses sont traitees comme un signal
    confirmant (une vraie erreur tarifaire - bug de regle de prix, devise,
    taxe - touche typiquement une plage de dates, pas un seul jour ; decision
    validee explicitement, voir le plan)."""

    def _reclassifie(p: int) -> Classification:
        return classifier(
            p,
            echantillon_cents,
            n_min=n_min,
            seuil_erreur_z=seuil_erreur_z,
            seuil_affaire_z=seuil_affaire_z,
        )

    signal_1 = (
        signaux.prix_requete_immediate_cents is not None
        and _reclassifie(signaux.prix_requete_immediate_cents) != "normal"
    )
    signal_2 = bool(signaux.prix_dates_voisines_cents) and all(
        _reclassifie(p) != "normal" for p in signaux.prix_dates_voisines_cents
    )
    signal_3 = (
        signaux.plancher_absolu_cents is not None and prix_cents < signaux.plancher_absolu_cents
    )
    signal_4 = (
        signaux.dernier_prix_cents is not None
        and signaux.dernier_prix_age_heures is not None
        and signaux.dernier_prix_age_heures < fenetre_dernier_prix_heures
        and prix_cents < signaux.dernier_prix_cents * (1 - seuil_chute_pct)
    )

    verdict: VerdictCorroboration = (
        "erreur_prix" if signal_1 and (signal_2 or signal_3 or signal_4) else "bonne_affaire"
    )
    return ResultatCorroboration(
        verdict=verdict,
        signal_reconfirme=signal_1,
        signal_voisines_aussi_basses=signal_2,
        signal_plancher_absolu=signal_3,
        signal_vitesse_chute=signal_4,
    )


def dates_voisines_a_sonder(date_depart: str, ecart_jours: int = 3) -> tuple[str, str]:
    """Les 2 dates a +/- ecart_jours du candidat, a sonder pour le signal 2
    de corroboration (l'anomalie touche-t-elle aussi les dates voisines ?)."""
    depart = date.fromisoformat(date_depart)
    return (
        (depart - timedelta(days=ecart_jours)).isoformat(),
        (depart + timedelta(days=ecart_jours)).isoformat(),
    )


# ---------------------------------------------------------------- doit_alerter (2.3.d)


def doit_alerter(
    *,
    route_id: int,
    date_depart: str,
    type_alerte: TypeAlerte,
    prix_cents: int,
    alerte_precedente: dict | None,
    maintenant: datetime,
    cooldown_heures: float = 72.0,
    ratio_reduction_min: float = 0.90,
) -> bool:
    """Deduplication/cooldown (AUDIT.md 2.3d) : pas de realerte sur le meme
    (route_id, date_depart, type) avant cooldown_heures, sauf si le nouveau
    prix est sous ratio_reduction_min du prix deja alerte.

    `alerte_precedente` est la ligne de la table `alertes` qui correspond
    deja a cette cle (route_id, date_depart, type_alerte) - au plus une,
    grace a l'index UNIQUE idx_dedup - ou None si aucune alerte precedente.
    route_id/date_depart/type_alerte ne sont pas revalides contre
    alerte_precedente (l'appelant est cense l'avoir deja filtree via cette
    cle, ex. une clause SQL WHERE) ; ils documentent la cle au niveau de la
    signature. `maintenant` doit etre timezone-aware (UTC)."""
    if maintenant.tzinfo is None:
        raise ValueError("maintenant doit etre timezone-aware (UTC)")
    if alerte_precedente is None:
        return True
    age = maintenant - datetime.fromisoformat(alerte_precedente["envoyee_le"])
    if age >= timedelta(hours=cooldown_heures):
        return True
    return bool(prix_cents < alerte_precedente["prix_cents"] * ratio_reduction_min)
