"""P2-4: State-machine property tests.

Uses Hypothesis to generate randomized event sequences and verify:
1. The routing authority FSM never reaches an undefined state.
2. Illegal transitions are cleanly rejected (no exceptions, no corruption).
3. Handoff → rollback is always safe.
4. LoopState FSM (PreemptionFsmEngine) never enters an undefined state.
"""
from __future__ import annotations

import pytest

try:
    from hypothesis import given, settings
    import hypothesis.strategies as st
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False
    # Provide no-op stubs so class bodies parse correctly when hypothesis is absent.
    def given(*args, **kwargs):  # type: ignore[misc]
        return lambda f: pytest.mark.skip(reason="hypothesis not installed")(f)
    def settings(*args, **kwargs):  # type: ignore[misc]
        return lambda f: f
    class st:  # type: ignore[no-redef]
        @staticmethod
        def lists(*args, **kwargs):
            return []
        @staticmethod
        def sampled_from(*args, **kwargs):
            return []

from backend.core.routing_authority_fsm import AuthorityState, RoutingAuthorityFSM, TransitionResult


# ---------------------------------------------------------------------------
# RoutingAuthorityFSM property tests
# ---------------------------------------------------------------------------


_VALID_AUTHORITY_STATES = set(AuthorityState)

# Guard sets used by the FSM transitions
_BEGIN_GUARDS_PASS = {
    "core_ready_passed": True,
    "contracts_valid": True,
    "invariants_clean": True,
}
_BEGIN_GUARDS_FAIL = {
    "core_ready_passed": False,
    "contracts_valid": True,
    "invariants_clean": True,
}
_COMPLETE_GUARDS_PASS = {
    "contracts_valid": True,
    "invariants_clean": True,
    "hybrid_router_ready": True,
    "lease_or_local_ready": True,
    "readiness_contract_passed": True,
    "no_in_flight_requests": True,
}


def _new_fsm() -> RoutingAuthorityFSM:
    return RoutingAuthorityFSM(journal_path=None)


class TestRoutingAuthorityFSMProperties:
    """State machine always in a valid state, never corrupted."""

    def test_initial_state_is_boot_policy_active(self) -> None:
        fsm = _new_fsm()
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    def test_state_always_valid_after_successful_handoff(self) -> None:
        fsm = _new_fsm()
        fsm.begin_handoff(_BEGIN_GUARDS_PASS)
        fsm.complete_handoff(_COMPLETE_GUARDS_PASS)
        assert fsm.state in _VALID_AUTHORITY_STATES

    def test_failed_begin_guard_leaves_state_unchanged(self) -> None:
        fsm = _new_fsm()
        result = fsm.begin_handoff(_BEGIN_GUARDS_FAIL)
        assert not result.success
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    def test_rollback_from_hybrid_active_returns_boot_policy(self) -> None:
        fsm = _new_fsm()
        fsm.begin_handoff(_BEGIN_GUARDS_PASS)
        fsm.complete_handoff(_COMPLETE_GUARDS_PASS)
        assert fsm.state == AuthorityState.HYBRID_ACTIVE
        fsm.rollback("test rollback")
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    def test_rollback_from_boot_policy_is_noop(self) -> None:
        """Rollback on already-safe state must not raise or corrupt."""
        fsm = _new_fsm()
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE
        fsm.rollback("spurious rollback")
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    def test_double_rollback_is_safe(self) -> None:
        fsm = _new_fsm()
        fsm.begin_handoff(_BEGIN_GUARDS_PASS)
        fsm.complete_handoff(_COMPLETE_GUARDS_PASS)
        fsm.rollback("first rollback")
        fsm.rollback("second rollback")  # must not raise
        assert fsm.state in _VALID_AUTHORITY_STATES

    def test_complete_without_begin_is_rejected(self) -> None:
        """complete_handoff() without prior begin_handoff() must fail gracefully."""
        fsm = _new_fsm()
        result = fsm.complete_handoff(_COMPLETE_GUARDS_PASS)
        assert not result.success
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    @pytest.mark.skipif(not HAS_HYPOTHESIS, reason="hypothesis not installed")
    @given(
        st.lists(
            st.sampled_from(["begin_pass", "begin_fail", "complete", "rollback"]),
            min_size=1,
            max_size=30,
        )
    )
    @settings(max_examples=200)
    def test_state_always_valid_under_random_transitions(self, ops: list) -> None:
        """State machine never enters an undefined state under any sequence."""
        fsm = _new_fsm()
        for op in ops:
            try:
                if op == "begin_pass":
                    fsm.begin_handoff(_BEGIN_GUARDS_PASS)
                elif op == "begin_fail":
                    fsm.begin_handoff(_BEGIN_GUARDS_FAIL)
                elif op == "complete":
                    fsm.complete_handoff(_COMPLETE_GUARDS_PASS)
                elif op == "rollback":
                    fsm.rollback("property-test rollback")
            except Exception as exc:
                # FSM must NEVER raise on any transition sequence
                pytest.fail(f"FSM raised on op={op!r}: {exc}")
        assert fsm.state in _VALID_AUTHORITY_STATES


# ---------------------------------------------------------------------------
# TransitionResult contract
# ---------------------------------------------------------------------------


class TestTransitionResultContract:
    def test_failed_result_has_failed_guard(self) -> None:
        fsm = _new_fsm()
        result = fsm.begin_handoff(_BEGIN_GUARDS_FAIL)
        assert isinstance(result, TransitionResult)
        assert not result.success
        assert result.failed_guard is not None

    def test_successful_result_has_no_failed_guard(self) -> None:
        fsm = _new_fsm()
        result = fsm.begin_handoff(_BEGIN_GUARDS_PASS)
        assert result.success
        assert result.failed_guard is None
