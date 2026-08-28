from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_engine
from app.models import Base, IntakeSession, IntakeStatus
from app.schemas import ApprovalRequest, IntakeCreate, IntakeView
from app.services import approve_intake, audit

app = FastAPI(title="Ops Orchestrator", version="0.1.0")


def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ops-orchestrator"}


@app.get("/ready")
def ready() -> dict[str, str]:
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.post("/api/projects/intake", response_model=IntakeView, status_code=201)
def create_intake(payload: IntakeCreate, session: Session = Depends(db_session)) -> IntakeView:
    intake = IntakeSession(raw_request=payload.raw_request, status=IntakeStatus.AWAITING_APPROVAL)
    session.add(intake)
    session.flush()
    audit(session, actor="local-admin", action="INTAKE_CREATED", entity_type="intake", entity_id=str(intake.id))
    session.commit()
    return IntakeView(id=intake.id, status=intake.status.value, raw_request=intake.raw_request)


@app.post("/api/intakes/{intake_id}/approve")
def approve(intake_id: int, payload: ApprovalRequest, session: Session = Depends(db_session)) -> dict[str, str]:
    intake = session.get(IntakeSession, intake_id)
    if intake is None:
        raise HTTPException(status_code=404, detail="Intake not found")
    try:
        project, task = approve_intake(session, intake, payload, payload.actor)
        session.commit()
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"project_key": project.project_key, "task_key": task.task_key, "status": task.status.value}
