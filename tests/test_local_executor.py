from pathlib import Path

from app.executor import run_local_analysis
from app.models import Execution, Project, ProjectStatus, Task, TaskStatus


def test_local_analysis_never_creates_a_worktree(session, monkeypatch, tmp_path):
    project = Project(project_key="SAFE", name="Safe", status=ProjectStatus.ACTIVE)
    session.add(project); session.flush()
    task = Task(task_key="SAFE-0001", project_id=project.id, title="Plan", description="Plan safely", status=TaskStatus.QUEUED)
    session.add(task); session.flush()
    execution = Execution(task_id=task.id, executor="local", model="qwen2.5:3b", status="QUEUED")
    session.add(execution); session.commit()
    monkeypatch.setattr("app.executor.get_settings", lambda: type("S", (), {"artifacts_path": str(tmp_path), "ollama_base_url": "http://local"})())
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"response": "Safe plan"}
    monkeypatch.setattr("app.executor.httpx.post", lambda *args, **kwargs: Response())
    run_local_analysis(task, execution)
    assert execution.status == "COMPLETED"
    assert execution.worktree is None
    assert Path(execution.output_path).read_text().startswith("LOCAL READ-ONLY ANALYSIS")
