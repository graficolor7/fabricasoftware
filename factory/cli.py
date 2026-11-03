"""CLI de la fábrica.

    python -m factory status                       estado compacto del proyecto
    python -m factory supervisor review …          revisión de gate (STEP 1 + evidencia)
    python -m factory supervisor evidence …        STEP 2 con evidencia aportada a mano
    python -m factory supervisor delta …           re-revisión tras correcciones
    python -m factory supervisor retry …           repetir la ronda sin veredicto
    python -m factory supervisor baseline …        BASELINE_SUPERVISOR_REVIEW
    python -m factory supervisor close …           cerrar el gate (si el veredicto lo permite)
    python -m factory supervisor override …        aceptación de riesgo del PO (no borra el hallazgo)
    python -m factory supervisor validate …        validar un borrador y medir su tamaño (sin llamadas)
    python -m factory supervisor health            health check del proveedor (sin tokens)
    python -m factory supervisor show …            resumen de una revisión
    python -m factory trigger evaluate …           qué triggers activa un cambio (sin llamadas)
    python -m factory config                       configuración efectiva (sin secretos)

Todos aceptan --project-root (por defecto, el directorio actual).
Códigos de salida: 0 puede cerrar/continuar · 1 BLOCKED · 2 uso/paquete inválido · 3 SUPERVISOR_PENDING/UNAVAILABLE ·
4 esperando evidencia · 5 HUMAN_REVIEW_REQUIRED · 6 INVALID_SUPERVISOR_RESPONSE · 7 LOCKED · 9 VERIFICATION_FAILED.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from factory import git
from factory import packet as pk
from factory.config import load_config
from factory.errors import FactoryError
from factory.gate import add_override, close_gate
from factory.profile import evaluate_paths, load_profile, triggers
from factory.review import Orchestrator
from factory.security import redact
from factory.state import render_status, status_report
from factory.store import ReviewStore, read_json


def _load_json(path: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FactoryError(f"no se pudo leer {path}: {e}") from e


def _ctx(a):
    root = Path(a.project_root).resolve()
    profile = load_profile(root)
    overrides = {"SUPERVISOR_PROVIDER": getattr(a, "provider", None), "SUPERVISOR_MODEL": getattr(a, "model", None)}
    cfg = load_config(profile.get("config"), overrides=overrides)
    return root, profile, cfg


def _print_result(res, as_json: bool) -> int:
    d = res.as_dict()
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(f"supervisor: {d['status']}" + (f" · {d.get('gate_effect')}" if d.get("gate_effect") and d["status"] not in ("USAGE_ERROR", "INVALID_PACKET", "LOCKED", "VERIFICATION_FAILED") else "") + f" (exit {d['exit_code']})")
        for k in ("review_id", "round", "summary", "detail", "directory", "duplicate_of"):
            if d.get(k) not in (None, ""):
                print(f"  {k}: {d[k]}")
        for k in ("blockers", "pending_actions", "errors"):
            for x in d.get(k, []):
                print(f"  {k[:-1]}: {x}")
        for e in d.get("evidence_requests", []):
            print(f"  evidence_request: {e['type']} {e['ref']} {e.get('lines') or ''} · {e['reason']}")
        u = d.get("usage") or {}
        if u.get("calls"):
            print(f"  uso: {u['calls']} llamada(s), entrada {u['input_tokens']} (caché {u['cached_tokens']}), salida {u['output_tokens']}, "
                  f"modelo {','.join(u['models'])}, costo {u['cost_estimate'] if u['cost_estimate'] is not None else 'no calculado'}")
    return d["exit_code"]


def cmd_status(a) -> int:
    root = Path(a.project_root).resolve()
    rep = status_report(root, load_profile(root))
    print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else render_status(rep))
    return 0


def cmd_review(a) -> int:
    root, profile, cfg = _ctx(a)
    o = Orchestrator(root, profile, cfg)
    draft = _load_json(a.draft)
    kw = dict(gate=a.gate, milestone=a.milestone, commit=a.commit, run_verification=a.run_verification, force_new=a.force_new,
              auto_evidence=not a.no_auto_evidence)
    res = o.baseline(draft, **kw) if a.cmd == "baseline" else o.review(draft, trigger=a.trigger, **kw)
    return _print_result(res, a.json)


def cmd_evidence(a) -> int:
    root, profile, cfg = _ctx(a)
    provided = _load_json(a.evidence_file) if a.evidence_file else None
    return _print_result(Orchestrator(root, profile, cfg).evidence(a.review, provided, auto=not a.no_auto), a.json)


def cmd_delta(a) -> int:
    root, profile, cfg = _ctx(a)
    return _print_result(Orchestrator(root, profile, cfg).delta(a.review, _load_json(a.draft), a.commit, a.run_verification), a.json)


def cmd_retry(a) -> int:
    root, profile, cfg = _ctx(a)
    return _print_result(Orchestrator(root, profile, cfg).retry(a.review), a.json)


def _store(root, profile, cfg) -> ReviewStore:
    return ReviewStore(root, profile["project_id"], profile["review_id_prefix"], profile.get("reviews_dir", ".factory/reviews"), cfg["SUPERVISOR_LOCK_TTL_S"])


def cmd_close(a) -> int:
    root, profile, cfg = _ctx(a)
    out = close_gate(root, profile, _store(root, profile, cfg), a.milestone, a.gate, a.commit)
    print(json.dumps({"closed": out}, ensure_ascii=False, indent=2))
    return 0


def cmd_override(a) -> int:
    if not a.i_am_the_product_owner:
        raise FactoryError("un override solo lo autoriza el Product Owner humano: añade --i-am-the-product-owner (queda registrado)")
    root, profile, cfg = _ctx(a)
    row = add_override(root, profile, _store(root, profile, cfg), a.review, a.finding, a.reason, a.authorized_by, a.risk_accepted, a.expires)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(a) -> int:
    root, profile, cfg = _ctx(a)
    draft = pk.upgrade_v1(_load_json(a.draft))
    draft.setdefault("schema_version", "2")
    draft.setdefault("packet_kind", "INITIAL")
    rv = draft.setdefault("review", {})
    for k in ("milestone", "gate", "trigger"):
        if getattr(a, k, None):
            rv[k] = getattr(a, k)
    if draft["packet_kind"] == "BASELINE":
        rv.setdefault("trigger", "BASELINE_SUPERVISOR_REVIEW")
    proj = draft.setdefault("project", {})
    proj.setdefault("id", profile["project_id"])
    proj.setdefault("repository", profile["repository"])
    if git.is_repo(root):
        proj.setdefault("commit", git.head(root))
        proj.setdefault("branch", git.branch(root))
    p, errs = pk.finalize(draft, cfg["SUPERVISOR_MAX_CONTEXT_BYTES"])
    from factory.adapters.base import SupervisorAdapter
    from factory.config import prompt_path
    msg_bytes = len(SupervisorAdapter.user_message(p).encode("utf-8"))
    prompt_bytes = len(prompt_path(cfg).read_bytes())
    from factory.schemas import strict_verdict_schema
    schema_bytes = len(json.dumps(strict_verdict_schema()).encode("utf-8"))
    total = msg_bytes + prompt_bytes + schema_bytes
    out = {"valid": not errs, "errors": errs, "packet_bytes": p["meta"]["size_bytes"], "request_bytes_estimate": total,
           "input_tokens_estimate": f"~{total // 4} (aprox. 4 bytes/token; la cifra real la da la API en usage)",
           "packet_hash": pk.packet_hash(p), "injection_flags": p["meta"]["injection_flags"]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not errs else 2


def cmd_health(a) -> int:
    root, profile, cfg = _ctx(a)
    from factory.adapters import make_adapter
    h = make_adapter(cfg).health_check()
    h["api_key_present"] = bool(os.environ.get("OPENAI_API_KEY")) if cfg.provider == "openai" else None
    print(json.dumps(h, ensure_ascii=False, indent=2))
    return 0 if h["ok"] else 3


def cmd_show(a) -> int:
    root, profile, cfg = _ctx(a)
    d = _store(root, profile, cfg).review_dir(a.review)
    rec = read_json(d / "review.json", {})
    v = read_json(d / "verdict.json", {}) or {}
    out = {"review": rec, "verdict": {k: v.get(k) for k in ("round", "verdict", "summary", "commit", "model", "findings", "required_actions", "policy")}}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_trigger(a) -> int:
    root = Path(a.project_root).resolve()
    profile = load_profile(root)
    files = [f for _, f in git.changed_files(root, a.base, a.head)]
    hits = evaluate_paths(profile, files)
    out = {"changed_files": len(files), "supervisor_required": bool(hits), "triggers": hits,
           "note": "solo aviso: el supervisor se llama en el gate correspondiente, no en cada commit"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_config(a) -> int:
    root = Path(a.project_root).resolve()
    profile = load_profile(root)
    cfg = load_config(profile.get("config"))
    out = {"values": cfg.public(), "sources": dict(cfg.sources), "triggers": sorted(triggers(profile)),
           "OPENAI_API_KEY_present": bool(os.environ.get("OPENAI_API_KEY"))}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="factory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-root", default=".")
    sub = ap.add_subparsers(dest="group", required=True)

    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    c = sub.add_parser("config")
    c.set_defaults(fn=cmd_config)

    t = sub.add_parser("trigger")
    tsub = t.add_subparsers(dest="cmd", required=True)
    te = tsub.add_parser("evaluate")
    te.add_argument("--base", required=True)
    te.add_argument("--head", default="HEAD")
    te.set_defaults(fn=cmd_trigger)

    sp = sub.add_parser("supervisor")
    ssub = sp.add_subparsers(dest="cmd", required=True)

    def common(p, provider=True):
        p.add_argument("--json", action="store_true")
        if provider:
            p.add_argument("--provider", choices=["openai", "mock", "manual"])
            p.add_argument("--model")

    for name in ("review", "baseline"):
        r = ssub.add_parser(name)
        r.add_argument("--draft", required=True, help="borrador del paquete (JSON) preparado por el Director")
        r.add_argument("--gate", required=True)
        r.add_argument("--milestone", required=True)
        if name == "review":
            r.add_argument("--trigger", required=True)
        r.add_argument("--commit")
        r.add_argument("--run-verification", action="store_true")
        r.add_argument("--force-new", action="store_true")
        r.add_argument("--no-auto-evidence", action="store_true")
        common(r)
        r.set_defaults(fn=cmd_review)

    e = ssub.add_parser("evidence")
    e.add_argument("--review", required=True)
    e.add_argument("--evidence-file")
    e.add_argument("--no-auto", action="store_true")
    common(e)
    e.set_defaults(fn=cmd_evidence)

    dl = ssub.add_parser("delta")
    dl.add_argument("--review", required=True)
    dl.add_argument("--draft", required=True)
    dl.add_argument("--commit")
    dl.add_argument("--run-verification", action="store_true")
    common(dl)
    dl.set_defaults(fn=cmd_delta)

    rt = ssub.add_parser("retry")
    rt.add_argument("--review", required=True)
    common(rt)
    rt.set_defaults(fn=cmd_retry)

    cl = ssub.add_parser("close")
    cl.add_argument("--milestone", required=True)
    cl.add_argument("--gate", required=True)
    cl.add_argument("--commit")
    cl.set_defaults(fn=cmd_close, provider=None, model=None)

    ov = ssub.add_parser("override")
    ov.add_argument("--review", required=True)
    ov.add_argument("--finding", required=True)
    ov.add_argument("--reason", required=True)
    ov.add_argument("--authorized-by", required=True)
    ov.add_argument("--risk-accepted", required=True)
    ov.add_argument("--expires")
    ov.add_argument("--i-am-the-product-owner", action="store_true")
    ov.set_defaults(fn=cmd_override, provider=None, model=None)

    va = ssub.add_parser("validate")
    va.add_argument("--draft", required=True)
    va.add_argument("--milestone")
    va.add_argument("--gate")
    va.add_argument("--trigger")
    va.set_defaults(fn=cmd_validate, provider=None, model=None)

    he = ssub.add_parser("health")
    common(he)
    he.set_defaults(fn=cmd_health)

    sh = ssub.add_parser("show")
    sh.add_argument("--review", required=True)
    sh.set_defaults(fn=cmd_show, provider=None, model=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except FactoryError as e:
        print("factory: " + redact(str(e)), file=sys.stderr)
        return 2
