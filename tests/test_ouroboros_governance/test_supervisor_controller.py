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


# ---------------------------------------------------------------------------
# TestHibernationMode — enum + property guards (step 3 of HIBERNATION_MODE)
# ---------------------------------------------------------------------------


class TestHibernationMode:
    """Verify the HIBERNATION enum value and property semantics.

    Transition methods (enter_hibernation/wake_from_hibernation) are added
    in a later step. These tests validate the enum + guards only: that
    setting _mode directly to HIBERNATION yields the correct capability
    flags and the existing transitions refuse to mis-route out.
    """

    def test_hibernation_enum_exists(self):
        """AutonomyMode.HIBERNATION must exist with the expected value."""
        assert AutonomyMode.HIBERNATION.value == "HIBERNATION"

    def test_writes_blocked_in_hibernation(self, controller):
        """writes_allowed must be False in HIBERNATION."""
        controller._mode = AutonomyMode.HIBERNATION
        assert controller.writes_allowed is False

    def test_sandbox_blocked_in_hibernation(self, controller):
        """sandbox_allowed must be False in HIBERNATION — no new ops."""
        controller._mode = AutonomyMode.HIBERNATION
        assert controller.sandbox_allowed is False

    def test_interactive_allowed_in_hibernation(self, controller):
        """interactive_allowed must be True — operator can still inspect state."""
        controller._mode = AutonomyMode.HIBERNATION
        assert controller.interactive_allowed is True

    def test_is_hibernating_property(self, controller):
        """is_hibernating reflects the HIBERNATION mode."""
        assert controller.is_hibernating is False
        controller._mode = AutonomyMode.HIBERNATION
        assert controller.is_hibernating is True
        controller._mode = AutonomyMode.GOVERNED
        assert controller.is_hibernating is False

    @pytest.mark.asyncio
    async def test_pause_blocked_in_hibernation(self, controller):
        """pause() must refuse while HIBERNATING."""
        controller._mode = AutonomyMode.HIBERNATION
        with pytest.raises(RuntimeError, match="HIBERNAT"):
            await controller.pause()
        assert controller.mode is AutonomyMode.HIBERNATION  # unchanged

    @pytest.mark.asyncio
    async def test_resume_blocked_in_hibernation(self, controller):
        """resume() must refuse while HIBERNATING — wake_from_hibernation only."""
        controller._mode = AutonomyMode.HIBERNATION
        with pytest.raises(RuntimeError, match="HIBERNAT"):
            await controller.resume()
        assert controller.mode is AutonomyMode.HIBERNATION  # unchanged

    @pytest.mark.asyncio
    async def test_enable_governed_autonomy_blocked_in_hibernation(self, controller):
        """enable_governed_autonomy() must refuse while HIBERNATING."""
        controller._mode = AutonomyMode.HIBERNATION
        controller._gates_passed = True  # ensure the hibernation check fires first
        with pytest.raises(RuntimeError, match="HIBERNAT"):
            await controller.enable_governed_autonomy()
        assert controller.mode is AutonomyMode.HIBERNATION  # unchanged


# ---------------------------------------------------------------------------
# TestHibernationTransitions — enter/wake methods (step 4)
# ---------------------------------------------------------------------------


class TestHibernationTransitions:
    """Verify enter_hibernation/wake_from_hibernation transition correctness."""

    @pytest.mark.asyncio
    async def test_enter_from_governed_restores_on_wake(self, controller):
        """GOVERNED → HIBERNATION → GOVERNED is the canonical cycle."""
        await controller.start()
        await controller.mark_gates_passed()
        await controller.enable_governed_autonomy()
        assert controller.mode is AutonomyMode.GOVERNED

        entered = await controller.enter_hibernation(reason="all_providers_exhausted")
        assert entered is True
        assert controller.mode is AutonomyMode.HIBERNATION
        assert controller._pre_hibernation_mode is AutonomyMode.GOVERNED

        woke = await controller.wake_from_hibernation(reason="providers_recovered")
        assert woke is True
        assert controller.mode is AutonomyMode.GOVERNED
        assert controller._pre_hibernation_mode is None

    @pytest.mark.asyncio
    async def test_enter_from_sandbox_restores_sandbox(self, controller):
        """SANDBOX → HIBERNATION → SANDBOX — capability envelope preserved."""
        await controller.start()
        assert controller.mode is AutonomyMode.SANDBOX

        await controller.enter_hibernation(reason="outage")
        assert controller.mode is AutonomyMode.HIBERNATION

        await controller.wake_from_hibernation()
        assert controller.mode is AutonomyMode.SANDBOX

    @pytest.mark.asyncio
    async def test_enter_from_read_only_restores_read_only(self, controller):
        """READ_ONLY → HIBERNATION → READ_ONLY — pause state preserved."""
        await controller.start()
        await controller.pause()
        assert controller.mode is AutonomyMode.READ_ONLY

        await controller.enter_hibernation(reason="outage")
        await controller.wake_from_hibernation()
        assert controller.mode is AutonomyMode.READ_ONLY

    @pytest.mark.asyncio
    async def test_enter_hibernation_is_idempotent(self, controller):
        """A second enter_hibernation() call is a no-op returning False."""
        await controller.start()
        assert await controller.enter_hibernation(reason="first") is True
        assert await controller.enter_hibernation(reason="second") is False
        assert controller.mode is AutonomyMode.HIBERNATION
        # pre-hibernation mode must not be overwritten by the second call
        assert controller._pre_hibernation_mode is AutonomyMode.SANDBOX

    @pytest.mark.asyncio
    async def test_wake_without_hibernation_is_noop(self, controller):
        """wake_from_hibernation() on a non-hibernating controller returns False."""
        await controller.start()
        assert await controller.wake_from_hibernation() is False
        assert controller.mode is AutonomyMode.SANDBOX

    @pytest.mark.asyncio
    async def test_enter_from_disabled_rejected(self, controller):
        """DISABLED → HIBERNATION is rejected — nothing to hibernate."""
        assert controller.mode is AutonomyMode.DISABLED
        result = await controller.enter_hibernation(reason="pointless")
        assert result is False
        assert controller.mode is AutonomyMode.DISABLED

    @pytest.mark.asyncio
    async def test_enter_from_emergency_stop_raises(self, controller):
        """EMERGENCY_STOP blocks enter_hibernation() — human must clear first."""
        await controller.start()
        await controller.emergency_stop("test")
        with pytest.raises(RuntimeError, match="EMERGENCY"):
            await controller.enter_hibernation(reason="too late")

    @pytest.mark.asyncio
    async def test_hibernation_count_tracks_cycles(self, controller):
        """_hibernation_count increments on each successful entry."""
        await controller.start()
        for i in range(3):
            await controller.enter_hibernation(reason=f"cycle {i}")
            await controller.wake_from_hibernation()
        assert controller._hibernation_count == 3

    @pytest.mark.asyncio
    async def test_stop_during_hibernation_clears_state(self, controller):
        """stop() must clear pre_hibernation_mode and reason."""
        await controller.start()
        await controller.enter_hibernation(reason="test")
        assert controller._pre_hibernation_mode is AutonomyMode.SANDBOX
        await controller.stop()
        assert controller.mode is AutonomyMode.DISABLED
        assert controller._pre_hibernation_mode is None
        assert controller._hibernation_reason is None

    @pytest.mark.asyncio
    async def test_emergency_stop_during_hibernation_clears_state(self, controller):
        """emergency_stop() must clear pre_hibernation state."""
        await controller.start()
        await controller.enter_hibernation(reason="outage")
        await controller.emergency_stop("critical")
        assert controller.mode is AutonomyMode.EMERGENCY_STOP
        assert controller._pre_hibernation_mode is None
        assert controller._hibernation_reason is None

    @pytest.mark.asyncio
    async def test_hibernation_reason_recorded(self, controller):
        """enter_hibernation() records the reason for postmortem."""
        await controller.start()
        await controller.enter_hibernation(reason="dw+claude both down")
        assert controller._hibernation_reason == "dw+claude both down"
        # wake clears it
        await controller.wake_from_hibernation()
        assert controller._hibernation_reason is None
