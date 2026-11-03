"""Secretos, redacción e indicios de inyección de prompt.

- `find_secrets`: bloquea el envío si algo tiene forma de credencial.
- `redact`: se aplica a todo texto que va a logs, errores o registros.
- `injection_flags`: marca (no bloquea) texto del proyecto que intenta dar órdenes al supervisor.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

SECRET_PATTERNS = [
    ("openai/anthropic key", re.compile(r"sk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("github pat", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google api key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("bearer header", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{16,}")),
    ("assignment", re.compile(r"(?i)\b(?:[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD))\s*[=:]\s*['\"]?[A-Za-z0-9/+_.-]{12,}")),
]

# Variables de entorno cuyo valor nunca debe aparecer en nada que escribamos.
SECRET_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")

INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+|the\s+|any\s+)?(?:previous|prior|above|supervisor|system)\s+(?:instructions|policy|policies|rules|prompt)"),
    re.compile(r"(?i)disregard\s+(?:the\s+)?(?:previous|prior|above|supervisor|system)"),
    re.compile(r"(?i)\b(?:return|respond|answer|output)\s+(?:with\s+)?(?:a\s+)?[\"']?PASS[\"']?\b"),
    re.compile(r"(?i)you\s+are\s+now\s+"),
    re.compile(r"(?i)new\s+(?:system\s+)?instructions\s*:"),
    re.compile(r"(?i)ignora\s+(?:las\s+)?(?:instrucciones|reglas|pol[ií]tica)"),
    re.compile(r"(?i)(?:responde|devuelve)\s+(?:solo\s+)?[\"']?PASS[\"']?"),
    re.compile(r"(?i)<\s*/?\s*(?:system|developer)\s*>"),
]


def _live_secret_values() -> list[str]:
    vals = []
    for name in SECRET_ENV_VARS:
        v = os.environ.get(name)
        if v and len(v) >= 8:
            vals.append(v)
    return vals


def find_secrets(text: str) -> list[str]:
    """Nombres de los patrones encontrados (nunca el valor)."""
    hits = [name for name, pat in SECRET_PATTERNS if pat.search(text)]
    if any(v in text for v in _live_secret_values()):
        hits.append("valor de una variable de entorno secreta")
    return hits


def redact(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    for v in _live_secret_values():
        text = text.replace(v, "[REDACTED]")
    for _, pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def injection_flags(texts: Iterable[tuple[str, str]], limit: int = 20) -> list[str]:
    """`texts` = pares (dónde, texto). Devuelve 'dónde: fragmento' acotado."""
    flags: list[str] = []
    for where, text in texts:
        for pat in INJECTION_PATTERNS:
            m = pat.search(text or "")
            if m:
                frag = redact(text[max(0, m.start() - 20): m.end() + 20]).replace("\n", " ")
                flags.append(f"{where}: …{frag[:200]}…")
                break
        if len(flags) >= limit:
            break
    return flags
