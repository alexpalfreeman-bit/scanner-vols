"""
Alertes Telegram.
--------------------
Formatage (echappement HTML systematique) et envoi des messages d'alerte.
"""

import html
import logging
import random
import time
from collections.abc import Sequence
from datetime import datetime

import requests

from config import Env
from detection import TypeAlerte, doit_alerter
from providers.base import ResumeFournisseur
from storage import enregistrer_alerte

logger = logging.getLogger(__name__)

# Mirroir volontairement decouple de providers/duffel.py::RETRIABLES : alerting.py
# ne doit dependre que de providers.base (contrat generique), jamais d'un
# fournisseur concret comme providers.duffel - voir aussi SEUIL_TAUX_ZERO_OFFRES
# plus bas dans ce fichier, meme raisonnement.
RETRIABLES = {429, 500, 502, 503, 504}


def envoyer_telegram(message: str, env: Env, essais: int = 3) -> None:
    """POST vers l'API Telegram, avec retry leger (backoff exponentiel +
    jitter, meme formule que providers.duffel.post_resilient) sur exception
    reseau ou code HTTP transitoire (429/5xx). essais=3 (vs 4 chez Duffel) :
    le volume d'appels Telegram par run est bien plus faible qu'un appel
    fournisseur par destination, pas besoin du meme budget de tentatives.

    Un code non-transitoire (ex. 400/401) leve immediatement, sans retry :
    r.raise_for_status() vit dans le `else` du try, donc l'HTTPError qu'il
    leve n'est jamais capture par le `except requests.RequestException` qui
    gere les retries (HTTPError herite de RequestException - sans ce `else`,
    un 401 permanent serait retente `essais` fois pour rien).

    Ne relit pas Retry-After / parameters.retry_after : le mecanisme de
    rate-limit documente par Telegram est un champ du corps JSON, pas l'en-tete
    HTTP que lit post_resilient - un copier-coller de cette partie serait un
    no-op silencieux. Reste volontairement plus simple, cf. "retry leger"
    (AUDIT.md 2.5)."""
    url = f"https://api.telegram.org/bot{env.telegram_bot_token}/sendMessage"
    derniere_erreur: Exception = RuntimeError("aucune tentative")
    for tentative in range(essais):
        attente = (2**tentative) + random.uniform(0, 1)
        try:
            r = requests.post(
                url,
                data={"chat_id": env.telegram_chat_id, "text": message, "parse_mode": "HTML"},
                timeout=15,
            )
        except requests.RequestException as e:
            derniere_erreur = e
        else:
            if r.status_code not in RETRIABLES:
                r.raise_for_status()
                return
            derniere_erreur = RuntimeError(f"HTTP {r.status_code} sur {url}")
        if tentative < essais - 1:
            time.sleep(attente)
    raise derniere_erreur


def _echapper(valeur: object) -> str:
    """html.escape(str(valeur)) : point unique d'echappement, pour que le
    caractere systematique de l'echappement soit verifiable a un seul endroit
    plutot que reimplemente par discipline a chaque appel."""
    return html.escape(str(valeur))


def formater_alerte(route: dict, vol: dict, raisons: list[str], stats: dict | None) -> str:
    """Construit le message Telegram (parse_mode=HTML). Toute valeur qui ne
    vient pas d'une constante interne (compagnie, devise, prix, codes,
    raisons...) est passee par _echapper() : un "&" ou "<" quelque part (ex.
    dans un nom de compagnie, ou dans le code devise renvoye par l'API) ne
    doit jamais casser le message ni etre interprete comme du HTML.

    Exception assumee : stats["tendance"] n'est pas echappe - c'est un
    litteral ferme ("hausse"/"baisse"/"stable", jamais autre chose), utilise
    comme cle du dict emoji juste en-dessous : une valeur etrangere y
    leverait KeyError avant meme d'atteindre l'interpolation."""
    devise = _echapper(vol["devise"])
    prix = _echapper(vol["prix"])
    escales = "direct" if vol["escales"] == 0 else f"{_echapper(vol['escales'])} escale(s)"
    retour = (
        f" → retour {_echapper(route['date_retour'])}"
        if route.get("date_retour")
        else " (aller simple)"
    )
    origine = _echapper(route["origine"])
    destination = _echapper(route["destination"])
    date_depart = _echapper(route["date_depart"])
    compagnie = _echapper(vol["compagnie"])
    raisons_texte = _echapper(" + ".join(raisons))

    if stats and stats["tendance"]:
        emoji = {"hausse": "📈", "baisse": "📉", "stable": "➡️"}[stats["tendance"]]
        ligne_tendance = (
            f"{emoji} Tendance {stats['tendance']} ({stats['variation_pct']:+.0%}) — "
            f"médiane {stats['mediane']:.0f} {devise} sur {stats['n']} observation(s)"
        )
    elif stats:
        ligne_tendance = f"ℹ️ {stats['n']} observation(s) — pas encore assez pour une tendance"
    else:
        ligne_tendance = "ℹ️ Première observation pour cette destination"

    return (
        f"✈️ <b>ALERTE PRIX — {origine} → {destination}</b>\n"
        f"📅 Départ {date_depart}{retour}\n"
        f"💰 <b>{prix} {devise}</b> ({compagnie}, {escales})\n"
        f"🎯 {raisons_texte}\n"
        f"{ligne_tendance}"
    )


# ---------------------------------------------------------------- envoyer_alerte (2.5, gate 2.3.d)


def envoyer_alerte(
    *,
    route: dict,
    vol: dict,
    raisons: list[str],
    stats: dict | None,
    route_id: int,
    type_alerte: TypeAlerte,
    prix_cents: int,
    alerte_precedente: dict | None,
    maintenant: datetime,
    env: Env,
    cooldown_heures: float = 72.0,
    ratio_reduction_min: float = 0.90,
) -> bool:
    """Envoie l'alerte seulement si detection.doit_alerter l'autorise (dedup/
    cooldown, AUDIT.md 2.3d) : compose la decision (doit_alerter), le
    formatage (formater_alerte), l'envoi (envoyer_telegram) et la
    persistance (storage.enregistrer_alerte). Retourne True si un message a
    ete envoye, False s'il a ete supprime par le cooldown.

    La persistance n'a lieu qu'apres un envoi confirme reussi : l'appel a
    enregistrer_alerte suit directement envoyer_telegram, sans try/except
    entre les deux. Si envoyer_telegram leve (echec definitif), enregistrer_alerte
    n'est jamais atteint - sinon le cooldown de 72h supprimerait une alerte
    legitime jamais recue. Ne catch aucune exception (ValueError de
    doit_alerter sur horloge naive, erreurs reseau/HTTP de envoyer_telegram) :
    les laisse remonter, comme aujourd'hui scanner.py gere deja ses propres
    try/except autour de l'envoi - cette politique reste dans scanner.py, pas
    dupliquee ici.

    prix_cents doit correspondre a vol["prix"] en centimes entiers ; comme
    alerte_precedente dans doit_alerter, ce n'est pas revalide ici (parametre
    documentaire, l'appelant est repute coherent - CLAUDE.md, ne pas valider
    l'invalidable). date_depart n'est pas un parametre separe : derive de
    route["date_depart"] pour eviter une 3e source de verite sur la meme
    donnee.

    Cablee dans scanner.py (cablage go-live, Session B) : le route_id/prix_cents/
    type_alerte recus ici sont aussi ceux utilises pour la cle de persistance."""
    date_depart = route["date_depart"]
    if not doit_alerter(
        route_id=route_id,
        date_depart=date_depart,
        type_alerte=type_alerte,
        prix_cents=prix_cents,
        alerte_precedente=alerte_precedente,
        maintenant=maintenant,
        cooldown_heures=cooldown_heures,
        ratio_reduction_min=ratio_reduction_min,
    ):
        logger.info(
            "route_id=%s date_depart=%s type=%s : alerte supprimee (dedup/cooldown)",
            route_id,
            date_depart,
            type_alerte,
        )
        return False
    envoyer_telegram(formater_alerte(route, vol, raisons, stats), env)
    enregistrer_alerte(
        route_id=route_id,
        date_depart=date_depart,
        type_alerte=type_alerte,
        prix_cents=prix_cents,
        envoyee_le=maintenant.isoformat(timespec="seconds"),
    )
    return True


# ---------------------------------------------------------------- digest technique (2.5)

# Mirroir volontairement decouple de providers/duffel.py::SEUIL_TAUX_ZERO_OFFRES /
# APPELS_REUSSIS_MIN_POUR_AVERTISSEMENT : providers/base.py documente que
# ResumeFournisseur doit rester le contrat generique consomme par ce digest (un
# futur 2e fournisseur, ex. Amadeus, doit fonctionner sans changement ici) -
# meme raisonnement que RETRIABLES plus haut dans ce fichier.
SEUIL_TAUX_ZERO_OFFRES = 0.5
APPELS_REUSSIS_MIN_POUR_AVERTISSEMENT = 5

# Limite Telegram : 4096 caracteres/message. Les deux plafonds suivants bornent
# le pire cas (toutes les routes en erreur, details longs) tres en-dessous :
# MAX_ROUTES_ERREUR_AFFICHEES lignes x (~27 car. d'entete + MAX_LONGUEUR_DETAIL_ERREUR
# de detail) ~= 2540 caracteres, marge confortable pour l'en-tete et les
# fournisseurs (verifie par test, cf. tests/test_alerting.py).
MAX_ROUTES_ERREUR_AFFICHEES = 20
MAX_LONGUEUR_DETAIL_ERREUR = 100


def _est_echec(resultat: str) -> bool:
    """Meme convention que scanner.py/ecrire_resume_github : un resultat qui
    commence par "ERREUR" compte comme echec pour la politique de sortie du
    scan. Dependance a cette convention documentee ici, pas redecidee a
    chaque appelant."""
    return resultat.startswith("ERREUR")


def digest_necessaire(
    resultats: Sequence[tuple[str, str]],
    resumes: Sequence[ResumeFournisseur],
    *,
    budget_corroboration_epuise: bool = False,
) -> bool:
    """True si au moins une route a echoue (_est_echec), OU au moins un
    fournisseur est suspendu, OU le taux de reponses 0-offre d'un
    fournisseur est anormal, OU le budget de corroboration a ete epuise
    pendant le run (AUDIT.md Session B : atteindre ce plafond est une
    anomalie en soi, pas seulement un detail de cout). Ce dernier cas n'est
    pas une extrapolation : AUDIT.md 2.4 dit explicitement qu'un taux
    anormal doit produire "un avertissement dans le digest technique", et le
    Journal Phase 2.4 decrit le log GitHub Actions comme "seul filet de
    securite reel en l'absence du digest" - ce digest doit devenir ce filet,
    pas seulement afficher le taux quand il se declenche deja pour une autre
    raison."""
    if budget_corroboration_epuise:
        return True
    if any(_est_echec(resultat) for _, resultat in resultats):
        return True
    for resume in resumes:
        if resume.suspendu:
            return True
        if (
            resume.appels_reussis >= APPELS_REUSSIS_MIN_POUR_AVERTISSEMENT
            and resume.appels_zero_offres / resume.appels_reussis > SEUIL_TAUX_ZERO_OFFRES
        ):
            return True
    return False


def _ligne_fournisseur(resume: ResumeFournisseur) -> str:
    nom = _echapper(resume.nom)
    if resume.appels_reussis == 0:
        taux_texte = "aucun appel reussi"
    else:
        taux = resume.appels_zero_offres / resume.appels_reussis
        anomalie = " ⚠️" if taux > SEUIL_TAUX_ZERO_OFFRES else ""
        taux_texte = (
            f"{resume.appels_reussis} appel(s) reussi(s), {taux:.0%} reponses 0-offre{anomalie}"
        )
    suspendu = " — SUSPENDU" if resume.suspendu else ""
    return f"  • {nom} : {taux_texte}{suspendu}"


def formater_digest(
    resultats: Sequence[tuple[str, str]],
    resumes: Sequence[ResumeFournisseur],
    *,
    budget_corroboration_epuise: bool = False,
) -> str:
    """Message technique HTML (parse_mode=HTML) : compte de routes OK/en
    erreur, liste des routes en erreur (plafonnee a MAX_ROUTES_ERREUR_AFFICHEES,
    chaque detail echappe PUIS tronque a MAX_LONGUEUR_DETAIL_ERREUR - dans cet
    ordre, pour que la longueur affichee reste bornee meme si l'echappement
    allonge le texte, ex. "<" -> "&lt;" ; un detail peut contenir du HTML brut
    venant de extraire_message_erreur sur un 502, ex. r.text[:200]), une
    ligne par fournisseur, puis un avertissement si le budget de corroboration
    a ete epuise pendant le run (AUDIT.md Session B). Fonction pure : aucune
    lecture d'horloge, pas de timestamp absolu dans le message (Telegram
    horodate deja les messages recus)."""
    erreurs = [(nom, detail) for nom, detail in resultats if _est_echec(detail)]
    total = len(resultats)
    n_erreurs = len(erreurs)

    lignes = [
        "🛠️ <b>Digest technique du scan</b>",
        f"📊 {total - n_erreurs}/{total} routes OK, {n_erreurs} en erreur",
    ]

    if erreurs:
        lignes.append("")
        lignes.append("🔴 Routes en erreur :")
        for nom, detail in erreurs[:MAX_ROUTES_ERREUR_AFFICHEES]:
            detail_affiche = _echapper(detail)[:MAX_LONGUEUR_DETAIL_ERREUR]
            lignes.append(f"  • {_echapper(nom)} — {detail_affiche}")
        reste = n_erreurs - MAX_ROUTES_ERREUR_AFFICHEES
        if reste > 0:
            lignes.append(f"  ... et {reste} autre(s)")

    if resumes:
        lignes.append("")
        lignes.append("🔌 Fournisseurs :")
        lignes.extend(_ligne_fournisseur(resume) for resume in resumes)

    if budget_corroboration_epuise:
        lignes.append("")
        lignes.append("⚠️ Budget de corroboration épuisé pendant ce run (plafond atteint)")

    return "\n".join(lignes)


def envoyer_digest(
    resultats: Sequence[tuple[str, str]],
    resumes: Sequence[ResumeFournisseur],
    env: Env,
    *,
    budget_corroboration_epuise: bool = False,
) -> bool:
    """Envoie le digest technique seulement si digest_necessaire (AUDIT.md
    2.5) : cf. son docstring pour les conditions de declenchement. Retourne
    True si un message a ete envoye. Cablee dans scanner.py (cablage
    go-live, Session B)."""
    if not digest_necessaire(
        resultats, resumes, budget_corroboration_epuise=budget_corroboration_epuise
    ):
        return False
    envoyer_telegram(
        formater_digest(
            resultats, resumes, budget_corroboration_epuise=budget_corroboration_epuise
        ),
        env,
    )
    return True
