from __future__ import annotations

import hmac
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import tool_adapter as tools
from .logging import log_event

router = APIRouter(prefix="/api")
DATA = Path(os.getenv("INCIDENTLAB_DATA_DIR", "runtime-data")) / "incidentlab"
RUNS = DATA / "runs"
RUNS.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()

SCENARIOS: dict[str, dict[str, Any]] = {
    "booking-api-high-5xx": {
        "service": "booking-api", "severity": "SEV1", "faults": {"error_rate": 0.42, "latency_ms": 2800, "db_pool_exhausted": True},
        "dependencies": {"database": "DEGRADED"}, "root_cause": "booking-api deployment is returning elevated 5xx responses while database pressure is observed",
        "options": [{"id":"option-001","type":"rollback","risk":"risky_write","label":"Rollback booking-api to last known good version"}]
    },
    "latency-spike": {
        "service": "booking-api", "severity": "SEV2", "faults": {"latency_ms": 2800}, "dependencies": {"database":"HEALTHY"},
        "root_cause": "application latency regression", "options":[{"id":"option-001","type":"restart","risk":"safe_write","label":"Restart booking-api"}]
    },
    "database-pool-exhaustion": {
        "service": "booking-api", "severity": "SEV1", "faults": {"db_pool_exhausted": True, "error_rate":0.31, "latency_ms":1900},
        "dependencies":{"database":"DEGRADED"}, "root_cause":"database connection pool exhaustion", "options":[{"id":"option-001","type":"scale","risk":"risky_write","label":"Scale database connection capacity"}]
    },
    "dependency-timeout": {
        "service":"booking-api", "severity":"SEV2", "faults":{"external_timeout":True,"error_rate":0.18,"latency_ms":2400},
        "dependencies":{"payment":"DEGRADED"}, "root_cause":"upstream dependency timeout", "options":[{"id":"option-001","type":"restart","risk":"safe_write","label":"Restart booking-api and re-establish dependency connections"}]
    },
    "failed-deployment": {
        "service":"booking-api", "severity":"SEV1", "faults":{"error_rate":0.42,"latency_ms":2800},
        "dependencies":{"database":"HEALTHY"}, "root_cause":"failed booking-api deployment", "options":[{"id":"option-001","type":"rollback","risk":"risky_write","label":"Rollback failed deployment"}]
    },
    "partial-network-failure": {
        "service":"booking-api", "severity":"SEV2", "faults":{"external_timeout":True,"error_rate":0.22,"latency_ms":2100},
        "dependencies":{"payment":"DEGRADED","database":"HEALTHY"}, "root_cause":"partial network path failure", "options":[{"id":"option-001","type":"restart","risk":"safe_write","label":"Restart affected connection pool"}]
    },
}

class FaultRequest(BaseModel):
    scenario_id: str


def now() -> str: return datetime.now(UTC).isoformat()

def _write(run: dict[str, Any]) -> None:
    (RUNS / f"{run['run_id']}.json").write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")

def _services() -> dict[str, Any]:
    out={}
    for name in tools.URLS:
        try:
            out[name]=tools.getm(name)
            out[name]["status"]="HEALTHY" if out[name].get("healthy") else "DEGRADED"
        except (requests.RequestException, ValueError, KeyError) as exc:
            out[name]={"service":name,"healthy":False,"status":"UNAVAILABLE","error":str(exc)}
    return out

def _deps(services: dict[str, Any]) -> dict[str, Any]:
    booking=services.get("booking-api", {})
    return {
        "database":{"status":"DEGRADED" if booking.get("db_pool_exhausted") or booking.get("db_down") else "HEALTHY"},
        "payment":{"status":"DEGRADED" if booking.get("external_timeout") else "HEALTHY"},
        "inventory":{"status":"HEALTHY"},
    }

def _evidence(run, stage, agent, action, result, status="COMPLETED"):
    run["evidence"].append({"timestamp":now(),"run_id":run["run_id"],"incident_id":run["incident_id"],"stage":stage,"agent":agent,"action":action,"input":run.get("scenario_id"),"output":result,"status":status})

def _event(run, event, detail=None, stage=None, status="COMPLETED"):
    run["timeline"].append({"timestamp":now(),"event":event,"detail":detail,"stage":stage,"status":status})

def _new_run(scenario_id: str):
    s=SCENARIOS[scenario_id]
    rid=f"RUN-LAB-{uuid.uuid4().hex[:10]}"
    iid=f"INC-LAB-{uuid.uuid4().hex[:8]}"
    run={
        "run_id": rid,
        "incident_id": iid,
        "scenario_id": scenario_id,
        "service": s["service"],
        "severity": s["severity"],
        "state": "DETECTED",
        "timeline": [],
        "evidence": [],
        "supported_recovery_actions": [x["type"] for x in s.get("options", [])],
        "monitoring_delivery": None,
        "created_at": now(),
    }
    _event(run,"Incident detected","Stateful fault injection activated","INCIDENTLAB")
    _event(run,"Monitoring event generated",scenario_id,"INCIDENTLAB")
    return run

def _inject_service(scenario):
    # Reset all simulator services first so scenarios are deterministic.
    tools.reset_all()
    service=scenario["service"]
    for fault,value in scenario["faults"].items():
        tools.set_fault(service,fault,True,value)
    if scenario.get("faults",{}).get("error_rate",0) >= 0.3:
        tools.set_version(service,"v2.1-bad")

def correlation_key(service: str, scenario_id: str) -> str:
    return f"incidentlab:{service.strip().lower()}:{scenario_id.strip().lower()}"


def monitoring_payload(run: dict[str, Any], scenario: dict[str, Any], services: dict[str, Any]) -> dict[str, Any]:
    scenario_id = run["scenario_id"]
    symptom=(f"error_rate={scenario.get('faults',{}).get('error_rate','n/a')}, "
             f"latency_ms={scenario.get('faults',{}).get('latency_ms','n/a')}")
    key = correlation_key(scenario["service"], scenario_id)
    return {
        "title": f"[Incident][{scenario['severity']}] {scenario['service']} - {scenario_id}",
        "service": scenario["service"],
        "severity": scenario["severity"],
        "severity_label": f"sev:{scenario['severity'][3:]}",
        "scenario_id": scenario_id,
        "symptom": symptom,
        "customer_impact": "Simulated incident generated by IncidentLab",
        "environment": "incidentlab",
        "observed_since": now(),
        "source": "incidentlab",
        "run_id": run["run_id"],
        "incident_id": run["incident_id"],
        "fault_type": ",".join(scenario.get("faults", {}).keys()),
        "correlation_key": key,
        "deduplication_key": key,
        "metrics": services.get(scenario["service"],{}),
        "dependencies": _deps(services),
        "initial_evidence": run["evidence"],
        "incidentlab_reference": f"{os.getenv('INCIDENTLAB_PUBLIC_URL','http://localhost:8080').rstrip('/')}/api/incidents/{run['incident_id']}",
    }


def _deliver_monitoring(payload: dict[str, Any]) -> dict[str, Any]:
    ingress=os.getenv("INCIDENTLAB_MONITORING_URL","http://host.docker.internal:18088/hooks/monitoring")
    req=urllib.request.Request(
        ingress,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type":"application/json"},
        method="POST",
    )
    log_event(
        "MONITORING_EVENT_SENT",
        incident_id=payload.get("incident_id"),
        incidentlab_run_id=payload.get("run_id"),
        scenario_id=payload.get("scenario_id"),
        service=payload.get("service"),
        monitoring_url=ingress,
        correlation_key=payload.get("correlation_key"),
    )
    try:
        with urllib.request.urlopen(req,timeout=15) as resp:
            result=json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log_event(
            "MONITORING_EVENT_UNAVAILABLE",
            incident_id=payload.get("incident_id"),
            incidentlab_run_id=payload.get("run_id"),
            scenario_id=payload.get("scenario_id"),
            service=payload.get("service"),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return {"delivered":False,"monitoring_url":ingress,"error":f"{type(exc).__name__}: {exc}"}
    return {"delivered":True,"monitoring_url":ingress,**(result if isinstance(result,dict) else {"response":result})}

def start_demo(scenario_id):
    if scenario_id not in SCENARIOS: raise HTTPException(404,"unknown scenario")
    with LOCK:
        scenario=SCENARIOS[scenario_id]
        _inject_service(scenario)
        run=_new_run(scenario_id)
        services=_services()
        log_event("INCIDENTLAB_FAULT_INJECTED", incident_id=run["incident_id"], incidentlab_run_id=run["run_id"], scenario_id=scenario_id, service=run["service"])
        _evidence(run,"INCIDENTLAB","simulated-monitoring","fault injection",{"scenario":scenario_id,"services":services})
        payload=monitoring_payload(run,scenario,services)
        result=_deliver_monitoring(payload)
        run["monitoring_delivery"]=result
        if result.get("delivered"):
            run["github_issue_number"]=result.get("issue_number")
            run["github_issue_url"]=result.get("issue_url")
            run["state"]="MONITORING_DELIVERED"
            _event(run,"Monitoring ingress accepted",result.get("issue_url") or result.get("issue_number"),"INCIDENTLAB","COMPLETED")
        else:
            _event(run,"Monitoring ingress unavailable",result.get("error"),"INCIDENTLAB","UNAVAILABLE")
        _write(run)
        return run

def latest():
    files=sorted(RUNS.glob("*.json"), key=lambda p:p.stat().st_mtime, reverse=True)
    return json.loads(files[0].read_text(encoding="utf-8")) if files else None

def get_run(run_id):
    p=RUNS/f"{run_id}.json"
    if not p.exists(): raise HTTPException(404,"run not found")
    return json.loads(p.read_text(encoding="utf-8"))


def _require_control_token(authorization: str | None) -> None:
    expected = os.getenv("INCIDENTLAB_CONTROL_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "IncidentLab recovery control token is not configured")
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Invalid IncidentLab recovery control token")


def _clear_service_faults(service: str) -> None:
    for fault in ["error_rate", "latency_ms", "crash", "db_pool_exhausted", "db_down", "external_timeout"]:
        tools.set_fault(service, fault, False)

@router.get("/health")
def api_health(): return {"ok":True,"component":"incidentlab","mode":"stateful-simulator"}

@router.get("/services")
def services(): return _services()

@router.get("/services/{name}")
def service(name):
    if name not in tools.URLS: raise HTTPException(404,"service not found")
    return _services().get(name)

@router.get("/metrics")
def metrics(): return _services()

@router.get("/logs")
def logs():
    r=latest(); return [] if not r else [x for x in r["evidence"] if x["stage"] in {"S4","S5","RCA"}]

@router.get("/events")
def events():
    r=latest(); return [] if not r else r["timeline"]

@router.get("/dependencies")
def dependencies(): return _deps(_services())

@router.get("/state")
def state(): return {"services":_services(),"dependencies":_deps(_services()),"latest_run":latest()}

@router.get("/scenarios")
def scenarios(): return [{"id":k,**v} for k,v in SCENARIOS.items()]

@router.post("/faults/inject")
def inject(req: FaultRequest): return start_demo(req.scenario_id)

@router.post("/faults/reset")
def reset():
    with LOCK:
        tools.reset_all()
        return {"reset":True,"services":_services()}


@router.post("/v1/alertmanager")
def alertmanager_webhook(payload: dict[str, Any]):
    results=[]
    active=latest()
    for alert in payload.get("alerts") or []:
        if alert.get("status") != "firing":
            continue
        labels=alert.get("labels") or {}
        annotations=alert.get("annotations") or {}
        service=str(labels.get("service") or "unknown")
        scenario_id=(active or {}).get("scenario_id") if active and active.get("service") == service else str(labels.get("alertname") or "alertmanager").lower()
        severity_raw=str(labels.get("severity") or "warning").lower()
        severity="SEV1" if severity_raw in {"critical","sev1","1"} else "SEV2"
        key=correlation_key(service,scenario_id)
        services=_services()
        event={
            "title": f"[Incident][{severity}] {service} - {scenario_id}",
            "service": service,
            "severity": severity,
            "severity_label": f"sev:{severity[3:]}",
            "scenario_id": scenario_id,
            "symptom": annotations.get("description") or annotations.get("summary") or labels.get("alertname") or "Alertmanager firing alert",
            "customer_impact": "Simulated incident generated by IncidentLab",
            "environment": "incidentlab",
            "observed_since": alert.get("startsAt") or now(),
            "source": "prometheus-alertmanager",
            "run_id": (active or {}).get("run_id"),
            "incident_id": (active or {}).get("incident_id"),
            "correlation_key": key,
            "deduplication_key": key,
            "metrics": services.get(service,{}),
            "dependencies": _deps(services),
            "initial_evidence": (active or {}).get("evidence",[]),
            "incidentlab_reference": (
                f"{os.getenv('INCIDENTLAB_PUBLIC_URL','http://localhost:8080').rstrip('/')}/api/incidents/{active.get('incident_id')}"
                if active and active.get("incident_id") else None
            ),
        }
        results.append(_deliver_monitoring(event))
    return {"accepted":True,"results":results}

@router.post("/recovery/restart")
def restart(authorization: str | None = Header(None)):
    _require_control_token(authorization)
    r=latest()
    if not r: raise HTTPException(404,"no active run")
    result=tools.post(r["service"],"/admin/reset")
    _event(r,"Recovery API restart executed","Authenticated external controller changed simulator state","Recovery")
    _write(r)
    return {"ok":True,"action":"restart","state":result}

@router.post("/recovery/rollback")
def rollback(authorization: str | None = Header(None)):
    _require_control_token(authorization)
    r=latest()
    if not r: raise HTTPException(404,"no active run")
    service=r["service"]
    tools.set_version(service,"v2.0")
    _clear_service_faults(service)
    result=tools.getm(service)
    _event(r,"Recovery API rollback executed","Authenticated external controller changed simulator state","Recovery")
    _write(r)
    return {"ok":True,"action":"rollback","state":result}

@router.post("/recovery/scale")
def scale(authorization: str | None = Header(None)):
    _require_control_token(authorization)
    r=latest()
    if not r: raise HTTPException(404,"no active run")
    tools.set_fault(r["service"],"db_pool_exhausted",False)
    tools.set_fault(r["service"],"error_rate",False)
    tools.set_fault(r["service"],"latency_ms",False)
    result=tools.getm(r["service"])
    _event(r,"Recovery API scale executed","Authenticated external controller changed simulator state","Recovery")
    _write(r)
    return {"ok":True,"action":"scale","state":result}

@router.get("/incidents/{incident_id}")
def incident(incident_id):
    r=latest()
    if not r or r["incident_id"]!=incident_id: raise HTTPException(404,"incident not found")
    return r

@router.get("/incidents/{incident_id}/timeline")
def incident_timeline(incident_id): return incident(incident_id)["timeline"]

@router.get("/evidence")
def evidence():
    r=latest(); return [] if not r else r["evidence"]

@router.post("/demo/start")
def demo_start(req: FaultRequest): return start_demo(req.scenario_id)

@router.post("/demo/approve")
def demo_approve():
    raise HTTPException(410,"Approval is controlled by GitHub /opsswarm approve <option-id>; IncidentLab cannot bypass policy.")

@router.post("/demo/reset")
def demo_reset(): return reset()

@router.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(UI)

UI=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpsSwarm IncidentLab Control Center</title><style>
:root{font-family:Inter,Segoe UI,Arial,sans-serif;background:#08111f;color:#dbe7f5}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#07101c,#0b1525);min-height:100vh}.app{display:grid;grid-template-columns:230px 1fr;min-height:100vh}.nav{border-right:1px solid #1c3046;background:#091423;padding:22px 14px;position:sticky;top:0;height:100vh}.brand{font-weight:800;font-size:18px;margin:0 8px 26px}.brand small{display:block;color:#71859d;font-size:11px;margin-top:4px}.nav button{display:block;width:100%;text-align:left;background:transparent;color:#9eb2c9;border:0;border-radius:8px;padding:11px 12px;margin:3px 0;cursor:pointer}.nav button.active,.nav button:hover{background:#13253a;color:#fff}.main{padding:22px;overflow:auto}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}.top h1{margin:0;font-size:24px}.sub{color:#71859d;font-size:12px}.controls{display:flex;gap:8px;flex-wrap:wrap}.btn{border:1px solid #2a425d;background:#102238;color:#e9f3ff;border-radius:8px;padding:9px 12px;cursor:pointer}.btn.primary{background:#1d4f80;border-color:#3975ad}.btn.warn{background:#4a3614;border-color:#896a27}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{background:#0d1b2b;border:1px solid #1c3046;border-radius:12px;padding:15px}.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span6{grid-column:span 6}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}.label{font-size:11px;text-transform:uppercase;color:#71859d;letter-spacing:.08em}.value{font-size:20px;font-weight:700;margin-top:6px}.status{display:inline-flex;padding:4px 7px;border-radius:999px;font-size:11px;font-weight:700}.ok{background:#123a2a;color:#6ee7b7}.bad{background:#45202a;color:#ff8f9e}.wait{background:#493713;color:#ffd66b}.muted{color:#71859d}.workflow{display:grid;grid-template-columns:repeat(11,1fr);gap:6px;align-items:stretch}.stage{border:1px solid #223a54;border-radius:9px;padding:9px 5px;text-align:center;background:#0b1827}.stage b{display:block;font-size:12px}.stage small{font-size:9px;color:#71859d}.stage.done{border-color:#247457;background:#0e2a24}.stage.waiting{border-color:#8b6a2d;background:#2b2210}.stage.running{border-color:#356b9d;background:#112943}.agents{display:grid;grid-template-columns:1fr 1fr;gap:8px}.agent{border:1px solid #1d334b;border-radius:9px;padding:10px;background:#0a1725}.timeline{max-height:310px;overflow:auto}.event{border-left:2px solid #2b567f;padding:7px 10px;margin:5px 0}.event b{font-size:12px}.event small{display:block;color:#71859d}.pre{white-space:pre-wrap;background:#07101a;border:1px solid #1c3046;border-radius:8px;padding:10px;max-height:300px;overflow:auto;font-size:11px}.scenario{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #1b3045;padding:10px 0}.metricrow{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric{padding:9px;border-radius:8px;background:#0a1725}.metric strong{display:block;font-size:17px}.footer{color:#5f748b;font-size:11px;margin-top:14px}@media(max-width:1000px){.app{grid-template-columns:1fr}.nav{position:relative;height:auto;border-right:0}.workflow{grid-template-columns:repeat(4,1fr)}.span3,.span4,.span5,.span6,.span7,.span8{grid-column:span 12}.agents{grid-template-columns:1fr}}
</style></head><body><div class="app"><aside class="nav"><div class="brand">OpsSwarm<br>IncidentLab<small>Control Center · simulator</small></div><button class="active">Dashboard</button><button>Incidents</button><button>Fault Lab</button><button>Workflow</button><button>Agents</button><button>Evidence</button><button>Recovery</button><button>Verification</button><button>Settings</button></aside><main class="main"><div class="top"><div><h1>Incident Simulation Control Center</h1><div class="sub">IncidentLab simulates services/evidence; OpsSwarm remains workflow authority.</div></div><div class="controls"><button class="btn primary" onclick="start('booking-api-high-5xx')">Start Demo</button><button class="btn" onclick="resetLab()">Reset Environment</button><button class="btn" onclick="replay()">Replay Scenario</button></div></div><div class="grid"><div class="card span3"><div class="label">Incident ID</div><div id="incident" class="value">—</div></div><div class="card span3"><div class="label">Severity</div><div id="severity" class="value">—</div></div><div class="card span3"><div class="label">Service</div><div id="service" class="value">—</div></div><div class="card span3"><div class="label">Current State</div><div id="state" class="value">IDLE</div></div><div class="card span8"><div class="label">Workflow</div><div id="workflow" class="workflow" style="margin-top:10px"></div></div><div class="card span4"><div class="label">Policy / Approval</div><div id="policy" class="value" style="font-size:15px">—</div><div id="approval" class="sub" style="margin-top:7px">—</div><button id="approve" class="btn warn" style="display:none;margin-top:10px" onclick="approve()">Approve option-001</button></div><div class="card span5"><div class="label">Simulated Services</div><div id="services" style="margin-top:8px"></div></div><div class="card span7"><div class="label">Key Metrics</div><div id="metrics" class="metricrow" style="margin-top:8px"></div></div><div class="card span6"><div class="label">Specialist Agents · Read-only</div><div id="agents" class="agents" style="margin-top:8px"></div></div><div class="card span6"><div class="label">Realtime Timeline</div><div id="timeline" class="timeline" style="margin-top:8px"></div></div><div class="card span6"><div class="label">Recovery</div><div id="recovery" class="pre">—</div></div><div class="card span6"><div class="label">Evidence Viewer</div><div id="evidence" class="pre">—</div></div><div class="card span12"><div class="label">Fault Lab</div><div id="scenarios"></div></div></div><div class="footer">Demo mode is stateful. Human approval is displayed as a GitHub command and never grants authority through this UI.</div></main></div><script>
let last=null;const stages=['S8','S1','S2','S4','S5','RCA','S3','Policy','Recovery','S6','S7'];
async function j(url,opt){const r=await fetch(url,opt);const x=await r.json();if(!r.ok)throw new Error(x.detail||'request failed');return x}
function render(r){if(!r)return;last=r;document.querySelector('#incident').textContent=r.incident_id;document.querySelector('#severity').textContent=r.severity;document.querySelector('#service').textContent=r.service;document.querySelector('#state').textContent=r.state;document.querySelector('#policy').textContent=r.policy_decision||'—';document.querySelector('#approval').textContent=r.approval_state||'—';document.querySelector('#approve').style.display='none';document.querySelector('#workflow').innerHTML=stages.map(s=>{let z=r.stages?.[s]?.state||'PENDING';return `<div class="stage ${z==='COMPLETED'?'done':z==='WAITING'?'waiting':z==='RUNNING'?'running':''}"><b>${s}</b><small>${z}</small></div>`}).join('');document.querySelector('#agents').innerHTML=Object.entries(r.agent_status||{}).map(([n,v])=>`<div class="agent"><b>${n}</b><div class="sub">${v.read_only?'READ-ONLY · ':''}${v.state}</div></div>`).join('');document.querySelector('#timeline').innerHTML=(r.timeline||[]).slice().reverse().map(e=>`<div class="event"><b>${e.event}</b><div>${e.detail||''}</div><small>${e.timestamp} · ${e.stage||'system'}</small></div>`).join('');document.querySelector('#evidence').textContent=JSON.stringify((r.evidence||[]).slice(-12),null,2);document.querySelector('#recovery').textContent=JSON.stringify({state:r.recovery_state,options:r.recovery_options,verification:r.verification_state,final_report:r.final_report||null,postmortem:r.postmortem||null},null,2)}
async function refresh(){try{const s=await j('/api/state');const r=s.latest_run;if(r){render(r);const svc=s.services;document.querySelector('#services').innerHTML=Object.entries(svc).map(([n,v])=>`<div style="padding:7px 0;border-bottom:1px solid #1a2d42"><b>${n}</b> <span class="status ${v.healthy?'ok':'bad'}">${v.status||'UNKNOWN'}</span></div>`).join('');const b=svc[r.service]||{};document.querySelector('#metrics').innerHTML=[['Error rate',((b.error_rate??0)*100).toFixed(1)+'%'],['Latency',(b.latency_ms??0)+' ms'],['Database',b.db_pool_exhausted||b.db_down?'DEGRADED':'HEALTHY'],['Dependency',b.external_timeout?'DEGRADED':'HEALTHY']].map(x=>`<div class="metric"><span class="sub">${x[0]}</span><strong>${x[1]}</strong></div>`).join('')}}catch(e){console.error(e)}}
async function start(id){render(await j('/api/demo/start',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({scenario_id:id})}));await loadScenarios();}
async function approve(){if(last&&last.github_issue_url)window.open(last.github_issue_url,'_blank')}
async function resetLab(){await j('/api/demo/reset',{method:'POST'});location.reload()}
async function replay(){if(last)await start(last.scenario_id);else await start('booking-api-high-5xx')}
async function loadScenarios(){const xs=await j('/api/scenarios');document.querySelector('#scenarios').innerHTML=xs.map(x=>`<div class="scenario"><div><b>${x.id}</b><div class="sub">${x.service} · ${x.severity}</div></div><button class="btn" onclick="start('${x.id}')">Inject Fault</button></div>`).join('')}
loadScenarios();refresh();setInterval(refresh,1500);
</script></body></html>'''
