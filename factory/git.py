"""Acceso de solo lectura a git, sin shell y con argumentos validados."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from factory.errors import FactoryError

REF_RE = re.compile(r"^(?!-)[A-Za-z0-9._/@^~-]{1,200}$")


def _check_ref(ref: str) -> str:
    if not REF_RE.match(ref) or ".." in ref:
        raise FactoryError(f"referencia git no válida: {ref!r}")
    return ref


def run_git(root: Path, *args: str, max_bytes: int = 2_000_000, timeout: float = 60) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FactoryError(f"git falló: {e}") from e
    if r.returncode != 0:
        raise FactoryError(f"git {args[0]} falló: {r.stderr.decode('utf-8', 'replace')[:300]}")
    return r.stdout[:max_bytes].decode("utf-8", "replace")


def is_repo(root: Path) -> bool:
    try:
        run_git(root, "rev-parse", "--git-dir")
        return True
    except FactoryError:
        return False


def head(root: Path) -> str:
    return run_git(root, "rev-parse", "HEAD").strip()


def branch(root: Path) -> str:
    return run_git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def resolve(root: Path, ref: str) -> str:
    return run_git(root, "rev-parse", "--verify", _check_ref(ref) + "^{commit}").strip()


def is_clean(root: Path) -> bool:
    return run_git(root, "status", "--porcelain", "--untracked-files=no").strip() == ""


def changed_files(root: Path, base: str, head_ref: str = "HEAD") -> list[tuple[str, str]]:
    out = run_git(root, "diff", "--name-status", "--no-renames", _check_ref(base), _check_ref(head_ref), "--")
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            res.append((parts[0][:1], parts[-1]))
    return res


def diff_stat(root: Path, base: str, head_ref: str = "HEAD") -> str:
    return run_git(root, "diff", "--stat", "--no-color", _check_ref(base), _check_ref(head_ref), "--", max_bytes=20_000)


def diff_file(root: Path, base: str, head_ref: str, path: str, max_bytes: int) -> str:
    return run_git(root, "diff", "--no-color", _check_ref(base), _check_ref(head_ref), "--", path, max_bytes=max_bytes)


def show_commit(root: Path, ref: str, max_bytes: int) -> str:
    return run_git(root, "show", "--stat", "--no-color", "--format=commit %H%nAuthor: %an%nDate: %aI%n%n%s%n%n%b", _check_ref(ref), "--", max_bytes=max_bytes)
