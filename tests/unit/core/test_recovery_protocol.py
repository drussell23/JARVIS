# tests/unit/core/test_recovery_protocol.py
"""Tests for RecoveryProtocol — probe, reconcile, orchestrate."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.lifecycle_engine import (
    ComponentDeclaration,
    ComponentLocality,
)
from backend.core.recovery_protocol import (
    HealthCategory,
    ProbeResult,
    RecoveryOrchestrator,
    RecoveryProber,
    RecoveryReconciler,
)


def _run(coro):
    """Helper: run a coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_journal(epoch=1, lease_held=True, current_seq=42):
    """Create a mock OrchestrationJournal with required properties."""
    journal = MagicMock()
    type(journal).epoch = property(lambda self: epoch)
    type(journal).lease_held = property(lambda self: lease_held)
    type(journal).current_seq = property(lambda self: current_seq)
    journal.fenced_write = MagicMock(return_value=current_seq + 1)
    journal.get_all_component_states = MagicMock(return_value={})
    return journal


def _make_engine():
    """Create a mock LifecycleEngine."""
    engine = MagicMock()
    engine.transition_component = AsyncMock(return_value=100)
    engine.get_declaration = MagicMock(return_value=None)
    return engine


def _make_decl(
    name="test_comp",
    locality=ComponentLocality.IN_PROCESS,
    endpoint=None,
    health_path="/health",
    handshake_timeout_s=10.0,
):
    """Create a ComponentDeclaration for testing."""
    return ComponentDeclaration(
        name=name,
        locality=locality,
        endpoint=endpoint,
        health_path=health_path,
        handshake_timeout_s=handshake_timeout_s,
    )


# ══════════════════════════════════════════════════════════════════
# TestHealthCategory
# ══════════════════════════════════════════════════════════════════

class TestHealthCategory:
    def test_all_five_values_exist(self):
        assert HealthCategory.HEALTHY.value == "healthy"
        assert HealthCategory.CONTRACT_MISMATCH.value == "contract_mismatch"
        assert HealthCategory.DEPENDENCY_DEGRADED.value == "dependency_degraded"
        assert HealthCategory.SERVICE_DEGRADED.value == "service_degraded"
        assert HealthCategory.UNREACHABLE.value == "unreachable"

    def test_count_is_five(self):
        assert len(HealthCategory) == 5

    def test_enum_members_are_distinct(self):
        values = [m.value for m in HealthCategory]
        assert len(values) == len(set(values))


# ══════════════════════════════════════════════════════════════════
# TestProbeResult
# ══════════════════════════════════════════════════════════════════

class TestProbeResult:
    def test_default_construction(self):
        pr = ProbeResult(reachable=True, category=HealthCategory.HEALTHY)
        assert pr.reachable is True
        assert pr.category == HealthCategory.HEALTHY
        assert pr.instance_id == ""
        assert pr.api_version == ""
        assert pr.error == ""
        assert pr.probe_epoch == 0
        assert pr.probe_seq == 0

    def test_unreachable_construction(self):
        pr = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            error="connection refused",
        )
        assert pr.reachable is False
        assert pr.category == HealthCategory.UNREACHABLE
        assert pr.error == "connection refused"

    def test_full_construction(self):
        pr = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            instance_id="inst-abc",
            api_version="v2.1",
            error="",
            probe_epoch=5,
            probe_seq=123,
        )
        assert pr.instance_id == "inst-abc"
        assert pr.api_version == "v2.1"
        assert pr.probe_epoch == 5
        assert pr.probe_seq == 123


# ══════════════════════════════════════════════════════════════════
# TestRecoveryProber
# ══════════════════════════════════════════════════════════════════

class TestRecoveryProber:
    def test_skip_stopped_component(self):
        journal = _make_journal()
        prober = RecoveryProber(journal)
        result = _run(prober.classify_for_probe("comp_a", "STOPPED"))
        assert result is None

    def test_skip_registered_component(self):
        journal = _make_journal()
        prober = RecoveryProber(journal)
        result = _run(prober.classify_for_probe("comp_a", "REGISTERED"))
        assert result is None

    def test_classify_active_state_as_unverified(self):
        journal = _make_journal()
        prober = RecoveryProber(journal)
        for state in ("STARTING", "HANDSHAKING", "READY", "DEGRADED",
                       "DRAINING", "STOPPING", "FAILED", "LOST"):
            result = _run(prober.classify_for_probe("comp_a", state))
            assert result == "UNVERIFIED", f"Expected UNVERIFIED for state {state}"

    def test_abort_on_lease_lost(self):
        journal = _make_journal(lease_held=False)
        prober = RecoveryProber(journal)
        decl = _make_decl()
        result = _run(prober.probe_component(decl, "READY"))
        assert result is None

    def test_in_process_probe_healthy(self):
        journal = _make_journal()
        prober = RecoveryProber(journal)
        decl = _make_decl(name="svc_a")

        async def healthy_probe():
            return {"status": "healthy", "instance_id": "i-123", "api_version": "v1"}

        prober.register_runtime_probe("svc_a", healthy_probe)
        result = _run(prober.probe_component(decl, "READY"))

        assert result is not None
        assert result.reachable is True
        assert result.category == HealthCategory.HEALTHY
        assert result.instance_id == "i-123"
        assert result.api_version == "v1"
        assert result.probe_epoch == 1

    def test_in_process_probe_no_registered_probe(self):
        journal = _make_journal()
        prober = RecoveryProber(journal)
        decl = _make_decl(name="svc_unregistered")

        result = _run(prober.probe_component(decl, "READY"))

        assert result is not None
        assert result.reachable is False
        assert result.category == HealthCategory.UNREACHABLE
        assert "no runtime probe registered" in result.error or "probe attempts exhausted" in result.error

    def test_probe_retries_on_failure(self):
        """Flaky probe: fails on first attempt, succeeds on second."""
        journal = _make_journal()
        prober = RecoveryProber(journal)
        decl = _make_decl(name="svc_flaky")

        call_count = 0

        async def flaky_probe():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient failure")
            return {"status": "healthy", "instance_id": "i-flaky"}

        prober.register_runtime_probe("svc_flaky", flaky_probe)

        # Patch sleep to avoid real delays in test
        with patch("backend.core.recovery_protocol.asyncio.sleep", new_callable=AsyncMock):
            result = _run(prober.probe_component(decl, "READY", max_attempts=2))

        assert result is not None
        assert result.reachable is True
        assert result.category == HealthCategory.HEALTHY
        assert call_count == 2

    def test_all_attempts_exhausted_returns_unreachable(self):
        journal = _make_journal()
        prober = RecoveryProber(journal)
        decl = _make_decl(name="svc_dead")

        async def always_fail():
            raise ConnectionError("permanently down")

        prober.register_runtime_probe("svc_dead", always_fail)

        with patch("backend.core.recovery_protocol.asyncio.sleep", new_callable=AsyncMock):
            result = _run(prober.probe_component(decl, "READY", max_attempts=3))

        assert result is not None
        assert result.reachable is False
        assert result.category == HealthCategory.UNREACHABLE
        assert "3 probe attempts exhausted" in result.error

    def test_classify_health_response_variants(self):
        journal = _make_journal()
        prober = RecoveryProber(journal)

        # Healthy responses
        for status in ("healthy", "ok", "ready"):
            result = prober._classify_health_response({"status": status})
            assert result.category == HealthCategory.HEALTHY, f"Expected HEALTHY for {status}"

        # Degraded
        result = prober._classify_health_response({"status": "degraded"})
        assert result.category == HealthCategory.SERVICE_DEGRADED

        # Dependency degraded
        result = prober._classify_health_response({"status": "dependency_degraded"})
        assert result.category == HealthCategory.DEPENDENCY_DEGRADED

        # Contract mismatch
        result = prober._classify_health_response({"status": "contract_mismatch"})
        assert result.category == HealthCategory.CONTRACT_MISMATCH

        # Unknown defaults to service_degraded
        result = prober._classify_health_response({"status": "something_weird"})
        assert result.category == HealthCategory.SERVICE_DEGRADED

    def test_lease_lost_between_retries(self):
        """Lease lost after first failed probe attempt."""
        lease_held_state = [True]  # mutable so the mock can change it

        journal = MagicMock()
        type(journal).epoch = property(lambda self: 1)
        type(journal).lease_held = property(lambda self: lease_held_state[0])
        type(journal).current_seq = property(lambda self: 42)

        prober = RecoveryProber(journal)
        decl = _make_decl(name="svc_lease_drop")

        call_count = 0

        async def probe_that_causes_lease_loss():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate: after this probe, lease is lost
                lease_held_state[0] = False
                raise ConnectionError("fail")
            return {"status": "healthy"}

        prober.register_runtime_probe("svc_lease_drop", probe_that_causes_lease_loss)

        with patch("backend.core.recovery_protocol.asyncio.sleep", new_callable=AsyncMock):
            result = _run(prober.probe_component(decl, "READY", max_attempts=3))

        # Should return None (lease lost) rather than continuing retries
        assert result is None


# ══════════════════════════════════════════════════════════════════
# TestRecoveryReconciler
# ══════════════════════════════════════════════════════════════════

class TestRecoveryReconciler:
    def test_ready_unreachable_becomes_lost(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "READY", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "LOST"
        assert actions[0]["reason"] == "reconcile_mark_lost"
        engine.transition_component.assert_called_once()

    def test_ready_healthy_is_noop(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "READY", probe))

        assert len(actions) == 0
        engine.transition_component.assert_not_called()

    def test_ready_service_degraded_becomes_degraded(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.SERVICE_DEGRADED,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "READY", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "DEGRADED"
        assert actions[0]["reason"] == "reconcile_mark_degraded"

    def test_ready_dependency_degraded_becomes_degraded(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.DEPENDENCY_DEGRADED,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "READY", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "DEGRADED"

    def test_ready_contract_mismatch_becomes_failed(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.CONTRACT_MISMATCH,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "READY", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "FAILED"
        assert actions[0]["reason"] == "reconcile_mark_failed"

    def test_degraded_contract_mismatch_becomes_failed(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.CONTRACT_MISMATCH,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "DEGRADED", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "FAILED"

    def test_failed_healthy_triggers_recovery_chain(self):
        journal = _make_journal()
        engine = _make_engine()
        decl = _make_decl(name="svc_a", handshake_timeout_s=10.0)
        engine.get_declaration.return_value = decl

        seq_counter = [100]

        async def inc_seq(*_args, **_kwargs):
            seq_counter[0] += 1
            return seq_counter[0]

        engine.transition_component = AsyncMock(side_effect=inc_seq)
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            instance_id="i-new",
            api_version="v2",
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "FAILED", probe))

        # Recovery chain: STARTING, HANDSHAKING, READY = 3 transitions
        assert len(actions) == 3
        assert actions[0]["to"] == "STARTING"
        assert actions[1]["to"] == "HANDSHAKING"
        assert actions[2]["to"] == "READY"
        for a in actions:
            assert a["reason"] == "reconcile_recover"

    def test_lost_healthy_triggers_recovery_chain(self):
        journal = _make_journal()
        engine = _make_engine()
        decl = _make_decl(name="svc_a", handshake_timeout_s=10.0)
        engine.get_declaration.return_value = decl

        seq_counter = [100]

        async def inc_seq(*_args, **_kwargs):
            seq_counter[0] += 1
            return seq_counter[0]

        engine.transition_component = AsyncMock(side_effect=inc_seq)
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "LOST", probe))

        assert len(actions) == 3
        assert actions[0]["to"] == "STARTING"
        assert actions[1]["to"] == "HANDSHAKING"
        assert actions[2]["to"] == "READY"

    def test_starting_unreachable_becomes_failed(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "STARTING", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "FAILED"
        assert actions[0]["reason"] == "reconcile_mark_failed"

    def test_handshaking_unreachable_becomes_failed(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "HANDSHAKING", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "FAILED"

    def test_draining_unreachable_becomes_stopped(self):
        journal = _make_journal()
        engine = _make_engine()

        seq_counter = [100]

        async def inc_seq(*_args, **_kwargs):
            seq_counter[0] += 1
            return seq_counter[0]

        engine.transition_component = AsyncMock(side_effect=inc_seq)
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "DRAINING", probe))

        # DRAINING -> STOPPING -> STOPPED = 2 transitions
        assert len(actions) == 2
        assert actions[0]["to"] == "STOPPING"
        assert actions[1]["to"] == "STOPPED"

    def test_stopping_unreachable_becomes_stopped(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "STOPPING", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "STOPPED"

    def test_idempotency_key_format(self):
        journal = _make_journal(epoch=7)
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            instance_id="inst-42",
            api_version="v3.1",
            probe_epoch=7,
        )

        key = reconciler._make_idempotency_key("svc_x", "READY", probe)
        assert key == "reconcile:svc_x:7:READY->unreachable:inst-42:v3.1"

    def test_abort_on_lease_lost(self):
        journal = _make_journal(lease_held=False)
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "READY", probe))

        assert len(actions) == 0
        engine.transition_component.assert_not_called()

    def test_degraded_unreachable_becomes_lost(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "DEGRADED", probe))

        assert len(actions) == 1
        assert actions[0]["to"] == "LOST"

    def test_degraded_healthy_is_noop(self):
        journal = _make_journal()
        engine = _make_engine()
        reconciler = RecoveryReconciler(journal, engine)

        probe = ProbeResult(
            reachable=True,
            category=HealthCategory.HEALTHY,
            probe_epoch=1,
        )
        actions = _run(reconciler.reconcile("svc_a", "DEGRADED", probe))

        assert len(actions) == 0


# ══════════════════════════════════════════════════════════════════
# TestRecoveryOrchestrator
# ══════════════════════════════════════════════════════════════════

class TestRecoveryOrchestrator:
    def test_startup_recovery_skips_stopped(self):
        journal = _make_journal()
        engine = _make_engine()
        prober = RecoveryProber(journal)

        # Simulate two components: one STOPPED, one READY
        journal.get_all_component_states.return_value = {
            "svc_stopped": {"status": "STOPPED"},
            "svc_ready": {"status": "READY"},
        }

        ready_decl = _make_decl(name="svc_ready")
        engine.get_declaration.return_value = ready_decl

        # Register a probe for svc_ready
        async def healthy():
            return {"status": "healthy"}

        prober.register_runtime_probe("svc_ready", healthy)

        orchestrator = RecoveryOrchestrator(journal, engine, prober)
        summary = _run(orchestrator.run_startup_recovery())

        assert summary["aborted"] is False
        assert summary["skipped"] == 1   # STOPPED was skipped
        assert summary["probed"] == 1    # READY was probed
        assert summary["epoch"] == 1

    def test_startup_recovery_aborts_on_lease_loss(self):
        lease_held_state = [True]

        journal = MagicMock()
        type(journal).epoch = property(lambda self: 1)
        type(journal).lease_held = property(lambda self: lease_held_state[0])
        type(journal).current_seq = property(lambda self: 42)

        engine = _make_engine()
        prober = RecoveryProber(journal)

        # Two components — we'll lose the lease after processing the first
        journal.get_all_component_states.return_value = {
            "svc_a": {"status": "READY"},
            "svc_b": {"status": "READY"},
        }

        decl_a = _make_decl(name="svc_a")
        decl_b = _make_decl(name="svc_b")

        def get_decl(name):
            return {"svc_a": decl_a, "svc_b": decl_b}.get(name)

        engine.get_declaration.side_effect = get_decl

        probe_count = 0

        async def probe_that_drops_lease():
            nonlocal probe_count
            probe_count += 1
            if probe_count == 1:
                # After probing svc_a, lose the lease
                lease_held_state[0] = False
            return {"status": "healthy"}

        prober.register_runtime_probe("svc_a", probe_that_drops_lease)
        prober.register_runtime_probe("svc_b", probe_that_drops_lease)

        orchestrator = RecoveryOrchestrator(journal, engine, prober)
        summary = _run(orchestrator.run_startup_recovery())

        assert summary["aborted"] is True
        assert summary["probed"] >= 1

    def test_startup_recovery_no_components(self):
        journal = _make_journal()
        engine = _make_engine()
        prober = RecoveryProber(journal)

        journal.get_all_component_states.return_value = {}

        orchestrator = RecoveryOrchestrator(journal, engine, prober)
        summary = _run(orchestrator.run_startup_recovery())

        assert summary["aborted"] is False
        assert summary["probed"] == 0
        assert summary["reconciled"] == 0
        assert summary["skipped"] == 0

    def test_startup_recovery_without_lease(self):
        journal = _make_journal(lease_held=False)
        engine = _make_engine()
        prober = RecoveryProber(journal)

        orchestrator = RecoveryOrchestrator(journal, engine, prober)
        summary = _run(orchestrator.run_startup_recovery())

        assert summary["aborted"] is True
        assert "lease not held" in summary["errors"][0]

    def test_sparse_audit_delegates_to_startup_recovery(self):
        journal = _make_journal()
        engine = _make_engine()
        prober = RecoveryProber(journal)

        journal.get_all_component_states.return_value = {}

        orchestrator = RecoveryOrchestrator(journal, engine, prober)
        summary = _run(orchestrator.run_sparse_audit())

        assert summary["aborted"] is False
        assert summary["epoch"] == 1

    def test_startup_recovery_handles_missing_declaration(self):
        journal = _make_journal()
        engine = _make_engine()
        prober = RecoveryProber(journal)

        journal.get_all_component_states.return_value = {
            "svc_unknown": {"status": "READY"},
        }
        engine.get_declaration.return_value = None  # No declaration found

        orchestrator = RecoveryOrchestrator(journal, engine, prober)
        summary = _run(orchestrator.run_startup_recovery())

        assert summary["skipped"] == 1
        assert summary["probed"] == 0

    def test_startup_recovery_full_reconcile_cycle(self):
        """End-to-end: READY component is unreachable, gets marked LOST."""
        journal = _make_journal()
        engine = _make_engine()
        prober = RecoveryProber(journal)

        journal.get_all_component_states.return_value = {
            "svc_down": {"status": "READY"},
        }

        decl = _make_decl(name="svc_down")
        engine.get_declaration.return_value = decl

        # svc_down has no probe registered -> will be UNREACHABLE
        # which triggers READY -> LOST reconciliation

        orchestrator = RecoveryOrchestrator(journal, engine, prober)
        summary = _run(orchestrator.run_startup_recovery())

        assert summary["probed"] == 1
        assert summary["reconciled"] == 1  # READY -> LOST
        engine.transition_component.assert_called_once()
        call_kwargs = engine.transition_component.call_args
        assert call_kwargs[0][1] == "LOST" or call_kwargs.kwargs.get("new_status") == "LOST"
