from fastapi.testclient import TestClient

from incidentlab import simulator
from incidentlab.app import app


def _healthy_services():
    return {
        "auth": {"service": "auth", "healthy": True},
        "order": {"service": "order", "healthy": True},
        "inventory": {"service": "inventory", "healthy": True},
        "payment": {"service": "payment", "healthy": True},
        "booking-api": {
            "service": "booking-api",
            "healthy": False,
            "error_rate": 0.42,
            "latency_ms": 2800,
            "db_pool_exhausted": True,
        },
    }


def test_lab_fault_injection_survives_opsswarm_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(simulator, "RUNS", tmp_path)
    monkeypatch.setattr(simulator, "_inject_service", lambda scenario: None)
    monkeypatch.setattr(simulator, "_services", _healthy_services)
    monkeypatch.setattr(
        simulator,
        "_deliver_monitoring",
        lambda payload: {"delivered": False, "error": "OpsSwarm unavailable"},
    )

    run = simulator.start_demo("booking-api-high-5xx")

    assert run["state"] == "DETECTED"
    assert run["monitoring_delivery"]["delivered"] is False
    assert (tmp_path / f"{run['run_id']}.json").exists()


def test_alertmanager_and_direct_delivery_share_correlation(monkeypatch):
    active = {
        "run_id": "RUN-LAB-1",
        "incident_id": "INC-LAB-1",
        "scenario_id": "booking-api-high-5xx",
        "service": "booking-api",
        "evidence": [],
    }
    captured = []
    monkeypatch.setattr(simulator, "latest", lambda: active)
    monkeypatch.setattr(simulator, "_services", _healthy_services)
    monkeypatch.setattr(simulator, "_deliver_monitoring", lambda payload: captured.append(payload) or {"delivered": True})

    direct = simulator.monitoring_payload(active, simulator.SCENARIOS[active["scenario_id"]], _healthy_services())
    client = TestClient(app)
    response = client.post(
        "/api/v1/alertmanager",
        json={
            "alerts": [{
                "status": "firing",
                "labels": {"service": "booking-api", "severity": "critical", "alertname": "DemoMartHighErrorRate"},
                "annotations": {"description": "booking-api elevated 5xx"},
                "startsAt": "2026-09-24T00:00:00Z",
            }]
        },
    )

    assert response.status_code == 200
    assert captured[0]["correlation_key"] == direct["correlation_key"]


def test_recovery_requires_control_token(monkeypatch):
    monkeypatch.setenv("INCIDENTLAB_CONTROL_TOKEN", "unit-secret")
    monkeypatch.setattr(simulator, "latest", lambda: {"service": "booking-api", "timeline": []})
    monkeypatch.setattr(simulator, "_write", lambda run: None)
    monkeypatch.setattr(simulator.tools, "set_version", lambda *args, **kwargs: None)
    monkeypatch.setattr(simulator, "_clear_service_faults", lambda service: None)
    monkeypatch.setattr(simulator.tools, "getm", lambda service: {"service": service, "healthy": True, "status": "HEALTHY"})
    client = TestClient(app)

    denied = client.post("/api/recovery/rollback")
    allowed = client.post("/api/recovery/rollback", headers={"Authorization": "Bearer unit-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["action"] == "rollback"


def test_root_health_declares_no_embedded_opsswarm():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["opsswarm_embedded"] is False
