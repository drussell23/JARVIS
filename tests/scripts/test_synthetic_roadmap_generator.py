"""Deterministic Synthetic Roadmap Generator (the A1 `emit source=roadmap` hop).

The isomorphic A1 soak must emit a REAL HMAC-signed strategic GOAL through the
production roadmap pipeline so the auditor's first hop (emit→ingest→dequeue→
submit→accept) fires -- WITHOUT a hardcoded fake trace and WITHOUT bypassing the
signature gate. The proof here: the driver's generated payload verifies against
the ACTUAL production `roadmap_reader` (same canonical-JSON HMAC-SHA256), and a
wrong secret is genuinely rejected.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = str((Path(__file__).parent.parent.parent).resolve())
_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())
for _p in (_REPO_ROOT, _SCRIPTS_DIR, os.path.join(_REPO_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script(name: str):
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_SCRIPTS_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_driver = _load_script("isomorphic_a1_local")

from backend.core.ouroboros.governance.roadmap_reader import (  # noqa: E402
    RoadmapVerdict,
    read_roadmap,
)


def test_synthetic_roadmap_verifies_against_real_reader(tmp_path):
    env: dict = {}
    path = _driver._arm_synthetic_roadmap(env, str(tmp_path))

    assert Path(path).is_file()
    # The reader gates are armed by the generator (compose_env only sets the
    # orchestrator flag; the READER master gate was the missing piece).
    assert env["JARVIS_ROADMAP_READER_ENABLED"] == "true"
    assert env["JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE"] == "true"
    assert env["JARVIS_ROADMAP_READER_HMAC_SECRET"]
    assert env["JARVIS_ROADMAP_READER_PATH"] == str(path)

    # THE PROOF: the production reader verifies the signature with the SAME secret
    # the driver signed with -> VALID, and GOAL-001 is parsed.
    verdict, doc, diag = read_roadmap(
        path_override=Path(path),
        secret_override=env["JARVIS_ROADMAP_READER_HMAC_SECRET"],
    )
    assert verdict == RoadmapVerdict.VALID, diag
    assert doc is not None
    assert any(g.goal_id == "GOAL-001" for g in doc.goals)


def test_synthetic_roadmap_rejected_under_wrong_secret(tmp_path):
    # Genuinely signed (not a REQUIRE_SIGNATURE bypass) -> a different secret MUST
    # fail verification. This proves the provenance is real crypto.
    env: dict = {}
    path = _driver._arm_synthetic_roadmap(env, str(tmp_path))

    verdict, _doc, _diag = read_roadmap(
        path_override=Path(path), secret_override="a-different-wrong-secret"
    )
    assert verdict == RoadmapVerdict.INVALID_SIGNATURE


def test_synthetic_roadmap_secret_override_is_deterministic(tmp_path, monkeypatch):
    # An explicit secret makes the signed payload reproducible (no committed
    # literal in source; the override is the determinism knob).
    monkeypatch.setenv("JARVIS_A1_SYNTHETIC_ROADMAP_SECRET", "fixed-secret-abc123")
    env: dict = {}
    path = _driver._arm_synthetic_roadmap(env, str(tmp_path))

    assert env["JARVIS_ROADMAP_READER_HMAC_SECRET"] == "fixed-secret-abc123"
    verdict, doc, diag = read_roadmap(
        path_override=Path(path), secret_override="fixed-secret-abc123"
    )
    assert verdict == RoadmapVerdict.VALID, diag
