"""Envoltorio de compatibilidad 1.0 (adapters/supervisor/supervisor.py) y schemas/plantillas."""
import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters" / "supervisor"))
import supervisor as sup  # noqa: E402

from factory.adapters.mock import MockSupervisorAdapter  # noqa: E402
from factory.config import load_config  # noqa: E402

V1 = json.loads((ROOT / "tests" / "fixtures" / "packet_v1.json").read_text(encoding="utf-8"))


def run(tmp_path, packet, script):
    pp = tmp_path / "p.json"
    pp.write_text(json.dumps(packet), encoding="utf-8")
    out, md = tmp_path / "v.json", tmp_path / "v.md"
    code = sup.run(pp, out, md, adapter=MockSupervisorAdapter(load_config(env={}), script=script))
    return code, json.loads(out.read_text(encoding="utf-8")), md.read_text(encoding="utf-8")


@pytest.mark.parametrize("script,code,status", [
    (["PASS"], 0, "PASS"), (["PASS_WITH_ACTIONS"], 0, "PASS_WITH_ACTIONS"), (["BLOCKED"], 1, "BLOCKED"),
    (["NEED_EVIDENCE"], 4, "NEED_EVIDENCE"), (["API_FAILURE"], 3, "SUPERVISOR_PENDING"), (["INVALID_RESPONSE"], 5, "HUMAN_REVIEW_REQUIRED"),
    (["PASS_WITH_BLOCKING_ACTIONS"], 1, "PASS_WITH_ACTIONS"),
])
def test_legacy_wrapper_codes(tmp_path, script, code, status):
    c, res, md = run(tmp_path, V1, script)
    assert c == code and res["status"] == status
    assert f"Supervisor · {status}" in md


def test_legacy_invalid_packet_not_sent(tmp_path):
    bad = dict(V1)
    del bad["evidence_index"]
    c, res, _ = run(tmp_path, bad, ["PASS"])
    assert c == 2 and res["status"] == "INVALID_PACKET"


@pytest.mark.parametrize("name", sorted(p.name for p in (ROOT / "schemas").rglob("*.schema.json")))
def test_schemas_are_valid(name):
    path = next((ROOT / "schemas").rglob(name))
    jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_strict_verdict_schema_shape():
    """OpenAI strict: todo objeto con additionalProperties false y todas sus propiedades obligatorias."""
    def walk(s):
        t = s.get("type")
        if t == "object":
            assert s.get("additionalProperties") is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if t == "array":
            walk(s["items"])
    walk(json.loads((ROOT / "schemas" / "supervisor-verdict.schema.json").read_text(encoding="utf-8")))


@pytest.mark.parametrize("schema,tpl", [("supervisor-packet", "SUPERVISOR_PACKET.json"), ("supervisor-profile", "SUPERVISOR_PROFILE.json"),
                                        ("project-state", "PROJECT_STATE.json"), ("task", "TASK.json")])
def test_templates_valid(schema, tpl):
    from factory import schemas
    assert schemas.errors(schema, json.loads((ROOT / "templates" / tpl).read_text(encoding="utf-8"))) == []


def test_task_contract_rules():
    task = json.loads((ROOT / "templates" / "TASK.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / "task.schema.json").read_text(encoding="utf-8")))
    assert list(v.iter_errors(dict(task, RISK="CRITICAL", RECOMMENDED_MODEL_CLASS="WORKER")))
    assert list(v.iter_errors(dict(task, ACCEPTANCE_CRITERIA=[])))


def test_policy_rules():
    from factory import policy
    pkt = {"project": {"commit": "a" * 40}, "review": {"gate": "G6"}}
    base = {"schema_version": "2", "verdict": "PASS", "gate": "G9", "commit": "b" * 40, "summary": "s", "optional_improvements": [],
            "evidence_requests": [], "next_gate": "G7", "confidence": "HIGH", "required_actions": [],
            "findings": [{"id": "SUP-001", "severity": "HIGH", "domain": "correctness", "finding": "f", "evidence": [], "required_action": None, "blocking": False},
                         {"id": "SUP-002", "severity": "LOW", "domain": "documentation", "finding": "f", "evidence": [], "required_action": None, "blocking": True}]}
    v, notes = policy.apply(base, pkt, {"CRITICAL", "HIGH"})
    assert v["verdict"] == "BLOCKED" and v["commit"] == "a" * 40 and v["gate"] == "G6"
    assert v["policy"]["blocking_findings"] == ["SUP-001"]  # LOW no bloquea, HIGH sí
    v2, _ = policy.apply(dict(base, findings=[], required_actions=[{"id": "ACT-001", "action": "x", "blocking": False, "finding_id": None}]), pkt, {"CRITICAL"})
    assert v2["verdict"] == "PASS_WITH_ACTIONS" and v2["policy"]["gate_effect"] == policy.CAN_CONTINUE
    v3, _ = policy.apply(dict(base, findings=[], evidence_requests=[{"type": "test", "ref": "t", "lines": None, "reason": "r"}]), pkt, {"CRITICAL"})
    assert v3["verdict"] == "NEED_EVIDENCE"
