"""Adaptadores del supervisor. Elegir con SUPERVISOR_PROVIDER = openai | mock | manual."""
from __future__ import annotations

from factory.adapters.base import AdapterResult, SupervisorAdapter
from factory.errors import FactoryError


def make_adapter(cfg, **kwargs) -> SupervisorAdapter:
    p = cfg.provider
    if p == "openai":
        from factory.adapters.openai_adapter import OpenAISupervisorAdapter
        return OpenAISupervisorAdapter(cfg, **kwargs)
    if p == "mock":
        from factory.adapters.mock import MockSupervisorAdapter
        return MockSupervisorAdapter(cfg, **kwargs)
    if p == "manual":
        from factory.adapters.manual import ManualSupervisorAdapter
        return ManualSupervisorAdapter(cfg, **kwargs)
    raise FactoryError(f"proveedor desconocido: {p}")


__all__ = ["AdapterResult", "SupervisorAdapter", "make_adapter"]
