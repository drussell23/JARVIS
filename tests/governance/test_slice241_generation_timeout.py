"""Slice 241 T1 — GENERATION_TIMEOUT classification (stop blaming DW for OUR budget).

Root cause surfacing across the s235→240 arc: `RuntimeError('tool_loop_deadline_
exceeded')` — the Venom tool-loop blowing its OP-LEVEL generation/exploration
budget (bills $0, no socket involved) — falls through the sentinel classifier's
regex to the catch-all `else → FailureSource.LIVE_TRANSPORT` (candidate_generator
.py:3694). That FALSELY degrades DW's surface health, inflates the live_transport
lane-sever counter, and feeds the cascade — i.e. we blame DoubleWord's *network*
for OUR *budget*. (Same family as Slice 185, which already segregates internal
Python faults via dw_fault_taxonomy.is_internal_fault, but RuntimeError is
deliberately excluded there.)

Fix (precise, reuse the existing ==LIVE_TRANSPORT keying): a distinct non-transport
source GENERATION_TIMEOUT (weight 0.0 → never trips the DW topology breaker). The
classifier labels tool-loop budget exhaustion as GENERATION_TIMEOUT FIRST; the
existing degrade/sever consumers key on `is FailureSource.LIVE_TRANSPORT` and so
automatically stop reacting to it. Genuine transport errors are untouched.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import dw_fault_taxonomy as tax
from backend.core.ouroboros.governance.topology_sentinel import (
    FailureSource,
    failure_weight,
)


class TestIsGenerationTimeout:
    def test_tool_loop_deadline_is_generation_timeout(self):
        assert tax.is_generation_timeout(RuntimeError("tool_loop_deadline_exceeded")) is True

    def test_tool_loop_other_budget_markers(self):
        for msg in (
            "tool_loop_max_rounds_exceeded",
            "tool_loop_round_budget_starved",
            "tool_loop_starved_below_min_ttft_floor",
        ):
            assert tax.is_generation_timeout(RuntimeError(msg)) is True, msg

    def test_genuine_transport_errors_are_NOT_generation_timeout(self):
        # the precision invariant: real transport must still flow to LIVE_TRANSPORT
        for exc in (
            ConnectionResetError("connection reset by peer"),
            RuntimeError("RemoteProtocolError: peer closed connection"),
            RuntimeError("stream stalled: no chunk in 30s"),
            TimeoutError("read timeout"),
        ):
            assert tax.is_generation_timeout(exc) is False, repr(exc)

    def test_unrelated_runtimeerror_not_generation_timeout(self):
        assert tax.is_generation_timeout(RuntimeError("something else entirely")) is False

    def test_fail_soft_false(self):
        class _Bad:
            def __str__(self):
                raise ValueError("boom")
        assert tax.is_generation_timeout(_Bad()) is False


class TestFailureSourceEnum:
    def test_generation_timeout_source_exists(self):
        assert hasattr(FailureSource, "GENERATION_TIMEOUT")
        assert FailureSource.GENERATION_TIMEOUT.value == "generation_timeout"

    def test_generation_timeout_weight_is_zero(self):
        # weight 0.0 → a budget timeout NEVER contributes to the weighted-streak
        # that trips the DW model/topology breaker (it is not a DW health signal).
        assert failure_weight(FailureSource.GENERATION_TIMEOUT) == 0.0

    def test_live_transport_weight_unchanged(self):
        # genuine transport still carries its real weight (regression guard)
        assert failure_weight(FailureSource.LIVE_TRANSPORT) == 1.0


class TestClassifierWiring:
    """Source pins: the classifier labels GENERATION_TIMEOUT before the
    LIVE_TRANSPORT fallback, and the degrade/sever consumers still key on
    ==LIVE_TRANSPORT (so the new source auto-avoids them)."""

    def test_classifier_checks_generation_timeout_first(self):
        from backend.core.ouroboros.governance import candidate_generator as cg
        src = inspect.getsource(cg)
        assert "is_generation_timeout" in src, "classifier must consult is_generation_timeout"
        assert "GENERATION_TIMEOUT" in src
        # the check must precede the LIVE_TRANSPORT catch-all in the dispatch source
        gt_idx = src.index("is_generation_timeout")
        # there is at least one LIVE_TRANSPORT assignment after the GT check
        assert "FailureSource.LIVE_TRANSPORT" in src[gt_idx:]

    def test_degrade_and_sever_still_key_on_live_transport(self):
        # the whole point: these consumers branch on ==LIVE_TRANSPORT, so a
        # GENERATION_TIMEOUT (a different member) flows through them as a no-op.
        from backend.core.ouroboros.governance import candidate_generator as cg
        src = inspect.getsource(cg)
        assert "is FailureSource.LIVE_TRANSPORT" in src

    def test_generation_timeout_not_in_immortal_rupture_markers(self):
        # the immortal-retry rupture markers must NOT include a generation-timeout
        # marker (a budget timeout is not a vendor rupture to retry-around).
        from backend.core.ouroboros.governance import dw_immortal as imm
        src = inspect.getsource(imm)
        assert "tool_loop_deadline" not in src
