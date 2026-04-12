"""HIBERNATION_MODE step 8 — sealed-loop end-to-end integration test.

This file is the "does the whole protocol actually work?" check. It
wires every real component from the HIBERNATION pipeline together with
zero mocks beyond a flipping fake provider, then drives the full
outage → hibernation → recovery → wake cycle and asserts every
observable side effect fires in the correct order.

Components under test (all real — no stubs except the provider):

    BackgroundAgentPool     (pool.pause / pool.resume)
    IdleWatchdog            (watchdog.freeze / watchdog.unfreeze)
    SupervisorOuroborosController
    ProviderExhaustionWatcher
    HibernationProber
    CommProtocol + LogTransport
    GovernedLoopService._build_hibernation_observability_hooks factory

Why this file exists
--------------------
Every preceding HIBERNATION step has its own unit file:

    step 1 test_background_agent_pool (pool.pause/resume)
    step 2 test_idle_watchdog         (watchdog.freeze/unfreeze)
    step 3 test_supervisor_controller (AutonomyMode.HIBERNATION enum)
    step 4 test_supervisor_controller (enter/wake transitions)
    step 5 test_provider_exhaustion_watcher
    step 6 test_hibernation_prober
    step 6.5 test_hibernation_hooks   (controller hook pub/sub)
    step 7 test_hibernation_observability (comm emission)

Those are all narrow-scope tests. Step 8 is the sealed-loop check:
if any two components disagree on a contract boundary — e.g. the
watcher expects a method name the controller doesn't expose, or the
prober's wake signal races the observability hook — it surfaces here
as a single red test, even if every upstream unit test is green.

Why we mirror GovernedLoopService wiring here
---------------------------------------------
We cannot import the whole ``GovernedLoopService`` without dragging in
every brain-selection + ledger + intake-router side effect. Instead,
this test replicates the exact wiring that ``_build_components()``
performs (pool pause + watchdog freeze bridge hooks + observability
hook factory). The factory call in particular is the real deal — so a
refactor that breaks the factory signature breaks this test too.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import List

import pytest

from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopService,
)
from backend.core.ouroboros.governance.hibernation_prober import (
    HibernationProber,
)
from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
    ProviderExhaustionWatcher,
)
from backend.core.ouroboros.governance.supervisor_controller import (
    AutonomyMode,
    SupervisorOuroborosController,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _OrchestratorStub:
    """Minimal orchestrator surface so BackgroundAgentPool can start."""

    async def run_operation(self, *args, **kwargs) -> None:
        return None


class _FlippingProvider:
    """Provider whose ``health_probe`` returns False until :meth:`flip`
    is called, after which it returns True. Tracks probe count so tests
    can assert the prober actually called us.
    """

    def __init__(self, name: str = "primary") -> None:
        self.provider_name = name
        self._healthy = False
        self.probe_count = 0

    def flip(self) -> None:
        self._healthy = True

    async def health_probe(self) -> bool:
        self.probe_count += 1
        return self._healthy


# ---------------------------------------------------------------------------
# Assembly helper — replicates GovernedLoopService wiring
# ---------------------------------------------------------------------------


class _AssembledStack:
    """Bundle of the six real components wired exactly as the production
    service wires them. Owns teardown so ``async with`` blocks stay
    tidy — pool/watchdog cleanup is idempotent and must not leak tasks
    into subsequent tests.
    """

    def __init__(
        self,
        *,
        probe_initial_s: float = 0.01,
        probe_max_s: float = 0.02,
        probe_budget_s: float = 2.0,
        watcher_threshold: int = 1,
    ) -> None:
        self.pool = BackgroundAgentPool(orchestrator=_OrchestratorStub())
        self.watchdog = IdleWatchdog(timeout_s=60.0)
        self.watchdog.idle_event = asyncio.Event()

        self.transport = LogTransport()
        self.comm = CommProtocol(transports=[self.transport])

        # Stack-like wrapper — only needs ``.comm`` to satisfy the
        # observability factory's contract.
        self.stack = SimpleNamespace(comm=self.comm)

        self.controller = SupervisorOuroborosController()

        self.provider = _FlippingProvider(name="dw")
        self.prober = HibernationProber(
            controller=self.controller,
            providers=[self.provider],
            initial_delay_s=probe_initial_s,
            max_delay_s=probe_max_s,
            max_duration_s=probe_budget_s,
        )

        self.watcher = ProviderExhaustionWatcher(
            controller=self.controller,
            threshold=watcher_threshold,
            prober=self.prober,
        )

        self.bridge_fires: List[str] = []

    def wire(self) -> None:
        """Register every hook the production service registers."""
        pool = self.pool
        watchdog = self.watchdog
        fires = self.bridge_fires

        def _bridge_hibernate(*, reason: str) -> None:
            fires.append(f"hibernate:{reason}")
            pool.pause(reason=f"hibernation: {reason}")
            watchdog.freeze(reason=f"hibernation: {reason}")

        def _bridge_wake(*, reason: str) -> None:
            fires.append(f"wake:{reason}")
            watchdog.unfreeze(reason=f"wake: {reason}")
            pool.resume(reason=f"wake: {reason}")

        self.controller.register_hibernation_hooks(
            on_hibernate=_bridge_hibernate,
            on_wake=_bridge_wake,
            name="test.bridge",
        )

        on_hib_obs, on_wake_obs = (
            GovernedLoopService._build_hibernation_observability_hooks(
                self.stack,
            )
        )
        self.controller.register_hibernation_hooks(
            on_hibernate=on_hib_obs,
            on_wake=on_wake_obs,
            name="test.observability",
        )

    async def start(self) -> None:
        await self.pool.start()
        await self.watchdog.start()
        # ``start()`` puts the controller in SANDBOX. We deliberately do
        # NOT call ``enable_governed_autonomy()`` here — the governance
        # gates it checks live in GovernedLoopService and aren't wired up
        # in this standalone test assembly. Hibernation is valid from
        # SANDBOX and the pre-hibernation mode is restored on wake, so
        # the sealed-loop check rides SANDBOX → HIBERNATION → SANDBOX.
        await self.controller.start()

    async def stop(self) -> None:
        # Controller first so hooks clear before we yank subsystems.
        try:
            await self.controller.stop()
        except Exception:
            pass
        try:
            await self.prober.stop()
        except Exception:
            pass
        try:
            await self.pool.stop()
        except Exception:
            pass
        try:
            self.watchdog.stop()
        except Exception:
            pass

    async def wait_for(
        self,
        predicate,
        *,
        timeout_s: float = 2.0,
        tick_s: float = 0.01,
    ) -> bool:
        """Spin until *predicate* returns truthy or the timeout expires.

        Returns the final predicate value so callers can assert on it
        without an extra read after the loop exits. Keeps test code
        readable without hand-rolled deadline loops.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(tick_s)
        return predicate()


# ---------------------------------------------------------------------------
# Step 8 — the sealed-loop test
# ---------------------------------------------------------------------------


class TestHibernationSealedLoop:
    @pytest.mark.asyncio
    async def test_full_outage_to_recovery_cycle(self):
        """The decisive step 8 check: one record_exhaustion() call must
        drive the entire organism through HIBERNATION and back.

        Expected sequence of observable events:

            record_exhaustion
              → watcher.threshold met
              → controller.enter_hibernation
                  → mode = HIBERNATION
                  → bridge hook: pool.pause + watchdog.freeze
                  → obs hook:    HEARTBEAT(enter) + DECISION(entered)
              → watcher.prober.start (no-op if already probing)
            provider.flip()
              → prober.probe_loop reads True
              → controller.wake_from_hibernation
                  → mode = GOVERNED (restored)
                  → bridge hook: watchdog.unfreeze + pool.resume
                  → obs hook:    HEARTBEAT(wake) + DECISION(wake) + POSTMORTEM
              → prober task exits cleanly
        """
        stack = _AssembledStack(
            probe_initial_s=0.01,
            probe_max_s=0.02,
            probe_budget_s=2.0,
            watcher_threshold=1,
        )
        stack.wire()
        await stack.start()

        try:
            # --- Pre-conditions ------------------------------------
            assert stack.controller.mode is AutonomyMode.SANDBOX
            assert stack.pool.is_paused is False
            assert stack.watchdog.is_frozen is False
            assert stack.prober.is_probing is False
            assert stack.transport.messages == []

            # --- Drive the outage ----------------------------------
            triggered = await stack.watcher.record_exhaustion(
                reason="dw 5xx burst",
            )
            assert triggered is True
            assert stack.watcher.hibernations_triggered == 1

            # --- Post-enter state ----------------------------------
            assert stack.controller.mode is AutonomyMode.HIBERNATION
            assert stack.pool.is_paused is True
            assert stack.watchdog.is_frozen is True
            assert stack.prober.is_probing is True

            # Bridge hook fired once on the enter side; not yet on wake.
            # The watcher wraps the raw reason as
            # ``consecutive_exhaustion=N last='<reason>'`` so we check
            # containment rather than exact equality.
            assert len(stack.bridge_fires) == 1
            assert stack.bridge_fires[0].startswith("hibernate:")
            assert "dw 5xx burst" in stack.bridge_fires[0]

            # Exactly the two enter-side comm messages are in flight
            # before the provider recovers.
            await stack.wait_for(
                lambda: len(stack.transport.messages) >= 2,
            )
            enter_types = [
                m.msg_type for m in stack.transport.messages[:2]
            ]
            assert enter_types == [
                MessageType.HEARTBEAT,
                MessageType.DECISION,
            ]

            # Prober must have already probed at least once.
            await stack.wait_for(
                lambda: stack.provider.probe_count >= 1,
            )
            assert stack.provider.probe_count >= 1
            assert stack.prober.wake_count == 0  # still not healthy

            # --- Flip provider healthy and wait for wake -----------
            stack.provider.flip()

            # The controller flips ``_mode = target`` BEFORE awaiting
            # ``_fire_hibernation_hooks`` in ``wake_from_hibernation``,
            # which means checking only ``controller.mode`` here races
            # the bridge wake hook (pool.resume + watchdog.unfreeze).
            # Wait for the full post-wake quiescent state instead, so
            # downstream assertions see bridge side effects settled.
            woke = await stack.wait_for(
                lambda: (
                    stack.controller.mode is AutonomyMode.SANDBOX
                    and stack.pool.is_paused is False
                    and stack.watchdog.is_frozen is False
                ),
                timeout_s=2.0,
            )
            assert woke is True

            # --- Post-wake state -----------------------------------
            assert stack.controller.mode is AutonomyMode.SANDBOX
            assert stack.pool.is_paused is False
            assert stack.watchdog.is_frozen is False

            # Bridge fires for BOTH enter and wake now. Enter reason is
            # wrapped by the watcher (see earlier containment check).
            await stack.wait_for(lambda: len(stack.bridge_fires) >= 2)
            assert stack.bridge_fires[0].startswith("hibernate:")
            assert "dw 5xx burst" in stack.bridge_fires[0]
            assert stack.bridge_fires[1].startswith("wake:")

            # Prober completed cleanly and wake_count is 1.
            await stack.wait_for(lambda: stack.prober.is_probing is False)
            assert stack.prober.wake_count == 1
            snap = stack.prober.snapshot()
            # ``last_result`` is ``woken_by:<provider_name>`` — see
            # ``HibernationProber._probe_loop``.
            assert snap["last_result"] == "woken_by:dw"

            # Five-message observability sequence captured, sharing a
            # single op_id across enter and wake.
            await stack.wait_for(
                lambda: len(stack.transport.messages) >= 5,
            )
            all_types = [m.msg_type for m in stack.transport.messages]
            assert all_types == [
                MessageType.HEARTBEAT,
                MessageType.DECISION,
                MessageType.HEARTBEAT,
                MessageType.DECISION,
                MessageType.POSTMORTEM,
            ]
            op_ids = {m.op_id for m in stack.transport.messages}
            assert len(op_ids) == 1

            # Decisions carry the outage reason verbatim.
            decisions = [
                m for m in stack.transport.messages
                if m.msg_type is MessageType.DECISION
            ]
            assert decisions[0].payload["outcome"] == "hibernation_entered"
            assert decisions[0].payload["reason_code"] == "provider_exhaustion"
            assert "dw 5xx burst" in decisions[0].payload["diff_summary"]
            assert decisions[1].payload["outcome"] == "hibernation_wake"
            assert decisions[1].payload["reason_code"] == "provider_recovery"

            # Watcher counter still reports 1 hibernation triggered — wake
            # does NOT re-fire the threshold.
            assert stack.watcher.hibernations_triggered == 1
        finally:
            await stack.stop()

    @pytest.mark.asyncio
    async def test_two_consecutive_cycles_keep_state_clean(self):
        """A second outage after a successful recovery must behave like
        the first — no stale bridge state, no duplicate comm messages,
        no stuck prober task."""
        stack = _AssembledStack(
            probe_initial_s=0.01,
            probe_max_s=0.02,
            probe_budget_s=2.0,
            watcher_threshold=1,
        )
        stack.wire()
        await stack.start()

        try:
            # -- cycle 1 --
            await stack.watcher.record_exhaustion(reason="cycle1 down")
            assert stack.controller.mode is AutonomyMode.HIBERNATION
            stack.provider.flip()
            await stack.wait_for(
                lambda: stack.controller.mode is AutonomyMode.SANDBOX,
                timeout_s=2.0,
            )
            await stack.wait_for(
                lambda: len(stack.transport.messages) >= 5,
            )

            # Reset provider flag so cycle 2 probes fail-first again.
            stack.provider._healthy = False
            # Reset watcher so threshold fires anew.
            await stack.watcher.record_success()

            # -- cycle 2 --
            triggered2 = await stack.watcher.record_exhaustion(
                reason="cycle2 down",
            )
            assert triggered2 is True
            assert stack.controller.mode is AutonomyMode.HIBERNATION
            assert stack.pool.is_paused is True
            assert stack.watchdog.is_frozen is True

            stack.provider.flip()
            await stack.wait_for(
                lambda: stack.controller.mode is AutonomyMode.SANDBOX,
                timeout_s=2.0,
            )

            # Two full 5-message sequences → 10 messages total.
            await stack.wait_for(
                lambda: len(stack.transport.messages) >= 10,
            )
            assert len(stack.transport.messages) == 10

            # Two distinct op_ids — cycle counter incremented.
            op_ids = {m.op_id for m in stack.transport.messages}
            assert len(op_ids) == 2

            # Watcher counter reflects both triggers.
            assert stack.watcher.hibernations_triggered == 2

            # Bridge fires hibernate+wake twice each → 4 entries total.
            assert len(stack.bridge_fires) == 4
            assert stack.bridge_fires[0].startswith("hibernate:")
            assert stack.bridge_fires[1].startswith("wake:")
            assert stack.bridge_fires[2].startswith("hibernate:")
            assert stack.bridge_fires[3].startswith("wake:")
        finally:
            await stack.stop()

    @pytest.mark.asyncio
    async def test_budget_exhausted_leaves_controller_hibernating(self):
        """If the provider never recovers before the prober's budget
        expires, the controller stays in HIBERNATION and pool/watchdog
        stay paused. The operator can still issue a manual wake later.
        """
        stack = _AssembledStack(
            probe_initial_s=0.01,
            probe_max_s=0.01,
            probe_budget_s=0.1,  # trivially short budget
            watcher_threshold=1,
        )
        stack.wire()
        await stack.start()

        try:
            await stack.watcher.record_exhaustion(reason="persistent outage")
            assert stack.controller.mode is AutonomyMode.HIBERNATION

            # Wait for the prober to abandon its budget.
            await stack.wait_for(
                lambda: stack.prober.is_probing is False,
                timeout_s=1.0,
            )
            assert stack.prober.is_probing is False
            assert stack.prober.wake_count == 0
            assert stack.prober.snapshot()["last_result"] == "budget_exhausted"

            # Controller MUST still be hibernating — budget exhaustion
            # is not a wake signal.
            assert stack.controller.mode is AutonomyMode.HIBERNATION
            assert stack.pool.is_paused is True
            assert stack.watchdog.is_frozen is True

            # Manual wake still works and unwinds everything.
            await stack.controller.wake_from_hibernation(
                reason="operator override",
            )
            assert stack.controller.mode is AutonomyMode.SANDBOX
            assert stack.pool.is_paused is False
            assert stack.watchdog.is_frozen is False
        finally:
            await stack.stop()

    @pytest.mark.asyncio
    async def test_emergency_stop_during_probe_unwinds_cleanly(self):
        """Emergency stop while hibernating supersedes the mode, stops
        the prober task, and releases pool/watchdog through the wake
        hooks — step 6.5 bridge + step 7 obs must both fire on the wake
        path even though the mode target is EMERGENCY_STOP, not the
        pre-hibernation mode."""
        stack = _AssembledStack(
            probe_initial_s=0.05,
            probe_max_s=0.1,
            probe_budget_s=5.0,
            watcher_threshold=1,
        )
        stack.wire()
        await stack.start()

        try:
            await stack.watcher.record_exhaustion(reason="outage")
            assert stack.controller.mode is AutonomyMode.HIBERNATION
            assert stack.prober.is_probing is True

            await stack.controller.emergency_stop("operator halt")
            assert stack.controller.mode is AutonomyMode.EMERGENCY_STOP

            # Bridge wake side fired → pool + watchdog released.
            await stack.wait_for(
                lambda: stack.pool.is_paused is False
                and stack.watchdog.is_frozen is False,
                timeout_s=1.0,
            )
            assert stack.pool.is_paused is False
            assert stack.watchdog.is_frozen is False

            # Observability wake chain also ran.
            await stack.wait_for(
                lambda: len(stack.transport.messages) >= 5,
            )
            types = [m.msg_type for m in stack.transport.messages]
            assert MessageType.POSTMORTEM in types

            # The prober's loop should notice wake_count increment (the
            # prober itself was not the one that called wake here — the
            # controller did — so prober may still be running its own
            # task. Stop it explicitly so the teardown is clean.)
            await stack.prober.stop()
            assert stack.prober.is_probing is False
        finally:
            await stack.stop()
