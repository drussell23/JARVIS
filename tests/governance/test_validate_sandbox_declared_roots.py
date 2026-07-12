"""Slice 8 — declared-roots containment contract for the VALIDATE sandbox.

Run #18 root cause (repro'd in a 2s isolation harness): the orchestrator
writes candidates into a $TMPDIR sandbox, but test_runner._normalize only
accepted repo_root + env-prefix paths — under the real node's no-/tmp
policy (JARVIS_SANDBOX_PREFIXES=/nonexistent-sandbox-prefix, the exact
value IsomorphicEnv mirrors) EVERY runnable candidate self-destructed
BlockedPathError → failure_class='security'. The fix: the gate honors the
caller-DECLARED sandbox_dir + original_paths that LanguageRouter.run
already receives. These tests pin the contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.test_runner import (
    BlockedPathError,
    _normalize,
    _route,
)

_NODE_POLICY = "/nonexistent-sandbox-prefix"  # isomorphic_env.py:55 value


@pytest.fixture
def node_policy(monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX_PREFIXES", _NODE_POLICY)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def sandbox(tmp_path):
    sb = tmp_path / "ouroboros_validate_x"
    sb.mkdir()
    return sb


class TestDeclaredSandboxContainment:
    def test_node_policy_declared_sandbox_passes(self, node_policy, repo, sandbox):
        """THE Run #18 repro: under node policy, a candidate in the
        DECLARED per-op sandbox must not be security-blocked."""
        f = sandbox / "backend" / "core" / "leaf.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        norm = _normalize(f, repo, sandbox_dir=sandbox)
        assert norm == "backend/core/leaf.py"  # sandbox-relative SHAPE, not bare name

    def test_original_paths_shape_wins(self, node_policy, repo, sandbox):
        """Routing fidelity: the ORIGINAL repo-relative shape is preferred
        so directory-shape adapter rules (^mlforge/ etc.) match sandbox
        copies exactly as they match in-repo files."""
        f = sandbox / "mlforge" / "kernel.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        norm = _normalize(
            f, repo, sandbox_dir=sandbox,
            original_paths={f: repo / "mlforge" / "kernel.py"},
        )
        assert norm == "mlforge/kernel.py"

    def test_node_policy_undeclared_still_blocked(self, node_policy, repo, sandbox):
        """No declaration → legacy env-prefix policy governs; under node
        policy the tempdir is outside it → still a security rejection."""
        f = sandbox / "x.py"
        f.write_text("x = 1\n")
        with pytest.raises(BlockedPathError):
            _normalize(f, repo)

    def test_outside_declared_sandbox_still_blocked(self, node_policy, repo, sandbox, tmp_path):
        """Declaring sandbox A does NOT whitelist unrelated tempdir B —
        the gate is per-call and exact."""
        other = tmp_path / "unrelated"
        other.mkdir()
        f = other / "evil.py"
        f.write_text("x = 1\n")
        with pytest.raises(BlockedPathError):
            _normalize(f, repo, sandbox_dir=sandbox)

    def test_repo_root_containment_unchanged(self, node_policy, repo):
        f = repo / "pkg" / "mod.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        assert _normalize(f, repo, sandbox_dir=None) == "pkg/mod.py"

    def test_legacy_prefix_fallback_byte_identical(self, monkeypatch, repo, sandbox):
        """No declaration + default prefixes → today's exact behavior
        (bare filename) is preserved for undeclared callers."""
        monkeypatch.delenv("JARVIS_SANDBOX_PREFIXES", raising=False)
        f = sandbox / "sub" / "thing.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        assert _normalize(f, repo) == "thing.py"


class TestRouteWithDeclaredSandbox:
    def test_route_shape_fidelity_under_node_policy(self, node_policy, repo, sandbox):
        """mlforge/ files must route to python+cpp even as sandbox copies —
        the latent path.name collapse made this impossible before."""
        f = sandbox / "mlforge" / "kernel.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        required = _route(
            (f,), repo, sandbox_dir=sandbox,
            original_paths={f: repo / "mlforge" / "kernel.py"},
        )
        assert required == frozenset({"python", "cpp"})

    def test_route_default_python_for_backend_file(self, node_policy, repo, sandbox):
        f = sandbox / "backend" / "mod.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        required = _route((f,), repo, sandbox_dir=sandbox)
        assert required == frozenset({"python"})
