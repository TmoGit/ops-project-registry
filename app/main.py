import hmac
import re

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_engine
from app.models import Base, IntakeSession, IntakeStatus, Project, Task, TaskStatus
from app.schemas import ApprovalRequest, IntakeCreate, IntakeView
from app.services import approve_intake, audit
from app.triage import triage

app = FastAPI(title="Ops Orchestrator", version="0.1.0")
templates = Jinja2Templates(directory="app/templates")


def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ops-orchestrator"}


@app.get("/")
def dashboard(request: Request, session: Session = Depends(db_session)):
    return templates.TemplateResponse(request, "dashboard.html", {
        "projects": session.query(Project).count(),
        "pending": session.query(IntakeSession).filter_by(status=IntakeStatus.AWAITING_APPROVAL).count(),
        "blocked": session.query(Task).filter_by(status=TaskStatus.BLOCKED).count(),
        "project_rows": session.query(Project).order_by(Project.project_key).all(),
        "intake_rows": session.query(IntakeSession).filter_by(status=IntakeStatus.AWAITING_APPROVAL).order_by(IntakeSession.id.desc()).all(),
    })


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


@app.post("/api/intakes/{intake_id}/triage")
def run_triage(intake_id: int, session: Session = Depends(db_session)) -> dict:
    intake = session.get(IntakeSession, intake_id)
    if intake is None:
        raise HTTPException(status_code=404, detail="Intake not found")
    try:
        result = triage(intake.raw_request)
    except RuntimeError as error:
        intake.status = IntakeStatus.CLARIFYING
        audit(session, actor="qwen", action="TRIAGE_MANUAL_REVIEW", entity_type="intake", entity_id=str(intake.id))
        session.commit()
        raise HTTPException(status_code=422, detail=str(error)) from error
    intake.triage = result.model_dump()
    intake.status = IntakeStatus.CLARIFYING if result.clarification_required else IntakeStatus.AWAITING_APPROVAL
    audit(session, actor="qwen", action="TRIAGE_COMPLETED", entity_type="intake", entity_id=str(intake.id), new_value=intake.triage)
    session.commit()
    return intake.triage


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


@app.post("/api/mobile-actions/{action}")
def mobile_action(action: str, x_ops_bridge_secret: str = Header(default=""), session: Session = Depends(db_session)) -> dict[str, str]:
    secret = get_settings().mobile_bridge_secret
    if not secret or not hmac.compare_digest(secret, x_ops_bridge_secret):
        raise HTTPException(status_code=403, detail="Invalid bridge credential")
    match = re.fullmatch(r"OPS_REJECT_INTAKE_(\\d+)", action)
    if match:
        intake = session.get(IntakeSession, int(match.group(1)))
        if intake is None or intake.status not in {IntakeStatus.AWAITING_APPROVAL, IntakeStatus.CLARIFYING}:
            raise HTTPException(status_code=409, detail="Intake is not actionable")
        intake.status = IntakeStatus.REJECTED
        audit(session, actor="homeassistant-mobile", action="INTAKE_REJECTED", entity_type="intake", entity_id=str(intake.id))
        session.commit()
        return {"status": "rejected"}
    raise HTTPException(status_code=409, detail="Action requires a proposed record and cannot yet be approved by mobile")
