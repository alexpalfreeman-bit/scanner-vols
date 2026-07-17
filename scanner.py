"""
Scanner de prix de vols mondial (fournisseur : Duffel)
-------------------------------------------------------
Depuis l'origine definie dans config.yaml, et pour chaque destination de la
liste mondiale :
  1. Choisit une date de depart candidate par rotation (~3 a ~6 mois a l'avance)
  2. Cree un "offer request" Duffel pour un sejour de N nuits
  3. Retient l'offre la moins chere
  4. Enregistre le prix dans data/scanner.db (historique)
  5. Envoie une alerte Telegram si :
       - le prix passe sous le seuil fixe "prix_max" (optionnel) de la destination, OU
       - le prix est un nouveau minimum historique pour cette destination, OU
       - le prix est nettement sous la mediane historique de la destination
         (bonne affaire, ou possible erreur de prix si l'ecart est tres grand)

Usage local :  python scanner.py
Automatisation : voir .github/workflows/scan.yml

Note Duffel : le token commence par "duffel_test_" (bac a sable, prix NON reels)
ou "duffel_live_" (vrais prix). On choisit via la variable DUFFEL_ACCESS_TOKEN.

Ce module est l'orchestrateur : il enchaine config -> providers -> storage ->
detection -> alerting (voir CLAUDE.md pour le decoupage en modules).
"""

import io
import logging
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta

from alerting import envoyer_telegram, formater_alerte
from config import ErreurConfiguration, charger_config, charger_env, valider_config
from detection import (
    classifier,
    echantillon_comparable,
    est_nouveau_minimum,
    raison_prix_max,
    statistiques_destination,
)
from providers.duffel import FournisseurDuffel
from storage import enregistrer_observation, horizon_jours, lire_historique, lire_observations

# Force stdout/stderr en UTF-8 : sur Windows, la console utilise par defaut
# un codepage (ex. cp1252) qui plante sur les caracteres comme "->". Le
# isinstance() garde le typage correct (TextIO n'a pas reconfigure()) et
# protege le cas rare ou stdout/stderr aurait deja ete remplace ailleurs.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)


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


def horodatage_maintenant() -> str:
    """ISO 8601 UTC a la seconde, un appel par observation (pas une seule
    fois par run) : deux prix trouves a des instants differents ne doivent
    jamais partager le meme horodatage."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        env = charger_env()
        config = valider_config(charger_config())
    except ErreurConfiguration as e:
        logger.error("Configuration invalide : %s", e)
        return 1

    fournisseur = FournisseurDuffel(env, config)
    fournisseur.verifier_canari()
    historique = lire_historique()
    observations_brutes = lire_observations()
    detection_cfg = config.get("detection", {})

    resultats: list[tuple[str, str]] = []
    for route in generer_candidats(config):
        nom_route = f"{route['origine']}→{route['destination']} ({route['date_depart']})"
        try:
            offre = fournisseur.meilleure_offre(route)
        except Exception as e:
            logger.error("%s : erreur API : %s", nom_route, e)
            resultats.append((nom_route, f"ERREUR API : {e}"))
            continue
        finally:
            time.sleep(0.25)

        if offre is None:
            logger.warning("%s : aucun vol trouvé", nom_route)
            resultats.append((nom_route, "aucun vol trouvé"))
            continue

        stats = statistiques_destination(
            historique, route["origine"], route["destination"], offre.devise, config
        )

        horodatage = horodatage_maintenant()
        route_id = enregistrer_observation(
            origine=route["origine"],
            destination=route["destination"],
            observe_le=horodatage,
            date_depart=route["date_depart"],
            date_retour=route.get("date_retour") or None,
            prix_cents=offre.prix_cents,
            devise=offre.devise,
            compagnie=offre.compagnie,
            escales=offre.escales,
        )

        # Mode observation (Session A, AUDIT.md 2.3 câblage) : la classification
        # est calculée et loggée mais ne déclenche encore aucune alerte - le
        # câblage complet (envoyer_alerte/corroboration/digest) est prévu en
        # session suivante, une fois ces classifications sanity-checkées contre
        # du trafic réel.
        maintenant = datetime.fromisoformat(horodatage)
        horizon = horizon_jours(horodatage, route["date_depart"])
        echantillon = echantillon_comparable(
            observations_brutes,
            route_id=route_id,
            devise=offre.devise,
            date_depart_candidat=route["date_depart"],
            horizon_jours_candidat=horizon,
            maintenant=maintenant,
        )
        classification = classifier(offre.prix_cents, echantillon.prix_cents)
        logger.info(
            "%s : classification z-score = %s (niveau=%s, n=%s)",
            nom_route,
            classification,
            echantillon.niveau,
            echantillon.n,
        )

        prix_dollars = offre.prix_cents / 100
        raisons = []
        if route.get("prix_max") is not None and offre.devise != config["devise"]:
            logger.warning(
                "%s : prix_max ignoré (configuré en %s, offre en %s)",
                nom_route,
                config["devise"],
                offre.devise,
            )
        raison_seuil = raison_prix_max(
            prix_dollars, offre.devise, route.get("prix_max"), config["devise"]
        )
        if raison_seuil:
            raisons.append(raison_seuil)
        echantillon_min = detection_cfg.get("echantillon_min", 5)
        marge_minimum_pct = detection_cfg.get("marge_minimum_pct", 0.03)
        if stats and est_nouveau_minimum(prix_dollars, stats, echantillon_min, marge_minimum_pct):
            raisons.append(f"nouveau minimum historique (précédent {stats['min']} {offre.devise})")
        if stats and stats["n"] >= detection_cfg.get("echantillon_min", 5):
            seuil_bonne_affaire = detection_cfg.get("seuil_bonne_affaire_pct", 0.15)
            plafond = stats["mediane"] * (1 - seuil_bonne_affaire)
            if prix_dollars <= plafond:
                baisse_pct = 1 - prix_dollars / stats["mediane"]
                seuil_erreur = detection_cfg.get("seuil_erreur_prix_pct", 0.40)
                label = "possible erreur de prix" if baisse_pct >= seuil_erreur else "bonne affaire"
                raisons.append(
                    f"{label} : {baisse_pct:.0%} sous la médiane ({stats['mediane']:.0f} {offre.devise})"
                )

        if raisons:
            vol = {
                "prix": prix_dollars,
                "devise": offre.devise,
                "compagnie": offre.compagnie,
                "escales": offre.escales,
            }
            message = formater_alerte(route, vol, raisons, stats)
            try:
                envoyer_telegram(message, env)
                logger.info("%s : alerte envoyée (%s %s)", nom_route, prix_dollars, offre.devise)
                resultats.append((nom_route, f"alerte envoyée : {prix_dollars} {offre.devise}"))
            except Exception as e:
                logger.error("%s : erreur Telegram : %s", nom_route, e)
                resultats.append((nom_route, f"ERREUR TELEGRAM : {e}"))
        else:
            mediane = f"{stats['mediane']:.0f}" if stats else "?"
            logger.info(
                "%s : ok (%s %s, médiane %s)", nom_route, prix_dollars, offre.devise, mediane
            )
            resultats.append((nom_route, f"ok : {prix_dollars} {offre.devise}"))

    fournisseur.resume()
    ecrire_resume_github(resultats)
    total = len(resultats)
    echecs = sum(1 for _, r in resultats if r.startswith("ERREUR"))
    return 1 if total and echecs == total else 0


if __name__ == "__main__":
    sys.exit(main())
