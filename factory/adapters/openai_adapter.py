"""OpenAISupervisorAdapter: API Responses de OpenAI con salida estructurada estricta.

Verificado contra la documentación oficial (2026-11-03):
- `POST {base}/responses` con `model`, `instructions`, `input`, `max_output_tokens`, `store`,
  `reasoning.effort` (opcional; valores según el modelo) y
  `text.format = {type: "json_schema", name, schema, strict: true}`.
- Respuesta: `status` (`completed` | `incomplete` | `failed` …), `incomplete_details.reason`,
  `output[].content[]` con `output_text` o `refusal`, y `usage` (`input_tokens`,
  `input_tokens_details.cached_tokens`, `output_tokens`, `output_tokens_details.reasoning_tokens`).
- `GET {base}/models/{model}` para health check (no consume tokens).

Nada de esto fija un modelo: SUPERVISOR_MODEL es configuración.
La clave se lee de OPENAI_API_KEY en cada llamada y nunca se guarda ni se imprime.
"""
from __future__ import annotations

import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from factory import schemas
from factory.adapters.base import AdapterResult, SupervisorAdapter
from factory.errors import InvalidSupervisorResponse, SupervisorUnavailable
from factory.security import redact

# transport(method, url, body, headers, timeout) -> (status, headers, body)
Transport = Callable[[str, str, bytes | None, dict, float], tuple[int, dict, bytes]]
RETRYABLE_HTTP = {408, 409, 429, 500, 502, 503, 504}


def urllib_transport(method: str, url: str, body: bytes | None, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (socket.timeout, TimeoutError) as e:
        raise SupervisorUnavailable(f"TIMEOUT tras {timeout:.0f}s") from e
    except (urllib.error.URLError, OSError) as e:
        raise SupervisorUnavailable(f"error de red: {redact(str(e))[:200]}") from e


def _usage(resp: dict) -> dict:
    u = resp.get("usage") or {}
    return {
        "input_tokens": u.get("input_tokens"),
        "cached_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens"),
        "output_tokens": u.get("output_tokens"),
        "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "total_tokens": u.get("total_tokens"),
    }


def extract_output_text(resp: dict) -> str:
    status = resp.get("status")
    if status == "incomplete":
        reason = (resp.get("incomplete_details") or {}).get("reason", "desconocida")
        raise InvalidSupervisorResponse(f"respuesta incompleta ({reason}); subir SUPERVISOR_MAX_OUTPUT o reducir el paquete")
    if status not in (None, "completed"):
        raise SupervisorUnavailable(f"respuesta con status={status}: {redact(json.dumps(resp.get('error'))[:300])}")
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    for item in resp.get("output", []) or []:
        for c in item.get("content", []) or []:
            if c.get("type") == "refusal":
                raise InvalidSupervisorResponse("el supervisor se negó a responder: " + redact(str(c.get("refusal", "")))[:300])
            if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                return c["text"]
    raise InvalidSupervisorResponse("respuesta sin texto de salida")


class OpenAISupervisorAdapter(SupervisorAdapter):
    name = "openai"

    def __init__(self, cfg, transport: Transport | None = None, sleep: Callable[[float], None] | None = None,
                 api_key_env: str = "OPENAI_API_KEY"):
        super().__init__(cfg)
        self._transport = transport or urllib_transport
        self._sleep = sleep or time.sleep
        self._key_env = api_key_env

    # ------------------------------------------------------------------ util
    def _base(self) -> str:
        return str(self.cfg["SUPERVISOR_BASE_URL"]).rstrip("/")

    def _headers(self) -> dict:
        key = os.environ.get(self._key_env, "")
        if not key:
            raise SupervisorUnavailable(f"{self._key_env} no configurada", retryable=False)
        return {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def build_request(self, packet: dict) -> dict:
        req: dict[str, Any] = {
            "model": self.cfg.model,
            "instructions": self.system_prompt(),
            "input": [{"role": "user", "content": self.user_message(packet)}],
            "text": {"format": {"type": "json_schema", "name": "supervisor_verdict", "schema": schemas.strict_verdict_schema(), "strict": True}},
            "max_output_tokens": int(self.cfg["SUPERVISOR_MAX_OUTPUT"]),
            "store": False,
        }
        effort = str(self.cfg.get("SUPERVISOR_REASONING_LEVEL") or "").strip()
        if effort:
            req["reasoning"] = {"effort": effort}
        return req

    def _post(self, request: dict, phase: str, attempt: int) -> tuple[dict, dict]:
        """Una llamada lógica con reintentos (backoff exponencial con jitter). Devuelve (respuesta, telemetría)."""
        body = json.dumps(request).encode("utf-8")
        retries = int(self.cfg["SUPERVISOR_MAX_RETRIES"])
        base_s = float(self.cfg["SUPERVISOR_BACKOFF_BASE_S"])
        max_s = float(self.cfg["SUPERVISOR_BACKOFF_MAX_S"])
        timeout = float(self.cfg["SUPERVISOR_TIMEOUT"])
        tel: dict[str, Any] = {"phase": phase, "attempt": attempt, "model": self.cfg.model, "request_bytes": len(body), "http_tries": 0}
        last: SupervisorUnavailable | None = None
        t0 = time.monotonic()
        for i in range(retries + 1):
            tel["http_tries"] = i + 1
            retry_after = None
            try:
                status, headers, raw = self._transport("POST", self._base() + "/responses", body, self._headers(), timeout)
                tel["http_status"] = status
                if status == 200:
                    try:
                        resp = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        raise InvalidSupervisorResponse(f"cuerpo HTTP 200 no es JSON: {e}") from e
                    tel["duration_s"] = round(time.monotonic() - t0, 3)
                    tel["response_id"] = resp.get("id")
                    tel["response_model"] = resp.get("model")
                    tel["usage"] = _usage(resp)
                    return resp, tel
                detail = redact(raw.decode("utf-8", "replace"))[:300]
                if status in RETRYABLE_HTTP:
                    ra = {k.lower(): v for k, v in (headers or {}).items()}.get("retry-after")
                    try:
                        retry_after = float(ra) if ra is not None else None
                    except ValueError:
                        retry_after = None
                    last = SupervisorUnavailable(f"HTTP {status}: {detail}", retryable=True, retry_after=retry_after)
                else:
                    raise SupervisorUnavailable(f"HTTP {status} (revisar clave, modelo o parámetros): {detail}", retryable=False)
            except SupervisorUnavailable as e:
                if not e.retryable:
                    tel["duration_s"] = round(time.monotonic() - t0, 3)
                    tel["error"] = str(e)
                    raise
                last = e
                retry_after = e.retry_after
            if i < retries:
                delay = min(max_s, retry_after if retry_after is not None else base_s * (2 ** i)) + random.uniform(0, 0.25)
                self._sleep(delay)
        tel["duration_s"] = round(time.monotonic() - t0, 3)
        tel["error"] = str(last)
        assert last is not None
        last.telemetry = tel  # type: ignore[attr-defined]
        raise last

    # -------------------------------------------------------------- interfaz
    def _review(self, packet: dict, phase: str) -> AdapterResult:
        if not self.cfg["SUPERVISOR_LIVE_ENABLED"]:
            return AdapterResult(None, "SUPERVISOR_PENDING", self.cfg.model or None, [],
                                 "llamadas reales deshabilitadas (SUPERVISOR_LIVE_ENABLED=false); requiere autorización del Product Owner")
        if not self.cfg.model:
            return AdapterResult(None, "SUPERVISOR_PENDING", None, [], "SUPERVISOR_MODEL no configurado")
        request = self.build_request(packet)
        calls: list[dict] = []
        repairs = int(self.cfg["SUPERVISOR_MAX_REPAIR_ATTEMPTS"])
        problems: list[str] = []
        for attempt in range(1, repairs + 2):
            try:
                resp, tel = self._post(request, phase, attempt)
            except SupervisorUnavailable as e:
                calls.append(getattr(e, "telemetry", {"phase": phase, "attempt": attempt, "model": self.cfg.model, "error": str(e)}))
                return AdapterResult(None, "SUPERVISOR_UNAVAILABLE", self.cfg.model, calls, redact(str(e)))
            except InvalidSupervisorResponse as e:
                calls.append({"phase": phase, "attempt": attempt, "model": self.cfg.model, "error": str(e)})
                problems.append(str(e))
                continue
            calls.append(tel)
            try:
                text = extract_output_text(resp)
            except SupervisorUnavailable as e:
                tel["error"] = str(e)
                return AdapterResult(None, "SUPERVISOR_UNAVAILABLE", self.cfg.model, calls, redact(str(e)))
            except InvalidSupervisorResponse as e:
                tel["error"] = str(e)
                problems.append(str(e))
                if "incompleta" in str(e) or "negó" in str(e):
                    break  # reparar no ayuda: el problema no es el formato
                continue
            verdict, errs = self.parse_and_validate(text)
            if verdict is not None:
                return AdapterResult(verdict, "OK", resp.get("model") or self.cfg.model, calls)
            tel["error"] = "veredicto fuera de schema: " + "; ".join(errs[:5])
            problems.append(tel["error"])
            # Reparación limitada: se le dice exactamente qué falló. Nunca se interpreta texto libre.
            request = dict(request)
            request["input"] = list(request["input"]) + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": "Your previous answer did not match the required JSON schema: " + "; ".join(errs[:5]) + ". Answer again with ONLY the valid JSON verdict."},
            ]
        return AdapterResult(None, "INVALID_SUPERVISOR_RESPONSE", self.cfg.model, calls, redact(" | ".join(problems))[:1000])

    def health_check(self) -> dict:
        out = {"provider": self.name, "model": self.cfg.model or None, "ok": False, "detail": ""}
        if not self.cfg["SUPERVISOR_LIVE_ENABLED"]:
            out["detail"] = "SUPERVISOR_LIVE_ENABLED=false: no se contacta a OpenAI"
            return out
        if not self.cfg.model:
            out["detail"] = "SUPERVISOR_MODEL no configurado"
            return out
        try:
            status, _, raw = self._transport("GET", f"{self._base()}/models/{urllib.request.quote(self.cfg.model, safe='')}", None, self._headers(),
                                             min(30.0, float(self.cfg["SUPERVISOR_TIMEOUT"])))
        except SupervisorUnavailable as e:
            out["detail"] = redact(str(e))
            return out
        out["ok"] = status == 200
        out["detail"] = "modelo disponible para esta clave" if status == 200 else f"HTTP {status}: {redact(raw.decode('utf-8', 'replace'))[:200]}"
        return out
