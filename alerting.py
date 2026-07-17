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


def formater_alerte(route: dict, vol: dict, raisons: list[str], stats: dict | None) -> str:
    """Construit le message Telegram (parse_mode=HTML). Toute valeur qui ne
    vient pas d'une constante interne (compagnie, codes, raisons...) est
    passee par html.escape() : un "&" ou "<" dans un nom de compagnie ne doit
    jamais casser le message ni etre interprete comme du HTML."""
    escales = "direct" if vol["escales"] == 0 else f"{vol['escales']} escale(s)"
    retour = (
        f" → retour {html.escape(str(route['date_retour']))}"
        if route.get("date_retour")
        else " (aller simple)"
    )
    origine = html.escape(str(route["origine"]))
    destination = html.escape(str(route["destination"]))
    date_depart = html.escape(str(route["date_depart"]))
    compagnie = html.escape(str(vol["compagnie"]))
    raisons_texte = html.escape(" + ".join(raisons))

    if stats and stats["tendance"]:
        emoji = {"hausse": "📈", "baisse": "📉", "stable": "➡️"}[stats["tendance"]]
        ligne_tendance = (
            f"{emoji} Tendance {stats['tendance']} ({stats['variation_pct']:+.0%}) — "
            f"médiane {stats['mediane']:.0f} {vol['devise']} sur {stats['n']} observation(s)"
        )
    elif stats:
        ligne_tendance = f"ℹ️ {stats['n']} observation(s) — pas encore assez pour une tendance"
    else:
        ligne_tendance = "ℹ️ Première observation pour cette destination"

    return (
        f"✈️ <b>ALERTE PRIX — {origine} → {destination}</b>\n"
        f"📅 Départ {date_depart}{retour}\n"
        f"💰 <b>{vol['prix']} {vol['devise']}</b> ({compagnie}, {escales})\n"
        f"🎯 {raisons_texte}\n"
        f"{ligne_tendance}"
    )
