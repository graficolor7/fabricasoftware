#!/usr/bin/env python3
"""Adaptador del supervisor independiente (SupervisorAdapter).

Lee un Supervisor Packet, lo valida, se lo envía al proveedor configurado
(hoy: OpenAI, API Responses con salida JSON estricta), valida el veredicto,
aplica las reglas de la fábrica y escribe el resultado.

Uso:
    python supervisor.py --packet paquete.json --out veredicto.json [--md veredicto.md]

Variables de entorno:
    OPENAI_API_KEY   clave (obligatoria salvo con --dry-run)
    OPENAI_MODEL     modelo del supervisor (obligatorio; se fija en la config del repo)
    OPENAI_BASE_URL  opcional, por defecto https://api.openai.com/v1
    SUPERVISOR_TIMEOUT  segundos, por defecto 180

Códigos de salida (los usa CI como puerta):
    0  PASS o PASS_WITH_ACTIONS (sin acciones bloqueantes)
    1  BLOCKED
    2  paquete inválido o con posibles secretos (no se envía nada)
    3  SUPERVISOR_PENDING: proveedor no disponible (el estado del proyecto no cambia)
    4  EVIDENCE_REQUESTED: el supervisor pide más evidencia antes de decidir
    5  HUMAN_REVIEW_REQUIRED: respuesta inválida del supervisor tras reintento
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:
    import jsonschema
except ImportError:  # pragma: no cover
    sys.stderr.write("Falta la dependencia 'jsonschema' (pip install -r requirements.txt)\n")
    raise

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
PROMPT = ROOT / "prompts" / "CHATGPT_SUPERVISOR.md"
FACTORY_VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "unknown"

EXIT_OK, EXIT_BLOCKED, EXIT_BAD_PACKET, EXIT_PENDING, EXIT_EVIDENCE, EXIT_HUMAN = 0, 1, 2, 3, 4, 5

# Patrones de secretos que nunca deben salir hacia el supervisor.
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),            # OpenAI / Anthropic style
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),        # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),                  # AWS
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

MAX_PACKET_BYTES = 200_000


class SupervisorUnavailable(Exception):
    """El proveedor no respondió (red, 5xx, 429, timeout)."""


class InvalidVerdict(Exception):
    """El proveedor respondió, pero no con un veredicto válido."""


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def validate_packet(packet: Any) -> list[str]:
    errors = [e.message for e in jsonschema.Draft202012Validator(load_schema("supervisor-packet.schema.json")).iter_errors(packet)]
    raw = json.dumps(packet, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_PACKET_BYTES:
        errors.append(f"paquete demasiado grande ({len(raw)} bytes > {MAX_PACKET_BYTES}); enviar resumen y evidencia bajo demanda")
    for pat in SECRET_PATTERNS:
        if pat.search(raw):
            errors.append("el paquete contiene algo con forma de secreto o credencial; se bloquea el envío")
            break
    return errors


def validate_verdict(verdict: Any) -> list[str]:
    return [e.message for e in jsonschema.Draft202012Validator(load_schema("supervisor-verdict.schema.json")).iter_errors(verdict)]


def apply_policy(verdict: dict) -> tuple[dict, list[str]]:
    """Reglas deterministas de la fábrica sobre el veredicto del modelo.

    - Un hallazgo CRITICAL o marcado como bloqueante fuerza BLOCKED.
    - PASS con acciones obligatorias pasa a PASS_WITH_ACTIONS.
    - BLOCKED sin ningún hallazgo bloqueante es incoherente: se mantiene BLOCKED
      (el supervisor manda en el sentido conservador) pero se anota.
    """
    notes: list[str] = []
    v = dict(verdict)
    blocking = [f for f in v["findings"] if f["blocking"] or f["severity"] == "CRITICAL"]
    if blocking and v["verdict"] != "BLOCKED":
        notes.append(f"política: {len(blocking)} hallazgo(s) bloqueante(s) → BLOCKED (el modelo dijo {v['verdict']})")
        v["verdict"] = "BLOCKED"
    if v["verdict"] == "PASS" and v["required_actions"]:
        notes.append("política: PASS con acciones obligatorias → PASS_WITH_ACTIONS")
        v["verdict"] = "PASS_WITH_ACTIONS"
    if v["verdict"] == "BLOCKED" and not blocking:
        notes.append("aviso: BLOCKED sin hallazgos bloqueantes explícitos; se respeta el bloqueo")
    return v, notes


def build_request(packet: dict, model: str) -> dict:
    system = PROMPT.read_text(encoding="utf-8")
    schema = load_schema("supervisor-verdict.schema.json")
    for k in ("$schema", "$id", "title", "description"):
        schema.pop(k, None)
    return {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": "SUPERVISOR PACKET:\n" + json.dumps(packet, ensure_ascii=False, indent=2)},
        ],
        "text": {"format": {"type": "json_schema", "name": "supervisor_verdict", "schema": schema, "strict": True}},
    }


def extract_text(resp: dict) -> str:
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    for item in resp.get("output", []) or []:
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                return c["text"]
            if c.get("type") == "refusal":
                raise InvalidVerdict("el supervisor se negó a responder: " + str(c.get("refusal", "")))
    raise InvalidVerdict("respuesta sin texto de salida")


def openai_transport(request: dict) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SupervisorUnavailable("OPENAI_API_KEY no configurada")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    timeout = float(os.environ.get("SUPERVISOR_TIMEOUT", "180"))
    req = urllib.request.Request(
        base + "/responses",
        data=json.dumps(request).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        if e.code in (408, 409, 429) or e.code >= 500:
            raise SupervisorUnavailable(f"HTTP {e.code}: {body}") from e
        raise InvalidVerdict(f"HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SupervisorUnavailable(str(e)) from e


def ask_supervisor(packet: dict, model: str, transport: Callable[[dict], dict], attempts: int = 2) -> tuple[dict | None, dict]:
    """Devuelve (veredicto, telemetría). Un reintento solo si la respuesta fue inválida."""
    telemetry: dict[str, Any] = {"model": model, "attempts": 0, "usage": None, "errors": []}
    request = build_request(packet, model)
    telemetry["input_bytes"] = len(json.dumps(request).encode("utf-8"))
    t0 = time.monotonic()
    for i in range(attempts):
        telemetry["attempts"] = i + 1
        resp = transport(request)  # SupervisorUnavailable se propaga: no se reintenta a ciegas
        telemetry["usage"] = resp.get("usage")
        try:
            verdict = json.loads(extract_text(resp))
        except (InvalidVerdict, json.JSONDecodeError) as e:
            telemetry["errors"].append(str(e))
            continue
        errs = validate_verdict(verdict)
        if errs:
            telemetry["errors"].append("veredicto fuera de schema: " + "; ".join(errs[:5]))
            # Reintento con nueva evidencia: se le dice exactamente qué falló.
            request["input"].append({"role": "user", "content": "Tu respuesta anterior no cumplía el schema: " + "; ".join(errs[:5]) + ". Responde de nuevo solo con el JSON válido."})
            continue
        telemetry["duration_s"] = round(time.monotonic() - t0, 2)
        return verdict, telemetry
    telemetry["duration_s"] = round(time.monotonic() - t0, 2)
    return None, telemetry


def render_markdown(result: dict) -> str:
    v = result.get("verdict") or {}
    lines = [f"## Supervisor · {result['status']}", ""]
    if v:
        lines += [f"**Gate:** {v['gate']} · **Commit:** `{v['commit']}` · **Siguiente gate:** {v['next_gate']}", "", v["summary"], ""]
        if v["findings"]:
            lines += ["| ID | Severidad | Bloquea | Categoría | Hallazgo |", "|---|---|---|---|---|"]
            for f in v["findings"]:
                lines.append(f"| {f['id']} | {f['severity']} | {'sí' if f['blocking'] else 'no'} | {f['category']} | {f['title']} |")
            lines.append("")
        for title, key in (("Acciones obligatorias", "required_actions"), ("Mejoras opcionales", "optional_improvements")):
            if v[key]:
                lines += [f"**{title}:**", *[f"- {a}" for a in v[key]], ""]
        if v["evidence_requested"]:
            lines += ["**Evidencia solicitada:**", *[f"- `{e['kind']}` {e['ref']}: {e['reason']}" for e in v["evidence_requested"]], ""]
    for n in result.get("policy_notes", []):
        lines.append(f"> {n}")
    if result.get("detail"):
        lines += ["", f"> {result['detail']}"]
    t = result.get("telemetry") or {}
    lines += ["", f"<sub>fábrica {FACTORY_VERSION} · modelo {t.get('model')} · intentos {t.get('attempts')} · uso {json.dumps(t.get('usage'))}</sub>"]
    return "\n".join(lines) + "\n"


def run(packet_path: Path, out: Path, md: Path | None, transport: Callable[[dict], dict] | None = None, model: str | None = None) -> int:
    def finish(status: str, code: int, **extra: Any) -> int:
        result = {"status": status, "exit_code": code, "factory_version": FACTORY_VERSION, **extra}
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if md:
            md.write_text(render_markdown(result), encoding="utf-8")
        print(f"supervisor: {status} (exit {code})")
        return code

    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return finish("INVALID_PACKET", EXIT_BAD_PACKET, detail=f"no se pudo leer el paquete: {e}")
    errs = validate_packet(packet)
    if errs:
        return finish("INVALID_PACKET", EXIT_BAD_PACKET, detail="; ".join(errs[:10]))

    model = model or os.environ.get("OPENAI_MODEL")
    if not model:
        return finish("SUPERVISOR_PENDING", EXIT_PENDING, detail="OPENAI_MODEL no configurado")
    try:
        verdict, telemetry = ask_supervisor(packet, model, transport or openai_transport)
    except SupervisorUnavailable as e:
        return finish("SUPERVISOR_PENDING", EXIT_PENDING, detail=f"proveedor no disponible: {e}")
    if verdict is None:
        return finish("HUMAN_REVIEW_REQUIRED", EXIT_HUMAN, telemetry=telemetry, detail="el supervisor no devolvió un veredicto válido tras reintentar")

    final, notes = apply_policy(verdict)
    if final["commit"] and not packet["commit"].startswith(final["commit"][:7]) and not final["commit"].startswith(packet["commit"][:7]):
        notes.append(f"aviso: el veredicto cita el commit {final['commit']} y el paquete {packet['commit']}; se usa el del paquete")
        final["commit"] = packet["commit"]
    if final["evidence_requested"] and final["verdict"] != "BLOCKED":
        return finish("EVIDENCE_REQUESTED", EXIT_EVIDENCE, verdict=final, policy_notes=notes, telemetry=telemetry)
    code = EXIT_BLOCKED if final["verdict"] == "BLOCKED" else EXIT_OK
    return finish(final["verdict"], code, verdict=final, policy_notes=notes, telemetry=telemetry)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packet", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--md", type=Path)
    ap.add_argument("--validate-only", action="store_true", help="solo valida el paquete; no llama al proveedor")
    a = ap.parse_args(argv)
    if a.validate_only:
        try:
            errs = validate_packet(json.loads(a.packet.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            errs = [str(e)]
        print("\n".join(errs) if errs else "paquete válido")
        return EXIT_BAD_PACKET if errs else EXIT_OK
    return run(a.packet, a.out, a.md)


if __name__ == "__main__":
    sys.exit(main())
