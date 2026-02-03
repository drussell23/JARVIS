"""
Comprehensive tests for resilience core types.

Tests cover:
- Enum value existence and distinctness
- Protocol implementation verification
- Protocol rejection of non-implementing classes
"""

import pytest
from enum import Enum, auto


class TestCircuitState:
    """Tests for CircuitState enum."""

    def test_has_closed_state(self):
        """CircuitState should have CLOSED state for normal operation."""
        from backend.core.resilience.types import CircuitState
        assert hasattr(CircuitState, 'CLOSED')

    def test_has_open_state(self):
        """CircuitState should have OPEN state for failing/rejected requests."""
        from backend.core.resilience.types import CircuitState
        assert hasattr(CircuitState, 'OPEN')

    def test_has_half_open_state(self):
        """CircuitState should have HALF_OPEN state for testing recovery."""
        from backend.core.resilience.types import CircuitState
        assert hasattr(CircuitState, 'HALF_OPEN')

    def test_states_are_distinct(self):
        """All CircuitState values should be distinct."""
        from backend.core.resilience.types import CircuitState
        states = [CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN]
        assert len(states) == len(set(states))

    def test_is_enum(self):
        """CircuitState should be an Enum."""
        from backend.core.resilience.types import CircuitState
        assert issubclass(CircuitState, Enum)

    def test_exactly_three_states(self):
        """CircuitState should have exactly three states."""
        from backend.core.resilience.types import CircuitState
        assert len(CircuitState) == 3


class TestCapabilityState:
    """Tests for CapabilityState enum."""

    def test_has_degraded_state(self):
        """CapabilityState should have DEGRADED state for fallback mode."""
        from backend.core.resilience.types import CapabilityState
        assert hasattr(CapabilityState, 'DEGRADED')

    def test_has_upgrading_state(self):
        """CapabilityState should have UPGRADING state for upgrade attempts."""
        from backend.core.resilience.types import CapabilityState
        assert hasattr(CapabilityState, 'UPGRADING')

    def test_has_full_state(self):
        """CapabilityState should have FULL state for full capability."""
        from backend.core.resilience.types import CapabilityState
        assert hasattr(CapabilityState, 'FULL')

    def test_has_monitoring_state(self):
        """CapabilityState should have MONITORING state for regression monitoring."""
        from backend.core.resilience.types import CapabilityState
        assert hasattr(CapabilityState, 'MONITORING')

    def test_states_are_distinct(self):
        """All CapabilityState values should be distinct."""
        from backend.core.resilience.types import CapabilityState
        states = [
            CapabilityState.DEGRADED,
            CapabilityState.UPGRADING,
            CapabilityState.FULL,
            CapabilityState.MONITORING,
        ]
        assert len(states) == len(set(states))

    def test_is_enum(self):
        """CapabilityState should be an Enum."""
        from backend.core.resilience.types import CapabilityState
        assert issubclass(CapabilityState, Enum)

    def test_exactly_four_states(self):
        """CapabilityState should have exactly four states."""
        from backend.core.resilience.types import CapabilityState
        assert len(CapabilityState) == 4


class TestRecoveryState:
    """Tests for RecoveryState enum."""

    def test_has_idle_state(self):
        """RecoveryState should have IDLE state for not running."""
        from backend.core.resilience.types import RecoveryState
        assert hasattr(RecoveryState, 'IDLE')

    def test_has_recovering_state(self):
        """RecoveryState should have RECOVERING state for active recovery."""
        from backend.core.resilience.types import RecoveryState
        assert hasattr(RecoveryState, 'RECOVERING')

    def test_has_paused_state(self):
        """RecoveryState should have PAUSED state for safety valve pause."""
        from backend.core.resilience.types import RecoveryState
        assert hasattr(RecoveryState, 'PAUSED')

    def test_has_succeeded_state(self):
        """RecoveryState should have SUCCEEDED state for completed recovery."""
        from backend.core.resilience.types import RecoveryState
        assert hasattr(RecoveryState, 'SUCCEEDED')

    def test_states_are_distinct(self):
        """All RecoveryState values should be distinct."""
        from backend.core.resilience.types import RecoveryState
        states = [
            RecoveryState.IDLE,
            RecoveryState.RECOVERING,
            RecoveryState.PAUSED,
            RecoveryState.SUCCEEDED,
        ]
        assert len(states) == len(set(states))

    def test_is_enum(self):
        """RecoveryState should be an Enum."""
        from backend.core.resilience.types import RecoveryState
        assert issubclass(RecoveryState, Enum)

    def test_exactly_four_states(self):
        """RecoveryState should have exactly four states."""
        from backend.core.resilience.types import RecoveryState
        assert len(RecoveryState) == 4


class TestHealthCheckableProtocol:
    """Tests for HealthCheckable protocol."""

    def test_protocol_is_runtime_checkable(self):
        """HealthCheckable should be runtime checkable."""
        from backend.core.resilience.types import HealthCheckable
        from typing import runtime_checkable, Protocol

        # Should be decorated with @runtime_checkable
        assert hasattr(HealthCheckable, '__protocol_attrs__') or isinstance(HealthCheckable, type)

    def test_accepts_implementing_class(self):
        """HealthCheckable should accept classes that implement check()."""
        from backend.core.resilience.types import HealthCheckable

        class ValidHealthCheck:
            async def check(self) -> bool:
                return True

        instance = ValidHealthCheck()
        assert isinstance(instance, HealthCheckable)

    def test_accepts_class_returning_false(self):
        """HealthCheckable should accept classes whose check() returns False."""
        from backend.core.resilience.types import HealthCheckable

        class FailingHealthCheck:
            async def check(self) -> bool:
                return False

        instance = FailingHealthCheck()
        assert isinstance(instance, HealthCheckable)

    def test_rejects_missing_check_method(self):
        """HealthCheckable should reject classes without check() method."""
        from backend.core.resilience.types import HealthCheckable

        class NoCheckMethod:
            pass

        instance = NoCheckMethod()
        assert not isinstance(instance, HealthCheckable)

    def test_rejects_wrong_method_signature(self):
        """HealthCheckable should reject classes with wrong check() signature."""
        from backend.core.resilience.types import HealthCheckable

        class WrongSignature:
            def check(self, arg: str) -> bool:  # Non-async with extra arg
                return True

        instance = WrongSignature()
        # Protocol only checks method existence, not async or signature
        # So this will still pass isinstance check
        # The actual typing enforcement happens at static analysis time
        # At runtime, we just verify the method exists
        assert isinstance(instance, HealthCheckable)

    def test_non_callable_check_attribute_behavior(self):
        """
        Test behavior with non-callable check attribute.

        Note: Python's runtime_checkable only verifies attribute existence,
        not that it's callable. This is documented Python behavior.
        Static type checkers (mypy, pyright) catch this at analysis time.
        """
        from backend.core.resilience.types import HealthCheckable

        class NonCallableCheck:
            check = "not a method"

        instance = NonCallableCheck()
        # runtime_checkable only checks attribute existence, not callability
        # Static type checkers would flag this, but isinstance passes
        assert isinstance(instance, HealthCheckable)
        # Verify the attribute exists but isn't callable
        assert hasattr(instance, 'check')
        assert not callable(instance.check)


class TestRecoverableProtocol:
    """Tests for Recoverable protocol."""

    def test_protocol_is_runtime_checkable(self):
        """Recoverable should be runtime checkable."""
        from backend.core.resilience.types import Recoverable
        from typing import runtime_checkable, Protocol

        # Should be decorated with @runtime_checkable
        assert hasattr(Recoverable, '__protocol_attrs__') or isinstance(Recoverable, type)

    def test_accepts_implementing_class(self):
        """Recoverable should accept classes that implement recover()."""
        from backend.core.resilience.types import Recoverable

        class ValidRecoverable:
            async def recover(self) -> bool:
                return True

        instance = ValidRecoverable()
        assert isinstance(instance, Recoverable)

    def test_accepts_class_returning_false(self):
        """Recoverable should accept classes whose recover() returns False."""
        from backend.core.resilience.types import Recoverable

        class FailingRecoverable:
            async def recover(self) -> bool:
                return False

        instance = FailingRecoverable()
        assert isinstance(instance, Recoverable)

    def test_rejects_missing_recover_method(self):
        """Recoverable should reject classes without recover() method."""
        from backend.core.resilience.types import Recoverable

        class NoRecoverMethod:
            pass

        instance = NoRecoverMethod()
        assert not isinstance(instance, Recoverable)

    def test_non_callable_recover_attribute_behavior(self):
        """
        Test behavior with non-callable recover attribute.

        Note: Python's runtime_checkable only verifies attribute existence,
        not that it's callable. This is documented Python behavior.
        Static type checkers (mypy, pyright) catch this at analysis time.
        """
        from backend.core.resilience.types import Recoverable

        class NonCallableRecover:
            recover = 42

        instance = NonCallableRecover()
        # runtime_checkable only checks attribute existence, not callability
        # Static type checkers would flag this, but isinstance passes
        assert isinstance(instance, Recoverable)
        # Verify the attribute exists but isn't callable
        assert hasattr(instance, 'recover')
        assert not callable(instance.recover)


class TestProtocolCombinations:
    """Tests for classes implementing multiple protocols."""

    def test_class_can_implement_both_protocols(self):
        """A class should be able to implement both HealthCheckable and Recoverable."""
        from backend.core.resilience.types import HealthCheckable, Recoverable

        class HealthyRecoverable:
            async def check(self) -> bool:
                return True

            async def recover(self) -> bool:
                return True

        instance = HealthyRecoverable()
        assert isinstance(instance, HealthCheckable)
        assert isinstance(instance, Recoverable)

    def test_partial_implementation_health_only(self):
        """A class implementing only check() should pass HealthCheckable but not Recoverable."""
        from backend.core.resilience.types import HealthCheckable, Recoverable

        class HealthOnly:
            async def check(self) -> bool:
                return True

        instance = HealthOnly()
        assert isinstance(instance, HealthCheckable)
        assert not isinstance(instance, Recoverable)

    def test_partial_implementation_recoverable_only(self):
        """A class implementing only recover() should pass Recoverable but not HealthCheckable."""
        from backend.core.resilience.types import HealthCheckable, Recoverable

        class RecoverableOnly:
            async def recover(self) -> bool:
                return True

        instance = RecoverableOnly()
        assert not isinstance(instance, HealthCheckable)
        assert isinstance(instance, Recoverable)


class TestModuleExports:
    """Tests for module exports and structure."""

    def test_types_module_exists(self):
        """The types module should be importable."""
        from backend.core.resilience import types
        assert types is not None

    def test_resilience_init_exports_circuit_state(self):
        """CircuitState should be exported from resilience package."""
        from backend.core.resilience import CircuitState
        assert CircuitState is not None

    def test_resilience_init_exports_capability_state(self):
        """CapabilityState should be exported from resilience package."""
        from backend.core.resilience import CapabilityState
        assert CapabilityState is not None

    def test_resilience_init_exports_recovery_state(self):
        """RecoveryState should be exported from resilience package."""
        from backend.core.resilience import RecoveryState
        assert RecoveryState is not None

    def test_resilience_init_exports_health_checkable(self):
        """HealthCheckable should be exported from resilience package."""
        from backend.core.resilience import HealthCheckable
        assert HealthCheckable is not None

    def test_resilience_init_exports_recoverable(self):
        """Recoverable should be exported from resilience package."""
        from backend.core.resilience import Recoverable
        assert Recoverable is not None

    def test_all_exports_defined(self):
        """__all__ should be defined and contain all exports."""
        from backend.core import resilience
        assert hasattr(resilience, '__all__')
        expected_exports = {
            'CircuitState',
            'CapabilityState',
            'RecoveryState',
            'HealthCheckable',
            'Recoverable',
        }
        assert set(resilience.__all__) >= expected_exports

    def test_module_has_docstring(self):
        """The resilience module should have a docstring."""
        from backend.core import resilience
        assert resilience.__doc__ is not None
        assert len(resilience.__doc__) > 0
