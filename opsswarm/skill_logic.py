from __future__ import annotations
import re, json
from .models import IncidentContext, Task, Finding, RootCauseArtifact, RecoveryPlan, ExecutionResult, VerificationResult
from .prompts import *


def parse_issue(number:int, issue:dict)->IncidentContext:
    body=issue.get("body") or ""; title=issue.get("title") or ""
    labels=[x.get("name","") if isinstance(x,dict) else str(x) for x in issue.get("labels",[])]
    def field(name, default="unknown"):
        m=re.search(rf"(?ims)^###\s*{re.escape(name)}\s*$\s*(.+?)(?=^###\s|\Z)", body)
        return m.group(1).strip() if m else default
    symptoms=field("Symptoms","")
    sev="UNKNOWN"
    for lab in labels:
        if lab.lower().startswith("sev:"): sev="SEV"+lab.split(":",1)[1].strip()
    return IncidentContext(
        issue_number=number,
        title=title,
        body=body,
        service=field("Service"),
        environment=field("Environment"),
        severity=sev if sev != "UNKNOWN" else field("Severity", "UNKNOWN"),
        symptoms=[x.strip(" -") for x in symptoms.splitlines() if x.strip()] or [title],
        customer_impact=field("Customer impact"),
        actor=(issue.get("user") or {}).get("login"),
        labels=labels,
        incident_id=field("Incident ID", "") or None,
        incidentlab_run_id=field("IncidentLab Run ID", "") or None,
        scenario_id=field("Scenario ID", "") or None,
        deduplication_key=field("Deduplication key", "") or None,
        issue_url=issue.get("html_url"),
    )

def _task_from_agent(value: dict) -> Task:
    if not isinstance(value, dict):
        raise ValueError("S2 task must be an object")
    item = dict(value)
    item["type"] = str(item.get("type") or "").upper()
    item["risk"] = str(item.get("risk") or "read").lower()
    item["status"] = str(item.get("status") or "PENDING").upper()
    if not isinstance(item.get("expected_output"), str):
        item["expected_output"] = json.dumps(item.get("expected_output"), ensure_ascii=False, default=str) if item.get("expected_output") is not None else "Finding"
    if isinstance(item.get("required_capabilities"), str):
        item["required_capabilities"] = [item["required_capabilities"]]
    if isinstance(item.get("depends_on"), str):
        item["depends_on"] = [item["depends_on"]]
    profile = str(item.get("profile") or "").strip()
    if profile.startswith("opsswarm-"):
        profile = profile[len("opsswarm-"):]
    item["profile"] = profile

    allowed_types = {"OBSERVE", "INVESTIGATE", "DIAGNOSE"}
    allowed_profiles = {
        "observability-investigator",
        "application-investigator",
        "infrastructure-investigator",
        "database-investigator",
    }
    if item["type"] not in allowed_types:
        raise ValueError(f"S2 attempted non-investigation task type: {item['type']}")
    if item["risk"] != "read":
        raise ValueError(f"S2 attempted non-read-only task risk: {item['risk']}")
    if profile not in allowed_profiles:
        raise ValueError(f"S2 selected unsupported profile: {profile}")
    if item["status"] not in {"PENDING", "RUNNING", "DONE", "FAILED", "SKIPPED"}:
        item["status"] = "PENDING"
    return Task.model_validate(item)


async def build_tasks(oc, agent:str, run_id:str, incident:IncidentContext)->list[Task]:
    data=await oc.run_json(agent,f"{run_id}-s2",task_graph_prompt(incident))
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    return [_task_from_agent(x) for x in tasks]

def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _text_list(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [_as_text(x) for x in value]


def _finding_from_agent(value: dict) -> Finding:
    if not isinstance(value, dict):
        raise ValueError("S4 finding must be an object")
    item = dict(value)
    item["evidence"] = _text_list(item.get("evidence"))
    for key in ("finding", "hypothesis", "recommended_next_action"):
        if item.get(key) is not None and not isinstance(item.get(key), str):
            item[key] = _as_text(item[key])
    if not isinstance(item.get("raw"), dict):
        item["raw"] = {"agent_raw": item.get("raw")}
    return Finding.model_validate(item)


def _root_from_agent(value: dict) -> RootCauseArtifact:
    if not isinstance(value, dict):
        raise ValueError("RCA result must be an object")
    item = dict(value)
    item["status"] = str(item.get("status") or "uncertain").lower()
    if item["status"] not in {"confirmed", "uncertain"}:
        item["status"] = "uncertain"
    item["causal_chain"] = _text_list(item.get("causal_chain"))
    item["evidence_refs"] = _text_list(item.get("evidence_refs"))
    # RCA may describe remediation candidates as structured objects.  S3 is
    # still the only component allowed to turn them into typed/risked options.
    item["remediation_options"] = _text_list(item.get("remediation_options"))
    item["corrective_actions"] = _text_list(item.get("corrective_actions"))
    return RootCauseArtifact.model_validate(item)


def _recovery_plan_from_agent(value: dict) -> RecoveryPlan:
    if not isinstance(value, dict):
        raise ValueError("S3 recovery plan must be an object")
    item = dict(value)
    options = item.get("options") or []
    if not isinstance(options, list):
        raise ValueError("S3 options must be a list")
    normalized = []
    allowed_risks = {"read", "safe_write", "risky_write", "destructive"}
    for raw in options:
        if not isinstance(raw, dict):
            raise ValueError("S3 option must be an object")
        opt = dict(raw)
        opt["id"] = str(opt.get("id") or "").strip()
        if not opt["id"]:
            raise ValueError("S3 option id is required")
        risk = str(opt.get("risk") or "").lower().strip()
        if risk not in allowed_risks:
            raise ValueError(f"S3 option has unsupported risk: {risk}")
        opt["risk"] = risk
        profile = str(opt.get("profile") or "recovery-responder").strip()
        if profile.startswith("opsswarm-"):
            profile = profile[len("opsswarm-"):]
        if profile != "recovery-responder":
            raise ValueError(f"S3 selected unsupported remediation profile: {profile}")
        opt["profile"] = profile
        if isinstance(opt.get("capabilities"), str):
            opt["capabilities"] = [opt["capabilities"]]
        if opt.get("estimated_recovery") is not None and not isinstance(opt.get("estimated_recovery"), str):
            opt["estimated_recovery"] = str(opt["estimated_recovery"])
        normalized.append(opt)
    item["options"] = normalized
    rec = item.get("recommended_option")
    if isinstance(rec, dict):
        rec = rec.get("id")
    item["recommended_option"] = str(rec) if rec is not None else None
    return RecoveryPlan.model_validate(item)


def _execution_from_agent(value: dict) -> ExecutionResult:
    if not isinstance(value, dict):
        raise ValueError("Recovery result must be an object")
    item = dict(value)
    item["evidence"] = _text_list(item.get("evidence"))
    if not isinstance(item.get("raw"), dict):
        item["raw"] = {"agent_raw": item.get("raw")}
    return ExecutionResult.model_validate(item)


def _verification_from_agent(value: dict) -> VerificationResult:
    if not isinstance(value, dict):
        raise ValueError("S7 result must be an object")
    item = dict(value)
    item["evidence"] = _text_list(item.get("evidence"))
    if not isinstance(item.get("raw"), dict):
        item["raw"] = {"agent_raw": item.get("raw")}
    return VerificationResult.model_validate(item)


async def execute_task(oc, profile_agent:str, run_id:str, incident:IncidentContext, task:Task)->Finding:
    data=await oc.run_json(profile_agent,f"{run_id}-{task.id}",specialist_prompt(incident,task.model_dump_json()))
    return _finding_from_agent(data)

async def synthesize_root_cause(oc,agent,run_id,incident,findings,human_inputs)->RootCauseArtifact:
    data = await oc.run_json(agent,f"{run_id}-rca",root_cause_prompt(incident,findings,human_inputs))
    return _root_from_agent(data)

async def make_recovery_plan(oc,agent,run_id,incident,root,human_inputs)->RecoveryPlan:
    data = await oc.run_json(agent,f"{run_id}-plan",recovery_plan_prompt(incident,root,human_inputs))
    return _recovery_plan_from_agent(data)

async def execute_recovery(oc,agent,run_id,incident,root,option)->ExecutionResult:
    data = await oc.run_json(agent,f"{run_id}-recover",recovery_prompt(incident,root,option.model_dump_json()))
    return _execution_from_agent(data)

async def verify_recovery(oc,agent,run_id,incident,execution)->VerificationResult:
    data = await oc.run_json(agent,f"{run_id}-verify",verify_prompt(incident,execution.model_dump_json()))
    return _verification_from_agent(data)

async def make_extra_task(oc,agent,run_id,incident,request)->Task:
    return _task_from_agent(await oc.run_json(agent,f"{run_id}-extra",extra_investigation_prompt(incident,request)))
