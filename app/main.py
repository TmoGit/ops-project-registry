import hmac
import json
import os
import re
import secrets
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import SessionLocal, get_engine
from app.models import Attachment, AuditEvent, Clarification, Execution, IntakeSession, IntakeStatus, IntegrationConfiguration, IntegrationProvider, IntegrationStatus, Project, SystemSetting, Task, TaskStatus
from app.integrations import ADAPTERS
from app.ollama import list_models
from app.queue import enqueue_execution, get_queue
from app.routing import route_triage
from rq.job import Job
from app.schemas import ApprovalRequest, ClarificationAnswer, IntakeView, IntegrationUpdate, ProposedRecord
from app.services import approve_intake, audit, notify_mobile_approval
from app.state import transition_task
from app.triage import triage

app = FastAPI(title="Ops Orchestrator", version="0.2.0", docs_url="/docs", redoc_url=None)
templates = Jinja2Templates(directory="app/templates")


@app.middleware("http")
async def require_local_admin(request: Request, call_next):
    path = request.url.path
    public = path in {"/health", "/ready", "/login"} or path.startswith("/api/mobile-actions/")
    if public or request.session.get("admin") is True:
        return await call_next(request)
    if path.startswith("/api/") or path == "/openapi.json":
        return JSONResponse({"detail": "Local admin authentication required"}, status_code=401)
    return RedirectResponse(url="/login", status_code=303)


# Add this after the auth middleware so session decoding is the outer layer.
app.add_middleware(SessionMiddleware, secret_key=get_settings().session_secret, session_cookie="ops_admin_session", same_site="strict", https_only=get_settings().session_https_only, max_age=8 * 60 * 60)


def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/health")
def health() -> dict[str, str]: return {"status": "ok", "service": "ops-orchestrator"}


@app.get("/ready")
def ready() -> dict[str, str]:
    with get_engine().connect() as connection: connection.execute(text("SELECT 1"))
    Redis.from_url(get_settings().redis_url).ping()
    return {"status": "ready"}


@app.get("/login")
def login_page(request: Request):
    if request.session.get("admin"): return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, password: str = Form(default="")):
    if not hmac.compare_digest(password, get_settings().admin_password):
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid password."}, status_code=401)
    request.session.clear(); request.session["admin"] = True; request.session["session_id"] = secrets.token_urlsafe(18)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _warnings(session: Session) -> list[dict[str, str]]:
    result = []
    for task in session.query(Task).filter(Task.status.in_([TaskStatus.BLOCKED, TaskStatus.FAILED])).order_by(Task.updated_at.desc()).limit(20):
        result.append({"kind": "task", "level": "warning", "message": f"{task.task_key} is {task.status.value.lower()}: {task.title}"})
    for execution in session.query(Execution).filter_by(status="FAILED").order_by(Execution.id.desc()).limit(20):
        result.append({"kind": "execution", "level": "warning", "message": f"Execution {execution.id} failed for task {execution.task_id}."})
    unanswered = session.query(Clarification).filter(Clarification.answer.is_(None)).count()
    if unanswered: result.append({"kind": "clarification", "level": "warning", "message": f"{unanswered} clarification(s) still need an answer."})
    try: Redis.from_url(get_settings().redis_url, socket_connect_timeout=1).ping()
    except Exception: result.append({"kind": "redis", "level": "error", "message": "Redis is unavailable; execution queueing is disabled."})
    models = list_models()
    if not models["available"]: result.append({"kind": "ollama", "level": "error", "message": models["error"]})
    return result


def _default_model(session: Session) -> str:
    stored = session.get(SystemSetting, "default_local_model")
    return stored.value if stored else get_settings().default_local_model


_INTEGRATIONS = {"OPENAI": "OpenAI / Codex", "GITHUB": "GitHub", "GITEA": "Gitea", "AWS": "AWS"}
_SECRET_KEYS = {"token", "secret", "password", "api_key", "access_key", "private_key", "credential"}


def _integration_record(session: Session, provider: str, create: bool = False) -> IntegrationConfiguration | None:
    item = session.query(IntegrationConfiguration).filter_by(provider=IntegrationProvider(provider)).one_or_none()
    if item is None and create:
        item = IntegrationConfiguration(provider=IntegrationProvider(provider), display_name=_INTEGRATIONS[provider], enabled=True, status=IntegrationStatus.NOT_CONFIGURED, configuration_json={})
        session.add(item); session.flush()
    return item


def _integration_view(item: IntegrationConfiguration | None, provider: str) -> dict:
    if item is None:
        return {"provider": provider, "display_name": _INTEGRATIONS[provider], "enabled": False, "status": "NOT_CONFIGURED", "configuration": {}, "credential_source": None, "credential_reference": None, "last_tested_at": None, "last_test_status": None, "last_test_message": None}
    return {"provider": provider, "display_name": item.display_name, "enabled": item.enabled, "status": item.status.value, "configuration": item.configuration_json or {}, "credential_source": item.credential_source, "credential_reference": item.credential_reference, "last_tested_at": item.last_tested_at, "last_test_status": item.last_test_status, "last_test_message": item.last_test_message}


def _safe_integration_config(config: dict) -> dict:
    if not isinstance(config, dict): raise HTTPException(status_code=422, detail="configuration_json must be an object")
    def contains_secret(value) -> bool:
        if isinstance(value, dict): return any(any(word in str(key).lower() for word in _SECRET_KEYS) or contains_secret(child) for key, child in value.items())
        if isinstance(value, list): return any(contains_secret(child) for child in value)
        return False
    if contains_secret(config): raise HTTPException(status_code=422, detail="Secrets must be configured outside the application; use a credential reference.")
    return config


def _validate_integration_config(provider: str, config: dict) -> None:
    if provider in {"GITHUB", "GITEA"} and config:
        base_url = config.get("base_url")
        if base_url is not None and not isinstance(base_url, str):
            raise HTTPException(status_code=422, detail="base_url must be a string")
        if base_url and not base_url.startswith(("https://", "http://")):
            raise HTTPException(status_code=422, detail="base_url must start with https:// or http://")
    if provider == "AWS" and config.get("region") is not None:
        region = config["region"]
        if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region):
            raise HTTPException(status_code=422, detail="AWS region is not valid")


def _validate_credential_reference(source: str | None, reference: str | None) -> None:
    if reference and not source:
        raise HTTPException(status_code=422, detail="A credential source is required when a reference is supplied")
    if source == "ENVIRONMENT" and (not reference or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference)):
        raise HTTPException(status_code=422, detail="Environment credential reference must be an environment-variable name")
    if source == "FILE" and (not reference or not reference.startswith("/")):
        raise HTTPException(status_code=422, detail="File credential reference must be an absolute path")
    if source == "AWS_PROFILE" and (not reference or not re.fullmatch(r"[A-Za-z0-9_.-]+", reference)):
        raise HTTPException(status_code=422, detail="AWS profile reference is not valid")
    if source == "CLI_SESSION" and reference:
        raise HTTPException(status_code=422, detail="CLI session credentials do not use a reference")


def _configuration_status(item: IntegrationConfiguration) -> IntegrationStatus:
    if not item.enabled: return IntegrationStatus.DISABLED
    if not item.configuration_json: return IntegrationStatus.NOT_CONFIGURED
    if item.provider is not IntegrationProvider.OPENAI and not item.credential_source: return IntegrationStatus.NOT_CONFIGURED
    return IntegrationStatus.CONFIGURED


@app.get("/")
def dashboard(request: Request, session: Session = Depends(db_session)):
    return templates.TemplateResponse(request, "dashboard.html", {
        "projects": session.query(Project).count(), "pending": session.query(IntakeSession).filter_by(status=IntakeStatus.AWAITING_APPROVAL).count(),
        "blocked": session.query(Task).filter(Task.status.in_([TaskStatus.BLOCKED, TaskStatus.FAILED])).count(),
        "project_rows": session.query(Project).order_by(Project.project_key).all(),
        "intake_rows": session.query(IntakeSession).filter(IntakeSession.status.in_([IntakeStatus.AWAITING_APPROVAL, IntakeStatus.CLARIFYING])).order_by(IntakeSession.id.desc()).all(),
        "task_rows": session.query(Task).order_by(Task.updated_at.desc()).limit(30).all(), "execution_rows": session.query(Execution).order_by(Execution.id.desc()).limit(20).all(),
        "activity_rows": session.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(20).all(), "warnings": _warnings(session),
        "model_state": list_models(), "default_model": _default_model(session), "usage_count": session.query(Execution).count(),
        "integrations": [_integration_view(_integration_record(session, provider), provider) for provider in _INTEGRATIONS],
    })


@app.get("/integrations")
def integrations_page(request: Request, session: Session = Depends(db_session)):
    return templates.TemplateResponse(request, "integrations.html", {"integrations": [_integration_view(_integration_record(session, provider), provider) for provider in _INTEGRATIONS]})


@app.get("/api/integrations")
def list_integrations(session: Session = Depends(db_session)) -> list[dict]:
    return [_integration_view(_integration_record(session, provider), provider) for provider in _INTEGRATIONS]


@app.get("/api/integrations/{provider}")
def get_integration(provider: str, session: Session = Depends(db_session)) -> dict:
    provider = provider.upper()
    if provider not in _INTEGRATIONS: raise HTTPException(status_code=404, detail="Unknown integration provider")
    return _integration_view(_integration_record(session, provider), provider)


@app.put("/api/integrations/{provider}")
def update_integration(provider: str, payload: IntegrationUpdate, session: Session = Depends(db_session)) -> dict:
    provider = provider.upper()
    if provider not in _INTEGRATIONS: raise HTTPException(status_code=404, detail="Unknown integration provider")
    item = _integration_record(session, provider, create=True)
    config = item.configuration_json or {}
    if "configuration_json" in payload.model_fields_set:
        config = _safe_integration_config(payload.configuration_json or {})
        item.configuration_json = config
    if payload.display_name is not None: item.display_name = payload.display_name
    if payload.enabled is not None: item.enabled = payload.enabled
    if "credential_source" in payload.model_fields_set: item.credential_source = payload.credential_source
    if "credential_reference" in payload.model_fields_set: item.credential_reference = payload.credential_reference
    _validate_integration_config(provider, config)
    _validate_credential_reference(item.credential_source, item.credential_reference)
    item.status = _configuration_status(item); item.updated_by = "local-admin"
    audit(session, actor="local-admin", action="INTEGRATION_UPDATED", entity_type="integration", entity_id=provider, new_value={"configuration": config, "credential_source": item.credential_source, "credential_reference": item.credential_reference, "enabled": item.enabled})
    session.commit(); return _integration_view(item, provider)


@app.post("/api/integrations/{provider}/test")
def test_integration(provider: str, session: Session = Depends(db_session)) -> dict:
    provider = provider.upper()
    if provider not in _INTEGRATIONS: raise HTTPException(status_code=404, detail="Unknown integration provider")
    item = _integration_record(session, provider)
    if item is None or not item.enabled: raise HTTPException(status_code=409, detail="Integration is not configured or enabled")
    check = ADAPTERS[provider](item).test_connection(); item.last_tested_at = datetime.now(timezone.utc); item.last_test_status = check.status; item.last_test_message = check.message; item.status = IntegrationStatus(check.status)
    audit(session, actor="local-admin", action="INTEGRATION_TEST_SUCCEEDED" if check.status == "CONNECTED" else "INTEGRATION_TEST_FAILED", entity_type="integration", entity_id=provider, new_value={"status": check.status, "message": check.message})
    session.commit(); return _integration_view(item, provider)


@app.post("/api/integrations/{provider}/{action}")
def set_integration_enabled(provider: str, action: str, session: Session = Depends(db_session)) -> dict:
    provider, action = provider.upper(), action.lower()
    if provider not in _INTEGRATIONS or action not in {"enable", "disable"}: raise HTTPException(status_code=404, detail="Unknown integration action")
    item = _integration_record(session, provider, create=True); item.enabled = action == "enable"; item.status = _configuration_status(item)
    audit(session, actor="local-admin", action="INTEGRATION_ENABLED" if item.enabled else "INTEGRATION_DISABLED", entity_type="integration", entity_id=provider, new_value={"enabled": item.enabled})
    session.commit(); return _integration_view(item, provider)


_ALLOWED_ATTACHMENT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json", "application/pdf", "image/png", "image/jpeg", "image/webp", "text/x-shellscript", "application/x-sh", "text/x-python", "text/x-powershell"}
_ALLOWED_ATTACHMENT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".sh", ".bash", ".zsh", ".py", ".ps1", ".yaml", ".yml"}


def _safe_filename(name: str | None) -> str:
    candidate = Path(name or "attachment").name
    candidate = re.sub(r"[^A-Za-z0-9._-]", "_", candidate).strip("._")
    if not candidate or len(candidate) > 255 or Path(candidate).suffix.lower() not in _ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Attachment filename or extension is not allowed")
    return candidate


async def _store_attachments(intake: IntakeSession, files, session: Session) -> None:
    if len(files) > get_settings().attachment_max_count: raise HTTPException(status_code=422, detail="Too many attachments")
    root = Path(get_settings().attachments_path).resolve(); root.mkdir(mode=0o700, parents=True, exist_ok=True); created = []
    try:
        for upload in files:
            if not getattr(upload, "filename", None): continue
            filename = _safe_filename(upload.filename); content_type = (upload.content_type or "").lower()
            if content_type not in _ALLOWED_ATTACHMENT_TYPES: raise HTTPException(status_code=422, detail="Attachment type is not allowed")
            payload = await upload.read(get_settings().attachment_max_bytes + 1)
            if not payload or len(payload) > get_settings().attachment_max_bytes: raise HTTPException(status_code=422, detail="Attachment is empty or exceeds the size limit")
            stored_name = f"{uuid4().hex}{Path(filename).suffix.lower()}"; destination = (root / stored_name).resolve()
            if destination.parent != root: raise HTTPException(status_code=422, detail="Invalid attachment path")
            with destination.open("xb") as stream: stream.write(payload)
            os.chmod(destination, 0o600); created.append(destination)
            session.add(Attachment(intake=intake, original_name=filename, stored_name=stored_name, content_type=content_type, size_bytes=len(payload)))
    except Exception:
        for path in created: path.unlink(missing_ok=True)
        raise


def _apply_triage(intake: IntakeSession, session: Session) -> dict:
    result = triage(intake.raw_request, model=_default_model(session))
    triage_data = result.model_dump()
    triage_data["routing"] = route_triage(result)
    intake.triage = triage_data
    intake.status = IntakeStatus.CLARIFYING if result.clarification_required else IntakeStatus.AWAITING_APPROVAL
    if result.clarification_required:
        for question in result.clarification_questions:
            session.add(Clarification(intake_id=intake.id, question=question))
    audit(session, actor="local-model", action="TRIAGE_COMPLETED", entity_type="intake", entity_id=str(intake.id), new_value=triage_data)
    return triage_data


@app.post("/api/projects/intake", response_model=IntakeView, status_code=201)
async def create_intake(request: Request, session: Session = Depends(db_session)) -> IntakeView:
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form(); raw_request = str(form.get("raw_request", "")).strip(); files = form.getlist("attachments")
    else:
        payload = await request.json(); raw_request = str(payload.get("raw_request", "")).strip(); files = []
    if not raw_request: raise HTTPException(status_code=422, detail="raw_request is required")
    intake = IntakeSession(raw_request=raw_request, status=IntakeStatus.INTAKE); session.add(intake); session.flush()
    await _store_attachments(intake, files, session); audit(session, actor="local-admin", action="INTAKE_CREATED", entity_type="intake", entity_id=str(intake.id))
    try:
        _apply_triage(intake, session)
    except RuntimeError as error:
        intake.status = IntakeStatus.CLARIFYING
        intake.triage = {"routing": {"executor": "codex", "mode": "MANUAL_REVIEW_REQUIRED", "reason": "Automatic local triage was unavailable; manual Codex review is required."}, "error": str(error)}
        audit(session, actor="local-model", action="TRIAGE_MANUAL_REVIEW", entity_type="intake", entity_id=str(intake.id), new_value=intake.triage)
    session.commit()
    return IntakeView(id=intake.id, status=intake.status.value, raw_request=intake.raw_request, triage=intake.triage)


@app.get("/intakes/{intake_id}")
def intake_review(intake_id: int, request: Request, session: Session = Depends(db_session)):
    intake = session.get(IntakeSession, intake_id)
    if intake is None: raise HTTPException(status_code=404, detail="Intake not found")
    return templates.TemplateResponse(request, "intake.html", {"intake": intake, "record": intake.proposed_record or {}, "attachments": intake.attachments})


@app.post("/api/intakes/{intake_id}/triage")
def run_triage(intake_id: int, session: Session = Depends(db_session)) -> dict:
    intake = session.get(IntakeSession, intake_id)
    if intake is None: raise HTTPException(status_code=404, detail="Intake not found")
    try:
        result = _apply_triage(intake, session)
        session.commit()
        return result
    except RuntimeError as error:
        intake.status = IntakeStatus.CLARIFYING; audit(session, actor="qwen", action="TRIAGE_MANUAL_REVIEW", entity_type="intake", entity_id=str(intake.id)); session.commit(); raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/intakes/{intake_id}/clarifications")
def list_clarifications(intake_id: int, session: Session = Depends(db_session)) -> list[dict]:
    return [{"id": x.id, "question": x.question, "answer": x.answer, "blocking": x.blocking} for x in session.query(Clarification).filter_by(intake_id=intake_id).order_by(Clarification.id)]


@app.post("/api/clarifications/{clarification_id}/answer")
def answer_clarification(clarification_id: int, payload: ClarificationAnswer, session: Session = Depends(db_session)) -> dict[str, str]:
    item = session.get(Clarification, clarification_id)
    if item is None: raise HTTPException(status_code=404, detail="Clarification not found")
    item.answer = payload.answer; item.answered_at = datetime.now(timezone.utc); remaining = session.query(Clarification).filter_by(intake_id=item.intake_id, answer=None).count(); intake = session.get(IntakeSession, item.intake_id)
    if remaining == 0 and intake is not None: intake.status = IntakeStatus.AWAITING_APPROVAL
    audit(session, actor="local-admin", action="CLARIFICATION_ANSWERED", entity_type="clarification", entity_id=str(item.id)); session.commit(); return {"status": "awaiting_approval" if remaining == 0 else "clarifying"}


@app.post("/api/intakes/{intake_id}/approve")
def approve(intake_id: int, payload: ApprovalRequest, session: Session = Depends(db_session)) -> dict[str, str]:
    intake = session.get(IntakeSession, intake_id)
    if intake is None: raise HTTPException(status_code=404, detail="Intake not found")
    try:
        project, task = approve_intake(session, intake, payload, "local-admin")
        route = (intake.triage or {}).get("routing", {})
        task.assigned_executor = "local" if route.get("executor") == "local" else "codex"
        audit(session, actor="local-admin", action="TASK_EXECUTION_ROUTE_CONFIRMED", entity_type="task", entity_id=task.task_key, new_value={"executor": task.assigned_executor, "mode": route.get("mode")})
        session.commit()
    except ValueError as error: session.rollback(); raise HTTPException(status_code=409, detail=str(error)) from error
    return {"project_key": project.project_key, "task_key": task.task_key, "task_id": str(task.id), "status": task.status.value}


@app.post("/api/intakes/{intake_id}/reject")
def reject_intake(intake_id: int, session: Session = Depends(db_session)) -> dict[str, str]:
    intake = session.get(IntakeSession, intake_id)
    if intake is None or intake.status not in {IntakeStatus.AWAITING_APPROVAL, IntakeStatus.CLARIFYING}: raise HTTPException(status_code=409, detail="Intake is not actionable")
    intake.status = IntakeStatus.REJECTED; audit(session, actor="local-admin", action="INTAKE_REJECTED", entity_type="intake", entity_id=str(intake.id)); session.commit(); return {"status": "rejected"}


@app.post("/api/intakes/{intake_id}/proposal")
def save_proposal(intake_id: int, payload: ProposedRecord, session: Session = Depends(db_session)) -> dict:
    intake = session.get(IntakeSession, intake_id)
    if intake is None or intake.status is not IntakeStatus.AWAITING_APPROVAL: raise HTTPException(status_code=409, detail="Intake is not ready for a proposal")
    intake.proposed_record = payload.model_dump(exclude={"actor"}); sent = notify_mobile_approval(intake); audit(session, actor="local-admin", action="MOBILE_APPROVAL_NOTIFICATION_SENT" if sent else "MOBILE_APPROVAL_NOTIFICATION_SKIPPED", entity_type="intake", entity_id=str(intake.id)); session.commit(); return intake.proposed_record


@app.get("/intakes/{intake_id}/proposal")
def proposed_record_form(intake_id: int): return RedirectResponse(f"/intakes/{intake_id}", status_code=303)


@app.post("/intakes/{intake_id}/proposal")
def save_proposed_record_form(intake_id: int, project_key: str = Form(), project_name: str = Form(), task_title: str = Form(), task_description: str = Form(), test_command: str = Form(default=""), session: Session = Depends(db_session)):
    return save_proposal(intake_id, ProposedRecord(project_key=project_key, project_name=project_name, task_title=task_title, task_description=task_description, test_command=test_command or None), session)


@app.post("/api/tasks/{task_id}/queue")
def queue_task(task_id: int, session: Session = Depends(db_session)) -> dict:
    task = session.get(Task, task_id)
    if task is None or task.status is not TaskStatus.COMMITTED: raise HTTPException(status_code=409, detail="Task is not approved for execution")
    executor = "local" if task.assigned_executor == "local" else "codex"
    model = _default_model(session) if executor == "local" else "gpt-5.6-terra"
    execution = Execution(task_id=task.id, executor=executor, model=model, status="QUEUED"); session.add(execution); transition_task(task, TaskStatus.QUEUED); session.flush()
    try: job_id = enqueue_execution(execution.id); execution.result = {"rq_job_id": job_id, "retry": {"attempt": 1, "max_attempts": 3, "automatic": False}}; session.commit()
    except Exception as error: session.rollback(); raise HTTPException(status_code=503, detail="Execution queue unavailable") from error
    return {"execution_id": execution.id, "status": execution.status, "rq_job_id": job_id}


@app.post("/api/tasks/{task_id}/restart")
def restart_failed_task(task_id: int, session: Session = Depends(db_session)) -> dict:
    task = session.get(Task, task_id)
    if task is None or task.status is not TaskStatus.FAILED:
        raise HTTPException(status_code=409, detail="Only a failed task can be restarted")
    execution = Execution(task_id=task.id, executor="codex", model="gpt-5.6-terra", status="QUEUED")
    session.add(execution)
    transition_task(task, TaskStatus.QUEUED)
    audit(session, actor="local-admin", action="TASK_RESTARTED", entity_type="task", entity_id=task.task_key)
    session.flush()
    try:
        job_id = enqueue_execution(execution.id)
        execution.result = {"rq_job_id": job_id, "restart_of": task.task_key, "retry": {"attempt": 1, "max_attempts": 3, "automatic": False}}
        session.commit()
    except Exception as error:
        session.rollback()
        raise HTTPException(status_code=503, detail="Execution queue unavailable") from error
    return {"execution_id": execution.id, "status": execution.status, "rq_job_id": job_id}


@app.post("/api/tasks/{task_id}/deploy")
def deploy_completed_task(task_id: int, session: Session = Depends(db_session)) -> dict:
    """Promote a completed control-plane worktree through the guarded local release helper."""
    task = session.get(Task, task_id)
    if task is None or task.status is not TaskStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Only a completed task can be deployed")
    if task.project.repository_url:
        raise HTTPException(status_code=409, detail="This project requires its configured release controller; direct promotion is unavailable")
    execution = session.query(Execution).filter_by(task_id=task.id, status="COMPLETED").order_by(Execution.id.desc()).first()
    worktree = Path(execution.worktree).resolve() if execution and execution.worktree else None
    allowed_root = Path(get_settings().worktrees_path).resolve()
    if worktree is None or allowed_root not in worktree.parents or not worktree.is_dir():
        raise HTTPException(status_code=409, detail="Completed task has no safe deployable worktree")
    result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/local/sbin/ops-orchestrator-promote", str(worktree), task.task_key], text=True, capture_output=True, timeout=300)
    output = (result.stdout + result.stderr)[-4000:]
    if result.returncode:
        audit(session, actor="local-admin", action="TASK_DEPLOY_FAILED", entity_type="task", entity_id=task.task_key, new_value={"output": output})
        session.commit()
        raise HTTPException(status_code=409, detail=output or "Promotion preflight failed")
    audit(session, actor="local-admin", action="TASK_DEPLOY_SCHEDULED", entity_type="task", entity_id=task.task_key, new_value={"output": output})
    session.commit()
    return {"status": "deployed", "detail": output}


def _execution_view(e: Execution) -> dict:
    return {"id": e.id, "task_id": e.task_id, "status": e.status, "executor": e.executor, "model": e.model, "worktree": e.worktree, "output_path": e.output_path, "started_at": e.started_at, "completed_at": e.completed_at, "result": e.result}


@app.get("/api/executions")
def executions(session: Session = Depends(db_session)) -> list[dict]: return [_execution_view(e) for e in session.query(Execution).order_by(Execution.id.desc())]


@app.get("/api/executions/{execution_id}")
def execution_detail(execution_id: int, session: Session = Depends(db_session)) -> dict:
    execution = session.get(Execution, execution_id)
    if execution is None: raise HTTPException(status_code=404, detail="Execution not found")
    return _execution_view(execution)


@app.get("/api/executions/{execution_id}/log", response_class=PlainTextResponse)
def execution_log(execution_id: int, session: Session = Depends(db_session)) -> str:
    execution = session.get(Execution, execution_id)
    if execution is None: raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.output_path: return "Execution has not started."
    path = Path(execution.output_path).resolve(); artifacts = Path(get_settings().artifacts_path).resolve()
    if artifacts not in path.parents or not path.is_file(): raise HTTPException(status_code=404, detail="Execution log unavailable")
    return path.read_text(errors="replace")[-200_000:]


def _execution_pids(execution: Execution) -> list[int]:
    if not execution.worktree:
        return []
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            if execution.worktree in command and "codex exec" in command:
                matches.append(int(entry.name))
        except OSError:
            continue
    return matches


def _rq_state(execution: Execution) -> str | None:
    job_id = (execution.result or {}).get("rq_job_id")
    if not job_id:
        return None
    try:
        return Job.fetch(job_id, connection=get_queue().connection).get_status()
    except Exception:
        return None


def _execution_activity(log: str) -> list[dict[str, object]]:
    """Translate Codex JSONL into a small, human-readable activity feed."""
    events: list[dict[str, object]] = []
    for raw in log.splitlines():
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        item = payload.get("item", {}) if isinstance(payload, dict) else {}
        if not isinstance(item, dict):
            continue
        kind, status = item.get("type"), item.get("status")
        if kind == "agent_message" and item.get("text"):
            events.append({"kind": "message", "status": "complete", "text": item["text"]})
        elif kind == "command_execution":
            command = str(item.get("command", "Command"))
            output = str(item.get("aggregated_output") or "")[-6000:]
            events.append({"kind": "command", "status": status or "running", "command": command, "output": output, "exit_code": item.get("exit_code")})
        elif kind == "file_change":
            paths = [str(change.get("path", "")) for change in item.get("changes", []) if isinstance(change, dict)]
            events.append({"kind": "files", "status": status or "complete", "paths": paths})
    return events[-100:]


@app.get("/api/executions/{execution_id}/live")
def execution_live(execution_id: int, session: Session = Depends(db_session)) -> dict:
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    log = "Waiting for the worker to initialize live output."
    if execution.output_path:
        path = Path(execution.output_path).resolve(); artifacts = Path(get_settings().artifacts_path).resolve()
        if artifacts in path.parents and path.is_file():
            log = path.read_text(errors="replace")[-200_000:] or "Worker started; waiting for Codex output."
    pids = _execution_pids(execution)
    return {**_execution_view(execution), "log": log, "activity": _execution_activity(log), "process_running": bool(pids), "rq_status": _rq_state(execution), "can_stop": bool(pids) and execution.status == "RUNNING"}


@app.post("/api/executions/{execution_id}/stop")
def stop_execution(execution_id: int, session: Session = Depends(db_session)) -> dict:
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.status != "RUNNING":
        raise HTTPException(status_code=409, detail="Only a running execution can be stopped")
    pids = _execution_pids(execution)
    if not pids:
        raise HTTPException(status_code=409, detail="No active Codex process was found")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    execution.status = "STOP_REQUESTED"
    task = session.get(Task, execution.task_id)
    if task is not None:
        task.status = TaskStatus.WAITING_FOR_USER
    audit(session, actor="local-admin", action="EXECUTION_STOP_REQUESTED", entity_type="execution", entity_id=str(execution.id))
    session.commit()
    return {"status": "stop_requested", "pids": pids}


@app.get("/api/models")
def models(session: Session = Depends(db_session)) -> dict: return {**list_models(), "default_local_model": _default_model(session)}


@app.put("/api/models/default")
async def select_default_model(request: Request, session: Session = Depends(db_session)) -> dict:
    payload = await request.json(); selected = payload.get("model") if isinstance(payload, dict) else None; health = list_models()
    if not isinstance(selected, str) or selected not in health["models"]: raise HTTPException(status_code=422, detail="Choose a model currently reported by Ollama")
    setting = session.get(SystemSetting, "default_local_model")
    if setting is None: setting = SystemSetting(key="default_local_model", value=selected); session.add(setting)
    else: setting.value = selected
    audit(session, actor="local-admin", action="DEFAULT_LOCAL_MODEL_CHANGED", entity_type="setting", entity_id=setting.key, new_value={"model": selected}); session.commit(); return {"default_local_model": selected}


@app.get("/api/usage")
def usage(session: Session = Depends(db_session)) -> dict:
    return {"codex_weekly_quota": None, "codex_quota_note": "Account weekly Codex quota cannot be queried locally.", "codex_status_hint": "Run /status in Codex to view account quota.", "local_execution_count": session.query(Execution).count(), "recent_activity": [_execution_view(e) for e in session.query(Execution).order_by(Execution.id.desc()).limit(10)]}


@app.get("/api/warnings")
def warnings(session: Session = Depends(db_session)) -> list[dict[str, str]]: return _warnings(session)


@app.post("/api/mobile-actions/{action}")
def mobile_action(action: str, x_ops_bridge_secret: str = Header(default=""), session: Session = Depends(db_session)) -> dict[str, str]:
    secret = get_settings().mobile_bridge_secret
    if not secret or not hmac.compare_digest(secret, x_ops_bridge_secret): raise HTTPException(status_code=403, detail="Invalid bridge credential")
    match = re.fullmatch(r"OPS_REJECT_INTAKE_(\d+)", action)
    if match:
        intake = session.get(IntakeSession, int(match.group(1)))
        if intake is None or intake.status not in {IntakeStatus.AWAITING_APPROVAL, IntakeStatus.CLARIFYING}: raise HTTPException(status_code=409, detail="Intake is not actionable")
        intake.status = IntakeStatus.REJECTED; audit(session, actor="homeassistant-mobile", action="INTAKE_REJECTED", entity_type="intake", entity_id=str(intake.id)); session.commit(); return {"status": "rejected"}
    match = re.fullmatch(r"OPS_APPROVE_INTAKE_(\d+)", action)
    if match:
        intake = session.get(IntakeSession, int(match.group(1)))
        if intake is None or not intake.proposed_record: raise HTTPException(status_code=409, detail="Intake has no complete proposed record")
        project, task = approve_intake(session, intake, ApprovalRequest(**intake.proposed_record), "homeassistant-mobile"); session.commit(); return {"status": "approved", "task_key": task.task_key, "project_key": project.project_key}
    raise HTTPException(status_code=409, detail="Action requires a proposed record and cannot yet be approved by mobile")
