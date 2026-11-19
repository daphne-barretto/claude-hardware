"""The app starts cleanly from an empty or missing data directory (Render free plan)."""

from fastapi.testclient import TestClient

from app.main import app


def test_starts_with_missing_data_dir(tmp_path, monkeypatch) -> None:
    data = tmp_path / "wiped" / "data"
    monkeypatch.setenv("DATA_DIR", str(data))

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    assert data.is_dir()


def test_starts_with_empty_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
