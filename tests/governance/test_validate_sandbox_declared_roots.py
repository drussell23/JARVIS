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

    def test_original_paths_key_is_not_an_acceptance_path(
        self, node_policy, repo, sandbox, tmp_path
    ):
        """A path outside repo_root ∪ declared sandbox ∪ env prefixes must
        raise even when present as an original_paths key — the mapping
        affects SHAPE only, never containment (review Critical)."""
        evil_dir = tmp_path / "evil_unrelated"
        evil_dir.mkdir()
        evil = evil_dir / "evil.py"
        evil.write_text("x = 1\n")
        with pytest.raises(BlockedPathError):
            _normalize(
                evil, repo, sandbox_dir=sandbox,
                original_paths={evil: repo / "legit.py"},
            )

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


import ast as _ast
import asyncio
import tempfile

_REPO = Path(__file__).resolve().parents[2]
_LEAF_REL = Path("backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py")


class TestRun18EndToEnd:
    def test_run18_repro_full_router_under_node_policy(self, node_policy):
        """THE Run #18 scenario, end-to-end through the REAL router +
        PythonAdapter + real pytest on the real leaf pair: under node
        policy, the declared per-op sandbox must validate green — not
        die BlockedPathError → fc='security'."""
        from backend.core.ouroboros.governance.test_runner import (
            LanguageRouter,
            PythonAdapter,
        )
        router = LanguageRouter(
            repo_root=_REPO,
            adapters={"python": PythonAdapter(repo_root=_REPO)},
        )
        content = (_REPO / _LEAF_REL).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="ouroboros_validate_") as sb:
            sbp = Path(sb)
            f = sbp / _LEAF_REL
            f.parent.mkdir(parents=True)
            f.write_text(content, encoding="utf-8")
            result = asyncio.run(router.run(
                changed_files=(f,),
                sandbox_dir=sbp,
                timeout_budget_s=120,
                op_id="slice8-pin",
                original_paths={f: _REPO / _LEAF_REL},
            ))
        assert result.passed, (
            f"fc={result.failure_class}: "
            f"{result.dominant_failure and result.dominant_failure.test_result.stdout[:300]}"
        )


class TestOrchestratorDeclaresContract:
    """AST pin: the VALIDATE call site must keep declaring sandbox_dir +
    original_paths to the router — deleting either silently reverts the
    Run #18 class (the wired-but-inert lesson, structurally enforced)."""

    def test_run_validation_forwards_declared_roots(self):
        src = (
            _REPO / "backend" / "core" / "ouroboros" / "governance"
            / "orchestrator.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        hits = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                fn = node.func
                if getattr(fn, "attr", "") == "run" and any(
                    k.arg == "sandbox_dir" for k in node.keywords
                ):
                    hits.append({k.arg for k in node.keywords})
        assert any(
            {"sandbox_dir", "original_paths"} <= kw for kw in hits
        ), "no .run(sandbox_dir=..., original_paths=...) call in orchestrator"
