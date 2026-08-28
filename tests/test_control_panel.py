import httpx

from app.models import Attachment, IntakeSession, IntakeStatus, Task, TaskStatus
from app.schemas import TriageResult


def login(client):
    response = client.post("/login", data={"password": "test-admin-password"})
    assert response.status_code == 200


def safe_triage(*args, **kwargs):
    return TriageResult(task_type="analysis", complexity=1, risk="low", estimated_context_tokens=100, estimated_files=1, requires_host_write=False, requires_database_change=False, requires_production_change=False, parallelizable=False, recommended_executor="qwen", recommended_agents=1, clarification_required=False, clarification_questions=[], reason="safe")


def test_auth_protects_ui_and_api_but_not_health(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").url.path == "/login"
    assert client.post("/api/projects/intake", json={"raw_request": "x"}).status_code == 401
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    login(client)
    assert client.get("/").status_code == 200


def test_intake_attachment_is_sanitized_stored_and_only_linked_on_approval(client, session, monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.triage", safe_triage)
    monkeypatch.setenv("OPS_ATTACHMENTS_PATH", str(tmp_path / "attachments"))
    from app.config import get_settings
    get_settings.cache_clear()
    login(client)
    response = client.post("/api/projects/intake", data={"raw_request": "Use the attached notes"}, files=[("attachments", ("notes.md", b"safe notes", "text/markdown"))])
    assert response.status_code == 201
    intake = session.get(IntakeSession, response.json()["id"])
    attachment = session.query(Attachment).one()
    assert attachment.task_id is None
    assert (tmp_path / "attachments" / attachment.stored_name).read_bytes() == b"safe notes"
    monkeypatch.setattr("app.services.checkpoint", lambda *args: None)
    approved = client.post(f"/api/intakes/{intake.id}/approve", json={"project_key": "OPS", "project_name": "Operations", "task_title": "Notes", "task_description": "Read notes"})
    assert approved.status_code == 200
    session.expire_all()
    assert session.get(Attachment, attachment.id).task_id == int(approved.json()["task_id"])
    script = client.post("/api/projects/intake", data={"raw_request": "keep the script as a non-executable attachment"}, files=[("attachments", ("run.sh", b"echo nope", "text/plain"))])
    assert script.status_code == 201
    stored_script = session.query(Attachment).filter_by(intake_id=script.json()["id"]).one()
    assert (tmp_path / "attachments" / stored_script.stored_name).stat().st_mode & 0o777 == 0o600
    assert stored_script.task_id is None


def test_automatic_triage_persists_a_read_only_local_route(client, monkeypatch):
    monkeypatch.setattr("app.main.triage", safe_triage)
    login(client)
    response = client.post("/api/projects/intake", json={"raw_request": "Analyze the documentation"})
    assert response.status_code == 201
    assert response.json()["triage"]["routing"]["mode"] == "LOCAL_ANALYSIS_ONLY"


def test_reject_and_approve_create_expected_lifecycle(client, session, monkeypatch):
    login(client)
    monkeypatch.setattr("app.services.checkpoint", lambda *args: None)
    rejected = IntakeSession(raw_request="reject", status=IntakeStatus.AWAITING_APPROVAL)
    approved = IntakeSession(raw_request="approve", status=IntakeStatus.AWAITING_APPROVAL)
    session.add_all([rejected, approved]); session.commit()
    assert client.post(f"/api/intakes/{rejected.id}/reject").json() == {"status": "rejected"}
    response = client.post(f"/api/intakes/{approved.id}/approve", json={"project_key": "OPS", "project_name": "Operations", "task_title": "Task", "task_description": "Do it"})
    assert response.status_code == 200
    task = session.get(Task, int(response.json()["task_id"]))
    assert task.status is TaskStatus.COMMITTED


def test_ollama_client_failure_is_reported(monkeypatch):
    from app.ollama import list_models
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr("app.ollama.httpx.get", unavailable)
    result = list_models()
    assert result["available"] is False
    assert "Ollama unavailable" in result["error"]
