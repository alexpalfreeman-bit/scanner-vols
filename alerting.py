"""
Alertes Telegram.
--------------------
Formatage (echappement HTML systematique) et envoi des messages d'alerte.
"""

import html
import logging
import random
import time
from datetime import datetime

import requests

from config import Env
from detection import TypeAlerte, doit_alerter

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
    formatage (formater_alerte) et l'envoi (envoyer_telegram). Retourne True
    si un message a ete envoye, False s'il a ete supprime par le cooldown.

    Ne catch aucune exception (ValueError de doit_alerter sur horloge naive,
    erreurs reseau/HTTP de envoyer_telegram) : les laisse remonter, comme
    aujourd'hui scanner.py gere deja ses propres try/except autour de
    l'envoi - cette politique reste dans scanner.py, pas dupliquee ici.

    prix_cents doit correspondre a vol["prix"] en centimes entiers ; comme
    alerte_precedente dans doit_alerter, ce n'est pas revalide ici (parametre
    documentaire, l'appelant est repute coherent - CLAUDE.md, ne pas valider
    l'invalidable). date_depart n'est pas un parametre separe : derive de
    route["date_depart"] pour eviter une 3e source de verite sur la meme
    donnee.

    Pas encore appelee par scanner.py (cablage : session suivante) - testee
    ici uniquement avec des fixtures synthetiques, comme detection.doit_alerter
    lui-meme en Phase 2.3."""
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
    return True
