"""RQ entry point for isolated task execution with bounded soft retries."""
from datetime import datetime, timezone

from app.db import SessionLocal
from app.executor import run_codex, run_local_analysis
from app.models import Execution, Task, TaskStatus
from app.queue import enqueue_execution
from app.services import audit, notify_task_status
from app.state import transition_task

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30


def _retry_meta(execution: Execution) -> dict:
    result = execution.result if isinstance(execution.result, dict) else {}
    retry = result.get("retry") if isinstance(result.get("retry"), dict) else {}
    return {"attempt": max(1, int(retry.get("attempt", 1))), "max_attempts": MAX_ATTEMPTS, "automatic": bool(retry.get("automatic", False))}


def _fail_or_retry(session, task: Task, execution: Execution, error: str | None = None) -> bool:
    """Persist failure and queue one delayed, isolated retry when attempts remain."""
    result = dict(execution.result or {})
    if error:
        result["error"] = error
    retry = _retry_meta(execution)
    execution.status = "FAILED"
    execution.completed_at = execution.completed_at or datetime.now(timezone.utc)
    if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.WAITING_FOR_USER}:
        try:
            transition_task(task, TaskStatus.FAILED)
        except ValueError:
            task.status = TaskStatus.FAILED
    if retry["attempt"] >= MAX_ATTEMPTS or task.status in {TaskStatus.CANCELLED, TaskStatus.WAITING_FOR_USER}:
        result["retry"] = {**retry, "exhausted": retry["attempt"] >= MAX_ATTEMPTS}
        execution.result = result
        audit(session, actor="orchestrator", action="EXECUTION_RETRY_EXHAUSTED", entity_type="execution", entity_id=str(execution.id), new_value=result["retry"])
        return False
    next_attempt = retry["attempt"] + 1
    result["retry"] = {**retry, "scheduled_next_attempt": next_attempt, "delay_seconds": RETRY_DELAY_SECONDS}
    execution.result = result
    replacement = Execution(task_id=task.id, executor=execution.executor, model=execution.model, status="QUEUED", worktree=execution.worktree, result={"retry": {"attempt": next_attempt, "max_attempts": MAX_ATTEMPTS, "automatic": True, "retry_of_execution_id": execution.id}})
    session.add(replacement)
    transition_task(task, TaskStatus.QUEUED)
    session.flush()
    audit(session, actor="orchestrator", action="EXECUTION_RETRY_SCHEDULED", entity_type="execution", entity_id=str(replacement.id), new_value={"retry_of_execution_id": execution.id, "attempt": next_attempt, "max_attempts": MAX_ATTEMPTS, "delay_seconds": RETRY_DELAY_SECONDS})
    session.commit()
    try:
        job_id = enqueue_execution(replacement.id, delay_seconds=RETRY_DELAY_SECONDS)
        replacement.result = {**replacement.result, "rq_job_id": job_id}
        session.commit()
        return True
    except Exception as queue_error:
        replacement.status = "FAILED"
        replacement.result = {**replacement.result, "error": f"Automatic retry could not be queued: {queue_error}"}
        task.status = TaskStatus.FAILED
        audit(session, actor="orchestrator", action="EXECUTION_RETRY_QUEUE_FAILED", entity_type="execution", entity_id=str(replacement.id))
        session.commit()
        return False


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
            if execution.executor == "local":
                run_local_analysis(task, execution)
            else:
                run_codex(task, execution)
            if execution.status == "FAILED":
                if _fail_or_retry(session, task, execution):
                    return
            notify_task_status(task, execution)
            session.commit()
        except Exception as error:
            if _fail_or_retry(session, task, execution, str(error)):
                return
            notify_task_status(task, execution)
            session.commit()
