import subprocess
import shlex
from datetime import datetime, timezone
from pathlib import Path

from app.models import Execution, TaskStatus
from app.config import get_settings
from app.state import transition_task


def _source(task) -> Path:
    project = task.project
    if not project.repository_url:
        return Path(get_settings().registry_path)
    source = Path(f"/opt/ops-orchestrator/repos/{project.project_key}/source")
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        subprocess.run(["git", "clone", project.repository_url, str(source)], check=True)
    subprocess.run(["git", "fetch", "origin", project.default_branch], cwd=source, check=True)
    return source


def worktree_path(task) -> Path:
    """A task key is the only worktree leaf, preventing task overlap."""
    return Path(get_settings().worktrees_path) / task.project.project_key / task.task_key


def _prepare_worktree(task) -> Path:
    source = _source(task)
    root = worktree_path(task)
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "-b", f"agent/{task.task_key}", str(root), f"origin/{task.project.default_branch}"], cwd=source, check=True)
    return root


def _run_tests(root: Path, test_command: str | None) -> subprocess.CompletedProcess:
    if test_command:
        return subprocess.run(shlex.split(test_command), cwd=root, text=True, capture_output=True)
    if (root / "tests").exists():
        return subprocess.run(["python", "-m", "pytest", "-q"], cwd=root, text=True, capture_output=True)
    return subprocess.CompletedProcess([], 0, "No tests configured.", "")


def run_codex(task, execution: Execution) -> None:
    root = _prepare_worktree(task)
    (root / "TASK.md").write_text(f"# {task.task_key}\n\n{task.title}\n\n{task.description}\n")
    output = Path(get_settings().artifacts_path) / f"{task.task_key}-codex.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    execution.worktree, execution.output_path, execution.status, execution.started_at = str(root), str(output), "RUNNING", datetime.now(timezone.utc)
    transition_task(task, TaskStatus.RUNNING_CODEX)
    with output.open("w") as stream:
        result = subprocess.run(["/usr/local/bin/codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--json", "-m", "gpt-5.6-terra", "-C", str(root), "Read TASK.md and implement the approved task."], stdout=stream, stderr=subprocess.STDOUT)
    changed = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True).stdout.splitlines()
    transition_task(task, TaskStatus.TESTING)
    tests = _run_tests(root, task.project.test_command)
    execution.completed_at = datetime.now(timezone.utc)
    execution.result = {"returncode": result.returncode, "changed_files": changed, "tests_returncode": tests.returncode, "tests_output": tests.stdout[-4000:]}
    execution.status = "COMPLETED" if result.returncode == 0 and tests.returncode == 0 else "FAILED"
    transition_task(task, TaskStatus.COMPLETED if execution.status == "COMPLETED" else TaskStatus.FAILED)
