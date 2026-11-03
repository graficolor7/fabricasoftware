"""Cierre de gate y overrides del Product Owner."""
from __future__ import annotations

from pathlib import Path

from factory import git, policy, schemas
from factory.errors import FactoryError
from factory.state import load_state, state_path, update_gate_state
from factory.store import ReviewStore, now, read_json, write_json


def close_gate(root: Path, profile: dict, store: ReviewStore, milestone: str, gate: str, commit: str | None = None) -> dict:
    """Cierra un gate solo si la última revisión válida lo permite para el commit actual."""
    gate_key = f"{milestone}/{gate}"
    st = load_state(root, profile)
    g = st["gates"].get(gate_key)
    if not g:
        raise FactoryError(f"no hay revisión del supervisor para {gate_key}: el gate no puede cerrar")
    if g["gate_effect"] not in (policy.CAN_CLOSE, policy.CAN_CONTINUE):
        raise FactoryError(f"{gate_key} está en {g['status']} ({g['gate_effect']}): no puede cerrar")
    if g.get("provider") == "mock" and not profile.get("config", {}).get("SUPERVISOR_ALLOW_MOCK_CLOSURE"):
        raise FactoryError("la revisión la hizo el MockSupervisorAdapter: no cierra gates reales (solo pruebas)")
    target = commit or (git.head(root) if git.is_repo(root) else g["commit"])
    if target[:12] != g["commit"][:12]:
        raise FactoryError(f"la revisión válida es del commit {g['commit'][:12]} y se quiere cerrar en {target[:12]}: hace falta un delta")
    rid = g["review_id"]
    d = store.review_dir(rid)
    v = read_json(d / "verdict.json")
    if not v or v.get("round") != g["round"]:
        raise FactoryError("el veredicto persistido no coincide con el estado; no se cierra")
    rec = read_json(d / "review.json", {})
    rec["closed_by"] = {"review_id": rid, "round": g["round"], "commit": g["commit"], "verdict": v["verdict"], "at": now(),
                        "pending_actions": v["policy"]["pending_actions"], "accepted_risks": v["policy"]["accepted_risks"]}
    write_json(d / "review.json", rec)
    store.set_status(rid, "CLOSED")
    st["gates"][gate_key].update({"gate_effect": "CLOSED", "status": "CLOSED", "updated_at": now(), "blockers": []})
    st["updated_at"] = now()
    write_json(state_path(root, profile), st)
    return rec["closed_by"]


def add_override(root: Path, profile: dict, store: ReviewStore, review_id: str, finding_id: str, reason: str, authorized_by: str,
                 risk_accepted: str, expires: str | None = None) -> dict:
    """Registra la aceptación de riesgo del PO. El hallazgo NO se borra."""
    d = store.review_dir(review_id)
    v = read_json(d / "verdict.json")
    if not v:
        raise FactoryError("la revisión no tiene veredicto válido")
    f = next((x for x in v["findings"] if x["id"] == finding_id), None)
    if not f:
        raise FactoryError(f"{finding_id} no existe en el último veredicto de {review_id}")
    n = len(store.overrides()) + 1
    row = {"override_id": f"OVR-{store.prefix}-{n:04d}", "review_id": review_id, "finding_id": finding_id, "finding": f,
           "reason": reason, "authorized_by": authorized_by, "timestamp": now(), "risk_accepted": risk_accepted,
           "commit": v["commit"], "expires": expires}
    errs = schemas.errors("supervisor-override", row)
    if errs:
        raise FactoryError("override inválido: " + "; ".join(errs[:3]))
    store.add_override(row)
    # Recalcula el efecto del último veredicto con el override (el hallazgo sigue registrado).
    last_round = read_json(d / f"round-{v['round']:02d}.json")
    pkt_name = "packet.json"
    if last_round["kind"] in ("EVIDENCE", "DELTA"):
        pkt_name = f"{last_round['kind'].lower()}-{v['round']:02d}-packet.json"
    pkt = read_json(d / pkt_name)
    from factory.config import load_config
    cfg = load_config(profile.get("config"))
    eff, _ = policy.apply(last_round["verdict_raw"], pkt, cfg.blocking_severities, store.overrides(review_id))
    write_json(d / "verdict.json", {**{k: v[k] for k in ("review_id", "round", "commit", "packet_hash", "model", "created_at")}, **eff,
                                    "recomputed_after_override": row["override_id"]})
    meta = store.review_meta(review_id)
    update_gate_state(root, profile, meta, review_id, v["round"], v["commit"], eff["verdict"], eff["policy"]["gate_effect"], eff, v.get("model"),
                      provider=load_state(root, profile)["gates"].get(meta["gate_key"], {}).get("provider"))
    return row
