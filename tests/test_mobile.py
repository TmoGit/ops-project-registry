from app.models import IntakeSession, IntakeStatus


def test_mobile_approval_uses_complete_proposed_record(client, session, monkeypatch):
    monkeypatch.setenv("OPS_MOBILE_BRIDGE_SECRET", "test-secret")
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr("app.services.checkpoint", lambda *args: None)
    intake = IntakeSession(raw_request="add a status page", status=IntakeStatus.AWAITING_APPROVAL, proposed_record={"project_key": "OPS", "project_name": "Operations", "task_title": "Status page", "task_description": "Build it"})
    session.add(intake); session.commit()
    response = client.post(f"/api/mobile-actions/OPS_APPROVE_INTAKE_{intake.id}", headers={"X-Ops-Bridge-Secret": "test-secret"})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    session.expire_all()
    assert session.get(IntakeSession, intake.id).status is IntakeStatus.APPROVED


def test_mobile_reject_requires_valid_secret(client, session, monkeypatch):
    monkeypatch.setenv("OPS_MOBILE_BRIDGE_SECRET", "test-secret")
    from app.config import get_settings
    get_settings.cache_clear()
    intake = IntakeSession(raw_request="no", status=IntakeStatus.AWAITING_APPROVAL)
    session.add(intake); session.commit()
    assert client.post(f"/api/mobile-actions/OPS_REJECT_INTAKE_{intake.id}").status_code == 403
    assert client.post(f"/api/mobile-actions/OPS_REJECT_INTAKE_{intake.id}", headers={"X-Ops-Bridge-Secret": "test-secret"}).json() == {"status": "rejected"}
