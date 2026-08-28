from app.models import AuditEvent, IntegrationConfiguration, IntegrationStatus


def login(client):
    assert client.post("/login", data={"password": "test-admin-password"}).status_code == 200


def test_integrations_are_private_and_manage_references_only(client, session):
    assert client.get("/api/integrations").status_code == 401
    login(client)
    response = client.put("/api/integrations/GITHUB", json={"configuration_json": {"base_url": "https://github.com", "organization": "ops"}, "credential_source": "ENVIRONMENT", "credential_reference": "GITHUB_TOKEN"})
    assert response.status_code == 200
    body = response.json()
    assert body["credential_reference"] == "GITHUB_TOKEN"
    assert "token" not in body["configuration"]
    assert client.put("/api/integrations/GITHUB", json={"configuration_json": {"api_key": "not-allowed"}}).status_code == 422
    event = session.query(AuditEvent).filter_by(action="INTEGRATION_UPDATED").one()
    assert "not-allowed" not in str(event.new_value)


def test_enable_disable_and_failed_test_are_recorded(client, session):
    login(client)
    client.put("/api/integrations/AWS", json={"configuration_json": {"region": "us-east-1"}, "credential_source": "AWS_PROFILE", "credential_reference": "ops"})
    assert client.post("/api/integrations/AWS/disable").json()["status"] == "DISABLED"
    assert client.post("/api/integrations/AWS/enable").json()["enabled"] is True
    tested = client.post("/api/integrations/AWS/test")
    assert tested.status_code == 200
    item = session.query(IntegrationConfiguration).one()
    assert item.last_test_status in {"CONNECTED", "ERROR"}
    assert item.status in {IntegrationStatus.CONNECTED, IntegrationStatus.ERROR}


def test_updates_can_clear_credential_reference_without_erasing_configuration(client):
    login(client)
    created = client.put("/api/integrations/GITHUB", json={
        "configuration_json": {"base_url": "https://github.com"},
        "credential_source": "ENVIRONMENT",
        "credential_reference": "GITHUB_TOKEN",
    })
    assert created.status_code == 200

    updated = client.put("/api/integrations/GITHUB", json={
        "credential_source": None,
        "credential_reference": None,
    })
    assert updated.status_code == 200
    assert updated.json()["configuration"] == {"base_url": "https://github.com"}
    assert updated.json()["credential_source"] is None
    assert updated.json()["credential_reference"] is None
    assert updated.json()["status"] == "NOT_CONFIGURED"
