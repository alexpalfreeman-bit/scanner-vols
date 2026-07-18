"""
Scanner de prix de vols mondial (fournisseur : Duffel)
-------------------------------------------------------
Depuis l'origine definie dans config.yaml, et pour chaque destination de la
liste mondiale :
  1. Choisit une date de depart candidate par rotation (~3 a ~6 mois a l'avance)
  2. Cree un "offer request" Duffel pour un sejour de N nuits
  3. Retient l'offre la moins chere
  4. Enregistre le prix dans data/scanner.db (historique)
  5. Envoie une alerte Telegram (jusqu'a 3 messages independants, chacun avec
     sa propre deduplication/cooldown de 72h) si :
       - le prix passe sous le seuil fixe "prix_max" (optionnel) de la destination, OU
       - le prix est un nouveau minimum historique pour cette destination, OU
       - le prix est un outlier bas (z-score robuste) par rapport a un
         echantillon comparable (meme route/mois/horizon de reservation) :
         bonne affaire, ou erreur de prix probable si en plus corrobore par
         re-requete (option detection.corroboration_activee, config.yaml)

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
import statistics
import sys
import time
from datetime import UTC, date, datetime, timedelta

from alerting import envoyer_alerte, envoyer_digest
from config import Env, ErreurConfiguration, charger_config, charger_env, valider_config
from detection import (
    SignauxCorroboration,
    TypeAlerte,
    classifier,
    corroborer_erreur_prix,
    dates_voisines_a_sonder,
    echantillon_comparable,
    est_nouveau_minimum,
    raison_prix_max,
    statistiques_destination,
)
from providers.base import FournisseurVols, Offre
from providers.duffel import FournisseurDuffel, environnement_duffel
from storage import (
    enregistrer_observation,
    horizon_jours,
    lire_historique,
    lire_observations,
    obtenir_derniere_alerte,
)

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


# ---------------------------------------------------------------- corroboration (Session B)


def _route_avec_date_depart(route: dict, nouvelle_date_depart: str) -> dict:
    """Route candidate translatee a une nouvelle date de depart (sondes de
    corroboration, AUDIT.md 2.3c) : conserve la duree du sejour en
    translatant date_retour du meme delta si present - ne fabrique jamais un
    retour sur un aller simple."""
    nouvelle_route = dict(route)
    nouvelle_route["date_depart"] = nouvelle_date_depart
    if route.get("date_retour"):
        delta = date.fromisoformat(nouvelle_date_depart) - date.fromisoformat(route["date_depart"])
        nouvelle_route["date_retour"] = (
            date.fromisoformat(route["date_retour"]) + delta
        ).isoformat()
    return nouvelle_route


def _prix_si_valide(offre: Offre | None, devise_attendue: str) -> int | None:
    """None si la sonde n'a renvoye aucune offre, ou si sa devise differe de
    celle de l'offre candidate (jamais de comparaison cross-devise dans la
    corroboration - meme garde que raison_prix_max/statistiques_destination)."""
    if offre is None or offre.devise != devise_attendue:
        return None
    return offre.prix_cents


def sonder_corroboration(
    fournisseur: FournisseurVols, route: dict, devise: str, budget_restant: int
) -> tuple[SignauxCorroboration, int]:
    """Sonde les signaux 1 (re-requete immediate) et 2 (dates voisines +/- 3
    jours) de la corroboration erreur_prix (AUDIT.md 2.3c ; signaux 3/4 non
    cables cette session, cf. Journal Session A). Chaque sonde reseau est
    individuellement tolerante aux pannes (comme verifier_canari) : une
    exception ou une devise inattendue redescend simplement le signal a
    absent, jamais un crash du run. Le budget est decompte que la sonde
    reussisse ou non ; s'il est deja epuise en entree, aucun appel reseau
    n'est tente - purement mecanique, sans log (l'appelant, scanner.py::main,
    est seul a savoir si c'est une premiere transition a journaliser).
    Retourne les signaux mesures (partiels si le budget est insuffisant pour
    tout sonder) et le budget restant."""
    if budget_restant <= 0:
        return SignauxCorroboration(), budget_restant

    budget_restant -= 1
    try:
        offre = fournisseur.meilleure_offre(route)
        prix_immediat_cents = _prix_si_valide(offre, devise)
    except Exception as e:
        logger.warning(
            "corroboration %s : signal 1 (re-requête) en échec (%s)", route["destination"], e
        )
        prix_immediat_cents = None
    time.sleep(0.25)

    prix_voisines_cents: list[int] = []
    if budget_restant > 0:
        avant, apres = dates_voisines_a_sonder(route["date_depart"])
        for date_voisine in (avant, apres):
            if budget_restant <= 0:
                break
            budget_restant -= 1
            try:
                offre = fournisseur.meilleure_offre(_route_avec_date_depart(route, date_voisine))
                prix = _prix_si_valide(offre, devise)
                if prix is not None:
                    prix_voisines_cents.append(prix)
            except Exception as e:
                logger.warning(
                    "corroboration %s : signal 2 (date voisine %s) en échec (%s)",
                    route["destination"],
                    date_voisine,
                    e,
                )
            time.sleep(0.25)

    return (
        SignauxCorroboration(
            prix_requete_immediate_cents=prix_immediat_cents,
            prix_dates_voisines_cents=tuple(prix_voisines_cents),
        ),
        budget_restant,
    )


def _tenter_alerte(
    *,
    route: dict,
    vol: dict,
    stats: dict | None,
    route_id: int,
    type_alerte: TypeAlerte,
    raisons: list[str],
    prix_cents: int,
    maintenant: datetime,
    env: Env,
) -> tuple[bool, str | None]:
    """Recupere l'alerte precedente (dedup/cooldown) et tente l'envoi via
    alerting.envoyer_alerte ; convertit toute exception (Telegram, reseau) en
    message d'erreur plutot que de crasher le run - chacun des 3 types
    d'alerte independants (seuil/minimum/aubaine-ou-erreur_prix, AUDIT.md,
    Journal Session A) garde son autonomie vis-a-vis des 2 autres. Retourne
    (envoyee, erreur)."""
    alerte_precedente = obtenir_derniere_alerte(route_id, route["date_depart"], type_alerte)
    try:
        envoyee = envoyer_alerte(
            route=route,
            vol=vol,
            raisons=raisons,
            stats=stats,
            route_id=route_id,
            type_alerte=type_alerte,
            prix_cents=prix_cents,
            alerte_precedente=alerte_precedente,
            maintenant=maintenant,
            env=env,
        )
        return envoyee, None
    except Exception as e:
        logger.error("%s : erreur Telegram (%s) : %s", route["destination"], type_alerte, e)
        return False, str(e)


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

    environnement = environnement_duffel(env.duffel_access_token)
    if environnement == "production":
        logger.info("environnement Duffel detecte : production")
    else:
        logger.warning(
            "environnement Duffel detecte : %s - les observations de ce run seront "
            "enregistrees mais exclues du moteur de detection (audit data/scanner.db, "
            "Journal AUDIT.md)",
            environnement,
        )

    fournisseur = FournisseurDuffel(env, config)
    fournisseur.verifier_canari()
    historique = lire_historique()
    observations_brutes = lire_observations()
    detection_cfg = config.get("detection", {})
    budget_corroboration = detection_cfg.get("corroboration_max_requetes_par_run", 15)
    budget_corroboration_epuise = False

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
            environnement=environnement,
        )

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
        vol = {
            "prix": prix_dollars,
            "devise": offre.devise,
            "compagnie": offre.compagnie,
            "escales": offre.escales,
        }
        if route.get("prix_max") is not None and offre.devise != config["devise"]:
            logger.warning(
                "%s : prix_max ignoré (configuré en %s, offre en %s)",
                nom_route,
                config["devise"],
                offre.devise,
            )

        types_envoyes: list[str] = []
        erreurs_alerte: list[str] = []

        # --- type 'seuil' : prix sous le seuil fixe de la destination (Phase 1, conserve)
        raison_seuil = raison_prix_max(
            prix_dollars, offre.devise, route.get("prix_max"), config["devise"]
        )
        if raison_seuil:
            envoyee, erreur = _tenter_alerte(
                route=route,
                vol=vol,
                stats=stats,
                route_id=route_id,
                type_alerte="seuil",
                raisons=[raison_seuil],
                prix_cents=offre.prix_cents,
                maintenant=maintenant,
                env=env,
            )
            if erreur:
                erreurs_alerte.append(f"seuil: {erreur}")
            elif envoyee:
                types_envoyes.append("seuil")

        # --- type 'minimum' : nouveau minimum historique de la destination (Phase 1, conserve)
        echantillon_min = detection_cfg.get("echantillon_min", 5)
        marge_minimum_pct = detection_cfg.get("marge_minimum_pct", 0.03)
        if stats and est_nouveau_minimum(prix_dollars, stats, echantillon_min, marge_minimum_pct):
            raison_minimum = f"nouveau minimum historique (précédent {stats['min']} {offre.devise})"
            envoyee, erreur = _tenter_alerte(
                route=route,
                vol=vol,
                stats=stats,
                route_id=route_id,
                type_alerte="minimum",
                raisons=[raison_minimum],
                prix_cents=offre.prix_cents,
                maintenant=maintenant,
                env=env,
            )
            if erreur:
                erreurs_alerte.append(f"minimum: {erreur}")
            elif envoyee:
                types_envoyes.append("minimum")

        # --- type 'aubaine' / 'erreur_prix' : outlier bas via z-score (AUDIT.md 2.3, cablage Session B)
        if classification in ("bonne_affaire", "candidat_erreur_prix"):
            type_alerte: TypeAlerte = "aubaine"
            if classification == "candidat_erreur_prix" and detection_cfg.get(
                "corroboration_activee", False
            ):
                signaux, budget_corroboration = sonder_corroboration(
                    fournisseur, route, offre.devise, budget_corroboration
                )
                if budget_corroboration <= 0 and not budget_corroboration_epuise:
                    logger.warning(
                        "corroboration : plafond de re-requêtes atteint pour ce run (%d) "
                        "- corroboration dégradée pour le reste du run",
                        detection_cfg.get("corroboration_max_requetes_par_run", 15),
                    )
                budget_corroboration_epuise = (
                    budget_corroboration_epuise or budget_corroboration <= 0
                )
                verdict = corroborer_erreur_prix(
                    offre.prix_cents, echantillon.prix_cents, signaux
                ).verdict
                if verdict == "erreur_prix":
                    type_alerte = "erreur_prix"
            label = "possible erreur de prix" if type_alerte == "erreur_prix" else "bonne affaire"
            mediane_echantillon_cents = statistics.median(echantillon.prix_cents)
            baisse_pct = 1 - offre.prix_cents / mediane_echantillon_cents
            raison_zscore = (
                f"{label} : {baisse_pct:.0%} sous la médiane comparable "
                f"({mediane_echantillon_cents / 100:.0f} {offre.devise}, "
                f"{echantillon.n} obs., niveau {echantillon.niveau})"
            )
            envoyee, erreur = _tenter_alerte(
                route=route,
                vol=vol,
                stats=stats,
                route_id=route_id,
                type_alerte=type_alerte,
                raisons=[raison_zscore],
                prix_cents=offre.prix_cents,
                maintenant=maintenant,
                env=env,
            )
            if erreur:
                erreurs_alerte.append(f"{type_alerte}: {erreur}")
            elif envoyee:
                types_envoyes.append(type_alerte)

        if erreurs_alerte:
            resultats.append((nom_route, f"ERREUR TELEGRAM : {'; '.join(erreurs_alerte)}"))
        elif types_envoyes:
            resultats.append(
                (
                    nom_route,
                    f"alerte envoyée ({', '.join(types_envoyes)}) : {prix_dollars} {offre.devise}",
                )
            )
        else:
            mediane = f"{stats['mediane']:.0f}" if stats else "?"
            resultats.append((nom_route, f"ok : {prix_dollars} {offre.devise}, médiane {mediane}"))

    resume_duffel = fournisseur.resume()
    ecrire_resume_github(resultats)
    try:
        envoyer_digest(
            resultats,
            [resume_duffel],
            env,
            budget_corroboration_epuise=budget_corroboration_epuise,
        )
    except Exception as e:
        logger.error("erreur lors de l'envoi du digest technique : %s", e)
    total = len(resultats)
    echecs = sum(1 for _, r in resultats if r.startswith("ERREUR"))
    return 1 if total and echecs == total else 0


if __name__ == "__main__":
    sys.exit(main())
