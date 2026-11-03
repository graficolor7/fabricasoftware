"""Supervisor Packet v2: completar, validar, medir, hashear y derivar (EVIDENCE / DELTA).

El paquete es compacto: resúmenes, rutas, SHA y referencias. Nunca el repo completo.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from factory import FACTORY_VERSION, schemas
from factory.security import find_secrets, injection_flags

UNTRUSTED_TEXT_FIELDS = ("objective", "summary", "diff_summary", "content", "position", "resolution", "note", "text")


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def packet_hash(packet: dict) -> str:
    """Hash estable del contenido revisable (sin `meta`, que lleva fecha y tamaños)."""
    body = {k: v for k, v in packet.items() if k != "meta"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def size_bytes(packet: dict) -> int:
    return len(canonical(packet).encode("utf-8"))


def upgrade_v1(p: dict) -> dict:
    """Convierte un paquete v1 (fábrica 1.0, plano) a v2."""
    if p.get("schema_version") != "1":
        return p
    kind_map = {"GATE_CLOSURE": "MILESTONE_COMPLETION", "ARCHITECTURE_REVIEW": "ARCHITECTURE_MAJOR_CHANGE",
                "CONTRACT_FREEZE": "SHARED_CONTRACT_FREEZE", "SECURITY_REVIEW": "SECURITY_CRITICAL_CHANGE",
                "PRE_RELEASE": "PRE_RELEASE", "DISAGREEMENT": "MANUAL_SUPERVISOR_REQUEST"}
    v2: dict[str, Any] = {
        "schema_version": "2",
        "factory_version": p.get("factory_version", FACTORY_VERSION),
        "packet_kind": "INITIAL",
        "project": {"id": p["project_id"], "repository": p.get("repository", "unknown/unknown"), "branch": p.get("branch", "unknown"), "commit": p["commit"]},
        "review": {"milestone": p["milestone"], "gate": p["gate"], "trigger": kind_map.get(p.get("requested_verdict", ""), "MANUAL_SUPERVISOR_REQUEST"),
                   "objective": p["objective"], "requested_verdict": True},
        "implementation": {"summary": p.get("implemented_summary") or "(sin resumen)", "domains_changed": p.get("changed_domains", [])},
        "verification": {"unit": {"status": "PARTIAL", "summary": p.get("test_summary", "")[:2000]}},
        "known_failures": p.get("known_failures", []),
        "risks": p.get("risks", []),
        "open_questions": p.get("open_questions", []),
        "evidence_index": [{"ref": e["ref"], "kind": "other", **({"result": e["result"]} if e.get("result") else {})} for e in p.get("evidence_index", [])],
    }
    if p.get("project_version"):
        v2["project"]["version"] = p["project_version"]
    if p.get("diff_summary"):
        v2["implementation"]["diff_summary"] = p["diff_summary"]
    if p.get("architecture_version"):
        v2["architecture"] = {"version": p["architecture_version"], "changed": False, "adrs": []}
    if p.get("contract_versions"):
        v2["contracts"] = {"versions": p["contract_versions"], "changed": []}
    if p.get("requirements_status"):
        v2["review"]["acceptance_criteria"] = [{k: r[k] for k in ("id", "status", "evidence") if k in r} for r in p["requirements_status"]]
    for key in ("previous_findings", "resolved_findings"):
        if p.get(key):
            v2[key] = [{"id": f"V1-{i + 1}", "summary": s[:1000]} for i, s in enumerate(p[key])]
    if p.get("attached_evidence"):
        v2["attached_evidence"] = [{"ref": a["ref"], "type": "other", "content": a["content"]} for a in p["attached_evidence"]]
    for opt in ("benchmark_summary", "security_summary"):
        if p.get(opt):
            v2["verification"]["benchmarks" if opt.startswith("bench") else "security"] = {"status": "PARTIAL", "summary": p[opt][:2000]}
    return v2


def _walk_strings(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}/{i}")
    elif isinstance(obj, str):
        yield path, obj


def finalize(packet: dict, max_bytes: int) -> tuple[dict, list[str]]:
    """Añade `meta`, valida schema, secretos y tamaño. Devuelve (paquete, errores)."""
    p = copy.deepcopy(upgrade_v1(packet))
    p.setdefault("factory_version", FACTORY_VERSION)
    p.pop("meta", None)
    errs = schemas.errors("supervisor-packet", p)
    raw = canonical(p)
    secrets = find_secrets(raw)
    if secrets:
        errs.append("el paquete contiene algo con forma de secreto o credencial (" + ", ".join(sorted(set(secrets))) + "); no se envía")
    flags = injection_flags(_walk_strings(p))
    p["meta"] = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": f"fabricasoftware {FACTORY_VERSION}",
        "injection_flags": flags,
        "size_bytes": 0,
    }
    p["meta"]["size_bytes"] = size_bytes(p)
    if p["meta"]["size_bytes"] > max_bytes:
        errs.append(f"paquete demasiado grande ({p['meta']['size_bytes']} bytes > SUPERVISOR_MAX_CONTEXT_BYTES={max_bytes}); resume y pide evidencia bajo demanda")
    return p, errs


def evidence_packet(base: dict, attached: list[dict], unavailable: list[dict], last_verdict: dict) -> dict:
    """Mismo paquete + solo la evidencia pedida. Los hallazgos de la ronda anterior van como contexto."""
    p = copy.deepcopy(base)
    p.pop("meta", None)
    p["packet_kind"] = "EVIDENCE"
    p["attached_evidence"] = attached
    if unavailable:
        p["unavailable_evidence"] = unavailable
    prev = list(p.get("previous_findings", []))
    for f in last_verdict.get("findings", []):
        prev.append({"id": f["id"], "severity": f["severity"], "summary": f["finding"][:1000]})
    if prev:
        p["previous_findings"] = prev[-100:]
    return p


DELTA_REQUIRED = ("previous_review_id", "previous_commit", "new_commit", "findings_addressed", "changed_files",
                  "new_test_results", "new_evidence", "remaining_risks")
