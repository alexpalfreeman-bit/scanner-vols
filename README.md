# ✈️ Scanner de prix de vols mondial (au départ de YUL)

Surveille automatiquement le prix des vols vers une liste mondiale de destinations et t'envoie une alerte Telegram quand un prix est une bonne affaire, une possible erreur de prix, ou un nouveau minimum historique. Pas de date fixe : c'est le prix qui te dit où et quand partir. Coût de fonctionnement : quelques dollars par mois selon le nombre de destinations et la fréquence (voir plus bas), gratuit au début (1 000 recherches gratuites via Duffel).

## Comment ça marche

Depuis une seule origine (`YUL` par défaut) et pour chaque destination de la liste mondiale de `config.yaml`, `scanner.py` teste **une date de départ candidate par run**, choisie en rotation dans une fenêtre de ~3 à ~6 mois à l'avance (voir `sejour.candidats_semaines`) pour un séjour de `sejour.duree_nuits` nuits. Chaque destination cycle automatiquement à travers toutes les dates candidates au fil des jours — élargir la fenêtre de dates ne coûte rien de plus, ça prend juste plus de temps à couvrir.

Le prix trouvé est enregistré dans `data/history.csv`, puis comparé à l'historique de **cette destination** (toutes dates confondues) : médiane, minimum, et tendance récente vs ancienne. Une alerte Telegram part si le prix est un nouveau minimum, s'il est nettement sous la médiane (bonne affaire, ou possible erreur de prix si l'écart est très grand), ou s'il passe sous un seuil fixe optionnel (`prix_max`) que tu peux définir par destination. GitHub Actions relance le script une fois par jour, gratuitement, sans serveur.

Comme il n'y a pas d'historique au départ, la détection statistique démarre "à froid" (elle ne se déclenche qu'après `detection.echantillon_min` observations pour une destination) mais s'améliore ensuite automatiquement à mesure que `data/history.csv` grossit.

---

## Étape 1 — Compte Duffel (5 min)

1. Va sur **app.duffel.com/join** → crée un compte (~1 min, accès sandbox immédiat).
2. Dans le tableau de bord : clique le nom de ton organisation → **Developers** → **Access tokens** → **New token**.
3. Laisse la portée sur **Read write**, nomme-le `scanner-vols`, crée-le.
4. Copie le token. Un token de **test** commence par `duffel_test_` → c'est ta valeur `DUFFEL_ACCESS_TOKEN`.

⚠️ **Test vs live** : le token `duffel_test_` interroge un bac à sable — la mécanique fonctionne mais **les prix ne sont pas réels** (souvent seule « Duffel Airways » répond). Parfait pour valider le montage. Pour de vrais prix, il faut activer le compte et créer un token `duffel_live_` (voir plus bas).

## Étape 2 — Bot Telegram (5 min)

1. Dans Telegram, cherche **@BotFather** → `/newbot` → suis les instructions.
2. Note le **token** fourni = `TELEGRAM_BOT_TOKEN`.
3. **Ouvre la conversation avec ton bot et envoie-lui un message** (obligatoire, sinon il ne peut pas t'écrire).
4. Cherche **@userinfobot**, envoie `/start` : il te donne ton **Id** = `TELEGRAM_CHAT_ID`.

## Étape 3 — Test en local (15 min)

Prérequis : Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env      # Windows : copy .env.example .env
```

Remplis `.env` avec ton token Duffel et tes valeurs Telegram, ajuste `config.yaml`, puis :

```bash
python scanner.py
```

En mode test, ne te fie pas aux prix — vérifie seulement que ça tourne et que l'alerte Telegram arrive.

## Étape 4 — Automatisation gratuite (15 min)

1. Crée un dépôt sur GitHub (privé ou public — voir la note ci-dessous) et pousses-y ce dossier.
2. *Settings* → *Secrets and variables* → *Actions* → crée ces 3 secrets :
   - `DUFFEL_ACCESS_TOKEN`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Onglet *Actions* → *Run workflow* pour tester, puis le scan tourne tout seul une fois par jour.

GitHub désactive les workflows planifiés après ~60 jours d'inactivité humaine sur le dépôt — un commit occasionnel les garde en vie.

**Dépôt public :** ce projet est fait pour être partagé — chacun clone le dépôt et fait tourner sa propre instance avec ses propres secrets (jamais les tiens : `.env` est exclu par `.gitignore` et les secrets GitHub Actions ne sont jamais exposés dans les logs). Par contre `data/history.csv` sera visible publiquement dans le dépôt une fois committé — pas de donnée sensible dedans, mais ça révèle les destinations et le rythme de recherche.

---

## Coûts Duffel

Le plan self-service donne **1 000 recherches gratuites à vie**, puis environ **2 $ US par tranche de 1 000 recherches** réussies. Avec la rotation des dates, **1 recherche = 1 destination par run** (pas destination × dates). Le coût dépend donc du nombre de destinations dans `config.yaml` et de la fréquence dans `scan.yml` :

| Destinations | Fréquence | Recherches/jour | Autonomie gratuite | Coût/mois après |
|---|---|---|---|---|
| **35 (par défaut)** | **1×/jour** | **35** | **~29 jours** | **~2,10 $** |
| 35 | 1×/2 jours | 17,5 | ~57 jours | ~1,05 $ |
| 35 | 4×/jour | 140 | ~7 jours | ~8,40 $ |

Pour réduire le coût : retire des destinations de `config.yaml`, ou espace le cron dans `scan.yml` (ex. `"17 6 */2 * *"` pour un jour sur deux). Élargir `sejour.candidats_semaines` (la fenêtre de dates) ne coûte rien de plus, ça allonge juste le cycle de rotation.

## Passer aux vrais prix (mode live)

1. Dans le tableau de bord Duffel, complète l'activation du compte (infos d'organisation).
2. Crée un token **live** (`duffel_live_...`).
3. Remplace `DUFFEL_ACCESS_TOKEN` dans `.env` **et** dans les secrets GitHub.
4. Relance : les prix sont désormais réels.

## Prochaines étapes

- **v1.1** : résumé quotidien même sans alerte
- **v1.2** : graphique d'évolution des prix depuis `history.csv`
- **v2** : interface web (React) + comptes utilisateurs → première version vendable
- **v3** : Stripe + plan gratuit/payant → produit freemium
