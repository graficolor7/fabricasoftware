"""Pruebas de la fábrica: el adaptador del supervisor y los schemas.

Cubren los casos mínimos del protocolo: paquete inválido, evidencia faltante,
respuesta inválida del supervisor, supervisor no disponible, PASS,
PASS_WITH_ACTIONS, BLOCKED, secretos y contrato de tarea inválido.
"""
import copy
import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters" / "supervisor"))
import supervisor as sup  # noqa: E402

PACKET = json.loads((ROOT / "templates" / "SUPERVISOR_PACKET.json").read_text(encoding="utf-8"))
PACKET["commit"] = "2b44b9c"


def verdict(**over):
    v = {
        "verdict": "PASS",
        "gate": "G6",
        "commit": "2b44b9c",
        "summary": "Todo en orden.",
        "findings": [],
        "required_actions": [],
        "optional_improvements": [],
        "evidence_requested": [],
        "next_gate": "G7",
    }
    v.update(over)
    return v


def finding(severity="LOW", blocking=False):
    return {"id": "F-1", "severity": severity, "category": "tests", "title": "t", "detail": "d", "evidence": "e", "blocking": blocking}


def responses(*payloads):
    """Transporte falso que devuelve respuestas en orden y registra las peticiones."""
    calls = []

    def transport(req):
        calls.append(copy.deepcopy(req))
        p = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if isinstance(p, Exception):
            raise p
        text = p if isinstance(p, str) else json.dumps(p)
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}], "usage": {"input_tokens": 10, "output_tokens": 5}}

    transport.calls = calls
    return transport


def run(tmp_path, packet, transport, model="test-model"):
    pp = tmp_path / "p.json"
    pp.write_text(json.dumps(packet), encoding="utf-8")
    out = tmp_path / "v.json"
    md = tmp_path / "v.md"
    code = sup.run(pp, out, md, transport=transport, model=model)
    return code, json.loads(out.read_text(encoding="utf-8")), md.read_text(encoding="utf-8")


def test_pass(tmp_path):
    code, res, md = run(tmp_path, PACKET, responses(verdict()))
    assert code == sup.EXIT_OK and res["status"] == "PASS"
    assert "Supervisor · PASS" in md


def test_pass_with_actions_from_policy(tmp_path):
    code, res, _ = run(tmp_path, PACKET, responses(verdict(required_actions=["documentar X"])))
    assert code == sup.EXIT_OK and res["status"] == "PASS_WITH_ACTIONS"
    assert any("PASS_WITH_ACTIONS" in n for n in res["policy_notes"])


def test_blocked(tmp_path):
    code, res, _ = run(tmp_path, PACKET, responses(verdict(verdict="BLOCKED", findings=[finding("HIGH", True)])))
    assert code == sup.EXIT_BLOCKED and res["status"] == "BLOCKED"


def test_critical_finding_forces_blocked(tmp_path):
    code, res, _ = run(tmp_path, PACKET, responses(verdict(findings=[finding("CRITICAL", False)])))
    assert code == sup.EXIT_BLOCKED and res["verdict"]["verdict"] == "BLOCKED"


def test_evidence_requested(tmp_path):
    ev = [{"kind": "file", "ref": "src/a.ts", "reason": "ver el manejo de errores"}]
    code, res, md = run(tmp_path, PACKET, responses(verdict(verdict="PASS_WITH_ACTIONS", evidence_requested=ev)))
    assert code == sup.EXIT_EVIDENCE and res["status"] == "EVIDENCE_REQUESTED"
    assert "src/a.ts" in md


def test_invalid_then_valid_retries_with_feedback(tmp_path):
    t = responses({"verdict": "MAYBE"}, verdict())
    code, res, _ = run(tmp_path, PACKET, t)
    assert code == sup.EXIT_OK and res["telemetry"]["attempts"] == 2
    assert "no cumplía el schema" in t.calls[1]["input"][-1]["content"]


def test_invalid_twice_requires_human(tmp_path):
    code, res, _ = run(tmp_path, PACKET, responses("no es json", "tampoco"))
    assert code == sup.EXIT_HUMAN and res["status"] == "HUMAN_REVIEW_REQUIRED"


def test_supervisor_unavailable_is_pending(tmp_path):
    code, res, _ = run(tmp_path, PACKET, responses(sup.SupervisorUnavailable("HTTP 503")))
    assert code == sup.EXIT_PENDING and res["status"] == "SUPERVISOR_PENDING"


def test_missing_model_is_pending(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    code, res, _ = run(tmp_path, PACKET, responses(verdict()), model=None)
    assert code == sup.EXIT_PENDING


def test_invalid_packet_not_sent(tmp_path):
    bad = dict(PACKET)
    del bad["evidence_index"]
    t = responses(verdict())
    code, res, _ = run(tmp_path, bad, t)
    assert code == sup.EXIT_BAD_PACKET and t.calls == []


def test_empty_evidence_rejected(tmp_path):
    bad = dict(PACKET, evidence_index=[])
    code, _, _ = run(tmp_path, bad, responses(verdict()))
    assert code == sup.EXIT_BAD_PACKET


def test_secret_in_packet_blocks_send(tmp_path):
    bad = dict(PACKET, test_summary="clave sk-" + "a" * 40)
    t = responses(verdict())
    code, res, _ = run(tmp_path, bad, t)
    assert code == sup.EXIT_BAD_PACKET and t.calls == [] and "secreto" in res["detail"]


def test_commit_mismatch_uses_packet_commit(tmp_path):
    code, res, _ = run(tmp_path, PACKET, responses(verdict(commit="ffffffff")))
    assert res["verdict"]["commit"] == "2b44b9c"


def test_request_uses_strict_schema(tmp_path):
    t = responses(verdict())
    run(tmp_path, PACKET, t)
    fmt = t.calls[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert "$schema" not in fmt["schema"]


def test_refusal_is_invalid(tmp_path):
    def transport(req):
        return {"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]}
    code, _, _ = run(tmp_path, PACKET, transport)
    assert code == sup.EXIT_HUMAN


@pytest.mark.parametrize("name", sorted(p.name for p in (ROOT / "schemas").glob("*.json")))
def test_schemas_are_valid(name):
    jsonschema.Draft202012Validator.check_schema(json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8")))


def test_strict_verdict_schema_shape():
    """OpenAI strict: todo objeto con additionalProperties false y todas sus propiedades obligatorias."""
    def walk(s):
        if s.get("type") == "object":
            assert s.get("additionalProperties") is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(json.loads((ROOT / "schemas" / "supervisor-verdict.schema.json").read_text(encoding="utf-8")))


def test_task_contract_rules():
    task = json.loads((ROOT / "templates" / "TASK.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / "task.schema.json").read_text(encoding="utf-8")))
    assert not list(v.iter_errors(task))
    critical = dict(task, RISK="CRITICAL", RECOMMENDED_MODEL_CLASS="WORKER")
    assert list(v.iter_errors(critical)), "riesgo CRITICAL exige DIRECTOR + supervisor"
    vague = dict(task, ACCEPTANCE_CRITERIA=[])
    assert list(v.iter_errors(vague)), "sin criterios de aceptación no hay contrato"


def test_project_state_template_valid():
    st = json.loads((ROOT / "templates" / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / "project-state.schema.json").read_text(encoding="utf-8")))
    assert not list(v.iter_errors(st))
    assert list(v.iter_errors(dict(st, status="TODO_BIEN")))
