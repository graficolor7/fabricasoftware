"""Fábrica de software: herramientas del supervisor independiente.

Uso: python -m factory --help
"""
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
FACTORY_VERSION = (FACTORY_ROOT / "VERSION").read_text(encoding="utf-8").strip() if (FACTORY_ROOT / "VERSION").exists() else "unknown"
