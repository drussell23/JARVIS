"""Tests for TendrilManager — structured concurrent exploration with context isolation."""
import asyncio

import pytest

from backend.core.topology.tendril_manager import (
    TendrilManager,
    TendrilOutcome,
    TendrilState,
    ctx_tendril_id,
    ctx_tendril_repo,
)


# ---------------------------------------------------------------------------
# Fake target and strategy for testing
# ---------------------------------------------------------------------------


class _FakeCapability:
    def __init__(self, name="test_cap", domain="exploration", repo_owner="jarvis"):
        self.name = name
        self.domain = domain
        self.repo_owner = repo_owner


class _FakeTarget:
    def __init__(self, name="test_cap", domain="exploration", repo_owner="jarvis"):
        self.capability = _FakeCapability(name, domain, repo_owner)
        self.ucb_score = 1.0
        self.entropy_score = 0.5
        self.feasibility_score = 0.9
        self.rationale = "test"


class _FakeHardware:
    max_shadow_harness_workers = 2
    compute_tier = type("CT", (), {"value": "LOCAL_CPU"})()
    cpu_logical_cores = 4
    ram_total_mb = 16000
    ram_available_mb = 8000
    gpu = None
    os_family = "Darwin"
    hostname = "test"
    python_version = "3.11"
    max_parallel_inference_tasks = 4


class _FakeOutcome:
    def __init__(self, success=True):
        self.dead_end_class = type("DEC", (), {"value": "clean_success" if success else "timeout"})()
        self.capability_name = "test_cap"
        self.elapsed_seconds = 1.5
        self.partial_findings = '{"success": true}'
        self.unwind_actions_taken = []


class _FakeStrategy:
    """Fake strategy that returns immediately."""
    async def run(self, **kwargs):
        await asyncio.sleep(0.01)
        return type("Result", (), {
            "success": True,
            "phases_completed": ["RESEARCH", "SYNTHESIZE"],
            "failure_reason": "",
            "elapsed_seconds": 0.01,
            "synthesis": None,
            "validation": None,
        })()


class _FailingStrategy:
    """Strategy that raises an error."""
    async def run(self, **kwargs):
        raise RuntimeError("deliberate failure for testing")


# ---------------------------------------------------------------------------
# TendrilOutcome tests
# ---------------------------------------------------------------------------


class TestTendrilOutcome:
    def test_frozen(self):
        outcome = TendrilOutcome(
            capability_name="test",
            state=TendrilState.COMPLETED,
            elapsed_seconds=1.0,
        )
        with pytest.raises(AttributeError):
            outcome.capability_name = "changed"

    def test_default_values(self):
        outcome = TendrilOutcome(
            capability_name="test",
            state=TendrilState.COMPLETED,
            elapsed_seconds=0.5,
        )
        assert outcome.dead_end_class == ""
        assert outcome.partial_findings == ""
        assert outcome.context_vars_isolated is True


class TestTendrilState:
    def test_all_states(self):
        assert TendrilState.SPAWNING.value == "spawning"
        assert TendrilState.RUNNING.value == "running"
        assert TendrilState.COMPLETED.value == "completed"
        assert TendrilState.FAILED.value == "failed"
        assert TendrilState.CANCELLED.value == "cancelled"


# ---------------------------------------------------------------------------
# TendrilManager tests
# ---------------------------------------------------------------------------


class TestTendrilManager:
    def test_health_snapshot(self):
        tm = TendrilManager()
        h = tm.health()
        assert h["active_tendrils"] == 0
        assert h["completed_tendrils"] == 0
        assert h["max_concurrent"] == 2
        assert h["semaphore_available"] == 2

    @pytest.mark.asyncio
    async def test_spawn_exploration_success(self):
        tm = TendrilManager(hardware=_FakeHardware())
        target = _FakeTarget()

        # Mock the sentinel execution
        async def mock_run_sentinel(t, s):
            await asyncio.sleep(0.01)
            return TendrilOutcome(
                capability_name=t.capability.name,
                state=TendrilState.COMPLETED,
                elapsed_seconds=0.01,
            )

        tm._run_sentinel = mock_run_sentinel
        outcome = await tm.spawn_exploration(target)

        assert outcome.state == TendrilState.COMPLETED
        assert outcome.capability_name == "test_cap"
        assert tm.health()["completed_tendrils"] == 1

    @pytest.mark.asyncio
    async def test_spawn_exploration_failure(self):
        tm = TendrilManager(hardware=_FakeHardware())
        target = _FakeTarget()

        async def failing_sentinel(t, s):
            raise RuntimeError("sentinel crash")

        tm._run_sentinel = failing_sentinel
        outcome = await tm.spawn_exploration(target)

        assert outcome.state == TendrilState.FAILED
        assert "sentinel crash" in outcome.partial_findings

    @pytest.mark.asyncio
    async def test_context_isolation(self):
        """Verify that context variables set in a tendril don't leak to parent."""
        parent_id = ctx_tendril_id.get()
        parent_repo = ctx_tendril_repo.get()

        tm = TendrilManager(hardware=_FakeHardware())
        target = _FakeTarget(name="isolated_cap", repo_owner="reactor")

        # Track what the tendril sees
        tendril_values = {}

        async def capturing_sentinel(t, s):
            tendril_values["id"] = ctx_tendril_id.get()
            tendril_values["repo"] = ctx_tendril_repo.get()
            await asyncio.sleep(0.01)
            return TendrilOutcome(
                capability_name=t.capability.name,
                state=TendrilState.COMPLETED,
                elapsed_seconds=0.01,
            )

        tm._run_sentinel = capturing_sentinel
        await tm.spawn_exploration(target)

        # Tendril should have set its own context
        assert tendril_values["id"] == "tendril:isolated_cap"
        assert tendril_values["repo"] == "reactor"

        # Parent context should be unchanged
        assert ctx_tendril_id.get() == parent_id
        assert ctx_tendril_repo.get() == parent_repo

    @pytest.mark.asyncio
    async def test_bounded_concurrency(self):
        """Verify semaphore limits concurrent tendrils."""
        tm = TendrilManager(hardware=_FakeHardware())
        max_observed = 0
        lock = asyncio.Lock()

        async def counting_sentinel(t, s):
            nonlocal max_observed
            async with lock:
                current = tm._active_count
                if current > max_observed:
                    max_observed = current
            await asyncio.sleep(0.05)
            return TendrilOutcome(
                capability_name=t.capability.name,
                state=TendrilState.COMPLETED,
                elapsed_seconds=0.05,
            )

        tm._run_sentinel = counting_sentinel

        # Spawn 4 tendrils with max_concurrent=2
        targets = [_FakeTarget(name=f"cap_{i}") for i in range(4)]
        tasks = [
            asyncio.create_task(tm.spawn_exploration(t))
            for t in targets
        ]
        await asyncio.gather(*tasks)

        # Max concurrent should not exceed semaphore limit
        assert max_observed <= tm.MAX_CONCURRENT

    @pytest.mark.asyncio
    async def test_spawn_batch_empty(self):
        tm = TendrilManager(hardware=_FakeHardware())
        outcomes = await tm.spawn_batch([])
        assert outcomes == []

    @pytest.mark.asyncio
    async def test_spawn_batch_multiple(self):
        tm = TendrilManager(hardware=_FakeHardware())

        async def quick_sentinel(t, s):
            await asyncio.sleep(0.01)
            return TendrilOutcome(
                capability_name=t.capability.name,
                state=TendrilState.COMPLETED,
                elapsed_seconds=0.01,
            )

        tm._run_sentinel = quick_sentinel

        targets = [_FakeTarget(name=f"batch_{i}") for i in range(3)]
        outcomes = await tm.spawn_batch(targets)

        assert len(outcomes) == 3
        assert all(o.state == TendrilState.COMPLETED for o in outcomes)

    @pytest.mark.asyncio
    async def test_telemetry_emission_without_bus(self):
        """Should not raise when bus is None."""
        tm = TendrilManager(hardware=_FakeHardware())
        target = _FakeTarget()
        outcome = TendrilOutcome(
            capability_name="test",
            state=TendrilState.COMPLETED,
            elapsed_seconds=0.5,
        )
        # Should not raise
        tm._emit_telemetry(target, outcome)

    @pytest.mark.asyncio
    async def test_multiple_tendrils_independent_state(self):
        """Verify that two concurrent tendrils don't share mutable state."""
        tm = TendrilManager(hardware=_FakeHardware())
        results = {}

        async def stateful_sentinel(t, s):
            # Each tendril sets its own context var
            ctx_tendril_id.set(f"unique:{t.capability.name}")
            await asyncio.sleep(0.02)
            # Read it back — should still be our value
            results[t.capability.name] = ctx_tendril_id.get()
            return TendrilOutcome(
                capability_name=t.capability.name,
                state=TendrilState.COMPLETED,
                elapsed_seconds=0.02,
            )

        tm._run_sentinel = stateful_sentinel

        t1 = _FakeTarget(name="tendril_a")
        t2 = _FakeTarget(name="tendril_b")
        await asyncio.gather(
            tm.spawn_exploration(t1),
            tm.spawn_exploration(t2),
        )

        # Each tendril should have read back its OWN value
        assert results["tendril_a"] == "unique:tendril_a"
        assert results["tendril_b"] == "unique:tendril_b"
