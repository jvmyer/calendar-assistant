import pytest

from planner import pool


@pytest.fixture(autouse=True)
def _no_pool_pauses(monkeypatch):
    """Die Pausen zwischen Google-Schreibzugriffen (pool.PACE/RETRIES) in Tests überspringen."""
    monkeypatch.setattr(pool, "_sleep", lambda _s: None)
