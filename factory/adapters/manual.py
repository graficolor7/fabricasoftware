"""ManualSupervisorAdapter: un humano lleva el paquete a ChatGPT y trae el veredicto.

1ª llamada: escribe `manual-request-<fase>.md` (prompt + paquete) en el directorio de la
revisión y devuelve SUPERVISOR_PENDING. El humano pega la respuesta JSON en
`manual-response-<fase>.json` y vuelve a ejecutar el mismo comando: entonces se valida
como cualquier otro veredicto. Texto que no cumpla el schema → INVALID_SUPERVISOR_RESPONSE.
"""
from __future__ import annotations

from pathlib import Path

from factory.adapters.base import AdapterResult, SupervisorAdapter


class ManualSupervisorAdapter(SupervisorAdapter):
    name = "manual"

    def __init__(self, cfg, workdir: Path | None = None):
        super().__init__(cfg)
        self.workdir = workdir
        self.round_tag: str | None = None

    def bind(self, workdir: Path, round_tag: str) -> None:
        self.workdir = workdir
        self.round_tag = round_tag

    def _review(self, packet: dict, phase: str) -> AdapterResult:
        assert self.workdir is not None, "ManualSupervisorAdapter necesita un directorio de revisión"
        tag = self.round_tag or phase.lower()
        req = self.workdir / f"manual-request-{tag}.md"
        resp = self.workdir / f"manual-response-{tag}.json"
        model = self.cfg.model or "manual"
        if resp.exists():
            text = resp.read_text(encoding="utf-8")
            verdict, errs = self.parse_and_validate(text)
            call = {"phase": phase, "attempt": 1, "model": model, "manual": True, "request_bytes": req.stat().st_size if req.exists() else None}
            if verdict is None:
                call["error"] = "; ".join(errs[:3])
                return AdapterResult(None, "INVALID_SUPERVISOR_RESPONSE", model, [call], "respuesta manual fuera de schema: " + "; ".join(errs[:5]))
            return AdapterResult(verdict, "OK", model, [call])
        req.write_text("# Instrucciones del supervisor (pegar como instrucciones del sistema)\n\n" + self.system_prompt()
                       + "\n\n# Mensaje (pegar como mensaje del usuario)\n\n" + self.user_message(packet) + "\n", encoding="utf-8")
        return AdapterResult(None, "SUPERVISOR_PENDING", model, [],
                             f"revisión manual: lleva {req.name} a ChatGPT y guarda el JSON en {resp.name}; luego repite el comando")

    def health_check(self) -> dict:
        return {"provider": self.name, "model": self.cfg.model or "manual", "ok": True, "detail": "manual: depende de un humano"}
