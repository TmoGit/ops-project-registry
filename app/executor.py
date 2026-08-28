import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.models import Execution, TaskStatus


def run_codex(task, execution: Execution) -> None:
    root = Path(f"/opt/ops-orchestrator/worktrees/{task.task_key.split('-')[0]}/{task.task_key}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "TASK.md").write_text(f"# {task.task_key}\n\n{task.title}\n\n{task.description}\n")
    output = Path(f"/opt/ops-orchestrator/artifacts/{task.task_key}-codex.jsonl")
    execution.worktree, execution.output_path, execution.status, execution.started_at = str(root), str(output), "RUNNING", datetime.now(timezone.utc)
    task.status = TaskStatus.RUNNING_CODEX
    with output.open("w") as stream:
        result = subprocess.run(["/usr/local/bin/codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--json", "-m", "gpt-5.6-terra", "-C", str(root), "Read TASK.md and implement the approved task."], stdout=stream, stderr=subprocess.STDOUT)
    execution.completed_at = datetime.now(timezone.utc)
    execution.status = "COMPLETED" if result.returncode == 0 else "FAILED"
    task.status = TaskStatus.TESTING if result.returncode == 0 else TaskStatus.FAILED
