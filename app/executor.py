import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.models import Execution, TaskStatus


def _source(task) -> Path:
    project = task.project
    if not project.repository_url:
        return Path("/opt/ops-orchestrator/repos/project-registry")
    source = Path(f"/opt/ops-orchestrator/repos/{project.project_key}/source")
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        subprocess.run(["git", "clone", project.repository_url, str(source)], check=True)
    subprocess.run(["git", "fetch", "origin", project.default_branch], cwd=source, check=True)
    return source


def run_codex(task, execution: Execution) -> None:
    source = _source(task)
    root = Path(f"/opt/ops-orchestrator/worktrees/{task.task_key.split('-')[0]}/{task.task_key}")
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "-b", f"agent/{task.task_key}", str(root), f"origin/{task.project.default_branch}"], cwd=source, check=True)
    (root / "TASK.md").write_text(f"# {task.task_key}\n\n{task.title}\n\n{task.description}\n")
    output = Path(f"/opt/ops-orchestrator/artifacts/{task.task_key}-codex.jsonl")
    execution.worktree, execution.output_path, execution.status, execution.started_at = str(root), str(output), "RUNNING", datetime.now(timezone.utc)
    task.status = TaskStatus.RUNNING_CODEX
    with output.open("w") as stream:
        result = subprocess.run(["/usr/local/bin/codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--json", "-m", "gpt-5.6-terra", "-C", str(root), "Read TASK.md and implement the approved task."], stdout=stream, stderr=subprocess.STDOUT)
    changed = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True).stdout.splitlines()
    tests = subprocess.run(["/opt/ops-orchestrator/venv/bin/python", "-m", "pytest", "-q"], cwd=root, text=True, capture_output=True) if (root / "tests").exists() else subprocess.CompletedProcess([], 0, "No tests configured.", "")
    execution.completed_at = datetime.now(timezone.utc)
    execution.result = {"returncode": result.returncode, "changed_files": changed, "tests_returncode": tests.returncode, "tests_output": tests.stdout[-4000:]}
    execution.status = "COMPLETED" if result.returncode == 0 and tests.returncode == 0 else "FAILED"
    task.status = TaskStatus.COMPLETED if execution.status == "COMPLETED" else TaskStatus.FAILED
