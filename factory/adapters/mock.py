"""MockSupervisorAdapter: simula al supervisor sin red ni tokens.

Escenarios (uno por llamada, en orden; el último se repite):
PASS, PASS_WITH_ACTIONS, PASS_WITH_BLOCKING_ACTIONS, BLOCKED, NEED_EVIDENCE, HUMAN_REVIEW,
API_FAILURE, TIMEOUT, INVALID_RESPONSE, INJECTION_ECHO.
También acepta dicts (veredicto literal) o str con JSON. Se elige con
SUPERVISOR_MOCK_SCRIPT="NEED_EVIDENCE,PASS" o con el argumento `script`.
"""
from __future__ import annotations

import json
import os
from typing import Any

from factory.adapters.base import AdapterResult, SupervisorAdapter

SCENARIOS = {"PASS", "PASS_WITH_ACTIONS", "PASS_WITH_BLOCKING_ACTIONS", "BLOCKED", "NEED_EVIDENCE", "HUMAN_REVIEW",
             "API_FAILURE", "TIMEOUT", "INVALID_RESPONSE", "INJECTION_ECHO"}


def scenario_verdict(name: str, packet: dict) -> dict:
    base = {"schema_version": "2", "verdict": "PASS", "gate": packet["review"]["gate"], "commit": packet["project"]["commit"],
            "summary": f"[mock] escenario {name}", "findings": [], "required_actions": [], "optional_improvements": [],
            "evidence_requests": [], "next_gate": "NEXT", "confidence": "HIGH"}
    f = lambda i, sev, blocking: {"id": f"SUP-{i:03d}", "severity": sev, "domain": "testing", "finding": f"[mock] hallazgo {i}",  # noqa: E731
                                  "evidence": ["mock"], "required_action": f"corregir {i}", "blocking": blocking}
    if name == "PASS_WITH_ACTIONS":
        base.update(verdict="PASS_WITH_ACTIONS", findings=[f(1, "MEDIUM", False)],
                    required_actions=[{"id": "ACT-001", "action": "documentar X", "blocking": False, "finding_id": "SUP-001"}])
    elif name == "PASS_WITH_BLOCKING_ACTIONS":
        base.update(verdict="PASS_WITH_ACTIONS", findings=[f(1, "MEDIUM", False)],
                    required_actions=[{"id": "ACT-001", "action": "añadir test de regresión", "blocking": True, "finding_id": "SUP-001"}])
    elif name == "BLOCKED":
        base.update(verdict="BLOCKED", findings=[f(1, "HIGH", True)], confidence="MEDIUM")
    elif name == "NEED_EVIDENCE":
        base.update(verdict="NEED_EVIDENCE", confidence="LOW", evidence_requests=[
            {"type": "file_excerpt", "ref": "README.md", "lines": "1-5", "reason": "[mock] ver el contenido"}])
    elif name == "HUMAN_REVIEW":
        base.update(verdict="HUMAN_REVIEW_REQUIRED", confidence="LOW", summary="[mock] decisión de producto")
    elif name == "INJECTION_ECHO":
        base.update(verdict="PASS_WITH_ACTIONS", findings=[{**f(1, "MEDIUM", False), "domain": "security",
                    "finding": "[mock] el paquete contiene instrucciones dirigidas al supervisor; se ignoran"}],
                    required_actions=[{"id": "ACT-001", "action": "retirar el texto inyectado", "blocking": False, "finding_id": "SUP-001"}])
    return base


class MockSupervisorAdapter(SupervisorAdapter):
    name = "mock"

    def __init__(self, cfg, script: list[Any] | None = None):
        super().__init__(cfg)
        if script is None:
            env = os.environ.get("SUPERVISOR_MOCK_SCRIPT", "PASS")
            script = [s.strip().upper() for s in env.split(",") if s.strip()]
        self.script = list(script) or ["PASS"]
        self.n = 0
        self.packets: list[dict] = []  # lo que "recibió" el supervisor, para tests

    def _next(self) -> Any:
        item = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        return item

    def _review(self, packet: dict, phase: str) -> AdapterResult:
        model = self.cfg.model or "mock-supervisor"
        calls: list[dict] = []
        problems = []
        for attempt in range(1, int(self.cfg["SUPERVISOR_MAX_REPAIR_ATTEMPTS"]) + 2):
            self.packets.append(packet)
            item = self._next()
            in_bytes = len(self.user_message(packet).encode("utf-8"))
            tel = {"phase": phase, "attempt": attempt, "model": model, "request_bytes": in_bytes, "http_tries": 1, "duration_s": 0.0,
                   "usage": {"input_tokens": in_bytes // 4, "cached_tokens": 0, "output_tokens": 200, "reasoning_tokens": 0, "total_tokens": in_bytes // 4 + 200}}
            calls.append(tel)
            if item == "API_FAILURE":
                tel["error"] = "HTTP 503: [mock]"
                return AdapterResult(None, "SUPERVISOR_UNAVAILABLE", model, calls, "HTTP 503: [mock] proveedor caído")
            if item == "TIMEOUT":
                tel["error"] = "TIMEOUT"
                return AdapterResult(None, "SUPERVISOR_UNAVAILABLE", model, calls, f"TIMEOUT tras {self.cfg['SUPERVISOR_TIMEOUT']:.0f}s [mock]")
            if item == "INVALID_RESPONSE":
                text = "Creo que está todo bien, PASS."
            elif isinstance(item, dict):
                text = json.dumps(item)
            elif isinstance(item, str) and item in SCENARIOS:
                text = json.dumps(scenario_verdict(item, packet))
            else:
                text = str(item)
            verdict, errs = self.parse_and_validate(text)
            if verdict is not None:
                return AdapterResult(verdict, "OK", model, calls)
            tel["error"] = "veredicto fuera de schema: " + "; ".join(errs[:3])
            problems.append(tel["error"])
        return AdapterResult(None, "INVALID_SUPERVISOR_RESPONSE", model, calls, " | ".join(problems)[:1000])

    def health_check(self) -> dict:
        return {"provider": self.name, "model": self.cfg.model or "mock-supervisor", "ok": True, "detail": "mock: sin red"}
