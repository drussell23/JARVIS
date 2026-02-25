# tests/unit/core/test_gcp_lifecycle_bridge.py
"""Tests for the GCP lifecycle bridge module.

Covers:
- Singleton lifecycle (get/reset)
- V2 disabled no-ops
- V2 enabled transitions
- State query methods
- Shutdown behavior
- Error resilience
"""
from unittest.mock import AsyncMock, patch

import pytest

from backend.core.gcp_lifecycle_schema import State


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_bridge():
    """Reset the bridge singleton before and after each test."""
    from backend.core import gcp_lifecycle_bridge as mod
    mod.reset_lifecycle_bridge()
    yield
    mod.reset_lifecycle_bridge()


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temp directory for journal database."""
    return tmp_path / "orchestration.db"


# ── Singleton Lifecycle ──────────────────────────────────────────────


class TestSingletonLifecycle:

    @pytest.mark.asyncio
    async def test_get_returns_same_instance(self, tmp_db):
        """get_lifecycle_bridge() returns the same bridge on repeated calls."""
        from backend.core.gcp_lifecycle_bridge import (
            get_lifecycle_bridge, reset_lifecycle_bridge,
        )
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", False):
            b1 = await get_lifecycle_bridge()
            b2 = await get_lifecycle_bridge()
            assert b1 is b2

    @pytest.mark.asyncio
    async def test_reset_clears_singleton(self):
        """reset_lifecycle_bridge() clears the cached instance."""
        from backend.core.gcp_lifecycle_bridge import (
            get_lifecycle_bridge, get_lifecycle_bridge_sync,
            reset_lifecycle_bridge,
        )
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", False):
            await get_lifecycle_bridge()
            assert get_lifecycle_bridge_sync() is not None
            reset_lifecycle_bridge()
            assert get_lifecycle_bridge_sync() is None

    @pytest.mark.asyncio
    async def test_sync_accessor_returns_none_before_init(self):
        """get_lifecycle_bridge_sync() returns None before any init."""
        from backend.core.gcp_lifecycle_bridge import get_lifecycle_bridge_sync
        assert get_lifecycle_bridge_sync() is None


# ── V2 Disabled (No-Op Mode) ────────────────────────────────────────


class TestV2Disabled:

    @pytest.mark.asyncio
    async def test_no_op_state(self):
        """When V2 is disabled, get_current_state() returns 'disabled'."""
        from backend.core.gcp_lifecycle_bridge import get_lifecycle_bridge
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", False):
            bridge = await get_lifecycle_bridge()
            assert bridge.get_current_state() == "disabled"

    @pytest.mark.asyncio
    async def test_no_op_notify(self):
        """When V2 is disabled, notify methods return success=False with reason."""
        from backend.core.gcp_lifecycle_bridge import get_lifecycle_bridge
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", False):
            bridge = await get_lifecycle_bridge()
            result = await bridge.notify_pressure_triggered(reason="test")
            assert result.success is False
            assert result.reason == "v2_disabled"

    @pytest.mark.asyncio
    async def test_is_active_false_when_disabled(self):
        """is_active() returns False when V2 is disabled."""
        from backend.core.gcp_lifecycle_bridge import get_lifecycle_bridge
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", False):
            bridge = await get_lifecycle_bridge()
            assert bridge.is_active() is False

    @pytest.mark.asyncio
    async def test_is_provisioning_false_when_disabled(self):
        """is_provisioning() returns False when V2 is disabled."""
        from backend.core.gcp_lifecycle_bridge import get_lifecycle_bridge
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", False):
            bridge = await get_lifecycle_bridge()
            assert bridge.is_provisioning() is False

    @pytest.mark.asyncio
    async def test_shutdown_no_op_when_disabled(self):
        """shutdown() is safe to call when V2 is disabled (no-op)."""
        from backend.core.gcp_lifecycle_bridge import get_lifecycle_bridge
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", False):
            bridge = await get_lifecycle_bridge()
            await bridge.shutdown()  # should not raise


# ── V2 Enabled (Full Engine) ────────────────────────────────────────


class TestV2Enabled:

    @pytest.mark.asyncio
    async def test_initialize_creates_engine(self, tmp_db):
        """When V2 is enabled, initialize() sets up journal + engine."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)
            assert bridge._initialized is True
            assert bridge._engine is not None
            assert bridge.get_current_state() == "idle"
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_pressure_triggered_transitions(self, tmp_db):
        """PRESSURE_TRIGGERED moves IDLE -> TRIGGERING."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)
            result = await bridge.notify_pressure_triggered(reason="memory")
            assert result.success is True
            assert result.to_state == State.TRIGGERING
            assert bridge.get_current_state() == "triggering"
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_full_provisioning_flow(self, tmp_db):
        """Walk through IDLE -> TRIGGERING -> PROVISIONING -> BOOTING -> ACTIVE."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)

            # IDLE -> TRIGGERING
            r1 = await bridge.notify_pressure_triggered(reason="memory")
            assert r1.success and r1.to_state == State.TRIGGERING

            # TRIGGERING -> PROVISIONING
            r2 = await bridge.notify_budget_approved()
            assert r2.success and r2.to_state == State.PROVISIONING

            # PROVISIONING -> BOOTING
            r3 = await bridge.notify_vm_create_accepted()
            assert r3.success and r3.to_state == State.BOOTING

            # BOOTING -> ACTIVE
            r4 = await bridge.notify_vm_ready(ip="10.0.0.1")
            assert r4.success and r4.to_state == State.ACTIVE

            assert bridge.is_active() is True
            assert bridge.is_provisioning() is False
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_is_provisioning_during_boot(self, tmp_db):
        """is_provisioning() returns True during TRIGGERING/PROVISIONING/BOOTING."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)

            await bridge.notify_pressure_triggered(reason="test")
            assert bridge.is_provisioning() is True  # TRIGGERING

            await bridge.notify_budget_approved()
            assert bridge.is_provisioning() is True  # PROVISIONING

            await bridge.notify_vm_create_accepted()
            assert bridge.is_provisioning() is True  # BOOTING
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_invalid_event_returns_failure(self, tmp_db):
        """An event with no valid transition returns success=False."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)
            # VM_STOPPED is not valid from IDLE
            result = await bridge.notify_vm_stopped()
            assert result.success is False
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, tmp_db):
        """Calling initialize() twice does not re-create the engine."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)
            engine_1 = bridge._engine
            await bridge.initialize(db_path=tmp_db)
            assert bridge._engine is engine_1
            await bridge.shutdown()


# ── Shutdown ─────────────────────────────────────────────────────────


class TestShutdown:

    @pytest.mark.asyncio
    async def test_shutdown_emits_session_shutdown(self, tmp_db):
        """shutdown() sends SESSION_SHUTDOWN event to engine."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)

            # Move to ACTIVE first (SESSION_SHUTDOWN is wildcard)
            await bridge.notify_pressure_triggered(reason="test")
            await bridge.notify_budget_approved()
            await bridge.notify_vm_create_accepted()
            await bridge.notify_vm_ready(ip="10.0.0.1")

            await bridge.shutdown()
            assert bridge._initialized is False

    @pytest.mark.asyncio
    async def test_shutdown_tolerates_engine_error(self, tmp_db):
        """shutdown() does not raise even if engine.handle_event fails."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)
            # Sabotage the engine
            bridge._engine.handle_event = AsyncMock(
                side_effect=RuntimeError("boom"),
            )
            await bridge.shutdown()  # should not raise
            assert bridge._initialized is False


# ── Error Resilience ─────────────────────────────────────────────────


class TestErrorResilience:

    @pytest.mark.asyncio
    async def test_emit_catches_engine_exception(self, tmp_db):
        """If the engine raises, _emit returns success=False with reason."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)
            bridge._engine.handle_event = AsyncMock(
                side_effect=RuntimeError("test error"),
            )
            result = await bridge.notify_pressure_triggered(reason="test")
            assert result.success is False
            assert "test error" in result.reason
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_uninitialized_bridge_returns_not_initialized(self):
        """Calling notify on an uninitialized bridge returns appropriate reason."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            # Do NOT call initialize
            result = await bridge.notify_pressure_triggered(reason="test")
            assert result.success is False
            assert result.reason == "not_initialized"

    @pytest.mark.asyncio
    async def test_get_current_state_uninitialized(self):
        """get_current_state() returns 'uninitialized' when V2 on but not init'd."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            assert bridge.get_current_state() == "uninitialized"


# ── Cooldown and Degraded Paths ──────────────────────────────────────


class TestAdditionalPaths:

    @pytest.mark.asyncio
    async def test_cooldown_path(self, tmp_db):
        """ACTIVE -> COOLING_DOWN -> STOPPING -> IDLE."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)

            # Get to ACTIVE
            await bridge.notify_pressure_triggered(reason="test")
            await bridge.notify_budget_approved()
            await bridge.notify_vm_create_accepted()
            await bridge.notify_vm_ready(ip="10.0.0.1")
            assert bridge.get_current_state() == "active"

            # ACTIVE -> COOLING_DOWN
            r1 = await bridge.notify_pressure_cooled()
            assert r1.success and r1.to_state == State.COOLING_DOWN

            # COOLING_DOWN -> STOPPING
            r2 = await bridge.notify_cooldown_expired()
            assert r2.success and r2.to_state == State.STOPPING

            # STOPPING -> IDLE
            r3 = await bridge.notify_vm_stopped()
            assert r3.success and r3.to_state == State.IDLE

            assert bridge.get_current_state() == "idle"
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_degraded_recovery(self, tmp_db):
        """ACTIVE -> DEGRADED -> ACTIVE on health recovery."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)

            # Get to ACTIVE
            await bridge.notify_pressure_triggered(reason="test")
            await bridge.notify_budget_approved()
            await bridge.notify_vm_create_accepted()
            await bridge.notify_vm_ready(ip="10.0.0.1")

            # ACTIVE -> DEGRADED
            r1 = await bridge.notify_vm_degraded()
            assert r1.success and r1.to_state == State.DEGRADED
            assert bridge.is_active() is True  # DEGRADED is still "active"

            # DEGRADED -> ACTIVE
            r2 = await bridge.notify_vm_ready(ip="10.0.0.1")
            assert r2.success and r2.to_state == State.ACTIVE
            await bridge.shutdown()

    @pytest.mark.asyncio
    async def test_spot_preemption_recovery(self, tmp_db):
        """ACTIVE -> TRIGGERING on spot preemption."""
        from backend.core.gcp_lifecycle_bridge import GCPLifecycleBridge
        bridge = GCPLifecycleBridge()
        with patch("backend.core.gcp_lifecycle_bridge._V2_ENABLED", True):
            await bridge.initialize(db_path=tmp_db)

            # Get to ACTIVE
            await bridge.notify_pressure_triggered(reason="test")
            await bridge.notify_budget_approved()
            await bridge.notify_vm_create_accepted()
            await bridge.notify_vm_ready(ip="10.0.0.1")

            # Preemption
            r = await bridge.notify_spot_preempted()
            assert r.success and r.to_state == State.TRIGGERING
            assert bridge.is_provisioning() is True
            await bridge.shutdown()
