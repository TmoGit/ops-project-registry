"""Redis RQ queue integration.  Jobs always create their own database session."""

from redis import Redis
from datetime import timedelta

from rq import Queue
from rq_scheduler import Scheduler

from app.config import get_settings


def get_queue() -> Queue:
    return Queue("ops-executions", connection=Redis.from_url(get_settings().redis_url))


def get_scheduler() -> Scheduler:
    return Scheduler(queue_name="ops-executions", connection=Redis.from_url(get_settings().redis_url))


def enqueue_execution(execution_id: int, *, delay_seconds: int = 0) -> str:
    from app.worker import run_execution_job

    queue = get_queue()
    if delay_seconds:
        job = get_scheduler().enqueue_in(timedelta(seconds=delay_seconds), run_execution_job, execution_id, job_timeout="2h", result_ttl=86400)
    else:
        job = queue.enqueue(run_execution_job, execution_id, job_timeout="2h", result_ttl=86400)
    return job.id
