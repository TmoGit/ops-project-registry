import subprocess

from app.memory import checkpoint


def test_github_sync_failure_preserves_checkpoint_and_marks_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_REGISTRY_PATH", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "push"] and command[-1] == "github":
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr("app.memory.subprocess.run", run)
    checkpoint("OPS", "OPS-0001", "original request", "title")
    assert (tmp_path / "projects/OPS/.ops/tasks/OPS-0001.md").exists()
    assert (tmp_path / "runtime/github-sync-failed").exists()
    assert ["git", "push", "origin"] in calls
