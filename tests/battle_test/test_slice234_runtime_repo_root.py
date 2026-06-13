"""Slice 234 — runtime-aware repo-root resolution.

The Slice 231/232 re-soak proved GOAL-001 now generates an APPROVED patch on DW,
then failed only at APPLY: ``[Errno 2] No such file or directory:
'/Users/.../backend/core/ouroboros/governance/semantic_index.py'`` — a HOST
absolute path inside a container whose code lives at ``/app``. Origin:
``HarnessConfig.from_env`` sourced ``repo_path`` from ``JARVIS_REPO_PATH``
(injected via the host ``.env`` env_file) UNCONDITIONALLY — the env was treated
as authority when it is only a hint. The same stale path broke OrangePR git
discovery (``not a git repository``).

Fix: resolve the repo root against the REAL runtime — honor ``JARVIS_REPO_PATH``
only if it exists in this runtime and looks like the repo, else derive from the
code's own on-disk location (container-safe, no ``.git`` required), else a
``.git`` anchor walk-up, else FAIL LOUD rather than silently joining patches
against a nonexistent root.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.harness import _resolve_runtime_repo_root

_REAL_REPO = Path(__file__).resolve().parents[2]  # the actual repo root


class TestRuntimeRepoRoot:
    def test_host_context_valid_env_is_honored(self):
        # JARVIS_REPO_PATH points at a real, plausible repo → use it.
        out = _resolve_runtime_repo_root(env_value=str(_REAL_REPO), start=None)
        assert out == _REAL_REPO.resolve()

    def test_container_context_stale_host_env_is_rejected(self, tmp_path):
        # JARVIS_REPO_PATH is a host path that does NOT exist in this runtime
        # (the container case) → fall back to the code's on-disk location.
        stale = "/Users/someone/Documents/repos/JARVIS-AI-Agent"
        out = _resolve_runtime_repo_root(env_value=stale, start=None)
        assert out == _REAL_REPO.resolve()
        assert out.is_dir()
        assert (out / "backend" / "core" / "ouroboros").is_dir()

    def test_env_exists_but_not_a_repo_is_rejected(self, tmp_path):
        # Path exists but lacks backend/core/ouroboros → not authoritative.
        out = _resolve_runtime_repo_root(env_value=str(tmp_path), start=None)
        assert out == _REAL_REPO.resolve()

    def test_no_env_derives_from_code_location(self):
        # env_value="" = explicitly no env → derive from the code location.
        out = _resolve_runtime_repo_root(env_value="", start=None)
        assert out == _REAL_REPO.resolve()

    def test_unresolvable_fails_loud(self, tmp_path):
        # No valid env AND a start point with no repo ancestor → raise, never
        # return a bogus root that APPLY would write into.
        with pytest.raises(RuntimeError):
            _resolve_runtime_repo_root(env_value="", start=tmp_path / "x" / "y")

    def test_empty_env_treated_as_unset(self):
        out = _resolve_runtime_repo_root(env_value="   ", start=None)
        assert out == _REAL_REPO.resolve()


class TestFromEnvUsesResolver:
    def test_from_env_repo_path_is_runtime_resolved(self, monkeypatch):
        # The real bug: from_env must not pass a stale host path straight through.
        from backend.core.ouroboros.battle_test.harness import HarnessConfig
        monkeypatch.setenv(
            "JARVIS_REPO_PATH", "/Users/nonexistent/repos/JARVIS-AI-Agent",
        )
        cfg = HarnessConfig.from_env()
        assert cfg.repo_path == _REAL_REPO.resolve()
        assert cfg.repo_path.is_dir()
