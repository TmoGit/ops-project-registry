import subprocess
import shlex
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path

from app.models import Execution, TaskStatus
from app.config import get_settings
from app.state import transition_task
import httpx


_SERVICE_ONLY_ENV = ("OPS_DATABASE_URL", "OPS_ADMIN_PASSWORD", "OPS_SESSION_SECRET", "OPS_BIND_HOST", "OPS_MOBILE_BRIDGE_SECRET", "OPS_HOMEOPS_BRIDGE_URL")


def _agent_environment() -> dict[str, str]:
    """Keep production credentials out of the isolated agent and its subprocesses."""
    environment = os.environ.copy()
    for name in _SERVICE_ONLY_ENV:
        environment.pop(name, None)
    return environment


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
        # The service process carries real dashboard credentials.  The orchestrator's
        # own tests define isolated defaults and must not inherit those production values.
        test_env = os.environ.copy()
        for name in (*_SERVICE_ONLY_ENV, "OPS_ENVIRONMENT", "OPS_ATTACHMENTS_PATH"):
            test_env.pop(name, None)
        return subprocess.run(["python", "-m", "pytest", "-q"], cwd=root, text=True, capture_output=True, env=test_env)
    return subprocess.CompletedProcess([], 0, "No tests configured.", "")


def run_codex(task, execution: Execution) -> None:
    root = _prepare_worktree(task)
    attachment_note = _materialize_attachments(task, root)
    (root / "TASK.md").write_text(f"# {task.task_key}\n\n{task.title}\n\n{task.description}{attachment_note}\n")
    output = Path(get_settings().artifacts_path) / f"{task.task_key}-codex-{execution.id}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    execution.worktree, execution.output_path, execution.status, execution.started_at = str(root), str(output), "RUNNING", datetime.now(timezone.utc)
    transition_task(task, TaskStatus.RUNNING_CODEX)
    from sqlalchemy.orm import object_session
    object_session(execution).commit()
    with output.open("w") as stream:
        result = subprocess.run(["/usr/local/bin/codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--json", "-m", "gpt-5.6-terra", "-C", str(root), "Read TASK.md and implement the approved task."], stdout=stream, stderr=subprocess.STDOUT, env=_agent_environment())
    session = object_session(execution)
    session.refresh(execution); session.refresh(task)
    if execution.status == "STOP_REQUESTED":
        execution.completed_at = datetime.now(timezone.utc)
        execution.result = {"returncode": result.returncode, "intervention": "stopped by local admin"}
        execution.status = "STOPPED"
        return
    changed = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True).stdout.splitlines()
    transition_task(task, TaskStatus.TESTING)
    tests = _run_tests(root, task.project.test_command)
    execution.completed_at = datetime.now(timezone.utc)
    execution.result = {"returncode": result.returncode, "changed_files": changed, "tests_returncode": tests.returncode, "tests_output": tests.stdout[-4000:]}
    execution.status = "COMPLETED" if result.returncode == 0 and tests.returncode == 0 else "FAILED"
    transition_task(task, TaskStatus.COMPLETED if execution.status == "COMPLETED" else TaskStatus.FAILED)


def run_local_analysis(task, execution: Execution) -> None:
    """Run a strictly read-only local analysis. No repository or host writes occur."""
    output = Path(get_settings().artifacts_path) / f"{task.task_key}-local-analysis-{execution.id}.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    execution.output_path, execution.status, execution.started_at = str(output), "RUNNING", datetime.now(timezone.utc)
    transition_task(task, TaskStatus.RUNNING_LOCAL)
    from sqlalchemy.orm import object_session
    object_session(execution).commit()
    prompt = "Provide a concise implementation plan and risks. Do not execute commands, modify files, or claim changes were made.\n\nTask:\n" + task.description
    response = httpx.post(f"{get_settings().ollama_base_url.rstrip('/')}/api/generate", json={"model": execution.model, "prompt": prompt, "stream": False}, timeout=120)
    response.raise_for_status()
    analysis = str(response.json().get("response", "")).strip()
    if not analysis:
        raise RuntimeError("Local model returned no analysis")
    output.write_text("LOCAL READ-ONLY ANALYSIS\n\n" + analysis)
    transition_task(task, TaskStatus.TESTING)
    execution.completed_at = datetime.now(timezone.utc)
    execution.result = {"mode": "LOCAL_ANALYSIS_ONLY", "repository_write": False, "host_write": False, "analysis": analysis[-12_000:]}
    execution.status = "COMPLETED"
    transition_task(task, TaskStatus.COMPLETED)


def _materialize_attachments(task, root: Path) -> str:
    """Copy approved attachment bytes into the isolated worktree; never execute them."""
    attachments = list(getattr(task, "attachments", []))
    if not attachments:
        return ""
    target = root / ".ops-attachments"
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError("Attachment context path is unsafe")
    source_root = Path(get_settings().attachments_path).resolve()
    names = []
    for item in attachments:
        source = (source_root / item.stored_name).resolve()
        if source.parent != source_root or not source.is_file():
            continue
        destination = target / f"{item.id}-{item.original_name}"
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise RuntimeError("Attachment destination is unsafe")
            destination.unlink()
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        names.append(destination.name)
    return "\n\nApproved read-only attachments are in `.ops-attachments/`: " + ", ".join(names) if names else ""
