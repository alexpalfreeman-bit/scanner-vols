"""
Alertes Telegram.
--------------------
Formatage (echappement HTML systematique) et envoi des messages d'alerte.
"""

import html

import requests

from config import Env


def envoyer_telegram(message: str, env: Env) -> None:
    url = f"https://api.telegram.org/bot{env.telegram_bot_token}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": env.telegram_chat_id, "text": message, "parse_mode": "HTML"},
        timeout=15,
    )
    r.raise_for_status()


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
