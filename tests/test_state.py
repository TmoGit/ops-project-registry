import pytest

from app.models import TaskStatus
from app.state import transition_task


class TaskStub:
    def __init__(self, status): self.status = status


def test_task_transitions_accept_valid_lifecycle():
    task = TaskStub(TaskStatus.COMMITTED)
    for target in (TaskStatus.QUEUED, TaskStatus.RUNNING_CODEX, TaskStatus.TESTING, TaskStatus.COMPLETED):
        transition_task(task, target)
    assert task.status is TaskStatus.COMPLETED


def test_task_transitions_reject_skipping_approval():
    with pytest.raises(ValueError, match="Invalid task transition"):
        transition_task(TaskStub(TaskStatus.COMMITTED), TaskStatus.COMPLETED)
