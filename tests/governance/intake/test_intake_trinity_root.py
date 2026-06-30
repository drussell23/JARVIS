"""Regression: intake storage virtualizes its root via JARVIS_TRINITY_ROOT.

Surfaced by firing the A1 ignition harness live -- the isomorphic soak makes the
organism believe it lives at the literal ``/opt/trinity/jarvis`` (production
path), which is not writable off the GCE node, so the intake WAL's
``mkdir(parents=True)`` died with ``Permission denied: '/opt/trinity'``.

The fix virtualizes the writable boundary: ``JARVIS_TRINITY_ROOT`` (when set) is
the authoritative writable state root; unset => ``project_root`` (production is
byte-identical -- nothing sets the env var on the real node).
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
)


def test_wal_and_lock_default_to_project_root(monkeypatch):
    monkeypatch.delenv("JARVIS_TRINITY_ROOT", raising=False)
    cfg = IntakeRouterConfig(project_root=Path("/opt/trinity/jarvis"))
    assert cfg.resolved_wal_path == Path("/opt/trinity/jarvis/.jarvis/intake_wal.jsonl")
    assert cfg.resolved_lock_path == Path("/opt/trinity/jarvis/.jarvis/intake_router.lock")


def test_trinity_root_env_overrides_writable_root(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path))
    cfg = IntakeRouterConfig(project_root=Path("/opt/trinity/jarvis"))
    assert cfg.resolved_wal_path == tmp_path / ".jarvis" / "intake_wal.jsonl"
    assert cfg.resolved_lock_path == tmp_path / ".jarvis" / "intake_router.lock"
    # And the parent is genuinely creatable under the writable root (the exact
    # operation that failed against literal /opt/trinity).
    cfg.resolved_wal_path.parent.mkdir(parents=True, exist_ok=True)
    assert cfg.resolved_wal_path.parent.is_dir()


def test_explicit_wal_path_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path / "ignored"))
    explicit = tmp_path / "explicit" / "wal.jsonl"
    cfg = IntakeRouterConfig(project_root=Path("/opt/trinity/jarvis"), wal_path=explicit)
    assert cfg.resolved_wal_path == explicit


def test_empty_env_falls_back_to_project_root(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", "   ")  # whitespace = unset
    cfg = IntakeRouterConfig(project_root=Path("/opt/trinity/jarvis"))
    assert cfg.resolved_wal_path == Path("/opt/trinity/jarvis/.jarvis/intake_wal.jsonl")
