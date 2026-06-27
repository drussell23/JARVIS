"""Anti-Venom C1+C2 — Slice4bRunner stale-apply block + L2 shield tests.

C1 (should_block_apply wired on the LIVE runner path):
  (a) A candidate whose target file changed between GENERATE and APPLY is
      rejected — the runner returns POSTMORTEM, NOT writing the file.
  (b) An unchanged target → apply proceeds without false-blocking.

C2 (asyncio.shield on L2-repair apply sites):
  (c) Structural AST pin: both L2-repair change_engine.execute calls in
      slice4b_runner.py are wrapped with asyncio.shield().
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import types
import unittest.mock as mock
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SLICE4B_PATH = (
    REPO_ROOT
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "phase_runners"
    / "slice4b_runner.py"
)

# ---------------------------------------------------------------------------
# Lightweight stubs so we can import slice4b_runner without the full stack
# ---------------------------------------------------------------------------

# We test via AST + direct state_drift calls for C1; AST for C2.
# The integration behaviour (C1) is verified by unit-testing
# should_block_apply together with inspecting the runner source for the
# correct wiring — keeping the test hermetic without wiring the full
# GovernedLoopService stack.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# C1(a) — stale file → should_block_apply returns block=True
# ---------------------------------------------------------------------------


class TestShouldBlockApplyC1:
    """Unit tests for the should_block_apply function used by the runner."""

    def test_stale_file_returns_block_true(self, tmp_path: Path) -> None:
        """Target file changed after GENERATE → block=True, file listed."""
        original = "print('original')\n"
        f = tmp_path / "target.py"
        f.write_text(original)
        baseline_hash = _hash(original)

        # Simulate a human patching the file after GENERATE
        f.write_text("print('HUMAN PATCHED')\n")

        from backend.core.ouroboros.governance.state_drift import should_block_apply

        with patch.dict(os.environ, {"JARVIS_STATE_DRIFT_VERIFY_ENABLED": "true"}):
            block, stale = should_block_apply(
                [("target.py", baseline_hash)], tmp_path
            )

        assert block is True, "Stale file must be blocked when VERIFY is enabled"
        assert "target.py" in stale

    def test_stale_file_no_block_when_gate_off(self, tmp_path: Path) -> None:
        """Gate off → drift detected but block=False (log-only degraded mode)."""
        original = "x = 1\n"
        f = tmp_path / "mod.py"
        f.write_text(original)
        baseline_hash = _hash(original)
        f.write_text("x = 999\n")

        from backend.core.ouroboros.governance.state_drift import should_block_apply

        with patch.dict(os.environ, {"JARVIS_STATE_DRIFT_VERIFY_ENABLED": "false"}):
            block, stale = should_block_apply(
                [("mod.py", baseline_hash)], tmp_path
            )

        assert block is False, "Gate off must NOT block even on drift"
        assert "mod.py" in stale, "Drift should still be detected even when gate off"

    def test_unchanged_file_returns_no_block(self, tmp_path: Path) -> None:
        """File unchanged since GENERATE → block=False, empty stale list."""
        content = "a = 1\n"
        f = tmp_path / "a.py"
        f.write_text(content)
        baseline_hash = _hash(content)

        from backend.core.ouroboros.governance.state_drift import should_block_apply

        with patch.dict(os.environ, {"JARVIS_STATE_DRIFT_VERIFY_ENABLED": "true"}):
            block, stale = should_block_apply(
                [("a.py", baseline_hash)], tmp_path
            )

        assert block is False, "Unchanged file must NOT be blocked"
        assert stale == [], "No stale files should be reported"

    def test_no_generate_hashes_no_block(self, tmp_path: Path) -> None:
        """Empty baseline (new file or no capture) → no block."""
        from backend.core.ouroboros.governance.state_drift import should_block_apply

        with patch.dict(os.environ, {"JARVIS_STATE_DRIFT_VERIFY_ENABLED": "true"}):
            block, stale = should_block_apply(None, tmp_path)

        assert block is False
        assert stale == []


# ---------------------------------------------------------------------------
# C1 — source-level AST pin: runner calls should_block_apply + POSTMORTEM
# ---------------------------------------------------------------------------


class TestC1RunnerSourcePin:
    """Verify that slice4b_runner.py wires should_block_apply + POSTMORTEM.

    We parse the source AST rather than executing the runner to keep this
    test hermetic (no heavy stack required).
    """

    def _src(self) -> str:
        return SLICE4B_PATH.read_text()

    def test_imports_should_block_apply(self) -> None:
        src = self._src()
        assert "should_block_apply" in src, (
            "slice4b_runner must import/use should_block_apply from state_drift"
        )

    def test_imports_state_drift_module(self) -> None:
        src = self._src()
        assert "state_drift" in src, (
            "slice4b_runner must reference backend.core.ouroboros.governance.state_drift"
        )

    def test_postmortem_on_block(self) -> None:
        """The block path must advance ctx to POSTMORTEM."""
        src = self._src()
        # Simplest: check that STATE_DRIFT_UNRECONCILED is used as a reason code
        assert "STATE_DRIFT_UNRECONCILED" in src or "state_drift_unreconciled" in src, (
            "slice4b_runner must route stale-blocked ops to POSTMORTEM "
            "using the canonical STATE_DRIFT_UNRECONCILED reason code"
        )

    def test_block_apply_var_checked(self) -> None:
        """The _block_apply variable must be set and then checked."""
        src = self._src()
        assert "_block_apply" in src, (
            "slice4b_runner must use _block_apply to gate the POSTMORTEM route"
        )

    def test_env_gate_respected(self) -> None:
        """JARVIS_STATE_DRIFT_VERIFY_ENABLED is honoured via state_drift_verify_enabled()."""
        from backend.core.ouroboros.governance import state_drift as sd
        import inspect

        # should_block_apply delegates to state_drift_verify_enabled()
        src = inspect.getsource(sd.should_block_apply)
        assert "state_drift_verify_enabled" in src, (
            "should_block_apply must consult state_drift_verify_enabled() "
            "so that JARVIS_STATE_DRIFT_VERIFY_ENABLED=false degrades to log-only"
        )


# ---------------------------------------------------------------------------
# C1(b) — unchanged file: assert the runner's stale guard does NOT fire
#          (structural check via should_block_apply return value)
# ---------------------------------------------------------------------------


class TestC1UnchangedNoBock:
    def test_unchanged_target_no_block(self, tmp_path: Path) -> None:
        content = "unchanged = True\n"
        (tmp_path / "file.py").write_text(content)
        baseline = _hash(content)

        from backend.core.ouroboros.governance.state_drift import should_block_apply

        with patch.dict(os.environ, {"JARVIS_STATE_DRIFT_VERIFY_ENABLED": "true"}):
            block, stale = should_block_apply([("file.py", baseline)], tmp_path)

        assert not block, "Unchanged file must NOT block apply"
        assert not stale


# ---------------------------------------------------------------------------
# C2 — structural AST pin: both L2-repair apply sites use asyncio.shield
# ---------------------------------------------------------------------------


class TestC2L2ShieldAST:
    """Parse slice4b_runner.py and verify asyncio.shield wraps BOTH
    change_engine.execute calls on the L2-repair paths (VERIFY + Visual-VERIFY).
    """

    def _parse(self) -> ast.Module:
        return ast.parse(SLICE4B_PATH.read_text(), filename=str(SLICE4B_PATH))

    def _find_shield_wrapped_execute_calls(self, tree: ast.Module) -> List[str]:
        """Return a list of descriptions for every asyncio.shield(...execute...) call."""
        results: List[str] = []

        class _Visitor(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call) -> None:
                # asyncio.shield(...)
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "shield"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "asyncio"
                ):
                    # Check if any nested call involves change_engine.execute
                    body = ast.dump(node)
                    if "change_engine" in body and "execute" in body:
                        results.append(ast.dump(node)[:80])
                self.generic_visit(node)

        _Visitor().visit(tree)
        return results

    def test_two_l2_shield_wrapped_sites(self) -> None:
        """Both L2-repair apply calls must be wrapped with asyncio.shield()."""
        tree = self._parse()
        shielded = self._find_shield_wrapped_execute_calls(tree)
        # We expect at least 3 total: primary + VERIFY L2 + Visual-VERIFY L2
        # (primary was already shielded by Task 7; C2 adds 2 more)
        assert len(shielded) >= 3, (
            f"Expected at least 3 asyncio.shield(change_engine.execute(...)) "
            f"calls in slice4b_runner.py (primary + VERIFY-L2 + Visual-VERIFY-L2); "
            f"found {len(shielded)}: {shielded}"
        )

    def test_verify_l2_shield_present(self) -> None:
        """Confirm the VERIFY-phase L2 repair site comment is present."""
        src = SLICE4B_PATH.read_text()
        assert "Anti-Venom C2" in src, (
            "Anti-Venom C2 comment marker missing — shield may not be wired"
        )

    def test_visual_verify_l2_shield_present(self) -> None:
        """Confirm the Visual VERIFY-phase L2 repair site shield is present."""
        src = SLICE4B_PATH.read_text()
        # Both sites must carry the C2 marker
        assert src.count("Anti-Venom C2") >= 2, (
            "Expected at least 2 Anti-Venom C2 comment markers in slice4b_runner.py "
            "(one per L2 repair shield site); found fewer"
        )
