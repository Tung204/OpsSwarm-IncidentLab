# OpsSwarm IncidentLab v1.2

IncidentLab is an **independent, stateful target-system simulator** for OpsSwarm Enterprise. It does not contain or vendor the OpsSwarm control plane, S1-S8 orchestration, policy engine, GitHub authority, or OpenClaw workspaces.

## Ownership boundary

IncidentLab owns:

- simulated services and deployment state;
- stateful fault injection and reset;
- service/metrics/dependency/evidence APIs;
- Prometheus + Alertmanager demo telemetry;
- normalized monitoring delivery to an external OpsSwarm Enterprise instance;
- authenticated recovery endpoints that an authorized Enterprise executor can call.

OpsSwarm Enterprise owns:

- GitHub Issue creation/correlation and human commands;
- TaskGraph/orchestration and OpenClaw agents;
- RCA and action proposals;
- capability/risk canonicalization and deterministic policy;
- approval/decision gates;
- write execution ordering and ambiguity handling;
- independent S7 verification and final Issue closure.

```text
IncidentLab fault / Alertmanager
        |
        v
normalized monitoring event
        |
        v
OpsSwarm-Enterprise /hooks/monitoring
        |
        v
GitHub Issue -> investigation -> RCA -> action proposal
        |
        v
Capability Gate -> deterministic Policy -> HUMAN/AUTO
        |
        v
Enterprise Action Executor -> IncidentLab authenticated recovery API
        |
        v
independent S7 reads IncidentLab -> RESOLVED only when verified
```

IncidentLab intentionally remains usable when OpsSwarm Enterprise is unavailable. Fault injection and local evidence still work; monitoring delivery is recorded as unavailable and **does not fall back to embedded orchestration**.

## Start the simulator

Copy `.env.example` to `.env` and set a strong `INCIDENTLAB_CONTROL_TOKEN`. The same secret must be available only to the trusted Enterprise action executor.

```powershell
.\scripts\demo-start.ps1
```

Open the UI at `http://localhost:8080/api/ui`.

Reset or stop:

```powershell
.\scripts\demo-reset.ps1
.\scripts\demo-stop.ps1
```

## External Enterprise integration

By default the Dockerized Lab sends normalized monitoring events to:

```text
http://host.docker.internal:18088/hooks/monitoring
```

Override it with `INCIDENTLAB_MONITORING_URL` when Enterprise runs elsewhere.

The same incident identity uses the canonical correlation key:

```text
incidentlab:<service>:<scenario-id>
```

Direct scenario delivery and Alertmanager delivery therefore converge on the same Enterprise/GitHub incident rather than creating duplicate Issues.

## Recovery boundary

These write endpoints require `Authorization: Bearer <INCIDENTLAB_CONTROL_TOKEN>`:

```text
POST /api/recovery/restart
POST /api/recovery/rollback
POST /api/recovery/scale
```

Missing token configuration fails closed with HTTP 503; a missing/wrong bearer token returns HTTP 401. IncidentLab has no approval-authority endpoint. Legacy `/api/demo/approve` returns HTTP 410.

Read/evidence endpoints include:

```text
GET /health
GET /api/services
GET /api/services/{name}
GET /api/metrics
GET /api/logs
GET /api/events
GET /api/dependencies
GET /api/state
GET /api/scenarios
GET /api/evidence
GET /api/incidents/{id}
GET /api/incidents/{id}/timeline
```

## Run a fault without Enterprise

```powershell
.\scripts\demo-run.ps1 -Scenario booking-api-high-5xx
```

If Enterprise is down, the injected fault remains active and the script reports monitoring delivery as unavailable. This is an expected boundary test, not an implicit fallback.

## True two-repository E2E

Start OpsSwarm Enterprise separately (the local validated setup uses `127.0.0.1:18088`), then run:

```powershell
.\scripts\demo-e2e.ps1 -Scenario booking-api-high-5xx -EnterpriseUrl http://127.0.0.1:18088
```

To exercise the explicit GitHub human gate:

```powershell
.\scripts\demo-e2e.ps1 -Scenario booking-api-high-5xx -Approve -EnterpriseUrl http://127.0.0.1:18088 -GitHubRepo OWNER/REPO
```

`-Approve` posts only the exact `/opsswarm approve <option-id>` command to GitHub. It never calls a hidden Lab approval path. A terminal `FAILED`/`ABORTED` or an approval run that does not reach `RESOLVED` exits non-zero.

## Scenarios

- `booking-api-high-5xx`
- `latency-spike`
- `database-pool-exhaustion`
- `dependency-timeout`
- `failed-deployment`
- `partial-network-failure`

## Tests

```powershell
uv sync --extra dev
.\.venv\Scripts\python.exe -m pytest -q
docker compose config -q
```

The boundary tests verify that the Lab runs without embedded OpsSwarm, survives Enterprise unavailability, converges monitoring transports on one correlation identity, and rejects unauthenticated recovery writes.
