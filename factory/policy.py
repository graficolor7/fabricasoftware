"""Reglas deterministas de la fábrica sobre el veredicto del modelo.

El modelo propone; la política decide el efecto sobre el gate. Las reglas son
conservadoras: nunca convierten un bloqueo en PASS (salvo override del PO, que no borra
el hallazgo) y nunca interpretan texto libre.
"""
from __future__ import annotations

import copy
import re

ID_RE = re.compile(r"^[A-Z]{2,5}-\d{1,5}$")

# Efecto sobre el gate
CAN_CLOSE = "CAN_CLOSE"
CAN_CONTINUE = "CAN_CONTINUE_WITH_PENDING_ACTIONS"
CANNOT_CLOSE = "CANNOT_CLOSE"
AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
HUMAN_REVIEW = "HUMAN_REVIEW"
GATE_OPEN = "GATE_OPEN"  # sin veredicto válido (pendiente, no disponible, inválido…)


def semantic_errors(v: dict) -> list[str]:
    """Incoherencias que hacen el veredicto inutilizable (se tratan como respuesta inválida)."""
    errs = []
    ids = [f["id"] for f in v["findings"]]
    if len(ids) != len(set(ids)):
        errs.append("IDs de hallazgo duplicados")
    act_ids = [a["id"] for a in v["required_actions"]]
    if len(act_ids) != len(set(act_ids)):
        errs.append("IDs de acción duplicados")
    for a in v["required_actions"]:
        if a["finding_id"] is not None and a["finding_id"] not in ids:
            errs.append(f"la acción {a['id']} apunta a un hallazgo inexistente {a['finding_id']}")
    if v["verdict"] == "NEED_EVIDENCE" and not v["evidence_requests"]:
        errs.append("NEED_EVIDENCE sin evidence_requests")
    if not v["summary"].strip():
        errs.append("summary vacío")
    return errs


def apply(verdict: dict, packet: dict, blocking_severities: set[str], overrides: list[dict] | None = None) -> tuple[dict, list[str]]:
    """Devuelve (veredicto efectivo, notas). No modifica el original."""
    v = copy.deepcopy(verdict)
    notes: list[str] = []
    model_verdict = v["verdict"]

    if v["commit"][:7] != packet["project"]["commit"][:7]:
        notes.append(f"el veredicto cita el commit {v['commit'][:12]!r}; se usa el del paquete {packet['project']['commit'][:12]}")
        v["commit"] = packet["project"]["commit"]
    if v["gate"] != packet["review"]["gate"]:
        notes.append(f"el veredicto cita el gate {v['gate']!r}; se usa el del paquete {packet['review']['gate']}")
        v["gate"] = packet["review"]["gate"]

    for f in v["findings"]:
        if f["severity"] in blocking_severities and not f["blocking"]:
            f["blocking"] = True
            notes.append(f"{f['id']}: severidad {f['severity']} bloquea por política")
        elif f["severity"] in ("LOW", "INFO") and f["blocking"]:
            f["blocking"] = False
            notes.append(f"{f['id']}: severidad {f['severity']} no bloquea por política; queda como acción pendiente")

    overridden = {o["finding_id"]: o for o in (overrides or [])}
    accepted = []
    for f in v["findings"]:
        if f["blocking"] and f["id"] in overridden:
            o = overridden[f["id"]]
            accepted.append(f"{f['id']} aceptado por {o['authorized_by']} ({o['override_id']})")
            f["blocking"] = False
            f["overridden_by"] = o["override_id"]
    blocking_ids = {f["id"] for f in v["findings"] if f["blocking"]}
    for a in v["required_actions"]:
        if a["finding_id"] in blocking_ids and not a["blocking"]:
            a["blocking"] = True
            notes.append(f"{a['id']}: corrige un hallazgo bloqueante → acción bloqueante")
        if a["finding_id"] in overridden and a["blocking"]:
            a["blocking"] = False
    blocking_actions = [a["id"] for a in v["required_actions"] if a["blocking"]]

    eff = model_verdict
    if eff in ("PASS", "PASS_WITH_ACTIONS") and blocking_ids:
        notes.append(f"{len(blocking_ids)} hallazgo(s) bloqueante(s) → BLOCKED (el modelo dijo {model_verdict})")
        eff = "BLOCKED"
    if eff in ("PASS", "PASS_WITH_ACTIONS") and v["evidence_requests"]:
        notes.append(f"{eff} con evidencia pendiente → NEED_EVIDENCE")
        eff = "NEED_EVIDENCE"
    if eff == "PASS" and v["required_actions"]:
        notes.append("PASS con acciones → PASS_WITH_ACTIONS")
        eff = "PASS_WITH_ACTIONS"
    if eff == "BLOCKED" and not blocking_ids:
        if accepted:
            notes.append("BLOCKED solo por hallazgos aceptados por el PO → PASS_WITH_ACTIONS")
            eff = "PASS_WITH_ACTIONS"
        else:
            notes.append("BLOCKED sin hallazgos bloqueantes explícitos; se respeta el bloqueo")

    effect = {
        "PASS": CAN_CLOSE,
        "PASS_WITH_ACTIONS": CANNOT_CLOSE if blocking_actions else CAN_CONTINUE,
        "BLOCKED": CANNOT_CLOSE,
        "NEED_EVIDENCE": AWAITING_EVIDENCE,
        "HUMAN_REVIEW_REQUIRED": HUMAN_REVIEW,
    }[eff]
    if eff == "PASS_WITH_ACTIONS" and blocking_actions:
        notes.append(f"acciones bloqueantes {blocking_actions}: se trata como BLOCKED hasta resolverlas")

    v["verdict"] = eff
    v["policy"] = {
        "model_verdict": model_verdict,
        "gate_effect": effect,
        "blocking_findings": sorted(blocking_ids),
        "blocking_actions": blocking_actions,
        "pending_actions": [f"{a['id']}: {a['action']}" for a in v["required_actions"] if not a["blocking"]],
        "accepted_risks": accepted,
        "notes": notes,
    }
    return v, notes
