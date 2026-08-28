from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Approval, AuditEvent, IntakeSession, IntakeStatus, Project, ProjectStatus, Task, TaskStatus
from app.memory import checkpoint
from app.config import get_settings
import httpx


def audit(session: Session, *, actor: str, action: str, entity_type: str, entity_id: str, old_value: dict | None = None, new_value: dict | None = None) -> None:
    session.add(AuditEvent(actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, old_value=old_value, new_value=new_value))


def approve_intake(session: Session, intake: IntakeSession, payload, actor: str) -> tuple[Project, Task]:
    if intake.status is not IntakeStatus.AWAITING_APPROVAL:
        raise ValueError("Intake is not awaiting approval")
    project = session.query(Project).filter_by(project_key=payload.project_key.upper()).one_or_none()
    if project is None:
        project = Project(project_key=payload.project_key.upper(), name=payload.project_name, status=ProjectStatus.ACTIVE, test_command=payload.test_command)
        session.add(project)
        session.flush()
        audit(session, actor=actor, action="PROJECT_CREATED", entity_type="project", entity_id=str(project.id), new_value={"key": project.project_key})
    elif payload.test_command is not None:
        project.test_command = payload.test_command
    task_count = session.query(Task).filter_by(project_id=project.id).count() + 1
    task = Task(task_key=f"{project.project_key}-{task_count:04d}", project_id=project.id, intake_id=intake.id, title=payload.task_title, description=payload.task_description, status=TaskStatus.COMMITTED)
    session.add(task)
    intake.status = IntakeStatus.APPROVED
    session.add(Approval(intake_id=intake.id, decision="APPROVE", decided_by=actor, decided_at=datetime.now(timezone.utc)))
    audit(session, actor=actor, action="INTAKE_APPROVED", entity_type="intake", entity_id=str(intake.id))
    audit(session, actor=actor, action="TASK_COMMITTED", entity_type="task", entity_id=task.task_key, new_value={"project": project.project_key})
    session.flush()
    checkpoint(project.project_key, task.task_key, intake.raw_request, task.title)
    return project, task


def notify_mobile_approval(intake: IntakeSession) -> bool:
    """Send Home Assistant-compatible actionable notification through HomeOps bridge.

    The bridge URL is deliberately opt-in; an unavailable bridge never loses the
    saved proposed record, and Home Assistant can retry or use the web form.
    """
    url = get_settings().homeops_bridge_url
    if not url:
        return False
    payload = {
        "title": "Ops approval required",
        "message": f"Review intake {intake.id}: {intake.raw_request[:160]}",
        "data": {"actions": [
            {"action": f"OPS_APPROVE_INTAKE_{intake.id}", "title": "Approve"},
            {"action": f"OPS_REJECT_INTAKE_{intake.id}", "title": "Reject", "destructive": True},
        ]},
    }
    try:
        response = httpx.post(url, json=payload, headers={"X-Ops-Bridge-Secret": get_settings().mobile_bridge_secret}, timeout=10)
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False
