"""Carga y validación de los JSON Schema de la fábrica."""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from typing import Any

import jsonschema

from factory import FACTORY_ROOT

SCHEMAS = FACTORY_ROOT / "schemas"


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def errors(name: str, data: Any, limit: int = 20) -> list[str]:
    v = jsonschema.Draft202012Validator(load(name))
    out = []
    for e in sorted(v.iter_errors(data), key=lambda e: list(map(str, e.path))):
        out.append(f"{'/'.join(map(str, e.path)) or '(raíz)'}: {e.message[:300]}")
        if len(out) >= limit:
            break
    return out


def strict_verdict_schema() -> dict:
    """Schema del veredicto listo para `text.format` (sin metadatos de JSON Schema)."""
    s = copy.deepcopy(load("supervisor-verdict"))
    for k in ("$schema", "$id", "title", "description"):
        s.pop(k, None)
    return s
