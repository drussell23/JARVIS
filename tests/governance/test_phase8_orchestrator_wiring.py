"""Phase 9.5 Part B — orchestrator producer-wiring regression spine.

Pins the source-level integration of phase8_producers hooks into
``GovernedOrchestrator.run`` per Phase 9.5 Part B. The producer module
itself is exhaustively tested by ``test_phase8_*`` suites; this file
proves the orchestrator actually CALLS those hooks at the right
points so the substrate ledgers receive production data.

Scope of pins (source-level):
  * Hook 1: ``check_flag_changes_and_publish`` is called once at op
    entry (before ``_run_pipeline``).
  * Hook 2: ``record_phase_latency`` + ``record_decision`` both fire
    in the ``finally`` block (terminal-phase hooks).
  * NEVER-raises contract: hooks are wrapped in ``try/except`` so
    orchestrator does not abort if producers fail.
  * Authority invariants: producer module is imported via
    ``backend.core.ouroboros.governance.observability.phase8_producers``
    (no shortcut imports that bypass the lazy substrate-flag gate).

Why source-level pins:
  Substrate hooks are NEVER-raises + master-flag-gated; the producer
  module is independently regression-tested. The orchestrator-side
  question is purely "are the calls present at the right place?" —
  best answered by AST/regex pinning, not by booting the whole
  pipeline.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ORCH_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "backend" / "core" / "ouroboros" / "governance" / "orchestrator.py"
)


@pytest.fixture(scope="module")
def orch_source() -> str:
    return ORCH_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Hook 1 — op-entry flag scan
# ---------------------------------------------------------------------------


def test_phase8_op_entry_flag_scan_hook_present(orch_source: str) -> None:
    """``check_flag_changes_and_publish`` is called at the top of
    ``run`` before the inner ``_run_pipeline`` invocation."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    pipeline_call_idx = orch_source.index(
        "self._run_pipeline(ctx)", run_idx,
    )
    assert "check_flag_changes_and_publish" in orch_source[
        run_idx:pipeline_call_idx
    ], (
        "expected check_flag_changes_and_publish to be wired in run() "
        "BEFORE the _run_pipeline call"
    )


def test_phase8_op_entry_uses_phase8_producers_module(
    orch_source: str,
) -> None:
    """The flag scan import targets the Phase 8 producer module —
    not a shortcut to the substrate that would bypass NEVER-raises."""
    assert (
        "from backend.core.ouroboros.governance.observability."
        "phase8_producers import"
    ) in orch_source


# ---------------------------------------------------------------------------
# Hook 2 — terminal-phase latency + decision
# ---------------------------------------------------------------------------


def test_phase8_terminal_record_phase_latency_called(orch_source: str) -> None:
    """``record_phase_latency`` fires once in the run() ``finally`` block."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    next_def_idx = orch_source.index(
        "async def _run_pipeline", run_idx,
    )
    section = orch_source[run_idx:next_def_idx]
    assert "record_phase_latency" in section
    assert "OP_TERMINAL" in section, (
        "expected OP_TERMINAL phase tag — the producer ledger is "
        "indexed by phase name and OP_TERMINAL is the agreed upon "
        "tag for op-level latency rows"
    )


def test_phase8_terminal_record_decision_called(orch_source: str) -> None:
    """``record_decision`` fires in finally with phase=OP_TERMINAL."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    next_def_idx = orch_source.index(
        "async def _run_pipeline", run_idx,
    )
    section = orch_source[run_idx:next_def_idx]
    assert "record_decision" in section
    assert 'phase="OP_TERMINAL"' in section


def test_phase8_terminal_decision_records_terminal_reason(
    orch_source: str,
) -> None:
    """The terminal decision row carries ``terminal_reason`` so the
    Phase 8 substrate ledger can correlate ops by completion reason."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    next_def_idx = orch_source.index(
        "async def _run_pipeline", run_idx,
    )
    section = orch_source[run_idx:next_def_idx]
    assert '"terminal_reason"' in section


def test_phase8_terminal_decision_records_elapsed_s(orch_source: str) -> None:
    """The terminal decision row carries ``elapsed_s`` so the Phase 8
    substrate ledger can compute op-level latency histograms."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    next_def_idx = orch_source.index(
        "async def _run_pipeline", run_idx,
    )
    section = orch_source[run_idx:next_def_idx]
    assert '"elapsed_s"' in section


# ---------------------------------------------------------------------------
# NEVER-raises invariants
# ---------------------------------------------------------------------------


def test_phase8_op_entry_hook_wrapped_in_try_except(
    orch_source: str,
) -> None:
    """Op-entry flag scan is wrapped in try/except so orchestrator
    survives a producer-side failure (matches the Phase 8 substrate
    NEVER-raises contract end-to-end)."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    flag_scan_idx = orch_source.index(
        "check_flag_changes_and_publish", run_idx,
    )
    # Walk back to the nearest "try:" — must be within 200 chars
    # (otherwise the except handler is too far away to cover the call).
    pre = orch_source[max(0, flag_scan_idx - 400):flag_scan_idx]
    assert pre.rstrip().endswith("try:") or "try:" in pre[-200:], (
        "expected try: block immediately preceding the flag-scan call "
        "(NEVER-raises contract)"
    )


def test_phase8_terminal_hook_wrapped_in_try_except(
    orch_source: str,
) -> None:
    """Terminal hooks are wrapped in try/except so cleanup never
    blocks on a producer failure."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    next_def_idx = orch_source.index(
        "async def _run_pipeline", run_idx,
    )
    section = orch_source[run_idx:next_def_idx]
    record_idx = section.index("record_phase_latency")
    pre = section[max(0, record_idx - 400):record_idx]
    assert "try:" in pre, (
        "expected try: block guarding the record_phase_latency call "
        "(NEVER-raises contract)"
    )


# ---------------------------------------------------------------------------
# Ordering invariants
# ---------------------------------------------------------------------------


def test_phase8_flag_scan_before_pipeline(orch_source: str) -> None:
    """Op-entry flag scan must fire BEFORE _run_pipeline so the
    decision-trace ledger captures any flag drift that would have
    affected this op's gates."""
    flag_scan_idx = orch_source.index("check_flag_changes_and_publish")
    pipeline_idx = orch_source.index("self._run_pipeline(ctx)")
    assert flag_scan_idx < pipeline_idx


def test_phase8_terminal_hooks_after_pipeline(orch_source: str) -> None:
    """Terminal latency/decision hooks fire AFTER _run_pipeline so
    they observe the final terminal phase, not the initial CLASSIFY."""
    pipeline_idx = orch_source.index("self._run_pipeline(ctx)")
    record_latency_idx = orch_source.index("record_phase_latency")
    record_decision_idx = orch_source.index('phase="OP_TERMINAL"')
    assert pipeline_idx < record_latency_idx
    assert pipeline_idx < record_decision_idx


def test_phase8_terminal_ctx_captured_on_success_path(
    orch_source: str,
) -> None:
    """On the success path, the ``return await self._run_pipeline``
    must capture the result before returning so the finally block
    sees the FINAL ctx (not the initial CLASSIFY ctx). This is the
    bug-fix structure that distinguishes Phase 9.5 Part B from naive
    instrumentation."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    next_def_idx = orch_source.index(
        "async def _run_pipeline", run_idx,
    )
    section = orch_source[run_idx:next_def_idx]
    # Capture-then-return idiom (vs naive `return await ...` which
    # leaves _phase8_terminal_ctx pointing at the initial CLASSIFY ctx).
    assert "_phase8_terminal_ctx = await self._run_pipeline(ctx)" in section


# ---------------------------------------------------------------------------
# Authority invariants
# ---------------------------------------------------------------------------


def test_phase8_wiring_uses_lazy_imports(orch_source: str) -> None:
    """Phase 8 producer imports are lazy (inside try blocks in run()
    body), not at module top — so the orchestrator file itself
    doesn't trigger producer module side-effects on import."""
    top_300_lines = "\n".join(
        orch_source.splitlines()[:300]
    )
    # Top-of-file imports must NOT reference phase8_producers.
    assert "phase8_producers" not in top_300_lines, (
        "phase8_producers must be imported lazily inside the run() "
        "method body — not at orchestrator module-import time"
    )


def test_phase8_wiring_does_not_set_substrate_flags(
    orch_source: str,
) -> None:
    """The orchestrator must NOT toggle substrate master flags
    (JARVIS_DECISION_TRACE_LEDGER_ENABLED etc.). Substrate flags
    are operator-controlled at the env layer; orchestrator is a
    pure consumer of the producer hook surface."""
    forbidden_flags = (
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        "JARVIS_MULTI_OP_TIMELINE_ENABLED",
    )
    for flag in forbidden_flags:
        assert flag not in orch_source, (
            f"{flag} appeared in orchestrator.py — substrate flags "
            "are operator-controlled at env layer; orchestrator must "
            "stay a pure consumer"
        )


def test_phase8_wiring_logs_to_phase8wiring_namespace(
    orch_source: str,
) -> None:
    """Phase 8 wiring failures log under a ``[Phase8Wiring]`` prefix
    so operators can grep for orchestrator-side hook failures
    distinct from producer-side ones (``[Phase8Producers]``)."""
    run_idx = orch_source.index("async def run(self, ctx: OperationContext)")
    next_def_idx = orch_source.index(
        "async def _run_pipeline", run_idx,
    )
    section = orch_source[run_idx:next_def_idx]
    assert "[Phase8Wiring]" in section


# ---------------------------------------------------------------------------
# Producer-module surface smoke (defensive)
# ---------------------------------------------------------------------------


def test_phase8_producer_module_exposes_record_phase_latency() -> None:
    """Producer module exports the symbol the orchestrator imports.
    Catches a producer-rename refactor that would break the wiring."""
    from backend.core.ouroboros.governance.observability import (
        phase8_producers,
    )
    assert hasattr(phase8_producers, "record_phase_latency")
    assert callable(phase8_producers.record_phase_latency)


def test_phase8_producer_module_exposes_record_decision() -> None:
    from backend.core.ouroboros.governance.observability import (
        phase8_producers,
    )
    assert hasattr(phase8_producers, "record_decision")
    assert callable(phase8_producers.record_decision)


def test_phase8_producer_module_exposes_check_flag_changes() -> None:
    from backend.core.ouroboros.governance.observability import (
        phase8_producers,
    )
    assert hasattr(phase8_producers, "check_flag_changes_and_publish")
    assert callable(phase8_producers.check_flag_changes_and_publish)


def test_phase8_producer_record_phase_latency_never_raises() -> None:
    """Defensive smoke: producer hook returns without raising even
    when substrate flags are off (the default state). This is the
    contract orchestrator depends on."""
    from backend.core.ouroboros.governance.observability import (
        phase8_producers,
    )
    # No exception expected; result may be False (substrate off) but
    # the call must complete cleanly.
    phase8_producers.record_phase_latency("OP_TERMINAL", 0.123)


def test_phase8_producer_record_decision_never_raises() -> None:
    from backend.core.ouroboros.governance.observability import (
        phase8_producers,
    )
    phase8_producers.record_decision(
        op_id="test-op-id", phase="OP_TERMINAL",
        decision="COMPLETE", factors={"x": 1}, rationale="smoke",
    )


def test_phase8_producer_check_flag_changes_never_raises() -> None:
    from backend.core.ouroboros.governance.observability import (
        phase8_producers,
    )
    phase8_producers.check_flag_changes_and_publish()
