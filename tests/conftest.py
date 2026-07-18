"""
Filets de securite globaux pour la suite de tests (CLAUDE.md : jamais de
vraie requete reseau, jamais d'ecriture dans data/scanner.db depuis un test).
Purement additif : un test qui mocke deja tout correctement ne voit jamais
ces fixtures se declencher.
"""

import pytest

import storage


@pytest.fixture(autouse=True)
def _isoler_scanner_db(tmp_path, monkeypatch):
    """Chaque test recoit par defaut une base isolee dans son propre
    tmp_path : storage.DB_FILE ne peut jamais pointer vers la vraie
    data/scanner.db pendant un test, sauf si le test re-monkeypatche
    explicitement storage.DB_FILE lui-meme (ce qui prime, puisque ca
    s'applique apres, sur le meme objet monkeypatch). Ajoutee suite a un
    incident reel constate en Session B : des tests de tests/test_alerting.py
    qui ne mockaient que envoyer_telegram ecrivaient reellement dans la vraie
    base des qu'enregistrer_alerte a ete cablee dans envoyer_alerte."""
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "_defaut_isole_test.db")


@pytest.fixture(autouse=True)
def _bloquer_reseau_reel(monkeypatch):
    """Bloque tout appel HTTP reel pendant les tests, meme si un test oublie
    de mocker un point d'entree specifique (ex. alerting.envoyer_telegram
    appele via un chemin non mocke) : requests.post/Session.post passent
    tous les deux par Session.request en interne."""

    def _bloque(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "Appel reseau reel intercepte pendant un test (requests.Session.request) "
            "- un mock manque quelque part (CLAUDE.md interdit les vraies requetes "
            "reseau depuis les tests)."
        )

    monkeypatch.setattr("requests.sessions.Session.request", _bloque)
