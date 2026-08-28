import json

import httpx

from app.triage import triage


def test_triage_retries_invalid_response(monkeypatch):
    attempts = []
    valid = {"task_type": "code", "complexity": 1, "risk": "low", "estimated_context_tokens": 1, "estimated_files": 1, "requires_host_write": False, "requires_database_change": False, "requires_production_change": False, "parallelizable": False, "recommended_executor": "qwen", "recommended_agents": 1, "clarification_required": False, "clarification_questions": [], "reason": "small"}
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return {"response": self.value}
    def post(*args, **kwargs):
        attempts.append(kwargs["json"])
        return Response("not-json" if len(attempts) == 1 else json.dumps(valid))
    monkeypatch.setattr("app.triage.httpx.post", post)
    assert triage("fix it").recommended_executor == "qwen"
    assert len(attempts) == 2
    assert "prior response failed validation" in attempts[1]["prompt"]
