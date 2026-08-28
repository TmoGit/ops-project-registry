import json

import typer
from sqlalchemy import select

from app.db import SessionLocal
from app.models import IntakeSession, IntakeStatus, Project, Task
from app.schemas import ApprovalRequest
from app.services import approve_intake, audit
from app.triage import triage as run_triage

cli = typer.Typer(help="Ops Orchestrator control plane")


@cli.command()
def intake(request: str) -> None:
    """Store a request verbatim; it is never committed automatically."""
    with SessionLocal() as session:
        item = IntakeSession(raw_request=request, status=IntakeStatus.AWAITING_APPROVAL)
        session.add(item)
        session.flush()
        audit(session, actor="opsctl", action="INTAKE_CREATED", entity_type="intake", entity_id=str(item.id))
        session.commit()
        typer.echo(f"Intake {item.id} awaiting review")


@cli.command()
def triage(intake_id: int) -> None:
    with SessionLocal() as session:
        item = session.get(IntakeSession, intake_id)
        if item is None:
            raise typer.Exit("Intake not found")
        result = triage_request(item.raw_request)
        item.triage = result
        item.status = IntakeStatus.CLARIFYING if result["clarification_required"] else IntakeStatus.AWAITING_APPROVAL
        audit(session, actor="qwen", action="TRIAGE_COMPLETED", entity_type="intake", entity_id=str(item.id), new_value=result)
        session.commit()
        typer.echo(json.dumps(result, indent=2))


def triage_request(raw_request: str) -> dict:
    return run_triage(raw_request).model_dump()


@cli.command()
def approve(intake_id: int, project_key: str, project_name: str, title: str, description: str) -> None:
    with SessionLocal() as session:
        item = session.get(IntakeSession, intake_id)
        if item is None:
            raise typer.Exit("Intake not found")
        project, task = approve_intake(session, item, ApprovalRequest(project_key=project_key, project_name=project_name, task_title=title, task_description=description), "opsctl")
        session.commit()
        typer.echo(f"Approved {project.project_key}: {task.task_key}")


@cli.command()
def projects() -> None:
    with SessionLocal() as session:
        for project in session.scalars(select(Project).order_by(Project.project_key)):
            typer.echo(f"{project.project_key}\t{project.status.value}\t{project.name}")


@cli.command()
def tasks(status: str | None = None) -> None:
    with SessionLocal() as session:
        query = select(Task).order_by(Task.task_key)
        if status:
            query = query.where(Task.status == status.upper())
        for task in session.scalars(query):
            typer.echo(f"{task.task_key}\t{task.status.value}\t{task.title}")


@cli.command()
def status() -> None:
    with SessionLocal() as session:
        typer.echo(f"projects={session.query(Project).count()} tasks={session.query(Task).count()} pending_intakes={session.query(IntakeSession).filter_by(status=IntakeStatus.AWAITING_APPROVAL).count()}")


if __name__ == "__main__":
    cli()
