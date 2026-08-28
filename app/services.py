from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Approval, AuditEvent, IntakeSession, IntakeStatus, Project, ProjectStatus, Task, TaskStatus


def audit(session: Session, *, actor: str, action: str, entity_type: str, entity_id: str, old_value: dict | None = None, new_value: dict | None = None) -> None:
    session.add(AuditEvent(actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, old_value=old_value, new_value=new_value))


def approve_intake(session: Session, intake: IntakeSession, payload, actor: str) -> tuple[Project, Task]:
    if intake.status is not IntakeStatus.AWAITING_APPROVAL:
        raise ValueError("Intake is not awaiting approval")
    project = session.query(Project).filter_by(project_key=payload.project_key.upper()).one_or_none()
    if project is None:
        project = Project(project_key=payload.project_key.upper(), name=payload.project_name, status=ProjectStatus.ACTIVE)
        session.add(project)
        session.flush()
        audit(session, actor=actor, action="PROJECT_CREATED", entity_type="project", entity_id=str(project.id), new_value={"key": project.project_key})
    task_count = session.query(Task).filter_by(project_id=project.id).count() + 1
    task = Task(task_key=f"{project.project_key}-{task_count:04d}", project_id=project.id, intake_id=intake.id, title=payload.task_title, description=payload.task_description, status=TaskStatus.COMMITTED)
    session.add(task)
    intake.status = IntakeStatus.APPROVED
    session.add(Approval(intake_id=intake.id, decision="APPROVE", decided_by=actor, decided_at=datetime.now(timezone.utc)))
    audit(session, actor=actor, action="INTAKE_APPROVED", entity_type="intake", entity_id=str(intake.id))
    audit(session, actor=actor, action="TASK_COMMITTED", entity_type="task", entity_id=task.task_key, new_value={"project": project.project_key})
    return project, task
