#!/usr/bin/env python3
"""Tests for lifecycle exception taxonomy (Disease 5+6 MVP)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from backend.core.lifecycle_exceptions import LifecyclePhase, LifecycleErrorCode


class TestLifecyclePhaseEnum:
    def test_all_phases_exist(self):
        assert LifecyclePhase.PRECHECK == "precheck"
        assert LifecyclePhase.BRINGUP == "bringup"
        assert LifecyclePhase.CONTRACT_GATE == "contract_gate"
        assert LifecyclePhase.RUNNING == "running"
        assert LifecyclePhase.DRAINING == "draining"
        assert LifecyclePhase.STOPPING == "stopping"
        assert LifecyclePhase.STOPPED == "stopped"

    def test_exactly_seven_phases(self):
        assert len(LifecyclePhase) == 7


class TestLifecycleErrorCodeEnum:
    @pytest.mark.parametrize("code", [
        "dep_unreachable", "contract_incompatible", "transition_invalid",
        "shutdown_reentrant", "task_orphan_detected", "epoch_stale",
        "timeout_exceeded", "resource_exhausted",
    ])
    def test_error_code_exists(self, code):
        assert LifecycleErrorCode(code) == code

    def test_exactly_eight_codes(self):
        assert len(LifecycleErrorCode) == 8


from backend.core.lifecycle_exceptions import (
    LifecycleSignal, ShutdownRequested, LifecycleCancelled,
)


class TestLifecycleSignals:
    """Control-flow signals (BaseException) must never be caught by except Exception."""

    def test_lifecycle_signal_is_base_exception(self):
        assert issubclass(LifecycleSignal, BaseException)
        assert not issubclass(LifecycleSignal, Exception)

    def test_shutdown_requested_fields(self):
        sig = ShutdownRequested(
            reason="operator", epoch=1,
            requested_by="signal:SIGTERM", at_monotonic=1000.0,
        )
        assert sig.reason == "operator"
        assert sig.epoch == 1
        assert sig.requested_by == "signal:SIGTERM"
        assert sig.at_monotonic == 1000.0

    def test_shutdown_requested_is_frozen(self):
        sig = ShutdownRequested(
            reason="test", epoch=0, requested_by="test", at_monotonic=0.0,
        )
        with pytest.raises(AttributeError):
            sig.reason = "changed"

    def test_lifecycle_cancelled_fields(self):
        sig = LifecycleCancelled(
            reason="task timeout", epoch=2,
            requested_by="watchdog", at_monotonic=2000.0,
            cancelled_task="health_monitor",
        )
        assert sig.cancelled_task == "health_monitor"

    def test_lifecycle_cancelled_default_task(self):
        sig = LifecycleCancelled(
            reason="cancel", epoch=0, requested_by="test", at_monotonic=0.0,
        )
        assert sig.cancelled_task == ""

    def test_except_exception_does_not_catch_signal(self):
        sig = ShutdownRequested(
            reason="test", epoch=0, requested_by="test", at_monotonic=0.0,
        )
        caught_by_exception = False
        try:
            raise sig
        except Exception:
            caught_by_exception = True
        except BaseException:
            pass
        assert not caught_by_exception, "except Exception must NOT catch LifecycleSignal"


from backend.core.lifecycle_exceptions import (
    LifecycleError, LifecycleFatalError, LifecycleRecoverableError,
    DependencyUnavailableError, TransitionRejected,
)


class TestLifecycleErrors:
    """Lifecycle errors carry state context and epoch staleness guard."""

    def test_lifecycle_error_fields(self):
        err = LifecycleError(
            "test error",
            error_code=LifecycleErrorCode.TRANSITION_INVALID,
            state_at_raise="running",
            phase=LifecyclePhase.RUNNING,
            epoch=3,
        )
        assert err.error_code == LifecycleErrorCode.TRANSITION_INVALID
        assert err.state_at_raise == "running"
        assert err.phase == LifecyclePhase.RUNNING
        assert err.epoch == 3
        assert err.cause is None
        assert "test error" in str(err)

    def test_lifecycle_error_with_cause(self):
        cause = ValueError("port 99999")
        err = LifecycleError(
            "wrapped", error_code=LifecycleErrorCode.DEP_UNREACHABLE,
            state_at_raise="starting_backend", phase=LifecyclePhase.BRINGUP,
            epoch=1, cause=cause,
        )
        assert err.cause is cause

    def test_fatal_is_lifecycle_error(self):
        assert issubclass(LifecycleFatalError, LifecycleError)
        assert issubclass(LifecycleFatalError, Exception)

    def test_recoverable_has_retry_hint(self):
        err = LifecycleRecoverableError(
            "timeout", retry_hint="backoff",
            error_code=LifecycleErrorCode.TIMEOUT_EXCEEDED,
            state_at_raise="starting_resources",
            phase=LifecyclePhase.BRINGUP, epoch=1,
        )
        assert err.retry_hint == "backoff"

    def test_recoverable_default_retry_hint(self):
        err = LifecycleRecoverableError(
            "test", error_code=LifecycleErrorCode.DEP_UNREACHABLE,
            state_at_raise="running", phase=LifecyclePhase.RUNNING, epoch=0,
        )
        assert err.retry_hint == "backoff"

    def test_dependency_unavailable_fields(self):
        err = DependencyUnavailableError(
            "Prime unreachable", dependency="jarvis_prime",
            fallback_available=True,
            error_code=LifecycleErrorCode.DEP_UNREACHABLE,
            state_at_raise="running", phase=LifecyclePhase.RUNNING, epoch=2,
        )
        assert err.dependency == "jarvis_prime"
        assert err.fallback_available is True
        assert isinstance(err, LifecycleRecoverableError)

    def test_transition_rejected_is_lifecycle_error(self):
        err = TransitionRejected(
            "already stopped",
            error_code=LifecycleErrorCode.TRANSITION_INVALID,
            state_at_raise="stopped", phase=LifecyclePhase.STOPPED, epoch=1,
        )
        assert isinstance(err, LifecycleError)
        assert not isinstance(err, LifecycleFatalError)

    def test_inheritance_hierarchy(self):
        """Verify the full hierarchy is correct."""
        assert issubclass(DependencyUnavailableError, LifecycleRecoverableError)
        assert issubclass(LifecycleRecoverableError, LifecycleError)
        assert issubclass(LifecycleFatalError, LifecycleError)
        assert issubclass(TransitionRejected, LifecycleError)
        assert issubclass(LifecycleError, Exception)
