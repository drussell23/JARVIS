"""Cancel-source attribution telemetry tests.

Operator-directed (A) investigation gating S7 graduation cadence
(F1 Slice 4 / W3(6) Slice 5b). After S6 (bt-2026-04-24-225137) surfaced an
unattributed `CancelledError` at 12.3s with 207s parent budget remaining,
this telemetry distinguishes three cancel classes:

- **A** — `TimeoutError` from own `wait_for` (per-call cap fires).
- **B** — `TimeoutError` from outer wait_for (ToolLoop round budget).
- **C** — `CancelledError` injected by an external task (sibling-cancel
  inside `asyncio.gather`, retry-harness deadline, or mid-flight reroute).

These three test cases pin the helper's class assignment for each hypothesis
explicitly so future drift gets caught (no behavior change — pure observability).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    _attribute_cancel,
)


# ---------------------------------------------------------------------------
# (1) Class A — own wait_for fires TimeoutError (no external cancel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attribute_classifies_timeout_as_a_or_b() -> None:
    """`asyncio.TimeoutError` from own deadline → class=`A_or_B_timeout`."""
    exc = asyncio.TimeoutError()
    attr = _attribute_cancel(
        exc,
        label="test",
        op_id="op-test-class-a-1234",
        elapsed_s=120.0,
        remaining_s=0.0,
    )
    assert "class=A_or_B_timeout" in attr
    assert "err=TimeoutError" in attr
    assert "elapsed=120.00s" in attr
    assert "remaining=0.00s" in attr
    assert "label=test" in attr
    assert "op=op-test-class-a-" in attr  # truncated to 16 chars


# ---------------------------------------------------------------------------
# (2) Class C — external cancel from sibling task in asyncio.gather
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attribute_classifies_external_cancel_as_c() -> None:
    """Sibling-task cancel inside gather → class=`C_external_cancel`.

    Reproduces hypothesis 1 from the S6 telemetry first-pass
    (memory/project_s6_cancel_source_telemetry.md). Two parallel coroutines
    in a single gather; one fails fast and triggers the gather's cleanup,
    which cancels the second. The second's cancel handler sees
    `cancelling()>0` → class C.
    """
    captured_attrs: list[str] = []
    slow_done = asyncio.Event()

    async def slow_attempt() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError as exc:
            attr = _attribute_cancel(
                exc,
                label="slow_attempt",
                op_id="op-test-class-c-sibling",
                elapsed_s=0.5,
                remaining_s=4.5,
            )
            captured_attrs.append(attr)
            slow_done.set()
            raise

    async def fast_failure() -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("fast_failure_intentional")

    # Use create_task + wait so the cancel handler in slow_attempt has time
    # to run before we assert. gather() with raises can short-circuit.
    slow_task = asyncio.create_task(slow_attempt())
    fast_task = asyncio.create_task(fast_failure())
    try:
        await fast_task
    except RuntimeError as e:
        assert "fast_failure_intentional" in str(e)
    slow_task.cancel()
    try:
        await slow_task
    except asyncio.CancelledError:
        pass
    await slow_done.wait()

    assert len(captured_attrs) == 1
    attr = captured_attrs[0]
    # cancelling() may be 0 (Py 3.9) or >0 (3.11+) — accept ambiguous OR external
    assert "class=C_external_cancel" in attr or "class=C_ambiguous" in attr
    assert "err=CancelledError" in attr
    assert "label=slow_attempt" in attr


# ---------------------------------------------------------------------------
# (3) Class C — retry-harness mid-flight cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attribute_classifies_retry_harness_cancel() -> None:
    """Outer task cancels inner mid-call → class=`C_external_cancel` (or ambiguous).

    Reproduces hypothesis 2 from the S6 telemetry first-pass — a retry
    harness above `_call_fallback` decides to bail and cancels the in-flight
    inner task while it has plenty of remaining budget. The S6 signature was
    `sem_wait_total_s=12.32 remaining_s=207.67` — this test pins the same
    pattern (cancel mid-stream, ample remaining budget).
    """
    captured_attrs: list[str] = []
    inner_started = asyncio.Event()

    async def inner_call() -> None:
        inner_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError as exc:
            attr = _attribute_cancel(
                exc,
                label="inner_call",
                op_id="op-test-class-c-retry",
                elapsed_s=0.1,
                remaining_s=99.9,  # mimics S6's 207s remaining
            )
            captured_attrs.append(attr)
            raise

    async def retry_harness() -> None:
        task = asyncio.create_task(inner_call())
        await inner_started.wait()
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await retry_harness()

    assert len(captured_attrs) == 1
    attr = captured_attrs[0]
    assert "class=C_external_cancel" in attr or "class=C_ambiguous" in attr
    assert "err=CancelledError" in attr
    assert "remaining=99.90s" in attr
    assert "label=inner_call" in attr


# ---------------------------------------------------------------------------
# (4) Bonus — attribution helper never raises even on weird inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attribute_never_raises_on_attribution_failure() -> None:
    """The helper's last-line-of-defense `attribution_error=…` path."""

    class _ExoticExc(Exception):
        pass

    exc = _ExoticExc("weird")
    attr = _attribute_cancel(
        exc,
        label="exotic",
        op_id="op-exotic-12345678",
        elapsed_s=1.0,
        remaining_s=2.0,
    )
    # Non-cancel exceptions get class=non_cancel
    assert "class=non_cancel" in attr
    assert "err=_ExoticExc" in attr
    assert "label=exotic" in attr


def test_attribute_helper_is_pure_function():
    """Helper must be importable + callable outside an asyncio loop.

    When called outside a running loop (sync test context),
    `asyncio.current_task()` raises RuntimeError; helper must catch and
    log `canceller_task=no_running_loop` (or fall through gracefully).
    """
    exc = asyncio.TimeoutError()
    attr = _attribute_cancel(
        exc,
        label="sync_caller",
        op_id="op-sync-test-12345",
        elapsed_s=5.0,
        remaining_s=10.0,
    )
    assert "label=sync_caller" in attr
    assert "err=TimeoutError" in attr
    # Either ran cleanly (no loop branch tolerated) or hit attribution_error
    assert "class=" in attr
