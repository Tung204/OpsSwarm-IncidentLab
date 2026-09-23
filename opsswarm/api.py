from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .commands import parse_command
from .config import load_config
from .github_client import GitHubClient
from .monitoring import (
    MonitoringIssueRegistry,
    ProcessedCommentRegistry,
    build_issue_body,
    deduplication_key,
    incident_correlation_key,
    is_monitoring_issue,
    log_event,
)
from .openclaw import OpenClawClient
from .orchestrator import Orchestrator
from .webhook import verify_signature

cfg = load_config()
data_dir = os.environ.get("OPSWARM_DATA_DIR") or os.environ.get("OPSSWARM_DATA_DIR") or "runtime-data"
gh = GitHubClient(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPO", cfg.get("repo", "")))
oc = OpenClawClient(
    os.environ.get("OPSWARM_OPENCLAW_GATEWAY_URL") or os.environ.get("OPSSWARM_OPENCLAW_GATEWAY_URL") or "http://127.0.0.1:18789",
    os.environ.get("OPENCLAW_GATEWAY_TOKEN", ""),
    int(os.environ.get("OPSWARM_OPENCLAW_TIMEOUT", cfg.get("openclaw", {}).get("timeout_seconds", 600))),
)
engine = Orchestrator(cfg, gh, oc, data_dir)
issue_registry = MonitoringIssueRegistry(data_dir)
comment_registry = ProcessedCommentRegistry(data_dir)
monitoring_lock = asyncio.Lock()
comment_lock = asyncio.Lock()
orchestration_inflight: set[int] = set()
comment_inflight: set[int] = set()
@asynccontextmanager
async def lifespan(app: FastAPI):
    if gh.repo and os.environ.get("GITHUB_TOKEN") and float(
        os.environ.get("OPSSWARM_GITHUB_POLL_SECONDS")
        or os.environ.get("OPSWARM_GITHUB_POLL_SECONDS")
        or "5"
    ) > 0:
        app.state.github_watcher = _spawn(_github_issue_watcher())
    else:
        log_event(
            "GITHUB_POLLER_DISABLED",
            repo=gh.repo,
            token_configured=bool(os.environ.get("GITHUB_TOKEN")),
        )
    try:
        yield
    finally:
        watcher = getattr(app.state, "github_watcher", None)
        if watcher:
            watcher.cancel()
        await gh.client.aclose()
        await oc.client.aclose()


app = FastAPI(title="OpsSwarm + IncidentLab", version="2.1.0-lab", lifespan=lifespan)

from .lab_router import router as lab_router

app.include_router(lab_router)


def _spawn(coro):
    task = asyncio.create_task(coro)

    def done(t: asyncio.Task):
        if t.cancelled():
            return
        try:
            t.result()
        except Exception as exc:
            log_event("BACKGROUND_TASK_FAILED", error_type=type(exc).__name__, error=str(exc))

    task.add_done_callback(done)
    return task


async def _start_orchestration(number: int, trigger: str) -> None:
    try:
        log_event("ORCHESTRATION_STARTED", issue_number=number, trigger=trigger)
        run = await engine.start_issue(number)
        log_event(
            "ORCHESTRATION_CHECKPOINT",
            issue_number=number,
            trigger=trigger,
            run_id=run.run_id,
            state=run.state.value,
            incident_id=getattr(run.incident, "incident_id", None) if run.incident else None,
            scenario_id=getattr(run.incident, "scenario_id", None) if run.incident else None,
        )
    except Exception as exc:
        log_event(
            "ORCHESTRATION_BLOCKED",
            issue_number=number,
            trigger=trigger,
            error_type=type(exc).__name__,
            error=str(exc),
        )


async def _handle_github_comment(number: int, comment: dict[str, Any], trigger: str) -> bool:
    comment_id = int(comment.get("id") or 0)
    actor = (comment.get("user") or {}).get("login") or "unknown"
    body = comment.get("body") or ""
    command = parse_command(body)
    # Polling fallback intentionally processes authoritative /opsswarm commands only.
    # Normal free-text context remains event-driven through the signed webhook.
    if trigger == "poll" and not command:
        return False
    async with comment_lock:
        if comment_id and comment_registry.contains(comment_id):
            log_event("GITHUB_COMMENT_DEDUPLICATED", issue_number=number, comment_id=comment_id, trigger=trigger)
            return False
        log_event(
            "GITHUB_COMMENT_PROCESSING",
            issue_number=number,
            comment_id=comment_id,
            actor=actor,
            trigger=trigger,
            command=command.name if command else None,
        )
        try:
            permission = await gh.permission(actor)
            await engine.handle_comment(number, actor, body, permission, command)
        except PermissionError as exc:
            log_event("GITHUB_COMMAND_REJECTED", issue_number=number, actor=actor, comment_id=comment_id, error=str(exc))
            await gh.comment(number, f"OpsSwarm command rejected: {exc}")
        except Exception as exc:
            log_event("GITHUB_COMMAND_FAILED", issue_number=number, actor=actor, comment_id=comment_id, error_type=type(exc).__name__, error=str(exc))
            await gh.comment(number, f"OpsSwarm could not process the command: `{type(exc).__name__}: {exc}`")
        finally:
            if comment_id:
                comment_registry.add(comment_id)
        return True


async def _run_orchestration_background(number: int, trigger: str) -> None:
    try:
        await _start_orchestration(number, trigger)
    finally:
        orchestration_inflight.discard(number)


def _queue_orchestration(number: int, trigger: str) -> bool:
    if number in engine.runs or number in orchestration_inflight:
        return False
    orchestration_inflight.add(number)
    log_event("ORCHESTRATION_QUEUED", issue_number=number, trigger=trigger)
    _spawn(_run_orchestration_background(number, trigger))
    return True


async def _run_comment_background(number: int, comment: dict[str, Any], trigger: str, comment_id: int) -> None:
    try:
        await _handle_github_comment(number, comment, trigger)
    finally:
        if comment_id:
            comment_inflight.discard(comment_id)


def _queue_polled_comment(number: int, comment: dict[str, Any]) -> bool:
    comment_id = int(comment.get("id") or 0)
    body = comment.get("body") or ""
    # Polling is an authoritative-command fallback only; signed webhooks remain
    # the event-driven path for ordinary free-text information.
    if not parse_command(body):
        return False
    if comment_id and (comment_registry.contains(comment_id) or comment_id in comment_inflight):
        return False
    if comment_id:
        comment_inflight.add(comment_id)
    log_event("GITHUB_COMMENT_QUEUED", issue_number=number, comment_id=comment_id, trigger="poll")
    _spawn(_run_comment_background(number, comment, "poll", comment_id))
    return True


async def _poll_issue_comments(number: int) -> None:
    try:
        comments = await gh.list_issue_comments(number, per_page=100)
        for comment in comments or []:
            _queue_polled_comment(number, comment)
    except Exception as exc:
        log_event("GITHUB_COMMENT_POLLER_WARNING", issue_number=number, error_type=type(exc).__name__, error=str(exc))


async def _github_issue_watcher() -> None:
    poll = max(1.0, float(os.environ.get("OPSSWARM_GITHUB_POLL_SECONDS") or os.environ.get("OPSWARM_GITHUB_POLL_SECONDS") or "5"))
    required = cfg.get("required_issue_label", "opsswarm")
    log_event("GITHUB_POLLER_STARTED", poll_seconds=poll, repo=gh.repo)
    while True:
        try:
            issues = await gh.list_issues("all", None, per_page=50)
            for issue in issues or []:
                if issue.get("pull_request") or not is_monitoring_issue(issue):
                    continue
                number = int(issue["number"])
                run = engine.runs.get(number)
                issue_state = str(issue.get("state") or "open").lower()

                # GitHub comments are the authority channel. A UI/manual close is not
                # equivalent to /opsswarm abort and must not make an unresolved
                # incident disappear from the system of record. Reconcile that drift.
                if issue_state == "closed" and run is not None and run.state.value != "RESOLVED":
                    try:
                        await gh.reopen_issue(number)
                        issue_state = "open"
                        log_event(
                            "ISSUE_REOPENED_ACTIVE_RUN",
                            issue_number=number,
                            run_id=run.run_id,
                            run_state=run.state.value,
                            prior_state_reason=issue.get("state_reason"),
                        )
                    except Exception as exc:
                        log_event(
                            "ISSUE_STATE_RECONCILIATION_WARNING",
                            issue_number=number,
                            run_id=run.run_id,
                            run_state=run.state.value,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )

                # Never start a brand-new orchestration from an already-closed Issue.
                # Existing persisted runs may still consume explicit commands so they
                # can recover/reconcile safely after a process restart.
                if issue_state == "open":
                    _queue_orchestration(number, "poll")
                if number in engine.runs or number in orchestration_inflight:
                    await _poll_issue_comments(number)
        except Exception as exc:
            log_event("GITHUB_POLLER_WARNING", error_type=type(exc).__name__, error=str(exc))
        await asyncio.sleep(poll)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "2.1.0-lab",
        "architecture": "github-system-of-record+openclaw-gateway",
        "repo": gh.repo,
        "openclaw": await oc.health(),
    }




@app.get("/metrics")
async def opsswarm_metrics():
    from fastapi.responses import PlainTextResponse
    body = "# HELP opsswarm_up OpsSwarm API availability.\n# TYPE opsswarm_up gauge\nopsswarm_up 1\n"
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@app.post("/api/v1/alertmanager")
async def alertmanager_webhook(payload: dict[str, Any]):
    """Bridge firing Alertmanager events into the canonical monitoring ingress.

    When the alert corresponds to the active IncidentLab run, reuse that run's
    correlation identity so the direct IncidentLab event and Prometheus path are
    idempotent and converge on one GitHub Issue.
    """
    from .incidentlab import latest as latest_lab_run
    from . import tool_adapter as tools

    results = []
    active = latest_lab_run()
    for alert in payload.get("alerts", []) or []:
        if alert.get("status") != "firing":
            log_event("ALERTMANAGER_RESOLVED", fingerprint=alert.get("fingerprint"))
            continue
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        service = labels.get("service") or "unknown"
        event: dict[str, Any] = {
            "title": f"[Monitoring][{labels.get('severity','warning')}] {service} - {labels.get('alertname','alert')}",
            "service": service,
            "environment": "incidentlab",
            "symptom": annotations.get("description") or annotations.get("summary") or labels.get("alertname") or "Prometheus alert",
            "customer_impact": "Monitoring alert generated from IncidentLab simulated service state",
            "observed_since": alert.get("startsAt") or payload.get("externalURL") or "unknown",
            "severity": "SEV1" if labels.get("severity") == "critical" else "SEV2",
            "severity_label": "sev:1" if labels.get("severity") == "critical" else "sev:2",
            "source": "prometheus-alertmanager",
            "fault_type": labels.get("alertname") or "monitoring-alert",
        }
        if active and active.get("service") == service and active.get("state") not in {"RESOLVED", "STOPPED"}:
            scenario_id = str(active.get("scenario_id") or "unknown").strip().lower()
            canonical_key = f"incidentlab:{str(service).strip().lower()}:{scenario_id}"
            event.update({
                "run_id": active.get("run_id"),
                "incident_id": active.get("incident_id"),
                "scenario_id": active.get("scenario_id"),
                "correlation_key": canonical_key,
                "deduplication_key": canonical_key,
                "incidentlab_reference": f"{os.environ.get('INCIDENTLAB_PUBLIC_URL','http://localhost:8080').rstrip('/')}/api/incidents/{active.get('incident_id')}",
            })
        else:
            event["deduplication_key"] = f"alertmanager:{alert.get('fingerprint') or service + ':' + str(labels.get('alertname'))}"
        try:
            event["metrics"] = tools.getm(service) if service in tools.URLS else {}
        except Exception as exc:
            event["initial_evidence"] = [{"source": "metrics", "status": "unavailable", "error": str(exc)}]
        result = await monitoring_event(event)
        results.append(result)
        log_event("ALERTMANAGER_MONITORING_INGRESS", service=service, issue_number=result.get("issue_number"), deduplicated=result.get("deduplicated"))
    return {"accepted": True, "results": results}


@app.get("/runs")
async def runs():
    return [r.model_dump(mode="json") for r in engine.runs.values()]


@app.get("/runs/{issue_number}")
async def run(issue_number: int):
    r = engine.runs.get(issue_number)
    if not r:
        raise HTTPException(404, "No run for issue")
    return r.model_dump(mode="json")


@app.get("/runs/{issue_number}/evidence")
async def evidence(issue_number: int):
    r = engine.runs.get(issue_number)
    if not r:
        raise HTTPException(404, "No run for issue")
    return engine.ev.list(r.run_id)


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
):
    body = await request.body()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if secret and not verify_signature(secret, body, x_hub_signature_256):
        log_event("GITHUB_WEBHOOK_REJECTED", github_event=x_github_event, reason="invalid_signature")
        raise HTTPException(401, "Invalid webhook signature")
    data = await request.json()
    if x_github_event == "issues" and data.get("action") == "opened":
        number = int(data["issue"]["number"])
        log_event("GITHUB_WEBHOOK_RECEIVED", github_event="issues.opened", issue_number=number)
        _spawn(_start_orchestration(number, "webhook"))
        return {"accepted": True, "issue": number, "trigger": "webhook"}
    if x_github_event == "issue_comment" and data.get("action") == "created":
        number = int(data["issue"]["number"])
        comment = data["comment"]
        actor = (comment.get("user") or {}).get("login") or "unknown"
        log_event("GITHUB_WEBHOOK_RECEIVED", github_event="issue_comment.created", issue_number=number, actor=actor, comment_id=comment.get("id"))
        processed = await _handle_github_comment(number, comment, "webhook")
        return {"accepted": True, "processed": processed}
    return {"ignored": True}


def _canonical_incident_title(payload: dict[str, Any]) -> str:
    service = str(payload.get("service") or "unknown-service").strip()
    scenario = str(payload.get("scenario_id") or payload.get("fault_type") or "unknown-scenario").strip()
    severity = str(payload.get("severity") or "SEV2").strip().upper()
    if not severity.startswith("SEV"):
        severity = "SEV2"
    return f"[Incident][{severity}] {service} - {scenario}"


def _severity_label(payload: dict[str, Any]) -> str:
    value = str(payload.get("severity_label") or payload.get("severity") or "SEV2").strip().lower()
    if value.startswith("sev:"):
        return value
    if value.startswith("sev") and value[3:].isdigit():
        return f"sev:{value[3:]}"
    return "sev:2"


@app.post("/hooks/monitoring")
async def monitoring_event(payload: dict[str, Any]):
    correlation_key = incident_correlation_key(payload)
    key = deduplication_key({**payload, "correlation_key": correlation_key})
    incident_id = payload.get("incident_id")
    incidentlab_run_id = payload.get("run_id")
    scenario_id = payload.get("scenario_id")
    async with monitoring_lock:
        # Correlate repeated alerts/runs for the same active service+scenario into one Issue.
        existing = issue_registry.find_active(correlation_key) or issue_registry.get(key)
        if existing and str(existing.get("status") or "ACTIVE").upper() == "ACTIVE":
            # Registry state is durable but GitHub is authoritative. A crash can occur
            # between closing an Issue and marking the registry RESOLVED, so reconcile
            # the GitHub state before reusing the Issue.
            try:
                current_issue = await gh.get_issue(int(existing.get("issue_number")))
                if str(current_issue.get("state") or "open").lower() == "closed":
                    issue_registry.mark_status(int(existing.get("issue_number")), "RESOLVED")
                    existing = None
            except Exception as exc:
                log_event("ISSUE_REUSE_STATE_CHECK_WARNING", issue_number=existing.get("issue_number"), error_type=type(exc).__name__, error=str(exc))
            if existing:
                log_event(
                "ISSUE_DEDUPLICATED",
                deduplication_key=key,
                incident_id=incident_id,
                incidentlab_run_id=incidentlab_run_id,
                scenario_id=scenario_id,
                issue_number=existing.get("issue_number"),
            )
            return {"accepted": True, "deduplicated": True, **existing}

        title = _canonical_incident_title(payload)
        body = build_issue_body(payload, key)
        issue = await gh.create_issue(title, body)
        number = int(issue["number"])
        record = {
            "issue_number": number,
            "issue_url": issue.get("html_url"),
            "deduplication_key": key,
            "correlation_key": correlation_key,
            "incident_id": incident_id,
            "incidentlab_run_id": incidentlab_run_id,
            "scenario_id": scenario_id,
            "orchestration_status": "WAITING_FOR_GITHUB_WEBHOOK_OR_POLL",
            "status": "ACTIVE",
            "issue_type": "incident",
        }
        # Persist immediately after Issue creation. Label failures cannot lose the mapping.
        issue_registry.put(key, record)
        log_event("ISSUE_CREATED", **record)

        labels = list(dict.fromkeys(cfg.get("labels", {}).get("base", ["opsswarm", "incident"]) + [_severity_label(payload)]))
        try:
            if labels:
                await gh.replace_labels(number, labels)
                log_event("ISSUE_LABELS_SYNCED", issue_number=number, labels=labels, incident_id=incident_id, incidentlab_run_id=incidentlab_run_id)
        except Exception as exc:
            log_event(
                "ISSUE_LABEL_WARNING",
                issue_number=number,
                labels=labels,
                incident_id=incident_id,
                incidentlab_run_id=incidentlab_run_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            try:
                await gh.comment(number, "OpsSwarm created this incident automatically. Label synchronization failed; orchestration will continue through the GitHub webhook/poller because the Issue carries a trusted monitoring marker.")
            except Exception as comment_exc:
                log_event("ISSUE_LABEL_COMMENT_WARNING", issue_number=number, error_type=type(comment_exc).__name__, error=str(comment_exc))

        # Deliberately do NOT call engine.start_issue here. GitHub is the system of record;
        # orchestration starts only from issues.opened webhook or the polling fallback.
        return {"accepted": True, "deduplicated": False, **record}
