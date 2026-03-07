"""Tests for ResourceManagerBase verdict bridge (Tasks 5 & 6).

Verifies that:
- _build_verdict() produces valid ResourceVerdict instances
- Verdict sequence increments monotonically
- safe_initialize() returns ResourceVerdict when types are available
- Failed init returns DEGRADED/CRASHED verdict
- Exception during init returns CRASHED verdict
- Custom get_init_verdict() is honoured
- _safe_initialize_bool() fallback still works
"""
import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.root_authority_types import (
    RecoveryAction,
    RequiredTier,
    ResourceVerdict,
    SubsystemState,
    VerdictReasonCode,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight concrete subclass of ResourceManagerBase
# ---------------------------------------------------------------------------

def _make_manager(cls_name, init_return=True, init_side_effect=None):
    """Dynamically create a concrete ResourceManagerBase subclass and instance."""
    from unified_supervisor import ResourceManagerBase

    body = {
        "initialize": AsyncMock(return_value=init_return, side_effect=init_side_effect),
        "health_check": AsyncMock(return_value=(True, "ok")),
        "cleanup": AsyncMock(),
    }
    Cls = type(cls_name, (ResourceManagerBase,), body)
    mgr = Cls(cls_name.lower())
    # Seed verdict bridge fields
    mgr._boot_epoch = 1
    mgr._correlation_id = "test-corr"
    # Replace circuit breaker with a pass-through mock
    mgr._circuit_breaker = MagicMock()
    if init_side_effect:
        mgr._circuit_breaker.execute = AsyncMock(side_effect=init_side_effect)
    else:
        mgr._circuit_breaker.execute = AsyncMock(return_value=init_return)
    return mgr


# ===================================================================
# Task 5: _build_verdict()
# ===================================================================

class TestBuildVerdict:
    """Test the _build_verdict() helper on ResourceManagerBase."""

    def test_healthy_verdict(self):
        mgr = _make_manager("HealthyMgr")
        v = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        assert isinstance(v, ResourceVerdict)
        assert v.state is SubsystemState.READY
        assert v.origin == "healthymgr"
        assert v.epoch == 1
        assert v.correlation_id == "test-corr"
        assert v.required_tier is RequiredTier.REQUIRED
        assert v.boot_allowed is True
        assert v.serviceable is True
        assert v.severity == 0

    def test_sequence_increments(self):
        mgr = _make_manager("SeqMgr")
        v1 = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        v2 = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        assert v2.sequence == v1.sequence + 1

    def test_monotonic_ns_populated(self):
        mgr = _make_manager("MonoMgr")
        before = time.monotonic_ns()
        v = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        after = time.monotonic_ns()
        assert before <= v.monotonic_ns <= after

    def test_wall_utc_is_iso(self):
        mgr = _make_manager("WallMgr")
        v = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        # Should parse without error
        dt = datetime.fromisoformat(v.wall_utc)
        assert dt.tzinfo is not None

    def test_evidence_defaults_to_empty(self):
        mgr = _make_manager("EvidMgr")
        v = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        assert v.evidence == {}

    def test_evidence_preserved(self):
        mgr = _make_manager("EvidMgr2")
        v = mgr._build_verdict(
            state=SubsystemState.DEGRADED,
            reason_code=VerdictReasonCode.INIT_RETURNED_FALSE,
            reason_detail="nope",
            boot_allowed=True,
            serviceable=False,
            evidence={"init_time_ms": 42},
        )
        assert v.evidence == {"init_time_ms": 42}

    def test_next_action_defaults_to_none(self):
        mgr = _make_manager("ActMgr")
        v = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        assert v.next_action is RecoveryAction.NONE

    def test_capabilities_from_manager(self):
        mgr = _make_manager("CapMgr")
        mgr._capabilities = ("voice", "tts")
        v = mgr._build_verdict(
            state=SubsystemState.READY,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            boot_allowed=True,
            serviceable=True,
        )
        assert v.capabilities == ("voice", "tts")

    def test_required_tier_reflects_manager(self):
        mgr = _make_manager("OptMgr")
        mgr._required_tier = RequiredTier.OPTIONAL
        v = mgr._build_verdict(
            state=SubsystemState.DEGRADED,
            reason_code=VerdictReasonCode.DISABLED_BY_CONFIG,
            reason_detail="disabled",
            boot_allowed=True,
            serviceable=False,
        )
        assert v.required_tier is RequiredTier.OPTIONAL


# ===================================================================
# Task 6: safe_initialize() returning ResourceVerdict
# ===================================================================

class TestSafeInitializeVerdict:
    """Test safe_initialize() returns ResourceVerdict."""

    @pytest.mark.asyncio
    async def test_successful_init_returns_ready_verdict(self):
        mgr = _make_manager("SuccessMgr")
        verdict = await mgr.safe_initialize()
        assert isinstance(verdict, ResourceVerdict)
        assert verdict.state is SubsystemState.READY
        assert verdict.reason_code is VerdictReasonCode.HEALTHY
        assert verdict.boot_allowed is True
        assert verdict.serviceable is True
        assert verdict.severity == 0
        # Backward compat fields still set
        assert mgr._ready is True
        assert mgr._health_status == "healthy"

    @pytest.mark.asyncio
    async def test_failed_init_returns_verdict(self):
        mgr = _make_manager("FailMgr", init_return=False)
        verdict = await mgr.safe_initialize()
        assert isinstance(verdict, ResourceVerdict)
        assert verdict.state in (SubsystemState.DEGRADED, SubsystemState.CRASHED)
        assert verdict.reason_code is VerdictReasonCode.INIT_RETURNED_FALSE
        assert verdict.serviceable is False
        assert verdict.retryable is True
        assert verdict.next_action is RecoveryAction.RETRY
        # Backward compat
        assert mgr._ready is not True  # Either False or not set
        assert mgr._health_status == "unhealthy"

    @pytest.mark.asyncio
    async def test_exception_returns_crashed_verdict(self):
        mgr = _make_manager("ExplodeMgr", init_side_effect=RuntimeError("boom"))
        verdict = await mgr.safe_initialize()
        assert isinstance(verdict, ResourceVerdict)
        assert verdict.state is SubsystemState.CRASHED
        assert verdict.reason_code is VerdictReasonCode.INIT_EXCEPTION
        assert "boom" in verdict.reason_detail
        assert verdict.serviceable is False
        assert verdict.retryable is True
        assert verdict.retry_after_s == 30.0
        assert verdict.next_action is RecoveryAction.RETRY
        assert verdict.evidence.get("exception") == "RuntimeError"
        # Backward compat
        assert mgr._health_status == "error"

    @pytest.mark.asyncio
    async def test_custom_get_init_verdict(self):
        """Subclass providing get_init_verdict() overrides default inference."""
        from unified_supervisor import ResourceManagerBase

        class CustomManager(ResourceManagerBase):
            async def initialize(self):
                return True

            async def health_check(self):
                return (True, "ok")

            async def cleanup(self):
                pass

            def get_init_verdict(self, bool_result):
                return self._build_verdict(
                    state=SubsystemState.DEGRADED,
                    reason_code=VerdictReasonCode.DISABLED_BY_CONFIG,
                    reason_detail="Disabled but non-fatal",
                    boot_allowed=True,
                    serviceable=False,
                )

        mgr = CustomManager("custom_mgr")
        mgr._boot_epoch = 1
        mgr._correlation_id = "test-corr"
        mgr._circuit_breaker = MagicMock()
        mgr._circuit_breaker.execute = AsyncMock(return_value=True)

        verdict = await mgr.safe_initialize()
        assert verdict.state is SubsystemState.DEGRADED
        assert verdict.reason_code is VerdictReasonCode.DISABLED_BY_CONFIG
        assert verdict.boot_allowed is True
        assert verdict.serviceable is False
        # Backward compat should reflect the verdict
        assert mgr._ready is False  # serviceable=False -> _ready=False
        assert mgr._health_status == SubsystemState.DEGRADED.value

    @pytest.mark.asyncio
    async def test_init_time_in_evidence(self):
        mgr = _make_manager("TimeMgr")
        verdict = await mgr.safe_initialize()
        assert "init_time_ms" in verdict.evidence
        assert isinstance(verdict.evidence["init_time_ms"], int)
        assert verdict.evidence["init_time_ms"] >= 0


class TestSafeInitializeBoolFallback:
    """Test _safe_initialize_bool() still works for backward compat."""

    @pytest.mark.asyncio
    async def test_bool_fallback_success(self):
        mgr = _make_manager("BoolSuccMgr")
        result = await mgr._safe_initialize_bool()
        assert result is True
        assert mgr._ready is True
        assert mgr._health_status == "healthy"

    @pytest.mark.asyncio
    async def test_bool_fallback_failure(self):
        mgr = _make_manager("BoolFailMgr", init_return=False)
        result = await mgr._safe_initialize_bool()
        assert result is False
        assert mgr._health_status == "unhealthy"

    @pytest.mark.asyncio
    async def test_bool_fallback_exception(self):
        mgr = _make_manager("BoolExcMgr", init_side_effect=RuntimeError("crash"))
        result = await mgr._safe_initialize_bool()
        assert result is False
        assert mgr._health_status == "error"
        assert mgr._error == "crash"


class TestVerdictBridgeFields:
    """Test that __init__ populates the verdict bridge fields."""

    def test_default_fields_present(self):
        mgr = _make_manager("FieldMgr")
        assert mgr._required_tier is RequiredTier.REQUIRED
        assert mgr._capabilities == ()
        assert mgr._verdict_sequence == 0
        assert mgr._boot_epoch == 1  # Set by _make_manager
        assert mgr._correlation_id == "test-corr"

    def test_required_tier_defaults_to_required(self):
        """Fresh manager defaults to REQUIRED tier."""
        from unified_supervisor import ResourceManagerBase

        class FreshManager(ResourceManagerBase):
            async def initialize(self):
                return True
            async def health_check(self):
                return (True, "ok")
            async def cleanup(self):
                pass

        mgr = FreshManager("fresh")
        assert mgr._required_tier is RequiredTier.REQUIRED


# ===================================================================
# Task 7: Manager RequiredTier declarations
# ===================================================================

class TestManagerTierDeclarations:
    """Verify each manager's __init__ source declares the correct RequiredTier."""

    def test_docker_daemon_enhancement(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.DockerDaemonManager.__init__)
        assert "RequiredTier.ENHANCEMENT" in src

    def test_gcp_instance_enhancement(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.GCPInstanceManager.__init__)
        assert "RequiredTier.ENHANCEMENT" in src

    def test_cost_tracker_optional(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.CostTracker.__init__)
        assert "RequiredTier.OPTIONAL" in src

    def test_dynamic_port_required(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.DynamicPortManager.__init__)
        # DynamicPortManager keeps default REQUIRED, so it only sets capabilities
        assert "port_allocation" in src

    def test_voice_cache_optional(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.SemanticVoiceCacheManager.__init__)
        assert "RequiredTier.OPTIONAL" in src

    def test_tiered_storage_optional(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.TieredStorageManager.__init__)
        assert "RequiredTier.OPTIONAL" in src

    def test_spot_resilience_optional(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.SpotInstanceResilienceHandler.__init__)
        assert "RequiredTier.OPTIONAL" in src

    def test_cache_manager_enhancement(self):
        import inspect, unified_supervisor as us
        src = inspect.getsource(us.IntelligentCacheManager.__init__)
        assert "RequiredTier.ENHANCEMENT" in src


# ===================================================================
# Task 8: VerdictAuthority wiring
# ===================================================================

class TestVerdictAuthorityWiring:
    """Test that VerdictAuthority can be imported and used."""

    def test_verdict_authority_importable(self):
        from backend.core.verdict_authority import VerdictAuthority
        va = VerdictAuthority()
        assert va.current_epoch == 0

    def test_begin_epoch(self):
        from backend.core.verdict_authority import VerdictAuthority
        va = VerdictAuthority()
        epoch = va.begin_epoch()
        assert epoch == 1
        assert va.current_epoch == 1

    def test_submit_and_read(self):
        """Submit a verdict and verify it can be read back."""
        from backend.core.verdict_authority import VerdictAuthority

        async def _run():
            va = VerdictAuthority()
            va.begin_epoch()
            mgr = _make_manager("TestSubmit")
            verdict = mgr._build_verdict(
                state=SubsystemState.READY,
                reason_code=VerdictReasonCode.HEALTHY,
                reason_detail="ok",
                boot_allowed=True,
                serviceable=True,
            )
            accepted = await va.submit_verdict("test", verdict)
            assert accepted is True
            stored = va.get_component_status("test")
            assert stored is verdict

        asyncio.run(_run())


class TestResourceRegistryLastVerdicts:
    """Test that ResourceManagerRegistry stores and exposes verdicts."""

    def test_last_verdicts_initially_empty(self):
        from unified_supervisor import ResourceManagerRegistry
        registry = ResourceManagerRegistry()
        assert registry.get_last_verdicts() == {}

    def test_get_last_verdicts_returns_copy(self):
        from unified_supervisor import ResourceManagerRegistry
        registry = ResourceManagerRegistry()
        v1 = registry.get_last_verdicts()
        v1["injected"] = "bad"
        assert registry.get_last_verdicts() == {}

    def test_parallel_init_stores_verdicts(self):
        """Parallel initialization stores ResourceVerdict objects."""
        from unified_supervisor import ResourceManagerRegistry

        async def _run():
            registry = ResourceManagerRegistry()
            mgr = _make_manager("ParVerdictMgr")
            registry.register(mgr)
            results = await registry.initialize_all(parallel=True)
            assert "parverdictmgr" in results
            verdicts = registry.get_last_verdicts()
            assert len(verdicts) >= 1
            v = verdicts["parverdictmgr"]
            assert hasattr(v, 'serviceable')
            assert isinstance(v, ResourceVerdict)

        asyncio.run(_run())

    def test_sequential_init_stores_verdicts(self):
        """Sequential initialization stores ResourceVerdict objects."""
        from unified_supervisor import ResourceManagerRegistry

        async def _run():
            registry = ResourceManagerRegistry()
            mgr = _make_manager("SeqVerdictMgr")
            registry.register(mgr)
            results = await registry.initialize_all(parallel=False)
            assert "seqverdictmgr" in results
            verdicts = registry.get_last_verdicts()
            assert len(verdicts) >= 1
            v = verdicts["seqverdictmgr"]
            assert isinstance(v, ResourceVerdict)
            assert v.state is SubsystemState.READY

        asyncio.run(_run())

    def test_failed_init_stores_verdict(self):
        """Failed initialization stores verdict with serviceable=False."""
        from unified_supervisor import ResourceManagerRegistry

        async def _run():
            registry = ResourceManagerRegistry()
            mgr = _make_manager("FailVerdictMgr", init_return=False)
            registry.register(mgr)
            await registry.initialize_all(parallel=False)
            verdicts = registry.get_last_verdicts()
            assert len(verdicts) >= 1
            v = verdicts["failverdictmgr"]
            assert isinstance(v, ResourceVerdict)
            assert v.serviceable is False

        asyncio.run(_run())


# ===================================================================
# Task 9: No hardcoded "resources": {"status": "complete"}
# ===================================================================

class TestNoHardcodedResourceComplete:
    """Verify the codebase has no hardcoded resources-complete overwrites."""

    def test_no_hardcoded_resources_complete(self):
        import re
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        # Find all instances of "resources": {"status": "complete"}
        matches = re.findall(r'"resources":\s*\{"status":\s*"complete"\}', content)
        assert len(matches) == 0, (
            f"Found {len(matches)} hardcoded 'resources: complete' literals. "
            "These must read from VerdictAuthority instead."
        )
