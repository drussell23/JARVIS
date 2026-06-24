"""Tests for scripts/cross_repo_first_surgery.py -- the first-surgery chaos harness.

Asserts the REAL machinery is driven:
  * the fixture builds (two git repos with the real Body->Nerves dependency);
  * the chaos candidate COMPILES (ast.parse OK) but CHANGES the contract
    (symbol/signature differs);
  * the harness drives apply -> FRACTURE -> rollback and BOTH files are restored
    to their original snapshots (the rollback verification -- the REAL
    SagaApplyStrategy compensating rollback, not a fake copy);
  * the blast-radius visualizer is rendered (Body file mapped to Nerves mutation);
  * gate-off (JARVIS_CHAOS_INJECTOR_ENABLED unset) refuses to run.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import cross_repo_first_surgery as surgery  # noqa: E402
from backend.core.ouroboros.governance.saga.saga_types import (  # noqa: E402
    FileOp,
    SagaTerminalState,
)


# --------------------------------------------------------------------------- #
# Fixture builds + the real dependency graph
# --------------------------------------------------------------------------- #
def test_fixture_builds_two_repos(tmp_path):
    fx = surgery.build_fixture(tmp_path / "fx")
    try:
        assert (fx.reactor_root / surgery._REACTOR_FILE).exists()
        assert (fx.jarvis_root / surgery._JARVIS_FILE).exists()
        # The Body file genuinely imports + calls the Nerves adapter.
        jarvis_src = (fx.jarvis_root / surgery._JARVIS_FILE).read_text()
        assert "from telemetry_adapter import TelemetryAdapter" in jarvis_src
        assert 'adapter.emit("x", 1.0)' in jarvis_src
        # Both are real git repos (HEAD resolvable).
        for root in (fx.reactor_root, fx.jarvis_root):
            rc = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True
            ).returncode
            assert rc == 0
    finally:
        import shutil

        shutil.rmtree(fx.root, ignore_errors=True)


def test_chaos_candidate_compiles_but_breaks_contract():
    """The chaos candidate is ast.parse-OK yet changes the symbol + signature."""
    # Original: a class with method `emit(self, metric, value)`.
    orig_tree = ast.parse(surgery._REACTOR_ORIGINAL)
    chaos_tree = ast.parse(surgery._REACTOR_CHAOS)  # must NOT raise -> compiles

    def _methods(tree):
        out = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = [a.arg for a in node.args.args]
        return out

    orig_m = _methods(orig_tree)
    chaos_m = _methods(chaos_tree)

    assert "emit" in orig_m and "value" in orig_m["emit"]
    # CHAOS: the method name changed AND the value param is gone.
    assert "emit" not in chaos_m, "chaos must rename/remove the original symbol"
    assert "emit_metric" in chaos_m
    assert "value" not in chaos_m["emit_metric"], "chaos must drop the value param"
    # The contract genuinely differs (this is what fractures the handshake).
    assert orig_m != chaos_m


def test_chaos_patch_preimage_is_original(tmp_path):
    fx = surgery.build_fixture(tmp_path / "fx")
    try:
        patch_map = surgery.make_chaos_patch_map(fx)
        reactor_patch = patch_map["reactor"]
        pf = reactor_patch.files[0]
        assert pf.op == FileOp.MODIFY
        # Preimage MUST be the original (so the real rollback can restore it).
        assert pf.preimage == fx.reactor_original
        # new_content carries the chaos.
        new = dict(reactor_patch.new_content)[surgery._REACTOR_FILE]
        assert b"emit_metric" in new
    finally:
        import shutil

        shutil.rmtree(fx.root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The blast-radius trace + visualizer (real machinery, fixture graph)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_blast_trace_finds_jarvis_dependent(tmp_path):
    fx = surgery.build_fixture(tmp_path / "fx")
    try:
        from backend.core.ouroboros.governance.multi_repo.cross_repo_blast_context import (
            trace_cross_repo_blast,
        )

        oracle = surgery.make_fixture_oracle()
        registry = surgery.make_registry(fx)
        target = surgery.make_target_node()
        blast = await trace_cross_repo_blast(
            target_node_id=target, oracle=oracle, registry=registry
        )
        # The cross-repo dependent (jarvis) is genuinely traced.
        assert blast.total_dependents == 1
        dep = blast.dependents[0]
        assert dep.repo == "jarvis"
        assert dep.symbol == "record_metric"
        # The rendered visualizer tree maps the Body file to the Nerves mutation.
        tree = surgery.render_blast_tree_if_available(blast)
        assert "CROSS-REPO BLAST RADIUS" in tree
        assert "[jarvis]" in tree
        assert surgery._JARVIS_FILE in tree
    finally:
        import shutil

        shutil.rmtree(fx.root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The full apply -> FRACTURE -> rollback drive (the headline assertion)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_full_surgery_fractures_then_rolls_back(tmp_path, monkeypatch):
    # Cache off so the arming handshake re-evaluates against this process state.
    monkeypatch.setenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", "0")
    result = await surgery.run_surgery(base=tmp_path / "surgery")

    # Chaos was genuinely applied across the repo scope via the real saga.
    assert result.chaos_applied is True
    # The air-gapped sandbox gate FRACTURED (the broken handshake was detected).
    assert result.sandbox_verdict.fracture is True
    assert result.sandbox_verdict.passed is False
    assert result.sandbox_verdict.air_gapped is True  # air-gap held; handshake broke
    assert result.sandbox_verdict.handshake_ok is False
    assert "handshake_failed" in result.sandbox_verdict.reason

    # The REAL compensating rollback restored BOTH repos byte-for-byte.
    assert result.reactor_restored is True
    assert result.jarvis_restored is True
    assert result.rollback_verified is True

    # The visualizer was printed in the narrative.
    joined = "\n".join(result.narrative)
    assert "CROSS-REPO BLAST RADIUS" in joined
    assert "ROLLBACK VERIFIED" in joined


@pytest.mark.asyncio
async def test_apply_writes_chaos_then_rollback_restores(tmp_path):
    """Lower-level proof: the saga writes the chaos to disk, rollback undoes it."""
    fx = surgery.build_fixture(tmp_path / "fx")
    try:
        ctx = surgery.make_cross_repo_ctx(fx)
        strategy = surgery.make_saga_strategy(fx)
        patch_map = surgery.make_chaos_patch_map(fx)

        # Apply: the chaos lands on disk.
        apply_result = await strategy.execute(ctx, patch_map)
        assert apply_result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
        on_disk = (fx.reactor_root / surgery._REACTOR_FILE).read_bytes()
        assert b"emit_metric" in on_disk
        assert on_disk != fx.reactor_original

        # Compensate (REAL): restore from preimage.
        all_ok = await strategy.compensate_after_verify_failure(
            saga_result=apply_result,
            patch_map=patch_map,
            op_id=ctx.op_id,
            reason_code="cross_repo_fracture",
        )
        assert all_ok is True
        restored = (fx.reactor_root / surgery._REACTOR_FILE).read_bytes()
        assert restored == fx.reactor_original  # byte-identical to pre-surgery
        # jarvis was never touched -> still original.
        assert (fx.jarvis_root / surgery._JARVIS_FILE).read_bytes() == fx.jarvis_original
    finally:
        import shutil

        shutil.rmtree(fx.root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Gate-off: the CLI refuses to run without the chaos flag
# --------------------------------------------------------------------------- #
def test_cli_refuses_without_chaos_flag(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAOS_INJECTOR_ENABLED", raising=False)
    rc = surgery.main(["--run"])
    assert rc == 2  # refused


def test_cli_refuses_without_run_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "1")
    rc = surgery.main([])  # no --run
    assert rc == 2
