"""Redis RQ queue integration.  Jobs always create their own database session."""

from redis import Redis
from rq import Queue

from app.config import get_settings


def get_queue() -> Queue:
    return Queue("ops-executions", connection=Redis.from_url(get_settings().redis_url))


def enqueue_execution(execution_id: int) -> str:
    from app.worker import run_execution_job

    job = get_queue().enqueue(run_execution_job, execution_id, job_timeout="2h", result_ttl=86400)
    return job.id
