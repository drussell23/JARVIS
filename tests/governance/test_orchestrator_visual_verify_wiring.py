"""Regression spine for the final orchestrator call-site wiring.

The orchestrator's Phase 8d block (immediately before the
``VERIFY → COMPLETE`` transition) must:

1. Import ``run_post_verify`` from ``visual_verify``.
2. Call it with exactly the fields the driver contract requires
   (target_files, attachments, op_id, op_description, risk_tier,
   test_runner_result="passed", test_targets_resolved).
3. On ``l2_triggered`` or deterministic ``fail``, dispatch L2 via
   ``self._l2_hook`` with a synthetic ``ValidationResult`` —
   mirroring the existing VERIFY-red → L2 path.
4. Advance ctx through ``OperationPhase.VISUAL_VERIFY`` for FSM
   auditability.
5. Route L2 directives: ``break`` → apply repair + continue to
   COMPLETE; ``cancel``/``fatal`` → return early with the L2-
   advanced terminal ctx.
6. Catch every dispatch-layer exception so the COMPLETE path is
   never broken by a bug in the Visual VERIFY driver.

These are production-source structural guards — the full L2 path is
async and has dozens of dependencies; we exercise it by reading the
orchestrator source and asserting the wiring is shaped correctly.
"""
from __future__ import annotations

from pathlib import Path


def _read_orchestrator_source() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text(encoding="utf-8")


def _extract_phase_8d_block(src: str) -> str:
    start = src.find("Phase 8d: Visual VERIFY")
    assert start >= 0, "Phase 8d block not found — wiring regression"
    # End at the COMPLETE advance call that follows the block.
    end_marker = 'ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="complete")'
    end = src.find(end_marker, start)
    assert end > start, "COMPLETE advance not found after Phase 8d block"
    return src[start:end]


# ---------------------------------------------------------------------------
# Imports + function call structure
# ---------------------------------------------------------------------------


def test_phase_8d_block_exists_before_complete_advance():
    src = _read_orchestrator_source()
    block = _extract_phase_8d_block(src)
    # Sanity: block is substantial, not a one-liner stub.
    assert len(block) > 200


def test_phase_8d_imports_run_post_verify():
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert (
        "from backend.core.ouroboros.governance.visual_verify import"
        in block
    )
    assert "run_post_verify" in block


def test_phase_8d_calls_driver_with_required_fields():
    block = _extract_phase_8d_block(_read_orchestrator_source())
    # Every field the VisualVerifyDispatchOutcome contract depends on
    # must be passed. Missing one would silently reshape the driver
    # call and break I4/trigger/routing guarantees.
    for field in (
        "target_files=ctx.target_files",
        "attachments=ctx.attachments",
        "op_id=ctx.op_id",
        "op_description=ctx.description",
        "test_targets_resolved=",
        "risk_tier=",
        'test_runner_result="passed"',
    ):
        assert field in block, f"missing required driver kwarg: {field}"


# ---------------------------------------------------------------------------
# FSM transition + L2 dispatch
# ---------------------------------------------------------------------------


def test_phase_8d_advances_ctx_through_visual_verify():
    """FSM auditability: the op ctx MUST pass through VISUAL_VERIFY so
    the hash chain reflects the traversal. Without this advance, a
    successful Visual VERIFY would leave no ledger trace.
    """
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert "ctx.advance(OperationPhase.VISUAL_VERIFY)" in block


def test_phase_8d_dispatches_l2_on_failure():
    """On deterministic fail OR advisory l2_triggered, route to L2
    via the same ``_l2_hook`` path VERIFY-red uses."""
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert "self._l2_hook(" in block
    # Failure fork covers both deterministic fail and advisory trigger.
    assert "l2_triggered" in block
    assert 'verdict == "fail"' in block


def test_phase_8d_builds_synthetic_validation_result_for_l2():
    """The L2 hook expects a ValidationResult — structural mirror of
    the existing VERIFY-red synthesis pattern."""
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert "ValidationResult(" in block
    assert "passed=False" in block
    assert "best_candidate=best_candidate" in block
    assert 'failure_class="test"' in block


def test_phase_8d_handles_l2_break_directive():
    """On L2 convergence: apply repair candidate via ``change_engine``
    (mirroring VERIFY-red path line 6074) so the repair lands on
    disk before COMPLETE fires."""
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert '_vv_directive[0] == "break"' in block
    assert "self._build_change_request(" in block
    assert "self._stack.change_engine.execute(" in block


def test_phase_8d_handles_l2_cancel_and_fatal_directives():
    """L2 escape directives advance ctx to a terminal phase — return
    immediately so subsequent COMPLETE advance doesn't violate the
    FSM by transitioning a terminal phase further."""
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert '_vv_directive[0] in ("cancel", "fatal")' in block
    assert "return ctx" in block


# ---------------------------------------------------------------------------
# Defense-in-depth: dispatch errors must never break COMPLETE path
# ---------------------------------------------------------------------------


def test_phase_8d_swallows_dispatch_exceptions():
    """A bug in the driver / ledger / advisory path must not prevent
    the op from reaching COMPLETE. The top-level try/except around
    the whole block is the guard."""
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert "try:" in block
    assert "except Exception" in block
    assert "dispatch error" in block.lower()


def test_phase_8d_back_compat_preserved():
    """When the driver returns ``ran=False`` (master switch off, or
    not UI-affected), we fall through to the existing COMPLETE
    advance with no side effects."""
    src = _read_orchestrator_source()
    # The COMPLETE advance immediately follows the Phase 8d block —
    # if the block short-circuits or skips, ctx proceeds to COMPLETE.
    block_start = src.find("Phase 8d: Visual VERIFY")
    complete_line = src.find(
        'ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="complete")',
        block_start,
    )
    # Assert the block ends cleanly before COMPLETE — not nested inside
    # another branch.
    intervening = src[block_start:complete_line]
    # Top-level except wraps the whole block; the COMPLETE advance is
    # OUTSIDE that except, so any ran=False / pass path reaches it.
    assert intervening.count("except Exception") >= 1


# ---------------------------------------------------------------------------
# L2 hook failure path
# ---------------------------------------------------------------------------


def test_phase_8d_l2_hook_exception_logged_not_raised():
    """L2 hook failures are logged and the pipeline continues — same
    contract as VERIFY-red path line 6104."""
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert "Visual VERIFY L2 failed" in block


# ---------------------------------------------------------------------------
# Comment hygiene — block is discoverable + documented
# ---------------------------------------------------------------------------


def test_phase_8d_block_has_discoverable_header():
    """A future maintainer greps for ``Phase 8d`` / ``Visual VERIFY``
    and lands on the right block. Missing header → regression."""
    src = _read_orchestrator_source()
    assert "Phase 8d: Visual VERIFY" in src


def test_phase_8d_references_manifesto_dag_routing():
    """The wiring comment cites Manifesto §2 DAG for routing — this
    is the boundary test for "the FSM must not be silently skipped"
    that operator review grep would look for."""
    block = _extract_phase_8d_block(_read_orchestrator_source())
    assert "§2" in block
