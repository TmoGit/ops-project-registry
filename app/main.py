from fastapi import FastAPI
from sqlalchemy import text

from app.db import engine

app = FastAPI(title="Ops Orchestrator", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ops-orchestrator"}


@app.get("/ready")
def ready() -> dict[str, str]:
    with engine().connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}
