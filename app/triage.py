import json

import httpx

from app.config import get_settings
from app.schemas import TriageResult


PROMPT = """You are a task-intake classifier. Return only a JSON object matching this schema:
{task_type:string,complexity:integer 1-5,risk:low|medium|high|critical,
estimated_context_tokens:integer,estimated_files:integer,requires_host_write:boolean,
requires_database_change:boolean,requires_production_change:boolean,parallelizable:boolean,
recommended_executor:qwen|codex,recommended_agents:integer,clarification_required:boolean,
clarification_questions:array of strings,reason:string}.
Do not approve execution. Be conservative: production or infrastructure changes are high risk.
Request:\n"""


def triage(raw_request: str, model: str | None = None) -> TriageResult:
    settings = get_settings()
    request = {"model": model or settings.default_local_model, "prompt": PROMPT + raw_request, "stream": False, "format": "json"}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = httpx.post(f"{settings.ollama_base_url}/api/generate", json=request, timeout=120)
            response.raise_for_status()
            return TriageResult.model_validate(json.loads(response.json()["response"]))
        except (httpx.HTTPError, KeyError, ValueError) as error:
            last_error = error
            request["prompt"] = PROMPT + raw_request + "\nYour prior response failed validation. Return corrected JSON only. recommended_agents must be an integer of at least 1."
    raise RuntimeError("Triage requires manual review") from last_error
