"""Perfil del supervisor del proyecto y triggers.

Triggers universales (todos los proyectos). El perfil puede añadir más, nunca quitar.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from factory import schemas
from factory.errors import FactoryError

PROFILE_PATH = ".factory/supervisor.json"

UNIVERSAL_TRIGGERS = {
    "MILESTONE_COMPLETION": "cierre de milestone",
    "ARCHITECTURE_FREEZE": "congelación de la arquitectura",
    "ARCHITECTURE_MAJOR_CHANGE": "cambio mayor de arquitectura",
    "SHARED_CONTRACT_FREEZE": "congelación de un contrato compartido",
    "SHARED_CONTRACT_BREAKING_CHANGE": "cambio incompatible de un contrato compartido",
    "SECURITY_CRITICAL_CHANGE": "cambio crítico de seguridad",
    "CRITICAL_BUG_UNRESOLVED": "fallo crítico sin resolver",
    "DOCUMENT_MODEL_FREEZE": "congelación del modelo de documento",
    "FILE_FORMAT_FREEZE": "congelación del formato de archivo",
    "RENDERING_ARCHITECTURE_FREEZE": "congelación de la arquitectura de render",
    "PRE_RELEASE": "pre-release",
    "MANUAL_SUPERVISOR_REQUEST": "petición manual (Director o PO)",
    "BASELINE_SUPERVISOR_REVIEW": "punto cero de un proyecto existente",
}

# Lo que NUNCA dispara al supervisor por sí solo (lo cubre CI), salvo escalado por riesgo.
NON_TRIGGERS = ["formatting", "typos", "documentation formatting", "small refactors", "ordinary unit test additions",
                "minor UI adjustments", "routine commits", "successful mechanical changes"]


def load_profile(project_root: Path) -> dict:
    p = project_root / PROFILE_PATH
    if not p.exists():
        raise FactoryError(f"falta {PROFILE_PATH} en {project_root} (ver templates/SUPERVISOR_PROFILE.json)")
    try:
        prof = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FactoryError(f"{PROFILE_PATH} no es JSON válido: {e}") from e
    errs = schemas.errors("supervisor-profile", prof)
    if errs:
        raise FactoryError(f"{PROFILE_PATH} inválido: " + "; ".join(errs[:5]))
    return prof


def triggers(profile: dict) -> dict[str, str]:
    out = dict(UNIVERSAL_TRIGGERS)
    for t in profile.get("triggers", {}).get("additional", []):
        out[t["id"]] = t["description"]
    return out


def check_trigger(profile: dict, trigger: str) -> None:
    if trigger not in triggers(profile):
        raise FactoryError(f"'{trigger}' no es un trigger del supervisor para {profile['project_id']}. "
                           f"Válidos: {', '.join(sorted(triggers(profile)))}. Cambios rutinarios los cubre CI.")


def evaluate_paths(profile: dict, paths: list[str]) -> list[dict]:
    """Reglas de escalado por rutas: qué triggers proponen los archivos cambiados."""
    hits = []
    for rule in profile.get("escalation_rules", []):
        matched = sorted({p for p in paths for g in rule["paths"] if fnmatch.fnmatch(p, g)})
        if matched:
            hits.append({"trigger": rule["trigger"], "description": rule["description"], "files": matched[:20]})
    return hits
