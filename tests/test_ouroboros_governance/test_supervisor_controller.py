"""Tests for the Supervisor Ouroboros Controller — Lifecycle Authority."""

import pytest

from backend.core.ouroboros.governance.supervisor_controller import (
    AutonomyMode,
    SupervisorOuroborosController,
)


@pytest.fixture
def controller():
    """Return a fresh SupervisorOuroborosController instance."""
    return SupervisorOuroborosController()


# ---------------------------------------------------------------------------
# TestLifecycleAuthority
# ---------------------------------------------------------------------------


class TestLifecycleAuthority:
    """Tests for core lifecycle state transitions."""

    def test_starts_in_disabled_mode(self, controller):
        """A freshly created controller must be in DISABLED mode."""
        assert controller.mode is AutonomyMode.DISABLED

    @pytest.mark.asyncio
    async def test_start_enters_sandbox_mode(self, controller):
        """start() transitions from DISABLED to SANDBOX."""
        await controller.start()
        assert controller.mode is AutonomyMode.SANDBOX

    @pytest.mark.asyncio
    async def test_stop_returns_to_disabled(self, controller):
        """stop() transitions back to DISABLED and resets gates_passed."""
        await controller.start()
        await controller.mark_gates_passed()
        await controller.stop()
        assert controller.mode is AutonomyMode.DISABLED
        # gates_passed must be reset after stop
        with pytest.raises(RuntimeError, match="gates"):
            await controller.enable_governed_autonomy()

    @pytest.mark.asyncio
    async def test_pause_enters_read_only(self, controller):
        """pause() transitions to READ_ONLY."""
        await controller.start()
        await controller.pause()
        assert controller.mode is AutonomyMode.READ_ONLY

    @pytest.mark.asyncio
    async def test_resume_from_pause(self, controller):
        """start -> pause -> resume transitions back to SANDBOX."""
        await controller.start()
        await controller.pause()
        await controller.resume()
        assert controller.mode is AutonomyMode.SANDBOX

    @pytest.mark.asyncio
    async def test_enable_governed_autonomy(self, controller):
        """enable_governed_autonomy raises RuntimeError if gates not passed."""
        await controller.start()
        with pytest.raises(RuntimeError, match="gates"):
            await controller.enable_governed_autonomy()

    @pytest.mark.asyncio
    async def test_enable_governed_autonomy_with_gates(self, controller):
        """enable_governed_autonomy succeeds after mark_gates_passed."""
        await controller.start()
        await controller.mark_gates_passed()
        await controller.enable_governed_autonomy()
        assert controller.mode is AutonomyMode.GOVERNED

    @pytest.mark.asyncio
    async def test_emergency_stop(self, controller):
        """emergency_stop sets mode and blocks resume."""
        await controller.start()
        await controller.emergency_stop("critical failure")
        assert controller.mode is AutonomyMode.EMERGENCY_STOP
        with pytest.raises(RuntimeError, match="emergency"):
            await controller.resume()


# ---------------------------------------------------------------------------
# TestWritePermissions
# ---------------------------------------------------------------------------


class TestWritePermissions:
    """Tests for writes_allowed, sandbox_allowed, interactive_allowed."""

    @pytest.mark.asyncio
    async def test_writes_only_in_governed(self, controller):
        """writes_allowed is True only in GOVERNED mode."""
        assert controller.writes_allowed is False  # DISABLED

        await controller.start()
        assert controller.writes_allowed is False  # SANDBOX

        await controller.mark_gates_passed()
        await controller.enable_governed_autonomy()
        assert controller.writes_allowed is True  # GOVERNED

    @pytest.mark.asyncio
    async def test_sandbox_allowed_in_sandbox_and_governed(self, controller):
        """sandbox_allowed is True in SANDBOX and GOVERNED modes."""
        assert controller.sandbox_allowed is False  # DISABLED

        await controller.start()
        assert controller.sandbox_allowed is True  # SANDBOX

        await controller.mark_gates_passed()
        await controller.enable_governed_autonomy()
        assert controller.sandbox_allowed is True  # GOVERNED

    @pytest.mark.asyncio
    async def test_interactive_allowed_unless_disabled(self, controller):
        """interactive_allowed is True in all modes except DISABLED."""
        assert controller.interactive_allowed is False  # DISABLED

        await controller.start()
        assert controller.interactive_allowed is True  # SANDBOX

        await controller.pause()
        assert controller.interactive_allowed is True  # READ_ONLY


# ---------------------------------------------------------------------------
# TestSafeModeBoot
# ---------------------------------------------------------------------------


class TestSafeModeBoot:
    """Tests for safe-mode boot behavior."""

    @pytest.mark.asyncio
    async def test_safe_mode_blocks_writes(self):
        """When _safe_mode=True, start enters SAFE_MODE; writes_allowed is False."""
        ctrl = SupervisorOuroborosController()
        ctrl._safe_mode = True
        await ctrl.start()
        assert ctrl.mode is AutonomyMode.SAFE_MODE
        assert ctrl.writes_allowed is False

    @pytest.mark.asyncio
    async def test_safe_mode_allows_interactive(self):
        """When _safe_mode=True, start enters SAFE_MODE; interactive_allowed is True."""
        ctrl = SupervisorOuroborosController()
        ctrl._safe_mode = True
        await ctrl.start()
        assert ctrl.interactive_allowed is True
