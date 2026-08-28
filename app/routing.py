"""Conservative policy for selecting an approved execution mode."""

from app.schemas import TriageResult


LOCAL_ANALYSIS_TYPES = ("analysis", "plan", "research", "review", "document")


def route_triage(result: TriageResult) -> dict[str, str]:
    """Return an advisory route. Local mode is strictly read-only analysis."""
    task_type = result.task_type.lower()
    guarded = (
        result.clarification_required
        or result.risk in {"high", "critical"}
        or result.requires_host_write
        or result.requires_database_change
        or result.requires_production_change
        or result.complexity >= 4
        or result.estimated_context_tokens > 16_000
        or result.estimated_files > 8
    )
    if guarded:
        return {"executor": "codex", "mode": "CODEX_REVIEW_REQUIRED", "reason": "Codex/manual review is required because the request is ambiguous, high-risk, production-affecting, or large."}
    if result.risk == "low" and result.recommended_executor == "qwen" and any(word in task_type for word in LOCAL_ANALYSIS_TYPES):
        return {"executor": "local", "mode": "LOCAL_ANALYSIS_ONLY", "reason": "The local model may produce a read-only analysis or plan. It cannot modify repositories, hosts, databases, or production."}
    return {"executor": "codex", "mode": "CODEX_REVIEW_REQUIRED", "reason": "Codex is recommended because this request is outside the narrow low-risk local analysis/planning policy."}
