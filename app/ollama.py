"""Small, read-only Ollama client used by the local admin panel."""
import httpx

from app.config import get_settings


def list_models() -> dict:
    try:
        response = httpx.get(f"{get_settings().ollama_base_url.rstrip('/')}/api/tags", timeout=3)
        response.raise_for_status()
        names = [item["name"] for item in response.json().get("models", []) if isinstance(item.get("name"), str)]
        return {"available": True, "models": names, "error": None}
    except (httpx.HTTPError, ValueError, KeyError) as error:
        return {"available": False, "models": [], "error": f"Ollama unavailable: {error}"}
