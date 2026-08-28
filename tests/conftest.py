import os
from pathlib import Path

os.environ.setdefault("OPS_DATABASE_URL", "sqlite+pysqlite:////tmp/ops-orchestrator-tests.db")
os.environ.setdefault("OPS_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("OPS_SESSION_SECRET", "test-session-secret-that-is-long")
os.environ.setdefault("OPS_ENVIRONMENT", "development")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import app, db_session


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ops.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr("app.main.SessionLocal", factory)
    monkeypatch.setattr("app.worker.SessionLocal", factory)
    with factory() as value:
        yield value


@pytest.fixture
def client(session):
    def override():
        try:
            yield session
        finally:
            pass
    app.dependency_overrides[db_session] = override
    from fastapi.testclient import TestClient
    with TestClient(app) as value:
        yield value
    app.dependency_overrides.clear()
