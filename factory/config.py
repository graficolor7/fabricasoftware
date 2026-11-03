"""Configuración central del supervisor.

Precedencia: variables de entorno > `config` del perfil del proyecto > config/supervisor.defaults.json.
Cambiar de proveedor o de modelo es cambiar una variable; nunca código.
La clave de API NO forma parte de esta configuración: el adaptador la lee del entorno
en el momento de la llamada y nunca la guarda.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from factory import FACTORY_ROOT
from factory.errors import FactoryError

DEFAULTS_FILE = FACTORY_ROOT / "config" / "supervisor.defaults.json"

INT_KEYS = {
    "SUPERVISOR_MAX_OUTPUT", "SUPERVISOR_MAX_RETRIES", "SUPERVISOR_MAX_REPAIR_ATTEMPTS",
    "SUPERVISOR_MAX_EVIDENCE_ROUNDS", "SUPERVISOR_MAX_DELTA_ROUNDS", "SUPERVISOR_MAX_CALLS_PER_GATE",
    "SUPERVISOR_MAX_CONTEXT_BYTES", "SUPERVISOR_MAX_FILES_PER_REQUEST", "SUPERVISOR_MAX_EVIDENCE_BYTES_PER_FILE",
    "SUPERVISOR_MAX_DISAGREEMENT_ROUNDS", "SUPERVISOR_LOCK_TTL_S",
}
FLOAT_KEYS = {"SUPERVISOR_TIMEOUT", "SUPERVISOR_BACKOFF_BASE_S", "SUPERVISOR_BACKOFF_MAX_S"}
BOOL_KEYS = {"SUPERVISOR_REQUIRE_GREEN_VERIFICATION", "SUPERVISOR_LIVE_ENABLED", "SUPERVISOR_ALLOW_MOCK_CLOSURE"}
PRICE_KEYS = {"SUPERVISOR_PRICE_INPUT_PER_1M", "SUPERVISOR_PRICE_CACHED_INPUT_PER_1M", "SUPERVISOR_PRICE_OUTPUT_PER_1M"}
PROVIDERS = {"openai", "mock", "manual"}
ON_UNAVAILABLE = {"SUPERVISOR_PENDING", "HUMAN_REVIEW_REQUIRED"}
# Nombres prohibidos como clave de configuración: los secretos solo van por entorno.
FORBIDDEN_KEYS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


@dataclass(frozen=True)
class SupervisorConfig:
    values: Mapping[str, Any] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    @property
    def provider(self) -> str:
        return self.values["SUPERVISOR_PROVIDER"]

    @property
    def model(self) -> str:
        return self.values["SUPERVISOR_MODEL"]

    @property
    def blocking_severities(self) -> set[str]:
        return {s.strip().upper() for s in str(self.values["SUPERVISOR_BLOCKING_SEVERITIES"]).split(",") if s.strip()} | {"CRITICAL"}

    def prices(self) -> dict[str, float] | None:
        """Tarifas configuradas por el PO a partir de la página oficial de precios. Si falta alguna, no hay costo."""
        out = {}
        for k in PRICE_KEYS:
            v = self.values.get(k)
            if v in (None, ""):
                return None
            out[k] = float(v)
        return out

    def public(self) -> dict[str, Any]:
        """Versión apta para registros (no contiene secretos por construcción)."""
        return {k: v for k, v in sorted(self.values.items())}


def load_config(profile_config: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None,
                overrides: Mapping[str, Any] | None = None) -> SupervisorConfig:
    env = os.environ if env is None else env
    raw = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    values: dict[str, Any] = {k: v for k, v in raw.items() if not k.startswith("_")}
    sources = {k: "defaults" for k in values}
    for k, v in (profile_config or {}).items():
        if k not in values:
            raise FactoryError(f"clave de configuración desconocida en el perfil: {k}")
        values[k], sources[k] = v, "profile"
    for k in list(values):
        if k in env and env[k] != "":
            values[k], sources[k] = env[k], "env"
    # Compatibilidad con la fábrica 1.0: OPENAI_MODEL / SUPERVISOR_TIMEOUT.
    if not values.get("SUPERVISOR_MODEL") and env.get("OPENAI_MODEL"):
        values["SUPERVISOR_MODEL"], sources["SUPERVISOR_MODEL"] = env["OPENAI_MODEL"], "env:OPENAI_MODEL"
    if env.get("OPENAI_BASE_URL") and sources.get("SUPERVISOR_BASE_URL") == "defaults":
        values["SUPERVISOR_BASE_URL"], sources["SUPERVISOR_BASE_URL"] = env["OPENAI_BASE_URL"], "env:OPENAI_BASE_URL"
    for k, v in (overrides or {}).items():
        if v is not None:
            values[k], sources[k] = v, "cli"

    for k in values:
        if any(f in k for f in FORBIDDEN_KEYS):
            raise FactoryError(f"{k}: los secretos no se configuran aquí; usa variables de entorno")
    try:
        for k in INT_KEYS:
            values[k] = int(values[k])
        for k in FLOAT_KEYS:
            values[k] = float(values[k])
        for k in BOOL_KEYS:
            values[k] = _to_bool(values[k])
    except (TypeError, ValueError) as e:
        raise FactoryError(f"configuración numérica inválida: {e}") from e
    values["SUPERVISOR_PROVIDER"] = str(values["SUPERVISOR_PROVIDER"]).strip().lower()
    if values["SUPERVISOR_PROVIDER"] not in PROVIDERS:
        raise FactoryError(f"SUPERVISOR_PROVIDER debe ser uno de {sorted(PROVIDERS)}")
    if values["SUPERVISOR_ON_UNAVAILABLE"] not in ON_UNAVAILABLE:
        raise FactoryError(f"SUPERVISOR_ON_UNAVAILABLE debe ser uno de {sorted(ON_UNAVAILABLE)}")
    for k in ("SUPERVISOR_MAX_EVIDENCE_ROUNDS", "SUPERVISOR_MAX_DELTA_ROUNDS", "SUPERVISOR_MAX_REPAIR_ATTEMPTS", "SUPERVISOR_MAX_RETRIES"):
        if values[k] < 0:
            raise FactoryError(f"{k} no puede ser negativo")
    if values["SUPERVISOR_MAX_CALLS_PER_GATE"] < 1:
        raise FactoryError("SUPERVISOR_MAX_CALLS_PER_GATE debe ser al menos 1")
    return SupervisorConfig(values=values, sources=sources)


def prompt_path(cfg: SupervisorConfig) -> Path:
    p = (FACTORY_ROOT / str(cfg["SUPERVISOR_PROMPT"])).resolve()
    if FACTORY_ROOT not in p.parents:
        raise FactoryError("SUPERVISOR_PROMPT debe estar dentro del repo de la fábrica")
    return p
