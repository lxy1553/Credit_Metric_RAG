from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from credit.api import create_app
from credit.demo import bootstrap
from credit.memory import MemoryStore
from credit.security import DEMO_USERS


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "credit.db"
    bootstrap(path, tmp_path / "sources", include_history=False)
    return path


@pytest.fixture
def client(db: Path) -> TestClient:
    with TestClient(create_app(db, DEMO_USERS, MemoryStore())) as test_client:
        yield test_client


def auth(token="demo-analyst"):
    return {"Authorization": f"Bearer {token}"}
