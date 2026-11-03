"""Registro reproducible de revisiones, identidad, idempotencia y locks.

Estructura en el repo del proyecto:

    .factory/reviews/<project_id>/
      index.json                      secuencia, revisiones y claves de idempotencia
      overrides.jsonl                 decisiones del PO (append-only)
      locks/                          locks de gate (ignorados por git)
      <MILESTONE>/<REVIEW_ID>/
        review.json                   resumen: rondas, estado, uso, quién cerró
        packet.json                   paquete inicial (o BASELINE)
        evidence-NN-packet.json       paquetes con la evidencia pedida
        delta-NN-packet.json          paquetes delta tras correcciones
        round-NN.json                 veredicto crudo + efectivo + llamadas de cada ronda
        verdict.json                  último veredicto efectivo
        evidence-index.json           toda la evidencia citada o entregada (con sha256)
        calls.jsonl                   telemetría por llamada (sin secretos)
        disagreement.json/.md         si hubo desacuerdo técnico

Escritura atómica (tmp + rename). Nada aquí contiene secretos: todo pasa por `redact`.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from factory.errors import FactoryError
from factory.security import redact

SAFE_RE = re.compile(r"[^A-Z0-9]+")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _token(s: str) -> str:
    t = SAFE_RE.sub("", s.upper())
    if not t:
        raise FactoryError(f"identificador vacío tras normalizar: {s!r}")
    return t[:24]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = redact(json.dumps(data, ensure_ascii=False, indent=2)) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps(row, ensure_ascii=False)) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class LockBusy(Exception):
    pass


class ReviewStore:
    def __init__(self, project_root: Path, project_id: str, prefix: str, reviews_dir: str = ".factory/reviews", lock_ttl_s: int = 3600):
        self.root = project_root.resolve()
        base = (self.root / reviews_dir).resolve()
        if self.root not in base.parents:
            raise FactoryError("reviews_dir debe estar dentro del repo del proyecto")
        self.dir = base / project_id
        self.project_id = project_id
        self.prefix = _token(prefix)
        self.lock_ttl_s = lock_ttl_s
        self.dir.mkdir(parents=True, exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            gi.write_text("locks/\n*.tmp\n", encoding="utf-8")

    # ------------------------------------------------------------ locks
    @contextmanager
    def lock(self, name: str, owner: str = "") -> Iterator[None]:
        locks = self.dir / "locks"
        locks.mkdir(exist_ok=True)
        path = locks / (re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".lock")
        info = {"pid": os.getpid(), "host": socket.gethostname(), "created_at": now(), "created_ts": time.time(), "owner": owner}
        for attempt in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                break
            except FileExistsError:
                try:
                    held = json.loads(path.read_text(encoding="utf-8") or "{}")
                except (OSError, json.JSONDecodeError):
                    held = {}
                age = time.time() - float(held.get("created_ts", time.time()))
                if attempt == 0 and age > self.lock_ttl_s:
                    path.unlink(missing_ok=True)  # lock huérfano
                    continue
                raise LockBusy(f"{name} ocupado por {held.get('owner') or '?'} (pid {held.get('pid')}, {held.get('created_at')})")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    @contextmanager
    def _index_lock(self) -> Iterator[None]:
        deadline = time.time() + 10
        while True:
            try:
                with self.lock("__index__", "index"):
                    yield
                return
            except LockBusy:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)

    # ------------------------------------------------------------ index
    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    def index(self) -> dict:
        return read_json(self.index_path, {"schema_version": "1", "seq": 0, "reviews": {}, "keys": {}})

    @staticmethod
    def idem_key(project_id: str, commit: str, gate_key: str, packet_hash: str) -> str:
        return f"{project_id}|{commit}|{gate_key}|{packet_hash}"

    def find_key(self, key: str) -> dict | None:
        return self.index()["keys"].get(key)

    def new_review(self, milestone: str, gate: str, trigger: str, gate_key: str) -> tuple[str, Path]:
        with self._index_lock():
            idx = self.index()
            idx["seq"] += 1
            rid = f"{self.prefix}-{_token(milestone)}-{_token(gate)}-{idx['seq']:04d}"
            idx["reviews"][rid] = {"milestone": milestone, "gate": gate, "gate_key": gate_key, "trigger": trigger,
                                   "created_at": now(), "status": "OPEN", "rounds": []}
            write_json(self.index_path, idx)
        d = self.dir / _token(milestone) / rid
        d.mkdir(parents=True, exist_ok=False)
        return rid, d

    def review_dir(self, review_id: str) -> Path:
        meta = self.index()["reviews"].get(review_id)
        if not meta:
            raise FactoryError(f"revisión desconocida: {review_id}")
        return self.dir / _token(meta["milestone"]) / review_id

    def review_meta(self, review_id: str) -> dict:
        meta = self.index()["reviews"].get(review_id)
        if not meta:
            raise FactoryError(f"revisión desconocida: {review_id}")
        return meta

    def record_round(self, review_id: str, rnd: dict, key: str | None, status: str) -> None:
        with self._index_lock():
            idx = self.index()
            meta = idx["reviews"][review_id]
            meta["rounds"].append({k: rnd[k] for k in ("round", "kind", "commit", "packet_hash", "status") if k in rnd})
            meta["status"] = status
            meta["updated_at"] = now()
            if key and rnd.get("status") in ("PASS", "PASS_WITH_ACTIONS", "BLOCKED", "NEED_EVIDENCE", "HUMAN_REVIEW_REQUIRED"):
                idx["keys"][key] = {"review_id": review_id, "round": rnd["round"]}
            write_json(self.index_path, idx)

    def set_status(self, review_id: str, status: str) -> None:
        with self._index_lock():
            idx = self.index()
            idx["reviews"][review_id]["status"] = status
            idx["reviews"][review_id]["updated_at"] = now()
            write_json(self.index_path, idx)

    def latest_for_gate(self, gate_key: str) -> str | None:
        rids = [r for r, m in self.index()["reviews"].items() if m["gate_key"] == gate_key]
        return rids[-1] if rids else None

    # ------------------------------------------------------------ overrides
    def overrides(self, review_id: str | None = None) -> list[dict]:
        p = self.dir / "overrides.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if review_id is None or r["review_id"] == review_id]

    def add_override(self, row: dict) -> None:
        with self._index_lock():
            append_jsonl(self.dir / "overrides.jsonl", row)
