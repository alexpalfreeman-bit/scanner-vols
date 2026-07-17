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
import io
import os
import random
import statistics
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import requests
import yaml

from config import Env, ErreurConfiguration, charger_env, valider_config

# Force stdout/stderr en UTF-8 : sur Windows, la console utilise par defaut
# un codepage (ex. cp1252) qui plante sur les caracteres comme "->". Le
# isinstance() garde le typage correct (TextIO n'a pas reconfigure()) et
# protege le cas rare ou stdout/stderr aurait deja ete remplace ailleurs.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DUFFEL_API_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
HISTORY_FILE = BASE_DIR / "data" / "history.csv"

COLONNES = [
    "horodatage_utc",
    "origine",
    "destination",
    "date_depart",
    "date_retour",
    "prix",
    "devise",
    "compagnie",
    "escales",
]


# ---------------------------------------------------------------- config


def charger_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return cast(dict, yaml.safe_load(f))


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
        candidats.append(
            {
                "origine": origine,
                "destination": code,
                "date_depart": depart.isoformat(),
                "date_retour": retour.isoformat(),
                "prix_max": dest.get("prix_max"),
                "direct_seulement": dest.get("direct_seulement", False),
            }
        )
    return candidats


# ---------------------------------------------------------------- recherche


RETRIABLES = {429, 500, 502, 503, 504}


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
    """Retourne l'offre la moins chere pour la route, ou None si rien trouve."""
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


# ---------------------------------------------------------------- historique


def lire_historique() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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


def ajouter_historique(ligne: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    nouveau_fichier = not HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLONNES)
        if nouveau_fichier:
            writer.writeheader()
        writer.writerow(ligne)


# ---------------------------------------------------------------- alertes


def envoyer_telegram(message: str, env: Env) -> None:
    url = f"https://api.telegram.org/bot{env.telegram_bot_token}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": env.telegram_chat_id, "text": message, "parse_mode": "HTML"},
        timeout=15,
    )
    r.raise_for_status()


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


def ecrire_resume_github(resultats: list[tuple[str, str]]) -> None:
    """Ajoute un resume Markdown a $GITHUB_STEP_SUMMARY si present (no-op en local)."""
    chemin = os.environ.get("GITHUB_STEP_SUMMARY")
    if not chemin:
        return
    lignes = ["## Résumé du scan", "", "| Route | Résultat |", "|---|---|"]
    lignes += [f"| {route} | {resultat} |" for route, resultat in resultats]
    try:
        with open(chemin, "a", encoding="utf-8") as f:
            f.write("\n".join(lignes) + "\n")
    except OSError:
        pass


def main() -> int:
    try:
        env = charger_env()
        config = valider_config(charger_config())
    except ErreurConfiguration as e:
        print(f"[ERREUR CONFIGURATION] {e}", file=sys.stderr)
        return 1

    session = creer_session_duffel(env)
    historique = lire_historique()
    detection_cfg = config.get("detection", {})
    maintenant = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")

    resultats: list[tuple[str, str]] = []
    for route in generer_candidats(config):
        nom_route = f"{route['origine']}→{route['destination']} ({route['date_depart']})"
        try:
            vol = chercher_meilleur_vol(session, route, config)
        except Exception as e:
            print(f"[ERREUR API] {nom_route} : {e}", file=sys.stderr)
            resultats.append((nom_route, f"ERREUR API : {e}"))
            continue
        finally:
            time.sleep(0.25)

        if vol is None:
            print(f"[AUCUN VOL] {nom_route}")
            resultats.append((nom_route, "aucun vol trouvé"))
            continue

        stats = statistiques_destination(
            historique, route["origine"], route["destination"], vol["devise"], config
        )

        ajouter_historique(
            {
                "horodatage_utc": maintenant,
                "origine": route["origine"],
                "destination": route["destination"],
                "date_depart": route["date_depart"],
                "date_retour": route.get("date_retour", ""),
                "prix": vol["prix"],
                "devise": vol["devise"],
                "compagnie": vol["compagnie"],
                "escales": vol["escales"],
            }
        )

        raisons = []
        if route.get("prix_max") is not None and vol["devise"] != config["devise"]:
            print(
                f"[AVERTISSEMENT] {nom_route} : prix_max ignoré (configuré en "
                f"{config['devise']}, offre en {vol['devise']})",
                file=sys.stderr,
            )
        raison_seuil = raison_prix_max(
            vol["prix"], vol["devise"], route.get("prix_max"), config["devise"]
        )
        if raison_seuil:
            raisons.append(raison_seuil)
        if stats and vol["prix"] < stats["min"]:
            raisons.append(f"nouveau minimum historique (précédent {stats['min']} {vol['devise']})")
        if stats and stats["n"] >= detection_cfg.get("echantillon_min", 5):
            seuil_bonne_affaire = detection_cfg.get("seuil_bonne_affaire_pct", 0.15)
            plafond = stats["mediane"] * (1 - seuil_bonne_affaire)
            if vol["prix"] <= plafond:
                baisse_pct = 1 - vol["prix"] / stats["mediane"]
                seuil_erreur = detection_cfg.get("seuil_erreur_prix_pct", 0.40)
                label = "possible erreur de prix" if baisse_pct >= seuil_erreur else "bonne affaire"
                raisons.append(
                    f"{label} : {baisse_pct:.0%} sous la médiane ({stats['mediane']:.0f} {vol['devise']})"
                )

        if raisons:
            message = formater_alerte(route, vol, raisons, stats)
            try:
                envoyer_telegram(message, env)
                print(f"[ALERTE ENVOYÉE] {nom_route} : {vol['prix']} {vol['devise']}")
                resultats.append((nom_route, f"alerte envoyée : {vol['prix']} {vol['devise']}"))
            except Exception as e:
                print(f"[ERREUR TELEGRAM] {nom_route} : {e}", file=sys.stderr)
                resultats.append((nom_route, f"ERREUR TELEGRAM : {e}"))
        else:
            mediane = f"{stats['mediane']:.0f}" if stats else "?"
            print(f"[OK] {nom_route} : {vol['prix']} {vol['devise']} (médiane : {mediane})")
            resultats.append((nom_route, f"ok : {vol['prix']} {vol['devise']}"))

    ecrire_resume_github(resultats)
    total = len(resultats)
    echecs = sum(1 for _, r in resultats if r.startswith("ERREUR"))
    return 1 if total and echecs == total else 0


if __name__ == "__main__":
    sys.exit(main())
