#!/usr/bin/env python3
"""Compatibilidad con la fábrica 1.0: revisión de un solo paquete, sin registro.

Para gates reales usa `python -m factory supervisor review` (registro, idempotencia,
locks, rondas de evidencia, delta). Este envoltorio usa los mismos adaptadores,
validación y política, y conserva la interfaz y los códigos de salida de 1.0:

    python supervisor.py --packet paquete.json --out veredicto.json [--md veredicto.md] [--validate-only]

0 PASS/PASS_WITH_ACTIONS sin bloqueos · 1 BLOCKED · 2 paquete inválido · 3 SUPERVISOR_PENDING ·
4 NEED_EVIDENCE · 5 HUMAN_REVIEW_REQUIRED / INVALID_SUPERVISOR_RESPONSE
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from factory import FACTORY_VERSION, packet as pk, policy  # noqa: E402
from factory.adapters import SupervisorAdapter, make_adapter  # noqa: E402
from factory.config import load_config  # noqa: E402

EXIT_OK, EXIT_BLOCKED, EXIT_BAD_PACKET, EXIT_PENDING, EXIT_EVIDENCE, EXIT_HUMAN = 0, 1, 2, 3, 4, 5


def render_markdown(result: dict) -> str:
    v = result.get("verdict") or {}
    lines = [f"## Supervisor · {result['status']}", ""]
    if v:
        lines += [f"**Gate:** {v['gate']} · **Commit:** `{v['commit']}` · **Siguiente gate:** {v['next_gate']} · **Confianza:** {v['confidence']}", "", v["summary"], ""]
        if v["findings"]:
            lines += ["| ID | Severidad | Bloquea | Dominio | Hallazgo |", "|---|---|---|---|---|"]
            for f in v["findings"]:
                lines.append(f"| {f['id']} | {f['severity']} | {'sí' if f['blocking'] else 'no'} | {f['domain']} | {f['finding']} |")
            lines.append("")
        if v["required_actions"]:
            lines += ["**Acciones:**", *[f"- {a['id']} ({'bloqueante' if a['blocking'] else 'no bloqueante'}): {a['action']}" for a in v["required_actions"]], ""]
        if v["optional_improvements"]:
            lines += ["**Mejoras opcionales:**", *[f"- {a}" for a in v["optional_improvements"]], ""]
        if v["evidence_requests"]:
            lines += ["**Evidencia solicitada:**", *[f"- `{e['type']}` {e['ref']} {e['lines'] or ''}: {e['reason']}" for e in v["evidence_requests"]], ""]
        for n in (v.get("policy") or {}).get("notes", []):
            lines.append(f"> {n}")
    if result.get("detail"):
        lines += ["", f"> {result['detail']}"]
    lines += ["", f"<sub>fábrica {FACTORY_VERSION} · modelo {result.get('model')}</sub>"]
    return "\n".join(lines) + "\n"


def run(packet_path: Path, out: Path, md: Path | None, adapter: SupervisorAdapter | None = None) -> int:
    def finish(status: str, code: int, **extra: Any) -> int:
        result = {"status": status, "exit_code": code, "factory_version": FACTORY_VERSION, **extra}
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if md:
            md.write_text(render_markdown(result), encoding="utf-8")
        print(f"supervisor: {status} (exit {code})")
        return code

    cfg = load_config()
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return finish("INVALID_PACKET", EXIT_BAD_PACKET, detail=f"no se pudo leer el paquete: {e}")
    p, errs = pk.finalize(packet, cfg["SUPERVISOR_MAX_CONTEXT_BYTES"])
    if errs:
        return finish("INVALID_PACKET", EXIT_BAD_PACKET, detail="; ".join(errs[:10]))
    ad = adapter or make_adapter(cfg)
    ar = ad.review(p)
    if ar.verdict is not None and policy.semantic_errors(ar.verdict):
        ar.verdict, ar.status, ar.detail = None, "INVALID_SUPERVISOR_RESPONSE", "; ".join(policy.semantic_errors(ar.verdict))
    if ar.verdict is None:
        if ar.status in ("SUPERVISOR_UNAVAILABLE", "SUPERVISOR_PENDING"):
            return finish("SUPERVISOR_PENDING", EXIT_PENDING, detail=ar.detail, model=ar.model, calls=ar.calls)
        return finish("HUMAN_REVIEW_REQUIRED", EXIT_HUMAN, detail=ar.status + ": " + ar.detail, model=ar.model, calls=ar.calls)
    eff, _ = policy.apply(ar.verdict, p, cfg.blocking_severities)
    effect = eff["policy"]["gate_effect"]
    code = {"CAN_CLOSE": EXIT_OK, "CAN_CONTINUE_WITH_PENDING_ACTIONS": EXIT_OK, "CANNOT_CLOSE": EXIT_BLOCKED,
            "AWAITING_EVIDENCE": EXIT_EVIDENCE, "HUMAN_REVIEW": EXIT_HUMAN}[effect]
    return finish(eff["verdict"], code, verdict=eff, model=ar.model, calls=ar.calls, packet_hash=pk.packet_hash(p))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packet", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--md", type=Path)
    ap.add_argument("--validate-only", action="store_true", help="solo valida el paquete; no llama al proveedor")
    a = ap.parse_args(argv)
    if a.validate_only:
        try:
            _, errs = pk.finalize(json.loads(a.packet.read_text(encoding="utf-8")), load_config()["SUPERVISOR_MAX_CONTEXT_BYTES"])
        except (OSError, json.JSONDecodeError) as e:
            errs = [str(e)]
        print("\n".join(errs) if errs else "paquete válido")
        return EXIT_BAD_PACKET if errs else EXIT_OK
    if not a.out:
        ap.error("--out es obligatorio salvo con --validate-only")
    return run(a.packet, a.out, a.md)


if __name__ == "__main__":
    sys.exit(main())
