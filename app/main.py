import hmac
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_engine
from app.models import Base, Clarification, Execution, IntakeSession, IntakeStatus, Project, Task, TaskStatus
from app.schemas import ApprovalRequest, ClarificationAnswer, IntakeCreate, IntakeView, ProposedRecord
from app.services import approve_intake, audit, notify_mobile_approval
from app.triage import triage
from app.queue import enqueue_execution
from app.state import transition_task

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
        "execution_rows": session.query(Execution).order_by(Execution.id.desc()).limit(20).all(),
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
    if result.clarification_required:
        intake.status = IntakeStatus.CLARIFYING
        for question in result.clarification_questions:
            session.add(Clarification(intake_id=intake.id, question=question))
    else:
        intake.status = IntakeStatus.AWAITING_APPROVAL
    audit(session, actor="qwen", action="TRIAGE_COMPLETED", entity_type="intake", entity_id=str(intake.id), new_value=intake.triage)
    session.commit()
    return intake.triage


@app.get("/api/intakes/{intake_id}/clarifications")
def list_clarifications(intake_id: int, session: Session = Depends(db_session)) -> list[dict]:
    return [{"id": item.id, "question": item.question, "answer": item.answer, "blocking": item.blocking} for item in session.query(Clarification).filter_by(intake_id=intake_id).order_by(Clarification.id)]


@app.post("/api/clarifications/{clarification_id}/answer")
def answer_clarification(clarification_id: int, payload: ClarificationAnswer, session: Session = Depends(db_session)) -> dict[str, str]:
    item = session.get(Clarification, clarification_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Clarification not found")
    item.answer = payload.answer
    item.answered_at = datetime.now(timezone.utc)
    remaining = session.query(Clarification).filter_by(intake_id=item.intake_id, answer=None).count()
    intake = session.get(IntakeSession, item.intake_id)
    if remaining == 0 and intake is not None:
        intake.status = IntakeStatus.AWAITING_APPROVAL
    audit(session, actor="local-admin", action="CLARIFICATION_ANSWERED", entity_type="clarification", entity_id=str(item.id))
    session.commit()
    return {"status": "awaiting_approval" if remaining == 0 else "clarifying"}


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


@app.post("/api/intakes/{intake_id}/proposal")
def save_proposal(intake_id: int, payload: ProposedRecord, session: Session = Depends(db_session)) -> dict:
    intake = session.get(IntakeSession, intake_id)
    if intake is None or intake.status is not IntakeStatus.AWAITING_APPROVAL:
        raise HTTPException(status_code=409, detail="Intake is not ready for a proposal")
    intake.proposed_record = payload.model_dump()
    sent = notify_mobile_approval(intake)
    audit(session, actor=payload.actor, action="MOBILE_APPROVAL_NOTIFICATION_SENT" if sent else "MOBILE_APPROVAL_NOTIFICATION_SKIPPED", entity_type="intake", entity_id=str(intake.id))
    session.commit()
    return intake.proposed_record


@app.get("/intakes/{intake_id}/proposal")
def proposed_record_form(intake_id: int, request: Request, session: Session = Depends(db_session)):
    intake = session.get(IntakeSession, intake_id)
    if intake is None:
        raise HTTPException(status_code=404, detail="Intake not found")
    return templates.TemplateResponse(request, "proposal.html", {"intake": intake, "record": intake.proposed_record or {}})


@app.post("/intakes/{intake_id}/proposal")
def save_proposed_record_form(intake_id: int, project_key: str = Form(), project_name: str = Form(), task_title: str = Form(), task_description: str = Form(), test_command: str = Form(default=""), session: Session = Depends(db_session)):
    return save_proposal(intake_id, ProposedRecord(project_key=project_key, project_name=project_name, task_title=task_title, task_description=task_description, test_command=test_command or None), session)


@app.post("/api/tasks/{task_id}/queue")
def queue_task(task_id: int, session: Session = Depends(db_session)) -> dict:
    task = session.get(Task, task_id)
    if task is None or task.status is not TaskStatus.COMMITTED:
        raise HTTPException(status_code=409, detail="Task is not approved for execution")
    execution = Execution(task_id=task.id, executor="codex", model="gpt-5.6-terra", status="QUEUED")
    session.add(execution)
    transition_task(task, TaskStatus.QUEUED)
    session.flush()
    try:
        job_id = enqueue_execution(execution.id)
        execution.result = {"rq_job_id": job_id}
        session.commit()
    except Exception as error:
        session.rollback()
        raise HTTPException(status_code=503, detail="Execution queue unavailable") from error
    return {"execution_id": execution.id, "status": execution.status, "rq_job_id": job_id}


@app.get("/api/executions")
def executions(session: Session = Depends(db_session)) -> list[dict]:
    return [_execution_view(e) for e in session.query(Execution).order_by(Execution.id.desc())]


def _execution_view(e: Execution) -> dict:
    return {"id": e.id, "task_id": e.task_id, "status": e.status, "executor": e.executor, "model": e.model, "worktree": e.worktree, "output_path": e.output_path, "started_at": e.started_at, "completed_at": e.completed_at, "result": e.result}


@app.get("/api/executions/{execution_id}")
def execution_detail(execution_id: int, session: Session = Depends(db_session)) -> dict:
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return _execution_view(execution)


@app.get("/api/executions/{execution_id}/log", response_class=PlainTextResponse)
def execution_log(execution_id: int, session: Session = Depends(db_session)) -> str:
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.output_path:
        return "Execution has not started."
    path = Path(execution.output_path).resolve()
    artifacts = Path(get_settings().artifacts_path).resolve()
    if artifacts not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Execution log unavailable")
    return path.read_text(errors="replace")[-200_000:]


@app.post("/api/mobile-actions/{action}")
def mobile_action(action: str, x_ops_bridge_secret: str = Header(default=""), session: Session = Depends(db_session)) -> dict[str, str]:
    secret = get_settings().mobile_bridge_secret
    if not secret or not hmac.compare_digest(secret, x_ops_bridge_secret):
        raise HTTPException(status_code=403, detail="Invalid bridge credential")
    match = re.fullmatch(r"OPS_REJECT_INTAKE_(\d+)", action)
    if match:
        intake = session.get(IntakeSession, int(match.group(1)))
        if intake is None or intake.status not in {IntakeStatus.AWAITING_APPROVAL, IntakeStatus.CLARIFYING}:
            raise HTTPException(status_code=409, detail="Intake is not actionable")
        intake.status = IntakeStatus.REJECTED
        audit(session, actor="homeassistant-mobile", action="INTAKE_REJECTED", entity_type="intake", entity_id=str(intake.id))
        session.commit()
        return {"status": "rejected"}
    match = re.fullmatch(r"OPS_APPROVE_INTAKE_(\d+)", action)
    if match:
        intake = session.get(IntakeSession, int(match.group(1)))
        if intake is None or not intake.proposed_record:
            raise HTTPException(status_code=409, detail="Intake has no complete proposed record")
        project, task = approve_intake(session, intake, ApprovalRequest(**intake.proposed_record), "homeassistant-mobile")
        session.commit()
        return {"status": "approved", "task_key": task.task_key, "project_key": project.project_key}
    raise HTTPException(status_code=409, detail="Action requires a proposed record and cannot yet be approved by mobile")
