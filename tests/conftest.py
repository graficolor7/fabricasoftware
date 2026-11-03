import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory.adapters.mock import MockSupervisorAdapter  # noqa: E402
from factory.config import load_config  # noqa: E402
from factory.review import Orchestrator  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

PROFILE = {
    "schema_version": "1",
    "project_id": "demo",
    "repository": "owner/demo",
    "review_id_prefix": "DEMO",
    "triggers": {"additional": [{"id": "PERFORMANCE_BASELINE_FREEZE", "description": "congelar baseline de rendimiento"}]},
    "escalation_rules": [{"trigger": "SHARED_CONTRACT_BREAKING_CHANGE", "paths": ["docs/API_CONTRACTS.md", "src/contracts/*"], "description": "contratos"}],
    "verification": [],
    "evidence": {"adr_files": ["docs/DECISIONS.md"], "contract_files": {"API": "docs/API_CONTRACTS.md"}},
    "config": {"SUPERVISOR_ALLOW_MOCK_CLOSURE": True},
}

DRAFT = {
    "review": {"objective": "Cierre de M1", "requested_verdict": True},
    "implementation": {"summary": "Herramientas básicas"},
    "verification": {"unit": {"status": "PASS", "summary": "10/10", "evidence_ref": "ci-1"}},
    "known_failures": [],
    "risks": [],
    "evidence_index": [{"ref": "ci-1", "kind": "ci_run", "result": "verde"}],
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}).stdout


class Project:
    def __init__(self, root: Path):
        self.root = root

    def write(self, rel: str, text: str | bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.write_bytes if isinstance(text, bytes) else p.write_text)(text)

    def commit(self, msg: str = "c") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", msg, "--allow-empty")
        return git(self.root, "rev-parse", "HEAD").strip()

    @property
    def profile(self) -> dict:
        return json.loads((self.root / ".factory/supervisor.json").read_text())

    def orch(self, script=None, env=None, **cfg_over) -> Orchestrator:
        cfg = load_config(self.profile.get("config"), env=env or {}, overrides=cfg_over)
        adapter = MockSupervisorAdapter(cfg, script=script or ["PASS"])
        return Orchestrator(self.root, self.profile, cfg, adapter)


@pytest.fixture
def project(tmp_path) -> Project:
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    p = Project(root)
    p.write(".factory/supervisor.json", json.dumps(PROFILE))
    p.write(".factory/PROJECT_STATE.json", json.dumps({"current_milestone": "M1", "next_action": "cerrar M1"}))
    p.write("README.md", "línea 1\nlínea 2\nlínea 3\nlínea 4\nlínea 5\nlínea 6\n")
    p.write("docs/DECISIONS.md", "# Decisiones\n\n## ADR-001 · Algo\nAceptada.\n\n## ADR-002 · Otra\nTexto.\n")
    p.write("docs/API_CONTRACTS.md", "# Contratos\nv1\n")
    p.write(".env", "OPENAI_API_KEY=sk-" + "x" * 40 + "\n")
    p.write(".gitignore", "")
    p.commit("init")
    return p


@pytest.fixture
def draft() -> dict:
    return json.loads(json.dumps(DRAFT))
