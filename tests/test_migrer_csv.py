import csv

import migrer_csv
import storage

CSV_LIGNES = [
    {
        "horodatage_utc": "2026-01-01 12:00",  # ancien format (avant Phase 1.6) : espace, sans secondes
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-01",
        "date_retour": "2026-06-13",
        "prix": "410.19",
        "devise": "USD",
        "compagnie": "British Airways",
        "escales": "0",
    },
    {
        "horodatage_utc": "2026-01-02T08:30:00+00:00",  # nouveau format (Phase 1.6)
        "origine": "YUL",
        "destination": "CDG",
        "date_depart": "2026-06-02",
        "date_retour": "2026-06-14",
        "prix": "405.5",
        "devise": "USD",
        "compagnie": "British Airways",
        "escales": "0",
    },
    {
        "horodatage_utc": "2026-01-03T09:00:00+00:00",
        "origine": "YUL",
        "destination": "LHR",
        "date_depart": "2026-05-01",
        "date_retour": "",  # aller simple
        "prix": "377.17",
        "devise": "CAD",
        "compagnie": "Air Canada",
        "escales": "1",
    },
]


def _ecrire_csv_source(chemin) -> None:
    with open(chemin, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_LIGNES[0].keys()))
        writer.writeheader()
        writer.writerows(CSV_LIGNES)


def _preparer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "HISTORY_FILE", tmp_path / "history.csv")
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")
    _ecrire_csv_source(storage.HISTORY_FILE)


def test_migrer_fichier_source_absent_echoue(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "HISTORY_FILE", tmp_path / "absent.csv")
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "scanner.db")

    assert migrer_csv.migrer() == 1


def test_migrer_meme_nombre_de_lignes_et_sommes_de_controle(tmp_path, monkeypatch) -> None:
    _preparer(tmp_path, monkeypatch)

    assert migrer_csv.migrer() == 0

    conn = storage.obtenir_connexion()
    try:
        (nb_observations,) = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        assert nb_observations == len(CSV_LIGNES)

        for devise in {ligne["devise"] for ligne in CSV_LIGNES}:
            attendu = sum(
                round(float(ligne["prix"]) * 100)
                for ligne in CSV_LIGNES
                if ligne["devise"] == devise
            )
            (somme,) = conn.execute(
                "SELECT SUM(prix_cents) FROM observations WHERE devise = ?", (devise,)
            ).fetchone()
            assert somme == attendu, f"somme de controle {devise} : {somme} != {attendu}"
    finally:
        conn.close()


def test_migrer_calcule_horizon_jours_pour_chaque_ligne(tmp_path, monkeypatch) -> None:
    _preparer(tmp_path, monkeypatch)
    migrer_csv.migrer()

    conn = storage.obtenir_connexion()
    try:
        horizons = [
            row["horizon_jours"]
            for row in conn.execute("SELECT horizon_jours FROM observations ORDER BY observe_le")
        ]
    finally:
        conn.close()

    # Ligne 1 : 2026-01-01 -> depart 2026-06-01 = 151 jours (formats "ancien" et "nouveau" mixtes)
    assert horizons == [151, 151, 118]


def test_migrer_tague_environnement_inconnu_et_exclu_du_lire_historique(
    tmp_path, monkeypatch
) -> None:
    """Le CSV source ne trace pas le token Duffel d'origine (audit
    data/scanner.db, Journal) : les lignes migrees sont taguees 'inconnu',
    jamais 'production' par reflexe, et restent donc hors du moteur de
    detection (lire_historique ne renvoie que 'production')."""
    _preparer(tmp_path, monkeypatch)
    migrer_csv.migrer()

    conn = storage.obtenir_connexion()
    try:
        environnements = {
            row["environnement"] for row in conn.execute("SELECT environnement FROM observations")
        }
    finally:
        conn.close()
    assert environnements == {"inconnu"}
    assert storage.lire_historique() == []


def test_migrer_refuse_si_deja_des_observations(tmp_path, monkeypatch) -> None:
    _preparer(tmp_path, monkeypatch)
    assert migrer_csv.migrer() == 0

    resultat = migrer_csv.migrer()

    assert resultat == 1
    conn = storage.obtenir_connexion()
    try:
        (nb_observations,) = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
    finally:
        conn.close()
    assert nb_observations == len(CSV_LIGNES)  # pas de doublon
