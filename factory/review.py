"""Orquestador de revisiones del supervisor.

`Orchestrator.review()`   STEP 1 (+ rondas de evidencia automáticas, máx. N)
`Orchestrator.evidence()` STEP 2 manual (evidencia que no se recolecta sola)
`Orchestrator.delta()`    nueva revisión tras correcciones (solo el delta)
`Orchestrator.retry()`    reintenta la última ronda tras un fallo del proveedor o revisión manual
`Orchestrator.baseline()` BASELINE_SUPERVISOR_REVIEW de un proyecto existente

Invariantes:
- Nunca se llama al proveedor sin paquete válido, trigger válido, lock del gate y cupo de llamadas.
- Nunca se actualiza el estado antes de persistir la ronda.
- Sin veredicto válido no hay PASS: el gate queda abierto.
"""
from __future__ import annotations

import copy
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory import FACTORY_VERSION, evidence, git, packet as pk, policy
from factory.adapters import SupervisorAdapter, make_adapter
from factory.adapters.manual import ManualSupervisorAdapter
from factory.config import SupervisorConfig
from factory.errors import FactoryError
from factory.profile import check_trigger
from factory.security import SECRET_ENV_VARS, redact
from factory.state import update_gate_state
from factory.store import LockBusy, ReviewStore, append_jsonl, now, read_json, write_json

EXIT = {
    "CAN_CLOSE": 0, "CAN_CONTINUE_WITH_PENDING_ACTIONS": 0, "CANNOT_CLOSE": 1, "INVALID_PACKET": 2, "USAGE_ERROR": 2,
    "SUPERVISOR_PENDING": 3, "SUPERVISOR_UNAVAILABLE": 3, "AWAITING_EVIDENCE": 4, "HUMAN_REVIEW_REQUIRED": 5,
    "INVALID_SUPERVISOR_RESPONSE": 6, "LOCKED": 7, "VERIFICATION_FAILED": 9,
}
VALID_STATUSES = ("PASS", "PASS_WITH_ACTIONS", "BLOCKED", "NEED_EVIDENCE", "HUMAN_REVIEW_REQUIRED")


@dataclass
class Result:
    status: str
    review_id: str | None = None
    round: int | None = None
    gate_effect: str = policy.GATE_OPEN
    summary: str = ""
    detail: str = ""
    blockers: list[str] = field(default_factory=list)
    pending_actions: list[str] = field(default_factory=list)
    evidence_requests: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    directory: str | None = None
    duplicate_of: str | None = None

    @property
    def exit_code(self) -> int:
        if self.status in ("PASS", "PASS_WITH_ACTIONS", "BLOCKED", "NEED_EVIDENCE"):
            return EXIT.get(self.gate_effect, 1)
        if self.status == "DUPLICATE":
            return EXIT.get(self.gate_effect, 0)
        return EXIT.get(self.status, 1)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v not in (None, [], {}, "")}
        d["exit_code"] = self.exit_code
        return d


def _sum_usage(calls: list[dict], prices: dict | None) -> dict:
    tot = {"calls": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "duration_s": 0.0}
    models = set()
    for c in calls:
        tot["calls"] += 1
        tot["duration_s"] += float(c.get("duration_s") or 0)
        if c.get("model"):
            models.add(c.get("response_model") or c["model"])
        for k in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
            tot[k] += int((c.get("usage") or {}).get(k) or 0)
    tot["duration_s"] = round(tot["duration_s"], 3)
    tot["models"] = sorted(models)
    if prices and tot["calls"]:
        uncached = max(0, tot["input_tokens"] - tot["cached_tokens"])
        tot["cost_estimate"] = round((uncached * prices["SUPERVISOR_PRICE_INPUT_PER_1M"] + tot["cached_tokens"] * prices["SUPERVISOR_PRICE_CACHED_INPUT_PER_1M"]
                                      + tot["output_tokens"] * prices["SUPERVISOR_PRICE_OUTPUT_PER_1M"]) / 1_000_000, 6)
        tot["cost_source"] = "tarifas configuradas por el PO (SUPERVISOR_PRICE_*)"
    else:
        tot["cost_estimate"] = None
        tot["cost_source"] = "no calculado: sin tarifas configuradas"
    return tot


class Orchestrator:
    def __init__(self, project_root: Path, profile: dict, cfg: SupervisorConfig, adapter: SupervisorAdapter | None = None):
        self.root = project_root.resolve()
        self.profile = profile
        self.cfg = cfg
        self.adapter = adapter or make_adapter(cfg)
        self.store = ReviewStore(self.root, profile["project_id"], profile["review_id_prefix"],
                                 profile.get("reviews_dir", ".factory/reviews"), cfg["SUPERVISOR_LOCK_TTL_S"])

    # ================================================================ helpers
    def _git_fill(self, p: dict, commit: str | None) -> None:
        proj = p.setdefault("project", {})
        proj["id"] = self.profile["project_id"]
        proj["repository"] = self.profile["repository"]
        draft_commit = proj.get("commit") if proj.get("commit") and set(str(proj.get("commit"))) != {"0"} else None
        if git.is_repo(self.root):
            proj["commit"] = git.resolve(self.root, commit or draft_commit or "HEAD")
            proj.setdefault("branch", git.branch(self.root))
        elif not proj.get("commit"):
            raise FactoryError("no es un repo git y el borrador no trae project.commit")
        proj.setdefault("branch", "unknown")

    def run_verification(self) -> tuple[dict, bool, list[str]]:
        """Corre los comandos deterministas del perfil (sin shell). Devuelve (verification, todo_verde, fallos)."""
        ver: dict[str, Any] = {}
        failures = []
        for cmd in self.profile.get("verification", []):
            cwd = (self.root / cmd.get("cwd", ".")).resolve()
            if cwd != self.root and self.root not in cwd.parents:
                raise FactoryError(f"verificación '{cmd['name']}': cwd fuera del repo")
            t0 = time.monotonic()
            try:
                # Los comandos del proyecto nunca ven las claves del supervisor.
                env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS and not k.startswith("SUPERVISOR_PRICE")}
                r = subprocess.run(cmd["argv"], cwd=cwd, capture_output=True, timeout=cmd.get("timeout_s", 1800), check=False, env=env)
                code, out = r.returncode, (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                code, out = -1, "TIMEOUT"
            except OSError as e:
                code, out = -2, f"no se pudo ejecutar: {e}"
            tail = redact("\n".join(out.strip().splitlines()[-15:]))[-1800:]
            check = {"status": "PASS" if code == 0 else "FAIL", "command": " ".join(cmd["argv"])[:500],
                     "summary": f"exit {code} en {time.monotonic() - t0:.1f}s · …{tail}",
                     "evidence_ref": "sha256:" + hashlib.sha256(out.encode("utf-8")).hexdigest()}
            if cmd["category"] == "other":
                ver.setdefault("other", {})[cmd["name"]] = check
            elif cmd["category"] in ver and ver[cmd["category"]]["status"] == "FAIL":
                pass
            else:
                ver[cmd["category"]] = check
            if code != 0 and cmd.get("required", True):
                failures.append(f"{cmd['name']} (exit {code})")
        return ver, not failures, failures

    def _round_count(self, rid: str) -> tuple[int, int, int, int]:
        """(rondas, llamadas, rondas de evidencia desde la última no-evidencia, rondas delta)."""
        d = self.store.review_dir(rid)
        rounds = sorted(d.glob("round-*.json"))
        calls = sum(len(read_json(r, {}).get("calls", [])) for r in rounds)
        ev = 0
        for r in reversed(rounds):
            k = read_json(r, {}).get("kind")
            if k == "EVIDENCE":
                ev += 1
            else:
                break
        deltas = sum(1 for r in rounds if read_json(r, {}).get("kind") == "DELTA")
        return len(rounds), calls, ev, deltas

    def _all_calls(self, rid: str) -> list[dict]:
        d = self.store.review_dir(rid)
        out = []
        for r in sorted(d.glob("round-*.json")):
            out += read_json(r, {}).get("calls", [])
        return out

    def _finish(self, rid: str, rnd: int, status: str, gate_effect: str, summary: str = "", detail: str = "",
                verdict: dict | None = None) -> Result:
        pol = (verdict or {}).get("policy", {})
        res = Result(status=status, review_id=rid, round=rnd, gate_effect=gate_effect, summary=summary, detail=redact(detail),
                     blockers=pol.get("blocking_findings", []) + [a for a in pol.get("blocking_actions", [])],
                     pending_actions=pol.get("pending_actions", []),
                     evidence_requests=(verdict or {}).get("evidence_requests", []) if status == "NEED_EVIDENCE" else [],
                     usage=_sum_usage(self._all_calls(rid), self.cfg.prices()), directory=str(self.store.review_dir(rid).relative_to(self.root)))
        d = self.store.review_dir(rid)
        meta = self.store.review_meta(rid)
        rec = read_json(d / "review.json", {})
        rec.update({"schema_version": "1", "review_id": rid, "project_id": self.profile["project_id"], "milestone": meta["milestone"],
                    "gate": meta["gate"], "trigger": meta["trigger"], "factory_version": FACTORY_VERSION, "provider": self.adapter.name,
                    "status": status, "gate_effect": gate_effect, "last_round": rnd, "updated_at": now(), "usage": res.usage,
                    "config": {k: v for k, v in self.cfg.public().items() if not k.startswith("SUPERVISOR_PRICE")}})
        rec.setdefault("created_at", meta["created_at"])
        write_json(d / "review.json", rec)
        return res

    def _persist_evidence_index(self, d: Path, p: dict, rnd: int) -> None:
        idx = read_json(d / "evidence-index.json", {"entries": []})
        for e in p.get("evidence_index", []):
            idx["entries"].append({"round": rnd, "source": "packet", **e})
        for a in p.get("attached_evidence", []):
            idx["entries"].append({"round": rnd, "source": "attached", "ref": a["ref"], "type": a["type"], "lines": a.get("lines"),
                                   "sha256": a.get("sha256"), "truncated": a.get("truncated", False)})
        for u in p.get("unavailable_evidence", []):
            idx["entries"].append({"round": rnd, "source": "unavailable", **u})
        for e in (p.get("delta") or {}).get("new_evidence", []):
            idx["entries"].append({"round": rnd, "source": "delta", **e})
        write_json(d / "evidence-index.json", idx)

    # ================================================================ ronda
    def _call(self, rid: str, kind: str, p: dict, key: str | None) -> Result:
        d = self.store.review_dir(rid)
        n_rounds, n_calls, _, _ = self._round_count(rid)
        rnd = n_rounds + 1
        meta = self.store.review_meta(rid)
        if n_calls >= self.cfg["SUPERVISOR_MAX_CALLS_PER_GATE"]:
            return self._stop_human(rid, rnd - 1, f"límite SUPERVISOR_MAX_CALLS_PER_GATE={self.cfg['SUPERVISOR_MAX_CALLS_PER_GATE']} alcanzado")
        if isinstance(self.adapter, ManualSupervisorAdapter):
            self.adapter.bind(d, f"{rnd:02d}-{kind.lower()}")
        fn = {"INITIAL": self.adapter.review, "BASELINE": self.adapter.review, "EVIDENCE": self.adapter.request_additional_evidence,
              "DELTA": self.adapter.review_delta}[kind]
        ar = fn(p)
        row: dict[str, Any] = {"schema_version": "1", "review_id": rid, "round": rnd, "kind": kind, "commit": p["project"]["commit"],
                               "packet_hash": pk.packet_hash(p), "created_at": now(), "provider": self.adapter.name, "model": ar.model,
                               "calls": ar.calls, "adapter_status": ar.status, "detail": redact(ar.detail)}
        for c in ar.calls:
            append_jsonl(d / "calls.jsonl", {"round": rnd, "kind": kind, **c})

        if ar.verdict is not None:
            sem = policy.semantic_errors(ar.verdict)
            if sem:
                ar.verdict, ar.status, ar.detail = None, "INVALID_SUPERVISOR_RESPONSE", "veredicto incoherente: " + "; ".join(sem)
                row["adapter_status"], row["detail"] = ar.status, ar.detail
        if ar.verdict is None:
            if ar.status == "SUPERVISOR_UNAVAILABLE":
                status = self.cfg["SUPERVISOR_ON_UNAVAILABLE"]
                row["status"] = status
                row["unavailable"] = True
            else:
                status = ar.status  # SUPERVISOR_PENDING | INVALID_SUPERVISOR_RESPONSE
                row["status"] = status
            write_json(d / f"round-{rnd:02d}.json", row)
            self.store.record_round(rid, row, None, status)
            update_gate_state(self.root, self.profile, meta, rid, rnd, row["commit"], status, policy.GATE_OPEN, None, ar.model,
                              detail=ar.detail, provider=self.adapter.name)
            return self._finish(rid, rnd, status, policy.GATE_OPEN, detail=ar.detail)

        effective, _ = policy.apply(ar.verdict, p, self.cfg.blocking_severities, self.store.overrides(rid))
        row["verdict_raw"] = ar.verdict
        row["verdict"] = effective
        row["status"] = effective["verdict"]
        write_json(d / f"round-{rnd:02d}.json", row)
        write_json(d / "verdict.json", {"review_id": rid, "round": rnd, "commit": row["commit"], "packet_hash": row["packet_hash"],
                                        "model": ar.model, "created_at": row["created_at"], **effective})
        self.store.record_round(rid, row, key, effective["verdict"])
        # Solo después de persistir: estado del proyecto.
        update_gate_state(self.root, self.profile, meta, rid, rnd, row["commit"], effective["verdict"], effective["policy"]["gate_effect"],
                          effective, ar.model, provider=self.adapter.name)
        res = self._finish(rid, rnd, effective["verdict"], effective["policy"]["gate_effect"], effective["summary"], verdict=effective)
        if kind == "DELTA":
            dis = self._check_disagreement(rid, p, effective)
            if dis:
                return dis
        return res

    def _stop_human(self, rid: str, rnd: int, reason: str) -> Result:
        d = self.store.review_dir(rid)
        meta = self.store.review_meta(rid)
        rec = read_json(d / "review.json", {})
        rec.setdefault("stops", []).append({"at": now(), "reason": reason})
        write_json(d / "review.json", rec)
        self.store.set_status(rid, "HUMAN_REVIEW_REQUIRED")
        commit = (read_json(d / "verdict.json", {}) or {}).get("commit") or read_json(d / "packet.json", {}).get("project", {}).get("commit", "")
        update_gate_state(self.root, self.profile, meta, rid, max(rnd, 0), commit, "HUMAN_REVIEW_REQUIRED", policy.HUMAN_REVIEW, None, None, detail=reason,
                          provider=self.adapter.name)
        return self._finish(rid, max(rnd, 0), "HUMAN_REVIEW_REQUIRED", policy.HUMAN_REVIEW, detail=reason)

    # ================================================================ evidencia
    def _evidence_loop(self, rid: str, base_packet: dict, res: Result, auto: bool, key_fn) -> Result:
        while res.status == "NEED_EVIDENCE":
            _, _, ev_rounds, _ = self._round_count(rid)
            if ev_rounds >= self.cfg["SUPERVISOR_MAX_EVIDENCE_ROUNDS"]:
                return self._stop_human(rid, res.round or 0, f"evidencia insuficiente tras {ev_rounds} ronda(s) (SUPERVISOR_MAX_EVIDENCE_ROUNDS)")
            if not auto:
                return res
            d = self.store.review_dir(rid)
            last = read_json(d / f"round-{res.round:02d}.json")
            requests = last["verdict_raw"]["evidence_requests"]
            attached, unavailable = evidence.collect(self.root, base_packet, requests, self.profile, self.cfg)
            if not attached:
                res.detail = "ninguna evidencia pedida se pudo recolectar sola; aporta la evidencia con `factory supervisor evidence --review " \
                             f"{rid} --evidence-file archivo.json`"
                return res
            res = self._send_evidence(rid, base_packet, last, attached, unavailable, key_fn)
        return res

    def _send_evidence(self, rid, base_packet, last_round, attached, unavailable, key_fn) -> Result:
        d = self.store.review_dir(rid)
        ep = pk.evidence_packet(base_packet, attached, unavailable, last_round["verdict_raw"])
        ep, errs = pk.finalize(ep, self.cfg["SUPERVISOR_MAX_CONTEXT_BYTES"])
        n_rounds, *_ = self._round_count(rid)
        if errs:
            return self._stop_human(rid, n_rounds, "paquete de evidencia rechazado: " + "; ".join(errs[:3]))
        write_json(d / f"evidence-{n_rounds + 1:02d}-packet.json", ep)
        self._persist_evidence_index(d, ep, n_rounds + 1)
        return self._call(rid, "EVIDENCE", ep, key_fn(ep))

    # ================================================================ API pública
    def review(self, draft: dict, *, gate: str | None = None, milestone: str | None = None, trigger: str | None = None,
               commit: str | None = None, run_verification: bool = False, force_new: bool = False, auto_evidence: bool = True,
               kind: str = "INITIAL") -> Result:
        p = copy.deepcopy(pk.upgrade_v1(draft))
        p["schema_version"] = "2"
        p["factory_version"] = FACTORY_VERSION
        p["packet_kind"] = kind
        rv = p.setdefault("review", {})
        if gate:
            rv["gate"] = gate
        if milestone:
            rv["milestone"] = milestone
        if trigger:
            rv["trigger"] = trigger
        rv.setdefault("requested_verdict", True)
        try:
            check_trigger(self.profile, rv.get("trigger", ""))
            if kind == "BASELINE" and rv.get("trigger") != "BASELINE_SUPERVISOR_REVIEW":
                raise FactoryError("una revisión BASELINE usa el trigger BASELINE_SUPERVISOR_REVIEW")
            self._git_fill(p, commit)
        except FactoryError as e:
            return Result(status="USAGE_ERROR", errors=[str(e)])

        if run_verification:
            ver, green, failures = self.run_verification()
            p.setdefault("verification", {}).update(ver)
            if not green and self.cfg["SUPERVISOR_REQUIRE_GREEN_VERIFICATION"]:
                return Result(status="VERIFICATION_FAILED", errors=failures,
                              detail="la verificación determinista falla: se corrige antes de gastar tokens del supervisor")

        p, errs = pk.finalize(p, self.cfg["SUPERVISOR_MAX_CONTEXT_BYTES"])
        if errs:
            return Result(status="INVALID_PACKET", errors=errs)
        gate_key = f"{rv['milestone']}/{rv['gate']}"
        key_fn = lambda q: ReviewStore.idem_key(self.profile["project_id"], q["project"]["commit"], gate_key, pk.packet_hash(q))  # noqa: E731
        key = key_fn(p)
        prev = self.store.find_key(key)
        if prev and not force_new:
            meta = self.store.review_meta(prev["review_id"])
            vd = read_json(self.store.review_dir(prev["review_id"]) / "verdict.json", {})
            return Result(status="DUPLICATE", review_id=prev["review_id"], round=prev["round"], duplicate_of=prev["review_id"],
                          gate_effect=(vd.get("policy") or {}).get("gate_effect", policy.GATE_OPEN), summary=vd.get("summary", ""),
                          detail=f"misma revisión ya registrada (estado {meta['status']}); usa --force-new para repetirla a propósito")
        try:
            with self.store.lock(gate_key, owner=f"{rv['trigger']}@{p['project']['commit'][:12]}"):
                rid, d = self.store.new_review(rv["milestone"], rv["gate"], rv["trigger"], gate_key)
                write_json(d / "packet.json", p)
                self._persist_evidence_index(d, p, 1)
                if force_new and prev:
                    write_json(d / "review.json", {"forced_repeat_of": prev["review_id"]})
                res = self._call(rid, kind, p, key)
                return self._evidence_loop(rid, p, res, auto_evidence, key_fn)
        except LockBusy as e:
            return Result(status="LOCKED", detail=str(e))

    def baseline(self, draft: dict, **kw) -> Result:
        return self.review(draft, trigger="BASELINE_SUPERVISOR_REVIEW", kind="BASELINE", **kw)

    def evidence(self, review_id: str, provided: list | None = None, auto: bool = True) -> Result:
        meta = self.store.review_meta(review_id)
        d = self.store.review_dir(review_id)
        n_rounds, *_ = self._round_count(review_id)
        last = read_json(d / f"round-{n_rounds:02d}.json")
        if not last or last.get("status") != "NEED_EVIDENCE":
            return Result(status="USAGE_ERROR", review_id=review_id, errors=["la última ronda no pidió evidencia"])
        base = self._base_packet(d, last)
        gate_key = meta["gate_key"]
        key_fn = lambda q: ReviewStore.idem_key(self.profile["project_id"], q["project"]["commit"], gate_key, pk.packet_hash(q))  # noqa: E731
        try:
            with self.store.lock(gate_key, owner=f"evidence@{review_id}"):
                _, _, ev_rounds, _ = self._round_count(review_id)
                if ev_rounds >= self.cfg["SUPERVISOR_MAX_EVIDENCE_ROUNDS"]:
                    return self._stop_human(review_id, n_rounds, "evidencia insuficiente tras el máximo de rondas")
                requests = last["verdict_raw"]["evidence_requests"]
                attached, unavailable = evidence.collect(self.root, base, requests, self.profile, self.cfg) if auto else ([], [])
                if provided:
                    manual = evidence.from_file(provided, requests, self.cfg)
                    got = {a["request_index"] for a in manual}
                    attached = [a for a in attached if a["request_index"] not in got] + manual
                    unavailable = [u for u in unavailable if u.get("request_index") not in got]
                if not attached:
                    return Result(status="USAGE_ERROR", review_id=review_id, errors=["no hay evidencia nueva que enviar"])
                res = self._send_evidence(review_id, base, last, attached, unavailable, key_fn)
                return self._evidence_loop(review_id, base, res, auto, key_fn)
        except LockBusy as e:
            return Result(status="LOCKED", review_id=review_id, detail=str(e))

    def _base_packet(self, d: Path, last_round: dict) -> dict:
        """Paquete sobre el que se añade evidencia: el delta vigente o el inicial."""
        deltas = sorted(d.glob("delta-*-packet.json"))
        if last_round.get("kind") in ("DELTA", "EVIDENCE") and deltas:
            return read_json(deltas[-1])
        return read_json(d / "packet.json")

    def retry(self, review_id: str) -> Result:
        """Repite la última ronda que quedó sin veredicto (proveedor caído, respuesta inválida o revisión manual)."""
        meta = self.store.review_meta(review_id)
        d = self.store.review_dir(review_id)
        n_rounds, *_ = self._round_count(review_id)
        last = read_json(d / f"round-{n_rounds:02d}.json")
        if not last or last.get("status") in VALID_STATUSES:
            return Result(status="USAGE_ERROR", review_id=review_id, errors=["la última ronda ya tiene veredicto válido; usa delta o evidence"])
        kind = last["kind"]
        if kind == "EVIDENCE":
            p = read_json(d / f"evidence-{last['round']:02d}-packet.json")
        elif kind == "DELTA":
            p = read_json(d / f"delta-{last['round']:02d}-packet.json")
        else:
            p = read_json(d / "packet.json")
        gate_key = meta["gate_key"]
        key_fn = lambda q: ReviewStore.idem_key(self.profile["project_id"], q["project"]["commit"], gate_key, pk.packet_hash(q))  # noqa: E731
        try:
            with self.store.lock(gate_key, owner=f"retry@{review_id}"):
                # El paquete se re-guarda con el número de ronda nuevo para que la reconstrucción sea exacta.
                if kind in ("EVIDENCE", "DELTA"):
                    write_json(d / f"{kind.lower()}-{n_rounds + 1:02d}-packet.json", p)
                res = self._call(review_id, kind, p, key_fn(p))
                return self._evidence_loop(review_id, p, res, True, key_fn)
        except LockBusy as e:
            return Result(status="LOCKED", review_id=review_id, detail=str(e))

    def delta(self, review_id: str, draft: dict, commit: str | None = None, run_verification: bool = False) -> Result:
        meta = self.store.review_meta(review_id)
        d = self.store.review_dir(review_id)
        last_v = read_json(d / "verdict.json")
        if not last_v:
            return Result(status="USAGE_ERROR", review_id=review_id, errors=["la revisión no tiene veredicto válido; usa retry"])
        if last_v["policy"]["gate_effect"] in (policy.CAN_CLOSE,):
            return Result(status="USAGE_ERROR", review_id=review_id, errors=["el gate ya puede cerrar; no hace falta delta"])
        _, _, _, n_delta = self._round_count(review_id)
        if n_delta >= self.cfg["SUPERVISOR_MAX_DELTA_ROUNDS"]:
            return self._stop_human(review_id, self._round_count(review_id)[0], f"límite SUPERVISOR_MAX_DELTA_ROUNDS={self.cfg['SUPERVISOR_MAX_DELTA_ROUNDS']}")
        initial = read_json(d / "packet.json")
        try:
            new_commit = git.resolve(self.root, commit or "HEAD") if git.is_repo(self.root) else commit
        except FactoryError as e:
            return Result(status="USAGE_ERROR", errors=[str(e)])
        if not new_commit or new_commit[:12] == last_v["commit"][:12]:
            return Result(status="USAGE_ERROR", review_id=review_id, errors=["el delta necesita un commit nuevo con las correcciones"])
        dd = dict(draft.get("delta") or {})
        dd["previous_review_id"] = review_id
        dd["previous_commit"] = last_v["commit"]
        dd["new_commit"] = new_commit
        if not dd.get("changed_files") and git.is_repo(self.root):
            own = (self.profile.get("reviews_dir", ".factory/reviews").rstrip("/") + "/", self.profile.get("supervisor_state", ".factory/SUPERVISOR_STATE.json"))
            dd["changed_files"] = [f for _, f in git.changed_files(self.root, last_v["commit"], new_commit)
                                   if not f.startswith(own[0]) and f != own[1]][:200]
        for k in ("findings_addressed", "changed_files", "new_test_results", "new_evidence", "remaining_risks"):
            dd.setdefault(k, [])
        open_findings = [{"id": f["id"], "severity": f["severity"], "summary": f["finding"][:1000]} for f in last_v["findings"]]
        p: dict[str, Any] = {
            "schema_version": "2", "factory_version": FACTORY_VERSION, "packet_kind": "DELTA",
            "project": {**initial["project"], "commit": new_commit, "base_commit": last_v["commit"]},
            "review": initial["review"],
            "implementation": draft.get("implementation") or {"summary": "Correcciones de los hallazgos de " + review_id},
            "verification": draft.get("verification", {}),
            "known_failures": draft.get("known_failures", []),
            "risks": draft.get("risks", dd["remaining_risks"]),
            "previous_findings": open_findings,
            "resolved_findings": draft.get("resolved_findings", []),
            "evidence_index": draft.get("evidence_index") or [{"ref": new_commit, "kind": "commit"}],
            "delta": dd,
        }
        if git.is_repo(self.root):
            p["project"]["branch"] = git.branch(self.root)
        if run_verification:
            ver, green, failures = self.run_verification()
            p["verification"].update(ver)
            if not green and self.cfg["SUPERVISOR_REQUIRE_GREEN_VERIFICATION"]:
                return Result(status="VERIFICATION_FAILED", review_id=review_id, errors=failures)
        p, errs = pk.finalize(p, self.cfg["SUPERVISOR_MAX_CONTEXT_BYTES"])
        if errs:
            return Result(status="INVALID_PACKET", review_id=review_id, errors=errs)
        gate_key = meta["gate_key"]
        key_fn = lambda q: ReviewStore.idem_key(self.profile["project_id"], q["project"]["commit"], gate_key, pk.packet_hash(q))  # noqa: E731
        key = key_fn(p)
        prev = self.store.find_key(key)
        if prev:
            return Result(status="DUPLICATE", review_id=prev["review_id"], round=prev["round"], duplicate_of=prev["review_id"],
                          gate_effect=last_v["policy"]["gate_effect"], detail="este delta ya se revisó")
        try:
            with self.store.lock(gate_key, owner=f"delta@{review_id}"):
                n_rounds, *_ = self._round_count(review_id)
                write_json(d / f"delta-{n_rounds + 1:02d}-packet.json", p)
                self._persist_evidence_index(d, p, n_rounds + 1)
                res = self._call(review_id, "DELTA", p, key)
                return self._evidence_loop(review_id, p, res, True, key_fn)
        except LockBusy as e:
            return Result(status="LOCKED", review_id=review_id, detail=str(e))

    # ================================================================ desacuerdo
    def _check_disagreement(self, rid: str, p: dict, effective: dict) -> Result | None:
        disputed = {x["id"]: x for x in (p.get("delta") or {}).get("disputed_findings", [])}
        if not disputed:
            return None
        d = self.store.review_dir(rid)
        rec = read_json(d / "review.json", {})
        counts = rec.setdefault("disputes", {})
        still = {f["id"]: f for f in effective["findings"] if f["id"] in disputed and f["blocking"]}
        for fid in still:
            counts[fid] = counts.get(fid, 0) + 1
        write_json(d / "review.json", rec)
        hot = [fid for fid, n in counts.items() if n >= self.cfg["SUPERVISOR_MAX_DISAGREEMENT_ROUNDS"] and fid in still]
        if not hot:
            return None
        report = {
            "type": "TECHNICAL_DISAGREEMENT_REPORT", "review_id": rid, "created_at": now(), "commit": p["project"]["commit"],
            "gate": p["review"]["gate"], "milestone": p["review"]["milestone"],
            "issues": [{
                "issue": still[fid]["finding"], "finding_id": fid, "severity": still[fid]["severity"],
                "opus_position": disputed[fid]["position"], "opus_evidence": disputed[fid].get("evidence", []),
                "supervisor_position": still[fid].get("required_action") or still[fid]["finding"], "supervisor_evidence": still[fid]["evidence"],
                "rounds_in_disagreement": counts[fid],
                "tradeoffs": "(a completar por el Director con datos, sin argumentar de nuevo con el supervisor)",
                "risk": f"{still[fid]['severity']} según el supervisor",
                "possible_resolutions": ["aplicar la acción del supervisor", "aceptar el riesgo con override del PO", "Change Request / ADR"],
                "recommended_decision": "pendiente del Product Owner",
            } for fid in hot],
            "status": "HUMAN_REVIEW_REQUIRED",
        }
        write_json(d / "disagreement.json", report)
        md = ["## TECHNICAL_DISAGREEMENT_REPORT · " + rid, "", f"- **Gate / commit:** {report['gate']} · `{report['commit'][:12]}`", ""]
        for it in report["issues"]:
            md += [f"### {it['finding_id']} ({it['severity']}, {it['rounds_in_disagreement']} rondas)", f"- **Tema:** {it['issue']}",
                   f"- **Posición del Director (Claude/Opus):** {it['opus_position']}", f"- **Posición del Supervisor:** {it['supervisor_position']}",
                   f"- **Evidencia Director:** {', '.join(it['opus_evidence']) or '—'}", f"- **Evidencia Supervisor:** {', '.join(it['supervisor_evidence']) or '—'}",
                   f"- **Riesgo:** {it['risk']}", "- **Resoluciones posibles:** " + "; ".join(it["possible_resolutions"]),
                   "- **Decisión del Product Owner:** (pendiente)", ""]
        (d / "disagreement.md").write_text(redact("\n".join(md)), encoding="utf-8")
        return self._stop_human(rid, self._round_count(rid)[0], f"desacuerdo técnico repetido en {', '.join(hot)}: ver disagreement.md")
