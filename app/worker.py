"""RQ entry point for isolated task execution."""

from app.db import SessionLocal
from app.executor import run_codex
from app.models import Execution, Task, TaskStatus
from app.services import notify_task_status


def run_execution_job(execution_id: int) -> None:
    """Run one execution; RQ invokes this outside the API process."""
    with SessionLocal() as session:
        execution = session.get(Execution, execution_id)
        if execution is None:
            return
        task = session.get(Task, execution.task_id)
        if task is None:
            execution.status = "FAILED"
            execution.result = {"error": "Task not found"}
            session.commit()
            return
        try:
            run_codex(task, execution)
            notify_task_status(task, execution)
            session.commit()
        except Exception as error:
            execution.status = "FAILED"
            execution.result = {"error": str(error)}
            if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                task.status = TaskStatus.FAILED
            notify_task_status(task, execution)
            session.commit()
            raise
