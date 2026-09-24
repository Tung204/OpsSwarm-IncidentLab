from opsswarm.openclaw import OpenClawClient


def test_agent_session_key_is_explicitly_owned():
    assert OpenClawClient._agent_session_key("opsswarm-incident-manager", "RUN-GH-1-s2") == "agent:opsswarm-incident-manager:RUN-GH-1-s2"
    scoped = "agent:opsswarm-database-investigator:existing"
    assert OpenClawClient._agent_session_key("opsswarm-database-investigator", scoped) == scoped


def test_s2_task_normalization_is_format_tolerant_but_read_only():
    from opsswarm.skill_logic import _task_from_agent
    task = _task_from_agent({
        "id": "T1",
        "type": "investigate",
        "objective": "inspect metrics",
        "profile": "opsswarm-observability-investigator",
        "risk": "read",
        "depends_on": [],
        "parallelizable": True,
        "expected_output": {"evidence": ["metrics"]},
        "status": "pending",
    })
    assert task.status == "PENDING"
    assert task.profile == "observability-investigator"
    assert isinstance(task.expected_output, str)


def test_s2_task_rejects_write_risk():
    import pytest
    from opsswarm.skill_logic import _task_from_agent
    with pytest.raises(ValueError, match="non-read-only"):
        _task_from_agent({
            "id": "T1", "type": "INVESTIGATE", "objective": "bad",
            "profile": "observability-investigator", "risk": "safe_write",
            "depends_on": [], "parallelizable": True, "expected_output": "Finding", "status": "PENDING"
        })


def test_incidentlab_prompts_keep_endpoint_placeholders_literal():
    from opsswarm.models import IncidentContext, Task, TaskType, Risk, ExecutionResult
    from opsswarm.prompts import specialist_prompt, verify_prompt
    incident = IncidentContext(issue_number=1, title="x", service="booking-api", environment="incidentlab")
    task = Task(id="T1", type=TaskType.OBSERVE, objective="observe", profile="observability-investigator", risk=Risk.READ)
    sp = specialist_prompt(incident, task.model_dump_json())
    assert "/api/services/{name}" in sp
    vp = verify_prompt(incident, ExecutionResult(option_id="o1", success=True, summary="ok").model_dump_json())
    assert "/api/services/{service}" in vp


def test_rca_accepts_structured_remediation_descriptions_without_granting_authority():
    from opsswarm.skill_logic import _root_from_agent
    root = _root_from_agent({
        "status":"confirmed", "proximate_cause":"pool exhausted", "root_cause":"bad release",
        "causal_chain":["deploy", "pool"], "evidence_refs":[{"endpoint":"/api/state"}],
        "confidence":0.95,
        "remediation_options":[{"id":"option-001","type":"rollback","risk":"risky_write"}],
        "corrective_actions":[]
    })
    assert root.status == "confirmed"
    assert isinstance(root.remediation_options[0], str)


def test_s3_normalization_preserves_and_validates_risk():
    import pytest
    from opsswarm.skill_logic import _recovery_plan_from_agent
    plan = _recovery_plan_from_agent({
        "options":[{"id":"option-001","description":"rollback","profile":"opsswarm-recovery-responder","risk":"RISKY_WRITE","capabilities":"rollback"}],
        "recommended_option":{"id":"option-001"}, "confidence":0.95, "requires_business_input":False
    })
    assert plan.options[0].risk.value == "risky_write"
    assert plan.recommended_option == "option-001"
    with pytest.raises(ValueError, match="unsupported risk"):
        _recovery_plan_from_agent({"options":[{"id":"x","description":"x","risk":"unbounded"}]})


def test_s3_normalizes_numeric_estimated_recovery_to_text():
    from opsswarm.skill_logic import _recovery_plan_from_agent
    plan = _recovery_plan_from_agent({
        "options":[{"id":"option-001","description":"rollback service","risk":"risky_write","estimated_recovery":5,"capabilities":["rollback"]}],
        "recommended_option":"option-001", "confidence":0.95, "requires_business_input":False
    })
    assert plan.options[0].estimated_recovery == "5"


def test_finding_normalizes_descriptive_objects_to_text():
    from opsswarm.skill_logic import _finding_from_agent
    f = _finding_from_agent({
        "task_id":"T5", "finding":"root cause synthesis",
        "evidence":[{"ref":"/api/state"}],
        "hypothesis":{"ranked":[{"rank":1,"cause":"bad release"}]},
        "confidence":0.9,
        "recommended_next_action":{"next_task":"S3"}
    })
    assert isinstance(f.hypothesis, str)
    assert isinstance(f.recommended_next_action, str)
    assert isinstance(f.evidence[0], str)


def test_execution_normalizes_evidence_and_raw_shapes():
    from opsswarm.skill_logic import _execution_from_agent
    result = _execution_from_agent({
        "option_id":"option-001", "success":True, "summary":"ok",
        "evidence":[{"status":"healthy"}], "ambiguous":False,
        "raw":"{\"ok\":true}"
    })
    assert isinstance(result.evidence[0], str)
    assert result.raw == {"agent_raw":"{\"ok\":true}"}
