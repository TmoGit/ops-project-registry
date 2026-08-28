from app.routing import route_triage
from app.schemas import TriageResult


def _triage(**changes):
    values = {"task_type": "analysis", "complexity": 1, "risk": "low", "estimated_context_tokens": 200, "estimated_files": 1, "requires_host_write": False, "requires_database_change": False, "requires_production_change": False, "parallelizable": False, "recommended_executor": "qwen", "recommended_agents": 1, "clarification_required": False, "clarification_questions": [], "reason": "safe"}
    values.update(changes)
    return TriageResult(**values)


def test_low_risk_analysis_can_use_local_read_only_mode():
    assert route_triage(_triage())["executor"] == "local"


def test_writes_and_large_requests_are_always_routed_to_codex():
    assert route_triage(_triage(requires_host_write=True))["executor"] == "codex"
    assert route_triage(_triage(estimated_files=9))["executor"] == "codex"
