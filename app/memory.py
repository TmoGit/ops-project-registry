import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings


def checkpoint(project_key: str, task_key: str, raw_request: str, title: str) -> None:
    root = Path(get_settings().registry_path) / "projects" / project_key / ".ops"
    intake = root / "intake"; tasks = root / "tasks"
    intake.mkdir(parents=True, exist_ok=True); tasks.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (intake / f"{stamp}-{task_key}.md").write_text(raw_request + "\n")
    (root / "project.yaml").write_text(f"project_id: {project_key}\nname: {project_key}\nstatus: active\n")
    (tasks / f"{task_key}.md").write_text(f"# {task_key}\n\nTitle: {title}\nStatus: COMMITTED\n")
    with (root / "project-log.ndjson").open("a") as log:
        log.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": "TASK_APPROVED", "task": task_key}) + "\n")
    repo = get_settings().registry_path
    subprocess.run(["git", "add", "projects"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", f"ops({project_key}): approve task {task_key}"], cwd=repo, check=True)
    # Gitea is canonical. DR mirror failure must never discard its checkpoint.
    subprocess.run(["git", "push", "origin"], cwd=repo, check=True)
    try:
        subprocess.run(["git", "push", "github"], cwd=repo, check=True)
    except subprocess.CalledProcessError:
        (Path(repo) / "runtime" / "github-sync-failed").parent.mkdir(exist_ok=True)
        (Path(repo) / "runtime" / "github-sync-failed").write_text("GitHub DR sync failed; retry required.\n")
