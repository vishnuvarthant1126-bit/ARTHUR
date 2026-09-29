"""Smoke test: the REAL app (real lifespan, real wiring) starts and answers.

Unit tests inject fakes and skip startup, so a bug in main.py's lifespan
(it happened once: create_task(gather(...))) would slip through. Here the
database, vector store and documents go to a temporary folder, so the user's
own data is never touched.
"""

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arthur.db"))
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("DOCUMENTS_PATH", str(tmp_path / "documents"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_real_app_starts_and_serves(isolated_settings):
    from app.main import create_app

    with TestClient(create_app()) as client:  # "with" runs the real startup and shutdown
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
        names = {t["name"] for t in client.get("/tools").json()}
        assert {"calculator", "web_search", "document_search"} <= names
        assert client.get("/voice/voices").status_code == 200
        assert client.get("/documents").json()["count"] == 0  # the temporary, empty store
