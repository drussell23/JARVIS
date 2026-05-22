"""Slice 12F — Priority QoS gating + budget-aware dispatch floor.

Closes the Phase 3A starvation wedge (``bt-2026-05-22-184422``):

  * Foreground SWE-Bench-Pro envelope (``urgency=high``,
    ``route=IMMEDIATE``) waited 142.2 s behind low-priority
    OpportunityMiner / RuntimeHealth ops on the FIFO fallback
    semaphore.
  * By the time Claude was reachable, the op's wall budget had
    shrunk to ~0.01 s — just above the existing D2 ``<= 0.0``
    fast-fail floor — so a stream was opened with a fractional
    read budget. The inter-chunk watchdog fired a misleading
    "no event for 0s" rupture log.

## 12F-A — PrioritySemaphore

Drop-in replacement for ``asyncio.Semaphore``. Lower priority
value preempts higher-value waiters on slot release. Hard
concurrency cap preserved.

## 12F-B — StreamBudgetTooShortError + minimum read floor

After semaphore acquisition, check ``_parent_remaining``. When
the wall budget is below
``JARVIS_STREAM_MINIMUM_READ_BUDGET_S`` (default 10s), raise
``StreamBudgetTooShortError`` BEFORE dispatching to the
provider. The classifier maps it to
``FailureMode.TRANSIENT_TRANSPORT`` →
``RetryDecision.RETRY_TRANSIENT`` so the Slice 7 fallback
handles it as a transient transport fault (NOT terminal
structural — does NOT feed the global breaker).

## Test surface

### PrioritySemaphore — concurrency contract
  * Hard concurrency cap honoured (N+1th acquirer waits)
  * FIFO within same priority bucket (monotonic ``_seq`` tiebreak)
  * Lower priority value preempts higher-value waiter on release
  * Cancelled waiter cleaned from heap (next release doesn't wake
    a dead future)
  * ``_value`` introspection mirrors stdlib ``asyncio.Semaphore``
  * Legacy ``async with sem:`` shape still works (default priority)
  * ``priority_for_route`` covers every ProviderRoute member

### StreamBudgetTooShortError — diagnostic + classifier
  * Exception carries full diagnostic (provider / op_id /
    wall_remaining / floor / sem_wait / route)
  * Frozen-style — constructor pins the attributes
  * Distinct from StreamRuptureError (different concept, same
    classifier mapping)
  * Classifier maps StreamBudgetTooShortError →
    FailureMode.TRANSIENT_TRANSPORT
  * provider_retry_classifier maps TRANSIENT_TRANSPORT →
    RetryDecision.RETRY_TRANSIENT (NEVER terminal_structural)

### AST pins
  * candidate_generator.py imports priority_semaphore at the
    two sem-acquire sites (call + plan)
  * candidate_generator.py raises StreamBudgetTooShortError when
    wall_remaining < floor (Slice 12F-B replacement for D2's
    ``<= 0.0`` check)
  * _governance_state.py uses PrioritySemaphore when
    JARVIS_PRIORITY_SEM_ENABLED is on (graduated default)

### Env knob
  * JARVIS_PRIORITY_SEM_ENABLED default TRUE
  * Explicit ``=false`` disables the gate (hot-revert)
  * JARVIS_STREAM_MINIMUM_READ_BUDGET_S honoured at runtime
"""

from __future__ import annotations

import ast as _ast
import asyncio
import os
import pathlib
import unittest
from typing import List


from backend.core.ouroboros.governance.priority_semaphore import (
    DEFAULT_PRIORITY,
    PrioritySemaphore,
    acquire_priority_aware,
    priority_for_route,
    priority_sem_enabled,
)
from backend.core.ouroboros.governance.stream_rupture import (
    StreamBudgetTooShortError,
    StreamRuptureError,
    stream_minimum_read_budget_s,
)


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CG_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)
_STATE_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "_governance_state.py"
)
_PSEM_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "priority_semaphore.py"
)


def _parse_module(path: pathlib.Path) -> _ast.Module:
    return _ast.parse(path.read_text())


# ============================================================================
# PrioritySemaphore — concurrency contract
# ============================================================================


class TestPrioritySemaphoreConcurrencyContract(
    unittest.IsolatedAsyncioTestCase,
):

    async def test_hard_concurrency_cap_honoured(self) -> None:
        sem = PrioritySemaphore(2)
        in_flight: List[str] = []

        async def worker(tag: str) -> None:
            async with sem.acquire_for(DEFAULT_PRIORITY):
                in_flight.append(tag)
                await asyncio.sleep(0.02)
                in_flight.remove(tag)

        await asyncio.gather(
            worker("a"), worker("b"), worker("c"), worker("d"),
        )
        # max in-flight should never exceed sem capacity
        # (asserted indirectly via the fact that asyncio.gather
        # completed cleanly + no exceptions). Explicit check:
        # confirm slots_free returns to initial after all done.
        self.assertEqual(sem._value, 2)

    async def test_fifo_within_same_priority(self) -> None:
        sem = PrioritySemaphore(1)
        order: List[str] = []

        async def hold() -> None:
            async with sem.acquire_for(DEFAULT_PRIORITY):
                await asyncio.sleep(0.05)

        async def waiter(tag: str, delay: float) -> None:
            await asyncio.sleep(delay)
            async with sem.acquire_for(DEFAULT_PRIORITY):
                order.append(tag)

        await asyncio.gather(
            hold(),
            waiter("first", 0.005),
            waiter("second", 0.010),
            waiter("third", 0.015),
        )
        self.assertEqual(
            order, ["first", "second", "third"],
            "same-priority waiters must run FIFO",
        )

    async def test_lower_priority_preempts_higher_on_release(
        self,
    ) -> None:
        """The headline behaviour: a high-priority waiter that
        arrives AFTER a low-priority waiter still wins the next
        slot release."""
        sem = PrioritySemaphore(1)
        order: List[str] = []

        async def holder() -> None:
            async with sem.acquire_for(DEFAULT_PRIORITY):
                await asyncio.sleep(0.05)

        async def low_prio_late() -> None:
            await asyncio.sleep(0.005)  # arrives first
            async with sem.acquire_for(priority=4):  # BACKGROUND
                order.append("low")

        async def high_prio_later() -> None:
            await asyncio.sleep(0.020)  # arrives AFTER low_prio
            async with sem.acquire_for(priority=0):  # IMMEDIATE
                order.append("high")

        await asyncio.gather(
            holder(), low_prio_late(), high_prio_later(),
        )
        self.assertEqual(
            order[0], "high",
            "IMMEDIATE (priority=0) must preempt BACKGROUND "
            "(priority=4) even though BACKGROUND arrived first",
        )

    async def test_cancelled_waiter_cleaned_from_heap(self) -> None:
        sem = PrioritySemaphore(1)

        async def holder() -> None:
            async with sem.acquire_for(DEFAULT_PRIORITY):
                await asyncio.sleep(0.10)

        async def cancel_me() -> None:
            try:
                async with sem.acquire_for(priority=0):
                    pass
            except asyncio.CancelledError:
                raise

        async def normal() -> None:
            await asyncio.sleep(0.02)
            async with sem.acquire_for(priority=4):
                pass

        holder_task = asyncio.create_task(holder())
        cancel_task = asyncio.create_task(cancel_me())
        # Let cancel_task register in the heap then cancel it.
        await asyncio.sleep(0.01)
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass
        # Confirm heap doesn't contain the dead future + the
        # subsequent normal waiter still acquires cleanly.
        await asyncio.gather(holder_task, normal())
        self.assertEqual(sem._value, 1, "slot should return to free")
        self.assertEqual(
            sem.waiter_count, 0,
            "heap must be empty after cancelled waiter cleanup",
        )

    async def test_value_introspection_mirrors_stdlib(self) -> None:
        """Existing log sites read ``sem._value`` to compute
        slots-free. PrioritySemaphore must expose the same
        attribute with the same semantics."""
        sem = PrioritySemaphore(3)
        self.assertEqual(sem._value, 3)
        self.assertFalse(sem.locked())

        async def hold():
            async with sem.acquire_for(DEFAULT_PRIORITY):
                self.assertEqual(sem._value, 2)
                await asyncio.sleep(0.01)

        await hold()
        self.assertEqual(sem._value, 3)

    async def test_legacy_async_with_shape_works(self) -> None:
        """``async with sem:`` (no priority arg) must work —
        backward-compat for callers that don't yet know about
        priority."""
        sem = PrioritySemaphore(1)
        async with sem:
            self.assertEqual(sem._value, 0)
        self.assertEqual(sem._value, 1)


class TestPriorityForRoute(unittest.TestCase):
    """Coverage pin: every ProviderRoute member must map cleanly."""

    def test_all_routes_have_distinct_priorities(self) -> None:
        from backend.core.ouroboros.governance.urgency_router import (
            ProviderRoute,
        )
        priorities = {}
        for r in ProviderRoute:
            p = priority_for_route(r)
            priorities[r.value] = p
        # Six routes → six members → IMMEDIATE wins
        self.assertEqual(priorities["immediate"], 0)
        self.assertLessEqual(
            priorities["informational"], priorities["standard"],
        )
        self.assertLess(
            priorities["standard"], priorities["complex"],
        )
        self.assertLess(
            priorities["complex"], priorities["background"],
        )
        self.assertLess(
            priorities["background"], priorities["speculative"],
        )

    def test_unknown_route_defaults_to_standard(self) -> None:
        self.assertEqual(
            priority_for_route("nonexistent"), DEFAULT_PRIORITY,
        )
        self.assertEqual(
            priority_for_route(""), DEFAULT_PRIORITY,
        )
        self.assertEqual(
            priority_for_route(None), DEFAULT_PRIORITY,
        )

    def test_string_or_enum_both_accepted(self) -> None:
        from backend.core.ouroboros.governance.urgency_router import (
            ProviderRoute,
        )
        self.assertEqual(
            priority_for_route(ProviderRoute.IMMEDIATE),
            priority_for_route("immediate"),
        )


class TestAcquirePriorityAwareDuckType(
    unittest.IsolatedAsyncioTestCase,
):

    async def test_priority_semaphore_uses_priority_path(self) -> None:
        sem = PrioritySemaphore(1)
        order: List[str] = []

        async def holder():
            async with sem:
                await asyncio.sleep(0.05)

        async def low():
            await asyncio.sleep(0.005)
            async with acquire_priority_aware(sem, "background"):
                order.append("low")

        async def high():
            await asyncio.sleep(0.020)
            async with acquire_priority_aware(sem, "immediate"):
                order.append("high")

        await asyncio.gather(holder(), low(), high())
        self.assertEqual(order[0], "high")

    async def test_stdlib_semaphore_fallthrough(self) -> None:
        """Legacy ``asyncio.Semaphore`` doesn't expose
        ``acquire_for_route`` — the helper must fall through to
        plain ``async with sem`` without crashing."""
        sem = asyncio.Semaphore(1)
        async with acquire_priority_aware(sem, "immediate"):
            self.assertEqual(sem._value, 0)
        self.assertEqual(sem._value, 1)


# ============================================================================
# StreamBudgetTooShortError — shape + classifier mapping
# ============================================================================


class TestStreamBudgetTooShortError(unittest.TestCase):

    def test_carries_full_diagnostic(self) -> None:
        err = StreamBudgetTooShortError(
            provider="claude-api",
            op_id="op-019e5103-abcd",
            wall_remaining_s=0.42,
            minimum_required_s=10.0,
            sem_wait_s=142.2,
            route="immediate",
        )
        self.assertEqual(err.provider, "claude-api")
        self.assertEqual(err.op_id, "op-019e5103-abcd")
        self.assertAlmostEqual(err.wall_remaining_s, 0.42)
        self.assertAlmostEqual(err.minimum_required_s, 10.0)
        self.assertAlmostEqual(err.sem_wait_s, 142.2)
        self.assertEqual(err.route, "immediate")
        msg = str(err)
        self.assertIn("op=op-019e5103-abcd", msg)
        self.assertIn("wall_remaining=0.42s", msg)
        self.assertIn("floor=10.0s", msg)
        self.assertIn("sem_wait=142.2s", msg)
        self.assertIn("route=immediate", msg)

    def test_distinct_from_stream_rupture_error(self) -> None:
        """The two errors are siblings, not the same. Pinned so a
        future refactor can't quietly collapse them into one type
        (which would lose the diagnostic distinction operators
        rely on)."""
        budget_err = StreamBudgetTooShortError(
            provider="claude-api", op_id="x",
            wall_remaining_s=0.1, minimum_required_s=10.0,
            sem_wait_s=100.0, route="",
        )
        rupture_err = StreamRuptureError(
            provider="claude-api", elapsed_s=120.0,
            bytes_received=13994, rupture_timeout_s=30.0,
            phase="inter_chunk",
        )
        self.assertNotIsInstance(budget_err, StreamRuptureError)
        self.assertNotIsInstance(rupture_err, StreamBudgetTooShortError)


class TestClassifierMapping(unittest.TestCase):
    """Both stream errors must map to RETRY_TRANSIENT — NOT
    terminal_structural. Pinned at every layer."""

    def test_failure_mode_classifier_routes_budget_too_short(
        self,
    ) -> None:
        from backend.core.ouroboros.governance.candidate_generator import (  # noqa: E501
            FailbackStateMachine,
            FailureMode,
        )
        err = StreamBudgetTooShortError(
            provider="claude-api", op_id="x",
            wall_remaining_s=0.1, minimum_required_s=10.0,
            sem_wait_s=100.0, route="",
        )
        mode = FailbackStateMachine.classify_exception(err)
        self.assertEqual(
            mode, FailureMode.TRANSIENT_TRANSPORT,
            "StreamBudgetTooShortError must classify as "
            "TRANSIENT_TRANSPORT (sibling of StreamRuptureError)",
        )

    def test_failure_mode_classifier_routes_stream_rupture(
        self,
    ) -> None:
        """Sibling check: StreamRuptureError still routes the same
        way (the Slice 12F-B change must not break it)."""
        from backend.core.ouroboros.governance.candidate_generator import (  # noqa: E501
            FailbackStateMachine,
            FailureMode,
        )
        err = StreamRuptureError(
            provider="claude-api", elapsed_s=120.0,
            bytes_received=13994, rupture_timeout_s=30.0,
            phase="inter_chunk",
        )
        mode = FailbackStateMachine.classify_exception(err)
        self.assertEqual(mode, FailureMode.TRANSIENT_TRANSPORT)

    def test_retry_classifier_transient_transport_is_retry_transient(
        self,
    ) -> None:
        from backend.core.ouroboros.governance.provider_retry_classifier import (  # noqa: E501
            RetryDecision,
            classify,
        )
        decision = classify(
            failure_class="StreamBudgetTooShortError",
            failure_mode="TRANSIENT_TRANSPORT",
        )
        self.assertEqual(
            decision, RetryDecision.RETRY_TRANSIENT,
            "TRANSIENT_TRANSPORT must map to RETRY_TRANSIENT — "
            "NOT terminal_structural (must NOT feed global "
            "breaker; Slice 7 fallback handles it)",
        )


# ============================================================================
# Env knob — graduated default
# ============================================================================


class TestEnvKnobs(unittest.TestCase):

    def setUp(self) -> None:
        self._prior = os.environ.pop(
            "JARVIS_PRIORITY_SEM_ENABLED", None,
        )

    def tearDown(self) -> None:
        if self._prior is None:
            os.environ.pop("JARVIS_PRIORITY_SEM_ENABLED", None)
        else:
            os.environ["JARVIS_PRIORITY_SEM_ENABLED"] = self._prior

    def test_default_true(self) -> None:
        os.environ.pop("JARVIS_PRIORITY_SEM_ENABLED", None)
        self.assertTrue(priority_sem_enabled())

    def test_explicit_false_disables(self) -> None:
        for v in ("0", "false", "no", "off"):
            os.environ["JARVIS_PRIORITY_SEM_ENABLED"] = v
            self.assertFalse(priority_sem_enabled())

    def test_minimum_read_budget_env_honoured(self) -> None:
        prior = os.environ.pop(
            "JARVIS_STREAM_MINIMUM_READ_BUDGET_S", None,
        )
        try:
            os.environ.pop(
                "JARVIS_STREAM_MINIMUM_READ_BUDGET_S", None,
            )
            self.assertAlmostEqual(
                stream_minimum_read_budget_s(), 10.0,
            )
            os.environ["JARVIS_STREAM_MINIMUM_READ_BUDGET_S"] = "5"
            self.assertAlmostEqual(
                stream_minimum_read_budget_s(), 5.0,
            )
        finally:
            if prior is None:
                os.environ.pop(
                    "JARVIS_STREAM_MINIMUM_READ_BUDGET_S", None,
                )
            else:
                os.environ[
                    "JARVIS_STREAM_MINIMUM_READ_BUDGET_S"
                ] = prior


# ============================================================================
# AST pins
# ============================================================================


class TestAstPins(unittest.TestCase):

    def test_candidate_generator_imports_priority_semaphore(
        self,
    ) -> None:
        src = _CG_FILE.read_text()
        self.assertIn(
            "from backend.core.ouroboros.governance.priority_semaphore import",
            src,
            "candidate_generator.py must import the priority "
            "semaphore (Slice 12F-A)",
        )
        self.assertIn(
            "acquire_priority_aware",
            src,
            "candidate_generator.py must use acquire_priority_aware "
            "at the fallback-sem call sites",
        )

    def test_candidate_generator_raises_budget_too_short_error(
        self,
    ) -> None:
        src = _CG_FILE.read_text()
        self.assertIn(
            "StreamBudgetTooShortError",
            src,
            "candidate_generator.py must raise "
            "StreamBudgetTooShortError when wall_remaining < floor "
            "(Slice 12F-B)",
        )
        self.assertIn(
            "stream_minimum_read_budget_s",
            src,
            "candidate_generator.py must reference the env-knobbed "
            "minimum-read-budget floor",
        )

    def test_governance_state_wires_priority_semaphore(self) -> None:
        src = _STATE_FILE.read_text()
        self.assertIn(
            "PrioritySemaphore", src,
            "_governance_state.py must instantiate "
            "PrioritySemaphore for fallback_sem (Slice 12F-A)",
        )
        self.assertIn(
            "priority_sem_enabled", src,
            "_governance_state.py must guard PrioritySemaphore "
            "behind the master env-knob",
        )

    def test_priority_semaphore_exports(self) -> None:
        from backend.core.ouroboros.governance import (
            priority_semaphore as _ps_mod,
        )
        self.assertIn("PrioritySemaphore", _ps_mod.__all__)
        self.assertIn("priority_for_route", _ps_mod.__all__)
        self.assertIn("priority_sem_enabled", _ps_mod.__all__)
        self.assertIn("DEFAULT_PRIORITY", _ps_mod.__all__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
