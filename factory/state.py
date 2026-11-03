"""Estado del supervisor del proyecto (.factory/SUPERVISOR_STATE.json) y `factory status`.

Solo se escribe después de persistir la ronda correspondiente. `factory status` lee
PROJECT_STATE + SUPERVISOR_STATE + git sin cargar documentación.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from factory import git, schemas
from factory.errors import FactoryError
from factory.store import now, read_json, write_json

DEFAULT_STATE = ".factory/SUPERVISOR_STATE.json"


def state_path(root: Path, profile: dict) -> Path:
    p = (root / profile.get("supervisor_state", DEFAULT_STATE)).resolve()
    if root.resolve() not in p.parents:
        raise FactoryError("supervisor_state debe estar dentro del repo")
    return p


def load_state(root: Path, profile: dict) -> dict:
    return read_json(state_path(root, profile), None) or {"schema_version": "1", "project_id": profile["project_id"], "updated_at": now(),
                                                          "last_review_id": None, "gates": {}}


def update_gate_state(root: Path, profile: dict, meta: dict, review_id: str, rnd: int, commit: str, status: str, gate_effect: str,
                      verdict: dict | None, model: str | None, detail: str = "") -> None:
    st = load_state(root, profile)
    pol = (verdict or {}).get("policy", {})
    prev = st["gates"].get(meta["gate_key"], {})
    entry: dict[str, Any] = {
        "milestone": meta["milestone"], "gate": meta["gate"], "trigger": meta["trigger"], "status": status, "gate_effect": gate_effect,
        "review_id": review_id, "round": rnd, "commit": commit, "verdict": (verdict or {}).get("verdict"),
        "supervisor_model": model,
        "blockers": pol.get("blocking_findings", []) + pol.get("blocking_actions", []) if verdict else prev.get("blockers", []),
        "pending_actions": pol.get("pending_actions", []) if verdict else prev.get("pending_actions", []),
        "accepted_risks": pol.get("accepted_risks", []) if verdict else prev.get("accepted_risks", []),
        "updated_at": now(),
    }
    if detail and not verdict:
        entry["blockers"] = list(entry["blockers"]) + [f"{status}: {detail[:200]}"]
    st["gates"][meta["gate_key"]] = entry
    st["last_review_id"] = review_id
    st["updated_at"] = now()
    errs = schemas.errors("supervisor-state", st)
    if errs:
        raise FactoryError("estado del supervisor inválido: " + "; ".join(errs[:3]))
    write_json(state_path(root, profile), st)


def status_report(root: Path, profile: dict) -> dict:
    ps = read_json(root / profile.get("project_state", ".factory/PROJECT_STATE.json"), {}) or {}
    st = load_state(root, profile)
    milestone = ps.get("current_milestone", "?")
    gates = st["gates"]
    cur = [g for k, g in gates.items() if g["milestone"] == milestone]
    last = max(cur or list(gates.values()) or [{}], key=lambda g: g.get("updated_at", ""))
    try:
        head = git.head(root)[:12]
        clean = git.is_clean(root)
    except FactoryError:
        head, clean = ps.get("last_verified_commit", "?"), None
    tests = "?"
    rid = last.get("review_id")
    if rid:
        from factory.store import ReviewStore
        store = ReviewStore(root, profile["project_id"], profile["review_id_prefix"], profile.get("reviews_dir", ".factory/reviews"))
        try:
            pkt = read_json(store.review_dir(rid) / "packet.json", {}) or {}
            ver = pkt.get("verification", {})
            tests = ", ".join(f"{k}={v['status']}" for k, v in ver.items() if isinstance(v, dict) and "status" in v) or "sin datos"
        except FactoryError:
            pass
    blockers = sorted({b for g in gates.values() for b in g.get("blockers", []) if g.get("gate_effect") != "CLOSED"})
    pending = sorted({a for g in gates.values() for a in g.get("pending_actions", [])})
    effect = last.get("gate_effect")
    if not last:
        nxt = ps.get("next_action") or "sin revisiones del supervisor todavía"
    elif effect in ("CAN_CLOSE", "CAN_CONTINUE_WITH_PENDING_ACTIONS"):
        nxt = f"cerrar el gate {last['gate']} (`factory supervisor close`)" + (" y convertir las acciones pendientes en tareas" if pending else "")
    elif effect == "CANNOT_CLOSE":
        nxt = f"remediar {', '.join(last.get('blockers', [])) or 'los bloqueos'} → commit → `factory supervisor delta --review {rid}`"
    elif effect == "AWAITING_EVIDENCE":
        nxt = f"`factory supervisor evidence --review {rid}`"
    elif effect == "HUMAN_REVIEW":
        nxt = "decisión del Product Owner (ver review.json / disagreement.md)"
    elif effect == "CLOSED":
        nxt = ps.get("next_action") or "siguiente gate"
    else:
        nxt = f"`factory supervisor retry --review {rid}` cuando el proveedor esté disponible"
    return {
        "PROJECT": profile["project_id"], "MILESTONE": milestone, "GATE": last.get("gate", "—"),
        "COMMIT": head + ("" if clean in (None, True) else " (árbol con cambios sin commit)"),
        "TEST STATUS": tests, "SUPERVISOR STATUS": last.get("status", "SIN_REVISIONES"), "LAST REVIEW": rid or "—",
        "BLOCKERS": blockers or ["ninguno"], "PENDING ACTIONS": pending or ["ninguna"], "NEXT ACTION": nxt,
    }


def render_status(rep: dict) -> str:
    out = []
    for k, v in rep.items():
        if isinstance(v, list):
            out.append(f"{k:<18} " + ("; ".join(v)))
        else:
            out.append(f"{k:<18} {v}")
    return "\n".join(out)
