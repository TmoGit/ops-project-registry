from types import SimpleNamespace

from app.executor import worktree_path


def test_worktree_paths_are_isolated_by_project_and_task(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_WORKTREES_PATH", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    one = SimpleNamespace(project=SimpleNamespace(project_key="OPS"), task_key="OPS-0001")
    two = SimpleNamespace(project=SimpleNamespace(project_key="OPS"), task_key="OPS-0002")
    three = SimpleNamespace(project=SimpleNamespace(project_key="WEB"), task_key="WEB-0001")
    assert len({worktree_path(one), worktree_path(two), worktree_path(three)}) == 3
    assert worktree_path(one).parent.name == "OPS"
