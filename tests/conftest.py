import pytest

from begroting import service


@pytest.fixture(autouse=True)
def tijdelijke_database(tmp_path, monkeypatch):
    """Elke test schrijft naar een eigen, tijdelijke SQLite-database."""
    pad = str(tmp_path / "test.db")
    monkeypatch.setattr(service, "DB_PAD", pad)
    service.wis_cache()
    return pad
