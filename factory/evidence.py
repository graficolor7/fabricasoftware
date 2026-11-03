"""Recolección de la evidencia que pide el supervisor (NEED_EVIDENCE).

Reglas:
- Solo lo pedido, como máximo SUPERVISOR_MAX_FILES_PER_REQUEST pedidos y
  SUPERVISOR_MAX_EVIDENCE_BYTES_PER_FILE bytes por pedido.
- Se lee del commit revisado (`git show <commit>:<ruta>`), no del árbol de trabajo.
- Rutas relativas dentro del repo: sin `..`, sin rutas absolutas, sin `.git`, sin archivos
  de secretos (`denied_globs`), y dentro de `allowed_roots` si el perfil los define.
- Binarios fuera. Todo texto pasa por `redact`.
- Lo que no se puede recolectar se declara en `unavailable_evidence`; nunca se inventa.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any

from factory import git
from factory.errors import FactoryError
from factory.security import find_secrets, redact

DEFAULT_DENIED = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*.keystore",
                  ".git/*", "**/.git/*", "node_modules/*", "**/node_modules/*", "**/secrets/*", "secrets/*",
                  "*.sqlite", "*.db", "**/.env", "**/.env.*"]
LINES_RE = re.compile(r"^\s*(\d{1,7})\s*(?:[-–:]\s*(\d{1,7}))?\s*$")
ADR_ID_RE = re.compile(r"^[A-Za-z]{2,10}-\d{1,5}[a-z]?$")
FILE_TYPES = {"file_excerpt", "test", "benchmark", "log"}
MAX_BLOB_BYTES = 5_000_000  # no se carga en memoria un archivo mayor para sacar un extracto


def safe_relpath(ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise FactoryError("ruta vacía")
    ref = ref.strip().replace("\\", "/")
    if "\x00" in ref or ref.startswith("/") or re.match(r"^[A-Za-z]:", ref) or ref.startswith("-"):
        raise FactoryError("ruta absoluta o inválida")
    parts = PurePosixPath(ref).parts
    if any(p == ".." for p in parts):
        raise FactoryError("ruta con '..' (path traversal)")
    norm = str(PurePosixPath(*[p for p in parts if p not in (".", "")]))
    if norm in ("", ".") or norm.split("/")[0] == ".git":
        raise FactoryError("ruta no permitida")
    return norm


def _denied(path: str, denied: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in denied)


def _slice_lines(text: str, lines: str | None) -> tuple[str, str | None]:
    if not lines:
        return text, None
    m = LINES_RE.match(lines)
    if not m:
        raise FactoryError(f"rango de líneas no válido: {lines!r}")
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if a < 1 or b < a:
        raise FactoryError(f"rango de líneas no válido: {lines!r}")
    rows = text.splitlines()
    sel = rows[a - 1: b]
    return "\n".join(f"{a + i}: {r}" for i, r in enumerate(sel)), f"{a}-{min(b, len(rows))}"


def _read_at_commit(root: Path, commit: str, path: str, max_bytes: int) -> str:
    if git.is_repo(root):
        spec = f"{git._check_ref(commit)}:{path}"
        try:
            size = int(git.run_git(root, "cat-file", "-s", spec).strip())
        except (FactoryError, ValueError) as e:
            raise FactoryError(f"no existe en el commit {commit[:12]}") from e
        if size > MAX_BLOB_BYTES:
            raise FactoryError(f"archivo de {size} bytes: demasiado grande para extraer (> {MAX_BLOB_BYTES}); pide un rango o un resumen")
        try:
            data = git.run_git(root, "show", spec, max_bytes=max_bytes * 4 + 1)
        except FactoryError as e:
            raise FactoryError(f"no existe en el commit {commit[:12]}") from e
    else:  # pragma: no cover - solo fuera de git
        full = (root / path).resolve()
        if root.resolve() not in full.parents:
            raise FactoryError("fuera del repo")
        data = full.read_bytes()[: max_bytes * 4 + 1].decode("utf-8", "replace")
    if "\x00" in data:
        raise FactoryError("archivo binario")
    return data


def _cap(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", "ignore") + "\n[… truncado por SUPERVISOR_MAX_EVIDENCE_BYTES_PER_FILE …]", True


def _adr_section(text: str, adr_id: str) -> str | None:
    rows = text.splitlines()
    start = None
    level = 0
    for i, r in enumerate(rows):
        m = re.match(r"^(#{1,6})\s+(.*)$", r)
        if m and re.search(rf"\b{re.escape(adr_id)}\b", m.group(2)):
            start, level = i, len(m.group(1))
            break
    if start is None:
        return None
    end = len(rows)
    for j in range(start + 1, len(rows)):
        m = re.match(r"^(#{1,6})\s+", rows[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    return "\n".join(rows[start:end])


def collect(root: Path, packet: dict, requests: list[dict], profile: dict, cfg) -> tuple[list[dict], list[dict]]:
    commit = packet["project"]["commit"]
    base = packet["project"].get("base_commit")
    ev_cfg = (profile or {}).get("evidence", {})
    denied = DEFAULT_DENIED + list(ev_cfg.get("denied_globs", []))
    allowed = [safe_relpath(a).rstrip("/") for a in ev_cfg.get("allowed_roots", [])]
    max_files = cfg["SUPERVISOR_MAX_FILES_PER_REQUEST"]
    max_bytes = cfg["SUPERVISOR_MAX_EVIDENCE_BYTES_PER_FILE"]
    attached: list[dict] = []
    unavailable: list[dict] = []

    for idx, req in enumerate(requests):
        ref = str(req.get("ref", ""))[:500]
        kind = req.get("type", "other")
        if idx >= max_files:
            unavailable.append({"request_index": idx, "ref": ref, "reason": f"límite SUPERVISOR_MAX_FILES_PER_REQUEST={max_files}"})
            continue
        try:
            lines_used = None
            if kind in FILE_TYPES:
                path = safe_relpath(ref)
                if _denied(path, denied):
                    raise FactoryError("ruta en la lista de archivos prohibidos (secretos o dependencias)")
                if allowed and not any(path == a or path.startswith(a + "/") for a in allowed):
                    raise FactoryError("ruta fuera de evidence.allowed_roots del perfil")
                text = _read_at_commit(root, commit, path, max_bytes)
                text, lines_used = _slice_lines(text, req.get("lines"))
                ref = path
            elif kind == "adr":
                if not ADR_ID_RE.match(ref):
                    raise FactoryError("id de ADR no válido")
                text = None
                for f in ev_cfg.get("adr_files", []):
                    try:
                        text = _adr_section(_read_at_commit(root, commit, safe_relpath(f), max_bytes * 10), ref)
                    except FactoryError:
                        text = None
                    if text:
                        break
                if not text:
                    raise FactoryError("ADR no encontrado en evidence.adr_files")
            elif kind == "contract":
                name, _, _ = ref.partition("#")
                contract_files = ev_cfg.get("contract_files", {})
                if name not in contract_files:
                    raise FactoryError("contrato no declarado en evidence.contract_files")
                path = safe_relpath(contract_files[name])
                text = _read_at_commit(root, commit, path, max_bytes * 10)
                text, lines_used = _slice_lines(text, req.get("lines"))
            elif kind == "diff":
                if not base:
                    raise FactoryError("el paquete no tiene project.base_commit")
                path = safe_relpath(ref)
                if _denied(path, denied):
                    raise FactoryError("ruta prohibida")
                text = git.diff_file(root, base, commit, path, max_bytes * 2) or "(sin cambios en esa ruta)"
            elif kind == "commit":
                text = git.show_commit(root, ref, max_bytes * 2)
            else:
                raise FactoryError(f"el tipo '{kind}' no se recolecta automáticamente; el Director debe aportarlo con --evidence-file")
        except FactoryError as e:
            unavailable.append({"request_index": idx, "ref": ref, "reason": str(e)[:500]})
            continue
        text, truncated = _cap(text, max_bytes)
        if find_secrets(text):
            text = redact(text)
        attached.append({"request_index": idx, "type": kind, "ref": ref, "lines": lines_used,
                         "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "truncated": truncated, "content": text})
    return attached, unavailable


def from_file(items: list[Any], requests: list[dict], cfg) -> list[dict]:
    """Evidencia aportada a mano por el Director (p. ej. un CI run). Se acota y se redacta igual."""
    out = []
    if not isinstance(items, list):
        raise FactoryError("--evidence-file debe ser una lista JSON")
    for it in items[: cfg["SUPERVISOR_MAX_FILES_PER_REQUEST"]]:
        if not isinstance(it, dict) or not isinstance(it.get("content"), str) or not isinstance(it.get("ref"), str):
            raise FactoryError("cada evidencia necesita 'ref' y 'content' (texto)")
        idx = it.get("request_index")
        if not isinstance(idx, int) or not (0 <= idx < len(requests)):
            raise FactoryError("request_index debe apuntar a un pedido del supervisor (solo se entrega lo pedido)")
        text, truncated = _cap(redact(it["content"]), cfg["SUPERVISOR_MAX_EVIDENCE_BYTES_PER_FILE"])
        out.append({"request_index": idx, "type": str(requests[idx].get("type", "other"))[:32], "ref": it["ref"][:500],
                    "lines": None, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "truncated": truncated, "content": text})
    return out
