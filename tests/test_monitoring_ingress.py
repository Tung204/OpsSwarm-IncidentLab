import asyncio
from pathlib import Path

from opsswarm import api
from opsswarm.monitoring import MONITORING_MARKER, MonitoringIssueRegistry, build_issue_body, deduplication_key, incident_correlation_key
from opsswarm.webhook import verify_signature


class FakeGitHub:
    def __init__(self, fail_labels=False):
        self.fail_labels = fail_labels
        self.created = []
        self.labels = []
        self.comments = []

    async def create_issue(self, title, body, labels=None):
        number = 100 + len(self.created) + 1
        item = {"number": number, "html_url": f"https://example.test/issues/{number}", "title": title, "body": body}
        self.created.append(item)
        return item

    async def set_labels(self, number, labels):
        if self.fail_labels:
            raise PermissionError("simulated label permission failure")
        self.labels.append((number, labels))
        return labels

    async def replace_labels(self, number, labels):
        return await self.set_labels(number, labels)

    async def comment(self, number, body):
        self.comments.append((number, body))
        return {"id": len(self.comments)}


def payload():
    return {
        "title": "[IncidentLab][SEV1] booking-api - booking-api-high-5xx",
        "service": "booking-api",
        "environment": "incidentlab",
        "symptom": "error_rate=0.42 latency_ms=2800",
        "customer_impact": "simulated",
        "observed_since": "2026-09-23T01:00:00Z",
        "severity": "SEV1",
        "severity_label": "sev:1",
        "run_id": "RUN-LAB-test",
        "incident_id": "INC-LAB-test",
        "scenario_id": "booking-api-high-5xx",
        "fault_type": "error_rate,latency_ms",
        "deduplication_key": "incidentlab:INC-LAB-test",
        "metrics": {"error_rate": 0.42, "latency_ms": 2800},
        "dependencies": {"database": {"status": "DEGRADED"}},
        "initial_evidence": [{"source": "test"}],
    }


def test_issue_body_contains_full_correlation_context():
    p = payload()
    key = deduplication_key(p)
    body = build_issue_body(p, key)
    assert MONITORING_MARKER in body
    for expected in [
        "booking-api", "RUN-LAB-test", "INC-LAB-test", "booking-api-high-5xx",
        "error_rate,latency_ms", "0.42", "2800", key,
        "WAITING_FOR_GITHUB_WEBHOOK_OR_POLL",
    ]:
        assert expected in body


def test_duplicate_event_returns_same_issue(tmp_path, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(api, "gh", fake)
    monkeypatch.setattr(api, "issue_registry", MonitoringIssueRegistry(str(tmp_path)))

    async def run():
        first = await api.monitoring_event(payload())
        second = await api.monitoring_event(payload())
        return first, second

    first, second = asyncio.run(run())
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert first["issue_number"] == second["issue_number"]
    assert len(fake.created) == 1



def test_correlation_is_transport_independent():
    p = payload()
    incidentlab = {**p, "source": "incidentlab", "correlation_key": None}
    alertmanager = {**p, "source": "prometheus-alertmanager", "correlation_key": None}
    assert incident_correlation_key(incidentlab) == incident_correlation_key(alertmanager)


def test_alertmanager_reuses_active_incidentlab_issue(tmp_path, monkeypatch):
    from opsswarm import incidentlab
    from opsswarm import tool_adapter

    fake = FakeGitHub()
    monkeypatch.setattr(api, "gh", fake)
    monkeypatch.setattr(api, "issue_registry", MonitoringIssueRegistry(str(tmp_path)))
    monkeypatch.setattr(
        incidentlab,
        "latest",
        lambda: {
            "run_id": "RUN-LAB-test",
            "incident_id": "INC-LAB-test",
            "scenario_id": "booking-api-high-5xx",
            "service": "booking-api",
            "state": "ISSUE_CREATED",
        },
    )
    monkeypatch.setattr(tool_adapter, "getm", lambda service: {"service": service, "error_rate": 0.42})

    direct = payload()
    direct["correlation_key"] = "incidentlab:booking-api:booking-api-high-5xx"
    direct["deduplication_key"] = direct["correlation_key"]
    first = asyncio.run(api.monitoring_event(direct))
    second = asyncio.run(
        api.alertmanager_webhook(
            {
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {"service": "booking-api", "severity": "critical", "alertname": "High5xx"},
                        "annotations": {"description": "booking-api elevated 5xx"},
                        "startsAt": "2026-09-23T01:00:01Z",
                        "fingerprint": "abc123",
                    }
                ]
            }
        )
    )

    assert second["results"][0]["issue_number"] == first["issue_number"]
    assert second["results"][0]["deduplicated"] is True
    assert len(fake.created) == 1


def test_incident_title_is_canonical_and_not_incidentlab_prefixed(tmp_path, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(api, "gh", fake)
    monkeypatch.setattr(api, "issue_registry", MonitoringIssueRegistry(str(tmp_path)))
    result = asyncio.run(api.monitoring_event(payload()))
    assert result["accepted"] is True
    assert fake.created[0]["title"] == "[Incident][SEV1] booking-api - booking-api-high-5xx"


def test_different_runs_same_active_incident_correlate_to_one_issue(tmp_path, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(api, "gh", fake)
    monkeypatch.setattr(api, "issue_registry", MonitoringIssueRegistry(str(tmp_path)))

    first_payload = payload()
    second_payload = {**first_payload, "run_id": "RUN-LAB-second", "incident_id": "INC-LAB-second", "deduplication_key": "incidentlab:INC-LAB-second"}
    first = asyncio.run(api.monitoring_event(first_payload))
    second = asyncio.run(api.monitoring_event(second_payload))

    assert first["issue_number"] == second["issue_number"]
    assert second["deduplicated"] is True
    assert incident_correlation_key(first_payload) == incident_correlation_key(second_payload)
    assert len(fake.created) == 1


def test_resolved_correlation_can_create_a_new_issue(tmp_path):
    registry = MonitoringIssueRegistry(str(tmp_path))
    p = payload()
    key = incident_correlation_key(p)
    registry.put("old-key", {"issue_number": 77, "correlation_key": key, "status": "ACTIVE"})
    assert registry.find_active(key)["issue_number"] == 77
    assert registry.mark_status(77, "RESOLVED") is True
    assert registry.find_active(key) is None

def test_label_failure_does_not_lose_created_issue(tmp_path, monkeypatch):
    fake = FakeGitHub(fail_labels=True)
    monkeypatch.setattr(api, "gh", fake)
    monkeypatch.setattr(api, "issue_registry", MonitoringIssueRegistry(str(tmp_path)))

    result = asyncio.run(api.monitoring_event(payload()))
    assert result["accepted"] is True
    assert result["issue_number"] == 101
    assert len(fake.created) == 1
    assert fake.comments, "label failure should be visible on the Issue when commenting is available"
    persisted = MonitoringIssueRegistry(str(tmp_path)).get(payload()["deduplication_key"])
    assert persisted["issue_number"] == 101


def test_webhook_signature_is_required_when_secret_is_used():
    import hashlib, hmac
    body = b'{"action":"opened"}'
    secret = "unit-secret"
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, good)
    assert not verify_signature(secret, body, None)
    assert not verify_signature(secret, body, "sha256=bad")


def test_polled_command_is_queued_without_blocking_watcher(monkeypatch, tmp_path):
    from opsswarm.monitoring import ProcessedCommentRegistry

    class CommentGitHub:
        async def list_issue_comments(self, number, per_page=100):
            return [{
                "id": 4242,
                "body": "/opsswarm provide observed-fact",
                "user": {"login": "owner"},
            }]

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(number, comment, trigger):
        started.set()
        await release.wait()
        return True

    async def run():
        monkeypatch.setattr(api, "gh", CommentGitHub())
        monkeypatch.setattr(api, "comment_registry", ProcessedCommentRegistry(str(tmp_path)))
        monkeypatch.setattr(api, "_handle_github_comment", slow_handler)
        api.comment_inflight.clear()
        await asyncio.wait_for(api._poll_issue_comments(15), timeout=0.2)
        await asyncio.wait_for(started.wait(), timeout=0.2)
        assert 4242 in api.comment_inflight
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert 4242 not in api.comment_inflight

    asyncio.run(run())


def test_permission_lookup_failure_marks_command_processed(monkeypatch, tmp_path):
    from opsswarm.monitoring import ProcessedCommentRegistry

    class PermissionFailureGitHub:
        def __init__(self):
            self.comments = []

        async def permission(self, username):
            raise RuntimeError("permission lookup unavailable")

        async def comment(self, number, body):
            self.comments.append((number, body))
            return {"id": 1}

    fake = PermissionFailureGitHub()
    registry = ProcessedCommentRegistry(str(tmp_path))
    monkeypatch.setattr(api, "gh", fake)
    monkeypatch.setattr(api, "comment_registry", registry)
    comment = {"id": 5150, "body": "/opsswarm abort", "user": {"login": "reader"}}

    first = asyncio.run(api._handle_github_comment(40, comment, "poll"))
    second = asyncio.run(api._handle_github_comment(40, comment, "poll"))

    assert first is True
    assert second is False
    assert registry.contains(5150)
    assert len(fake.comments) == 1


def test_github_client_rejects_invalid_issue_state_without_network():
    from opsswarm.github_client import GitHubClient
    client = GitHubClient("token", "owner/repo", base_url="https://example.invalid")
    async def run():
        try:
            try:
                await client.list_issues("invalid")
                assert False, "invalid state should fail before a request is made"
            except ValueError as exc:
                assert "unsupported issue state" in str(exc)
        finally:
            await client.client.aclose()
    asyncio.run(run())
