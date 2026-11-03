"""Flujo completo del supervisor con MockSupervisorAdapter (sin red ni tokens)."""
import json
import threading
import time

import pytest

from factory import policy
from factory.adapters.mock import MockSupervisorAdapter, scenario_verdict
from factory.config import load_config
from factory.errors import FactoryError
from factory.gate import add_override, close_gate
from factory.review import Orchestrator
from factory.state import load_state, status_report

KW = dict(gate="G6", milestone="M1", trigger="MILESTONE_COMPLETION")


def files(d):
    return sorted(p.name for p in d.iterdir())


def test_pass_persists_everything_and_updates_state(project, draft):
    o = project.orch(["PASS"])
    r = o.review(draft, **KW)
    assert r.status == "PASS" and r.gate_effect == policy.CAN_CLOSE and r.exit_code == 0
    assert r.review_id == "DEMO-M1-G6-0001"
    d = project.root / r.directory
    assert {"packet.json", "verdict.json", "round-01.json", "review.json", "evidence-index.json", "calls.jsonl"} <= set(files(d))
    v = json.loads((d / "verdict.json").read_text())
    assert v["model"] == "mock-supervisor" and v["packet_hash"] and v["commit"] == json.loads((d / "packet.json").read_text())["project"]["commit"]
    st = load_state(project.root, project.profile)
    assert st["gates"]["M1/G6"]["status"] == "PASS" and st["gates"]["M1/G6"]["review_id"] == r.review_id
    rec = json.loads((d / "review.json").read_text())
    assert rec["usage"]["calls"] == 1 and rec["usage"]["cost_estimate"] is None


def test_pass_with_non_blocking_actions_can_continue(project, draft):
    r = project.orch(["PASS_WITH_ACTIONS"]).review(draft, **KW)
    assert r.status == "PASS_WITH_ACTIONS" and r.gate_effect == policy.CAN_CONTINUE and r.exit_code == 0
    assert r.pending_actions == ["ACT-001: documentar X"]


def test_pass_with_blocking_actions_is_treated_as_blocked(project, draft):
    r = project.orch(["PASS_WITH_BLOCKING_ACTIONS"]).review(draft, **KW)
    assert r.status == "PASS_WITH_ACTIONS" and r.gate_effect == policy.CANNOT_CLOSE and r.exit_code == 1
    assert "ACT-001" in r.blockers


def test_blocked_cannot_close(project, draft):
    o = project.orch(["BLOCKED"])
    r = o.review(draft, **KW)
    assert r.status == "BLOCKED" and r.exit_code == 1 and "SUP-001" in r.blockers
    with pytest.raises(FactoryError):
        close_gate(project.root, project.profile, o.store, "M1", "G6")


def test_need_evidence_collects_only_requested_and_second_call(project, draft):
    o = project.orch(["NEED_EVIDENCE", "PASS"])
    r = o.review(draft, **KW)
    assert r.status == "PASS" and r.round == 2
    sent = o.adapter.packets[1]
    assert sent["packet_kind"] == "EVIDENCE"
    assert [a["ref"] for a in sent["attached_evidence"]] == ["README.md"]
    assert sent["attached_evidence"][0]["content"].startswith("1: línea 1") and "línea 6" not in sent["attached_evidence"][0]["content"]
    d = project.root / r.directory
    assert (d / "evidence-02-packet.json").exists()
    idx = json.loads((d / "evidence-index.json").read_text())
    assert any(e["source"] == "attached" and e["sha256"] for e in idx["entries"])


def test_evidence_rounds_are_capped_then_human_review(project, draft):
    o = project.orch(["NEED_EVIDENCE"])
    r = o.review(draft, **KW)
    assert r.status == "HUMAN_REVIEW_REQUIRED" and r.exit_code == 5
    assert o.adapter.n == 3  # inicial + 2 rondas de evidencia, ni una más
    assert "SUPERVISOR_MAX_EVIDENCE_ROUNDS" in r.detail


def test_need_evidence_manual_mode_waits(project, draft):
    o = project.orch(["NEED_EVIDENCE", "PASS"])
    r = o.review(draft, auto_evidence=False, **KW)
    assert r.status == "NEED_EVIDENCE" and r.gate_effect == policy.AWAITING_EVIDENCE and r.exit_code == 4
    assert r.evidence_requests[0]["ref"] == "README.md"
    r2 = o.evidence(r.review_id, provided=[{"request_index": 0, "ref": "README.md", "content": "texto aportado"}], auto=False)
    assert r2.status == "PASS"
    assert o.adapter.packets[-1]["attached_evidence"][0]["content"] == "texto aportado"


@pytest.mark.parametrize("scenario", ["API_FAILURE", "TIMEOUT"])
def test_provider_failure_never_passes(project, draft, scenario):
    o = project.orch([scenario])
    r = o.review(draft, **KW)
    assert r.status == "SUPERVISOR_PENDING" and r.gate_effect == policy.GATE_OPEN and r.exit_code == 3
    st = load_state(project.root, project.profile)["gates"]["M1/G6"]
    assert st["status"] == "SUPERVISOR_PENDING" and st["verdict"] is None
    assert not (project.root / r.directory / "verdict.json").exists()
    with pytest.raises(FactoryError):
        close_gate(project.root, project.profile, o.store, "M1", "G6")


def test_provider_failure_policy_can_escalate_to_human(project, draft):
    r = project.orch(["API_FAILURE"], SUPERVISOR_ON_UNAVAILABLE="HUMAN_REVIEW_REQUIRED").review(draft, **KW)
    assert r.status == "HUMAN_REVIEW_REQUIRED"


def test_retry_after_outage(project, draft):
    o = project.orch(["API_FAILURE", "PASS"])
    r = o.review(draft, **KW)
    assert r.status == "SUPERVISOR_PENDING"
    r2 = o.retry(r.review_id)
    assert r2.status == "PASS" and r2.round == 2


def test_invalid_response_after_repair_is_invalid(project, draft):
    o = project.orch(["INVALID_RESPONSE"])
    r = o.review(draft, **KW)
    assert r.status == "INVALID_SUPERVISOR_RESPONSE" and r.exit_code == 6 and r.gate_effect == policy.GATE_OPEN
    assert o.adapter.n == 2  # 1 + 1 reparación
    assert load_state(project.root, project.profile)["gates"]["M1/G6"]["verdict"] is None


def test_invalid_then_repaired(project, draft):
    o = project.orch(["INVALID_RESPONSE", "PASS"])
    r = o.review(draft, **KW)
    assert r.status == "PASS" and r.usage["calls"] == 2


def test_semantically_incoherent_verdict_is_invalid(project, draft):
    bad = scenario_verdict("NEED_EVIDENCE", {"review": {"gate": "G6"}, "project": {"commit": "a" * 40}})
    bad["evidence_requests"] = []
    r = project.orch([bad]).review(draft, **KW)
    assert r.status == "INVALID_SUPERVISOR_RESPONSE"


def test_human_review_verdict(project, draft):
    r = project.orch(["HUMAN_REVIEW"]).review(draft, **KW)
    assert r.status == "HUMAN_REVIEW_REQUIRED" and r.exit_code == 5


def test_duplicate_review_does_not_call_again(project, draft):
    o = project.orch(["PASS"])
    r1 = o.review(draft, **KW)
    r2 = o.review(draft, **KW)
    assert r2.status == "DUPLICATE" and r2.duplicate_of == r1.review_id and o.adapter.n == 1 and r2.exit_code == 0
    r3 = o.review(draft, force_new=True, **KW)
    assert r3.review_id == "DEMO-M1-G6-0002" and o.adapter.n == 2


def test_lock_prevents_concurrent_review(project, draft):
    o = project.orch(["PASS"])
    with o.store.lock("M1/G6", "otro"):
        r = o.review(draft, **KW)
    assert r.status == "LOCKED" and r.exit_code == 7 and o.adapter.n == 0


def test_concurrent_threads_only_one_reviews(project, draft):
    cfg = load_config(env={})

    class Slow(MockSupervisorAdapter):
        def _review(self, packet, phase):
            time.sleep(0.4)
            return super()._review(packet, phase)

    results = []

    def go():
        a = Slow(cfg, ["PASS"])
        results.append(Orchestrator(project.root, project.profile, cfg, a).review(draft, **KW).status)

    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("PASS") == 1 and set(results) <= {"PASS", "LOCKED", "DUPLICATE"}


def test_stale_lock_is_recovered(project, draft):
    o = project.orch(["PASS"], SUPERVISOR_LOCK_TTL_S=0)
    lockdir = o.store.dir / "locks"
    lockdir.mkdir(exist_ok=True)
    (lockdir / "M1_G6.lock").write_text(json.dumps({"created_ts": time.time() - 10, "pid": 1}))
    assert o.review(draft, **KW).status == "PASS"


def test_call_limit_stops(project, draft):
    o = project.orch(["NEED_EVIDENCE"], SUPERVISOR_MAX_CALLS_PER_GATE=1)
    r = o.review(draft, **KW)
    assert r.status == "HUMAN_REVIEW_REQUIRED" and o.adapter.n == 1 and "MAX_CALLS" in r.detail


def test_invalid_trigger_costs_nothing(project, draft):
    o = project.orch(["PASS"])
    r = o.review(draft, gate="G6", milestone="M1", trigger="TYPO_FIX")
    assert r.status == "USAGE_ERROR" and o.adapter.n == 0


def test_profile_trigger_accepted(project, draft):
    assert project.orch(["PASS"]).review(draft, gate="G6", milestone="M1", trigger="PERFORMANCE_BASELINE_FREEZE").status == "PASS"


def test_verification_failure_blocks_spending(project, draft):
    prof = project.profile
    prof["verification"] = [{"name": "falla", "category": "unit", "argv": ["python3", "-c", "import sys; print('boom'); sys.exit(3)"]}]
    project.write(".factory/supervisor.json", json.dumps(prof))
    project.commit("prof")
    o = project.orch(["PASS"])
    r = o.review(draft, run_verification=True, **KW)
    assert r.status == "VERIFICATION_FAILED" and r.exit_code == 9 and o.adapter.n == 0


def test_verification_success_goes_into_packet(project, draft):
    prof = project.profile
    prof["verification"] = [{"name": "ok", "category": "unit", "argv": ["python3", "-c", "print('10 passed')"]}]
    project.write(".factory/supervisor.json", json.dumps(prof))
    project.commit("prof")
    o = project.orch(["PASS"])
    r = o.review(draft, run_verification=True, **KW)
    assert r.status == "PASS"
    unit = o.adapter.packets[0]["verification"]["unit"]
    assert unit["status"] == "PASS" and "10 passed" in unit["summary"] and unit["evidence_ref"].startswith("sha256:")


def test_delta_review_sends_only_delta(project, draft):
    o = project.orch(["BLOCKED", "PASS"])
    r = o.review(draft, **KW)
    project.write("src/fix.ts", "export const x = 1\n")
    new = project.commit("fix SUP-001")
    dd = {"implementation": {"summary": "corrige SUP-001"},
          "delta": {"findings_addressed": [{"id": "SUP-001", "summary": "x", "resolution": "src/fix.ts"}], "new_test_results": ["11/11"]}}
    r2 = o.delta(r.review_id, dd)
    assert r2.status == "PASS" and r2.review_id == r.review_id
    p = o.adapter.packets[-1]
    assert p["packet_kind"] == "DELTA"
    assert p["delta"]["previous_review_id"] == r.review_id and p["delta"]["new_commit"] == new
    assert p["delta"]["changed_files"] == ["src/fix.ts"]
    assert p["previous_findings"][0]["id"] == "SUP-001"
    assert (project.root / r.directory / "delta-02-packet.json").exists()
    assert close_gate(project.root, project.profile, o.store, "M1", "G6")["review_id"] == r.review_id


def test_delta_requires_new_commit(project, draft):
    o = project.orch(["BLOCKED"])
    r = o.review(draft, **KW)
    assert o.delta(r.review_id, {}).status == "USAGE_ERROR"


def test_delta_rounds_capped(project, draft):
    o = project.orch(["BLOCKED"], SUPERVISOR_MAX_DELTA_ROUNDS=1)
    r = o.review(draft, **KW)
    project.write("a.txt", "1")
    project.commit()
    assert o.delta(r.review_id, {}).status == "BLOCKED"
    project.write("a.txt", "2")
    project.commit()
    assert o.delta(r.review_id, {}).status == "HUMAN_REVIEW_REQUIRED"


def test_repeated_disagreement_generates_report(project, draft):
    o = project.orch(["BLOCKED"])
    r = o.review(draft, **KW)
    dispute = {"delta": {"disputed_findings": [{"id": "SUP-001", "position": "El test ya cubre el caso", "evidence": ["tests/x.test.ts"]}]}}
    for i in range(2):
        project.write("a.txt", str(i))
        project.commit()
        res = o.delta(r.review_id, dispute)
    assert res.status == "HUMAN_REVIEW_REQUIRED" and "desacuerdo" in res.detail
    d = project.root / r.directory
    rep = json.loads((d / "disagreement.json").read_text())
    assert rep["type"] == "TECHNICAL_DISAGREEMENT_REPORT" and rep["issues"][0]["opus_position"] == "El test ya cubre el caso"
    assert "Posición del Supervisor" in (d / "disagreement.md").read_text()


def test_override_keeps_finding_and_records_risk(project, draft):
    o = project.orch(["BLOCKED"])
    r = o.review(draft, **KW)
    row = add_override(project.root, project.profile, o.store, r.review_id, "SUP-001", "Riesgo aceptado para la demo", "Joseph (PO)", "fallo menor en demo")
    assert row["override_id"] == "OVR-DEMO-0001"
    v = json.loads((project.root / r.directory / "verdict.json").read_text())
    assert v["findings"][0]["id"] == "SUP-001" and v["findings"][0]["overridden_by"] == "OVR-DEMO-0001"
    assert v["policy"]["accepted_risks"] and v["policy"]["gate_effect"] == policy.CAN_CONTINUE
    assert (o.store.dir / "overrides.jsonl").exists()


def test_close_gate_requires_reviewed_commit(project, draft):
    o = project.orch(["PASS"])
    o.review(draft, **KW)
    project.write("b.txt", "x")
    project.commit()
    with pytest.raises(FactoryError):
        close_gate(project.root, project.profile, o.store, "M1", "G6")


def test_baseline_review(project, draft):
    o = project.orch(["PASS_WITH_ACTIONS"])
    draft["baseline"] = {"current_state_ref": "docs/CURRENT_STATE.md", "test_status": "80/80"}
    r = o.baseline(draft, gate="BASELINE", milestone="M1")
    assert r.status == "PASS_WITH_ACTIONS" and o.adapter.packets[0]["packet_kind"] == "BASELINE"
    assert r.review_id == "DEMO-M1-BASELINE-0001"


def test_status_report(project, draft):
    o = project.orch(["BLOCKED"])
    r = o.review(draft, **KW)
    rep = status_report(project.root, project.profile)
    assert rep["PROJECT"] == "demo" and rep["MILESTONE"] == "M1" and rep["SUPERVISOR STATUS"] == "BLOCKED"
    assert rep["LAST REVIEW"] == r.review_id and "SUP-001" in rep["BLOCKERS"] and "delta" in rep["NEXT ACTION"]
    assert "unit=PASS" in rep["TEST STATUS"]


def test_manual_adapter_roundtrip(project, draft):
    from factory.adapters.manual import ManualSupervisorAdapter
    cfg = load_config(env={})
    o = Orchestrator(project.root, project.profile, cfg, ManualSupervisorAdapter(cfg))
    r = o.review(draft, **KW)
    assert r.status == "SUPERVISOR_PENDING"
    d = project.root / r.directory
    req = d / "manual-request-01-initial.md"
    assert req.exists() and "Independent Principal Software Engineer" in req.read_text()
    pkt = json.loads((d / "packet.json").read_text())
    (d / "manual-response-02-initial.json").write_text(json.dumps(scenario_verdict("PASS", pkt)))
    r2 = o.retry(r.review_id)
    assert r2.status == "PASS"


def test_v1_packet_is_upgraded(project):
    from factory.packet import upgrade_v1
    import pathlib
    v1 = json.loads((pathlib.Path(__file__).parent / "fixtures" / "packet_v1.json").read_text())
    from conftest import git
    v1["commit"] = git(project.root, "rev-parse", "HEAD").strip()
    o = project.orch(["PASS"])
    r = o.review(v1, gate="G6", milestone="M1", trigger="MILESTONE_COMPLETION")
    assert r.status == "PASS"
    assert upgrade_v1(v1)["schema_version"] == "2"
