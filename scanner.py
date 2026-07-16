"""
Scanner de prix de vols mondial (fournisseur : Duffel)
-------------------------------------------------------
Depuis l'origine definie dans config.yaml, et pour chaque destination de la
liste mondiale :
  1. Choisit une date de depart candidate par rotation (~3 a ~6 mois a l'avance)
  2. Cree un "offer request" Duffel pour un sejour de N nuits
  3. Retient l'offre la moins chere
  4. Enregistre le prix dans data/history.csv (historique)
  5. Envoie une alerte Telegram si :
       - le prix passe sous le seuil fixe "prix_max" (optionnel) de la destination, OU
       - le prix est un nouveau minimum historique pour cette destination, OU
       - le prix est nettement sous la mediane historique de la destination
         (bonne affaire, ou possible erreur de prix si l'ecart est tres grand)

Usage local :  python scanner.py
Automatisation : voir .github/workflows/scan.yml

Note Duffel : le token commence par "duffel_test_" (bac a sable, prix NON reels)
ou "duffel_live_" (vrais prix). On choisit via la variable DUFFEL_ACCESS_TOKEN.

Note technique : on appelle l'API Duffel directement en HTTP (pas via le
package PyPI "duffel-api", qui n'a pas ete mis a jour pour l'API v2 de Duffel
et fait planter silencieusement le parsing des offres).
"""

import csv
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

# Force stdout/stderr en UTF-8 : sur Windows, la console utilise par defaut
# un codepage (ex. cp1252) qui plante sur les caracteres comme "->".
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DUFFEL_API_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
HISTORY_FILE = BASE_DIR / "data" / "history.csv"

COLONNES = [
    "horodatage_utc", "origine", "destination",
    "date_depart", "date_retour", "prix", "devise",
    "compagnie", "escales",
]


# ---------------------------------------------------------------- config

def charger_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def creer_session_duffel() -> requests.Session:
    token = os.environ["DUFFEL_ACCESS_TOKEN"]
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Duffel-Version": DUFFEL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return session


# ---------------------------------------------------------------- candidats

def choisir_offset_semaines(code: str, offsets: list[int], jour: date) -> int:
    """Choisit un decalage (en semaines) dans `offsets`, de facon deterministe
    et sans etat a sauvegarder : chaque destination tourne a travers tous les
    decalages au fil des jours, avec un dephasage different par destination."""
    idx = (jour.toordinal() + sum(ord(c) for c in code)) % len(offsets)
    return offsets[idx]


def generer_candidats(config: dict) -> list[dict]:
    """Construit, pour chaque destination de config.yaml, une route candidate
    (une seule date par destination et par run, voir choisir_offset_semaines)."""
    origine = config["origine"]
    sejour = config.get("sejour", {})
    duree_nuits = sejour.get("duree_nuits", 10)
    offsets = sejour.get("candidats_semaines", [13, 16, 19, 22, 25])
    aujourdhui = date.today()

    candidats = []
    for entree in config["destinations"]:
        dest = entree if isinstance(entree, dict) else {"code": entree}
        code = dest["code"]
        decalage = choisir_offset_semaines(code, offsets, aujourdhui)
        depart = aujourdhui + timedelta(weeks=decalage)
        retour = depart + timedelta(days=duree_nuits)
        candidats.append({
            "origine": origine,
            "destination": code,
            "date_depart": depart.isoformat(),
            "date_retour": retour.isoformat(),
            "prix_max": dest.get("prix_max"),
            "direct_seulement": dest.get("direct_seulement", False),
        })
    return candidats


# ---------------------------------------------------------------- recherche

def chercher_meilleur_vol(session: requests.Session, route: dict, config: dict) -> dict | None:
    """Retourne l'offre la moins chere pour la route, ou None si rien trouve."""
    # Une "slice" = un trajet (aller). Un aller-retour = deux slices.
    slices = [{
        "origin": route["origine"],
        "destination": route["destination"],
        "departure_date": str(route["date_depart"]),
    }]
    if route.get("date_retour"):
        slices.append({
            "origin": route["destination"],
            "destination": route["origine"],
            "departure_date": str(route["date_retour"]),
        })

    passengers = [{"type": "adult"} for _ in range(config.get("adultes", 1))]

    body = {
        "data": {
            "cabin_class": config.get("classe", "economy"),
            "passengers": passengers,
            "slices": slices,
            "max_connections": 0 if route.get("direct_seulement") else 1,
        }
    }
    r = session.post(
        f"{DUFFEL_API_URL}/air/offer_requests",
        params={"return_offers": "true"},
        json=body,
        timeout=30,
    )
    if not r.ok:
        erreur = (r.json().get("errors") or [{}])[0]
        raise RuntimeError(f"{erreur.get('type')}: {erreur.get('title')}: {erreur.get('message')}")

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
    compagnie = (premier_segment.get("operating_carrier") or {}).get("name") \
        or (premier_segment.get("marketing_carrier") or {}).get("name", "?")

    return {
        "prix": round(float(meilleure["total_amount"]), 2),
        "devise": meilleure["total_currency"],
        "compagnie": compagnie,
        "escales": escales,
    }


# ---------------------------------------------------------------- historique

def lire_historique() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def statistiques_destination(historique: list[dict], origine: str, destination: str, config: dict) -> dict | None:
    """Statistiques de la destination (toutes dates confondues, puisque la
    date candidate change a chaque run) : minimum, mediane, et tendance
    recente vs ancienne. Retourne None sans aucune observation prealable."""
    lignes = sorted(
        (l for l in historique if l["origine"] == origine and l["destination"] == destination),
        key=lambda l: l["horodatage_utc"],
    )
    prix = [float(l["prix"]) for l in lignes]
    if not prix:
        return None

    cfg = config.get("detection", {})
    fenetre = cfg.get("fenetre_tendance", 5)
    seuil_variation = cfg.get("variation_tendance_pct", 0.05)

    recents = prix[-fenetre:]
    anciens = prix[-(fenetre * 2):-fenetre]

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


def ajouter_historique(ligne: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    nouveau_fichier = not HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLONNES)
        if nouveau_fichier:
            writer.writeheader()
        writer.writerow(ligne)


# ---------------------------------------------------------------- alertes

def envoyer_telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
        timeout=15,
    )
    r.raise_for_status()


def formater_alerte(route: dict, vol: dict, raisons: list[str], stats: dict | None) -> str:
    escales = "direct" if vol["escales"] == 0 else f"{vol['escales']} escale(s)"
    retour = f" → retour {route['date_retour']}" if route.get("date_retour") else " (aller simple)"

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
        f"✈️ <b>ALERTE PRIX — {route['origine']} → {route['destination']}</b>\n"
        f"📅 Départ {route['date_depart']}{retour}\n"
        f"💰 <b>{vol['prix']} {vol['devise']}</b> ({vol['compagnie']}, {escales})\n"
        f"🎯 {' + '.join(raisons)}\n"
        f"{ligne_tendance}"
    )


# ---------------------------------------------------------------- main

def main() -> int:
    config = charger_config()
    session = creer_session_duffel()
    historique = lire_historique()
    detection_cfg = config.get("detection", {})
    maintenant = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    erreurs = 0
    for route in generer_candidats(config):
        nom_route = f"{route['origine']}→{route['destination']} ({route['date_depart']})"
        try:
            vol = chercher_meilleur_vol(session, route, config)
        except Exception as e:
            print(f"[ERREUR API] {nom_route} : {e}", file=sys.stderr)
            erreurs += 1
            continue
        finally:
            time.sleep(0.25)

        if vol is None:
            print(f"[AUCUN VOL] {nom_route}")
            continue

        stats = statistiques_destination(historique, route["origine"], route["destination"], config)

        ajouter_historique({
            "horodatage_utc": maintenant,
            "origine": route["origine"],
            "destination": route["destination"],
            "date_depart": route["date_depart"],
            "date_retour": route.get("date_retour", ""),
            "prix": vol["prix"],
            "devise": vol["devise"],
            "compagnie": vol["compagnie"],
            "escales": vol["escales"],
        })

        raisons = []
        if route.get("prix_max") is not None and vol["prix"] <= route["prix_max"]:
            raisons.append(f"sous ton seuil fixe de {route['prix_max']} {vol['devise']}")
        if stats and vol["prix"] < stats["min"]:
            raisons.append(f"nouveau minimum historique (précédent {stats['min']} {vol['devise']})")
        if stats and stats["n"] >= detection_cfg.get("echantillon_min", 5):
            seuil_bonne_affaire = detection_cfg.get("seuil_bonne_affaire_pct", 0.15)
            plafond = stats["mediane"] * (1 - seuil_bonne_affaire)
            if vol["prix"] <= plafond:
                baisse_pct = 1 - vol["prix"] / stats["mediane"]
                seuil_erreur = detection_cfg.get("seuil_erreur_prix_pct", 0.40)
                label = "possible erreur de prix" if baisse_pct >= seuil_erreur else "bonne affaire"
                raisons.append(f"{label} : {baisse_pct:.0%} sous la médiane ({stats['mediane']:.0f} {vol['devise']})")

        if raisons:
            message = formater_alerte(route, vol, raisons, stats)
            try:
                envoyer_telegram(message)
                print(f"[ALERTE ENVOYÉE] {nom_route} : {vol['prix']} {vol['devise']}")
            except Exception as e:
                print(f"[ERREUR TELEGRAM] {nom_route} : {e}", file=sys.stderr)
                erreurs += 1
        else:
            mediane = f"{stats['mediane']:.0f}" if stats else "?"
            print(f"[OK] {nom_route} : {vol['prix']} {vol['devise']} (médiane : {mediane})")

    return 1 if erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
