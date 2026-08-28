from app.models import Task, TaskStatus

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.INTAKE: {TaskStatus.CLARIFYING, TaskStatus.DRAFT_PLAN, TaskStatus.CANCELLED},
    TaskStatus.CLARIFYING: {TaskStatus.DRAFT_PLAN, TaskStatus.CANCELLED},
    TaskStatus.DRAFT_PLAN: {TaskStatus.AWAITING_USER_APPROVAL, TaskStatus.CANCELLED},
    TaskStatus.AWAITING_USER_APPROVAL: {TaskStatus.COMMITTED, TaskStatus.CANCELLED},
    TaskStatus.COMMITTED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.QUEUED: {TaskStatus.RUNNING_LOCAL, TaskStatus.RUNNING_CODEX, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING_LOCAL: {TaskStatus.TESTING, TaskStatus.WAITING_FOR_USER, TaskStatus.FAILED},
    TaskStatus.RUNNING_CODEX: {TaskStatus.TESTING, TaskStatus.WAITING_FOR_USER, TaskStatus.FAILED},
    TaskStatus.TESTING: {TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.WAITING_FOR_USER: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
}


def transition_task(task: Task, target: TaskStatus) -> None:
    if target not in ALLOWED_TASK_TRANSITIONS.get(task.status, set()):
        raise ValueError(f"Invalid task transition: {task.status} -> {target}")
    task.status = target
