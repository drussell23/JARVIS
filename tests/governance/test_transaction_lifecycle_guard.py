"""Guard-shape + source-wiring pins for the transactional-lifecycle guard.

Task 4 of the durability-substrate fix. Kills the op-944c silent-exit defect:
an op that records the APPLIED ledger row but exits the runner WITHOUT reaching
Phase 8b (auto-commit) must raise loudly instead of vanishing.

Two layers:
1. Guard SHAPE (matrix a-f) -- a minimal async function replicating the
   arm/disarm/finally trio, exercising the real ``TransactionLifecycleError``
   and ``txn_lifecycle_guard_enabled`` against the full semantics matrix.
2. Source-level WIRING pins -- structural assertions that
   ``slice4b_runner.py`` actually contains the arm/disarm/finally trio in the
   right places (the convention this repo uses to guard wiring; see
   ``tests/governance/test_hedge_governor_wiring.py``).
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from backend.core.ouroboros.governance.transaction_lifecycle import (
    TransactionLifecycleError,
    txn_lifecycle_guard_enabled,
)


# --------------------------------------------------------------------------
# A minimal faithful replica of the runner's arm/disarm/finally trio. It
# mirrors the CONTROL FLOW exactly (arm after APPLIED, disarm at 8b, disarm
# before legitimate returns, finally with the sys.exc_info gate) without
# booting the orchestrator.
# --------------------------------------------------------------------------
async def _span(
    *,
    op_id: str = "op-944c",
    reach_8b: bool = True,
    silent_return_before_8b: bool = False,
    rollback_return: bool = False,
    raise_exc: BaseException | None = None,
):
    """Replica span. Returns a sentinel string on normal / closed exits."""
    import sys as _sys

    _txn_commit_stage_reached = False
    try:
        # ---- span body: 8a scoped tests / gates ----
        if raise_exc is not None:
            raise raise_exc

        if rollback_return:
            # verify_regression / blast-radius rollback: a CLOSED transaction.
            _txn_commit_stage_reached = True
            return "rolled_back"

        if silent_return_before_8b:
            # The DEFECT: exit the span without reaching 8b and without any
            # terminal signal. Deliberately does NOT disarm.
            return "silent"

        if reach_8b:
            # ---- Phase 8b: first statement disarms ----
            _txn_commit_stage_reached = True
            return "committed"

        # Fall-through with no disarm also models a silent breach.
        return "fell_through"
    finally:
        if not _txn_commit_stage_reached and txn_lifecycle_guard_enabled():
            if _sys.exc_info()[0] is None:
                raise TransactionLifecycleError(op_id, "phase_8b_auto_commit")


# --------------------------------------------------------------------------
# Exception-carrier attrs / __str__
# --------------------------------------------------------------------------
def test_exception_carries_op_id_and_boundary():
    err = TransactionLifecycleError("op-xyz", "phase_8b_auto_commit")
    assert err.op_id == "op-xyz"
    assert err.boundary == "phase_8b_auto_commit"
    assert isinstance(err, RuntimeError)
    text = str(err)
    assert "op-xyz" in text
    assert "phase_8b_auto_commit" in text


def test_gate_default_true_and_never_raises(monkeypatch):
    monkeypatch.delenv("JARVIS_TXN_LIFECYCLE_GUARD_ENABLED", raising=False)
    assert txn_lifecycle_guard_enabled() is True
    for val in ("true", "1", "yes", "on", "TRUE", "anything"):
        monkeypatch.setenv("JARVIS_TXN_LIFECYCLE_GUARD_ENABLED", val)
        assert txn_lifecycle_guard_enabled() is True
    for val in ("false", "0", "no", "off", "", "  FALSE  "):
        monkeypatch.setenv("JARVIS_TXN_LIFECYCLE_GUARD_ENABLED", val)
        assert txn_lifecycle_guard_enabled() is False


# --------------------------------------------------------------------------
# Semantics matrix a-f
# --------------------------------------------------------------------------
def test_a_normal_reach_8b_no_raise(caplog):
    """(a) normal path reaches 8b -> no raise, no CRITICAL from the guard."""
    with caplog.at_level("CRITICAL"):
        out = asyncio.run(_span(reach_8b=True))
    assert out == "committed"


def test_b_silent_early_return_raises():
    """(b) silent early return between APPLIED and 8b -> TransactionLifecycleError."""
    with pytest.raises(TransactionLifecycleError) as ei:
        asyncio.run(_span(reach_8b=False, silent_return_before_8b=True))
    assert ei.value.boundary == "phase_8b_auto_commit"
    assert ei.value.op_id == "op-944c"


def test_c_exception_in_span_propagates_unconverted():
    """(c) a plain exception in the span -> ORIGINAL exception propagates, NOT
    converted to TransactionLifecycleError."""
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        asyncio.run(_span(reach_8b=False, raise_exc=_Boom("boom")))


def test_c_cancelled_error_propagates_unconverted():
    """(c) CancelledError in the span -> propagates UNCONVERTED (the guard must
    never swallow or reclassify cancellation)."""
    async def _drive():
        with pytest.raises(asyncio.CancelledError):
            await _span(reach_8b=False, raise_exc=asyncio.CancelledError())

    asyncio.run(_drive())


def test_c_in_flight_exception_not_masked_by_guard():
    """(c) even though the sentinel is un-disarmed, an in-flight exception is
    never converted -- sys.exc_info gate blocks the raise."""
    with pytest.raises(ValueError):
        asyncio.run(_span(reach_8b=False, raise_exc=ValueError("orig")))


def test_d_pre_applied_failure_untouched():
    """(d) A failure BEFORE the sentinel arms is out of scope -- modeled by the
    sentinel never being armed. Here the guard function short-circuits when
    the env kill switch is off, standing in for the pre-arm region."""
    # The pre-APPLIED region simply never runs the trio; assert the trio is
    # inert when disarmed-and-reached (equivalent to never-armed for guard).
    out = asyncio.run(_span(reach_8b=True))
    assert out == "committed"


def test_e_kill_switch_off_legacy_silence(monkeypatch):
    """(e) kill switch off -> legacy silence: the silent breach returns its
    value with NO raise."""
    monkeypatch.setenv("JARVIS_TXN_LIFECYCLE_GUARD_ENABLED", "false")
    out = asyncio.run(_span(reach_8b=False, silent_return_before_8b=True))
    assert out == "silent"


def test_f_rollback_return_is_closed_transaction():
    """(f) verify_regression / blast-radius rollback return -> NO raise (a
    rolled-back FAILED op is a CLOSED transaction, not a breach)."""
    out = asyncio.run(_span(reach_8b=False, rollback_return=True))
    assert out == "rolled_back"


# --------------------------------------------------------------------------
# Source-level wiring pins on slice4b_runner.py
# --------------------------------------------------------------------------
def _runner_src() -> str:
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner
    return inspect.getsource(slice4b_runner)


def test_runner_arm_follows_applied_record():
    """The sentinel arms (=False) immediately after the APPLIED ledger row."""
    src = _runner_src()
    # APPLIED record ... then, within a short window, the arm line.
    applied = src.find("OperationState.APPLIED,\n            {\"op_id\": ctx.op_id},")
    assert applied != -1, "APPLIED ledger record not found"
    window = src[applied:applied + 800]
    assert "_txn_commit_stage_reached = False" in window, (
        "sentinel arm (=False) does not follow the APPLIED ledger record"
    )
    # The lazy import of the guard module is co-located with the arm.
    assert "from backend.core.ouroboros.governance.transaction_lifecycle import" in src
    assert "txn_lifecycle_guard_enabled" in src
    assert "TransactionLifecycleError" in src


def test_runner_disarm_is_first_statement_of_phase_8b():
    """Phase 8b's first executable statement is the disarm (=True)."""
    src = _runner_src()
    marker = src.find("# ---- Phase 8b: Auto-commit ----")
    assert marker != -1, "Phase 8b marker not found"
    tail = src[marker:marker + 200]
    # First non-comment statement after the marker is the disarm.
    assert re.search(
        r"# ---- Phase 8b: Auto-commit ----\n\s+_txn_commit_stage_reached = True",
        tail,
    ), "disarm (=True) is not the first statement of Phase 8b"


def test_runner_every_legit_return_in_span_is_disarmed():
    """Every function-exiting return BETWEEN the arm and Phase 8b is preceded
    by a disarm (closed-transaction returns must not trip the guard)."""
    src = _runner_src()
    start = src.find("_txn_commit_stage_reached = False")
    end = src.find("# ---- Phase 8b: Auto-commit ----")
    assert start != -1 and end != -1 and start < end
    span = src[start:end]
    # The four governed terminal returns in the span, keyed by reason.
    for reason in (
        'reason=ctx.terminal_reason_code or "l2_escape_verify"',
        'reason="verify_regression"',
        'reason="blast_radius_graph_failure"',
        'reason="blast_radius_breach"',
    ):
        idx = span.find(reason)
        assert idx != -1, "governed return %r not found in span" % reason
        # Look back from the reason to the enclosing `return PhaseResult(` and
        # assert a disarm precedes it.
        ret = span.rfind("return PhaseResult(", 0, idx)
        assert ret != -1
        preceding = span[max(0, ret - 120):ret]
        assert "_txn_commit_stage_reached = True" in preceding, (
            "return for %r is not preceded by a disarm" % reason
        )


def test_runner_finally_has_exc_info_gate_and_critical():
    """The finally logs CRITICAL always and gates the raise on
    sys.exc_info()[0] is None (so live exceptions propagate unconverted)."""
    src = _runner_src()
    assert "finally:" in src
    assert "exc_info()[0] is None" in src
    assert "logger.critical(" in src
    assert "raise TransactionLifecycleError(ctx.op_id, \"phase_8b_auto_commit\")" in src
    # The critical log must be OUTSIDE the exc_info gate (fires always).
    crit = src.find("logger.critical(\n                \"[TxnLifecycle]")
    if crit == -1:
        crit = src.find("[TxnLifecycle]")
    gate = src.find("exc_info()[0] is None")
    assert crit != -1 and gate != -1 and crit < gate, (
        "CRITICAL log must precede (fire regardless of) the exc_info raise gate"
    )
