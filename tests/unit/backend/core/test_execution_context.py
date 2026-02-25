"""Tests for execution context timeout budget propagation."""
import asyncio
import threading
import time
import pytest


class TestErrorTaxonomy:
    """Verify error types have correct inheritance and fields."""

    def test_budget_exhausted_not_timeout_error(self):
        from backend.core.execution_context import BudgetExhaustedError
        err = BudgetExhaustedError(
            owner="phase_preflight", phase="preflight",
            deadline_mono=1000.0, remaining_at_entry=0.0,
            local_cap=30.0, effective_timeout=0.0,
            elapsed=90.0, timeout_origin="budget",
        )
        assert not isinstance(err, TimeoutError)
        assert isinstance(err, Exception)
        assert err.owner == "phase_preflight"
        assert err.timeout_origin == "budget"

    def test_local_cap_exceeded_is_timeout_error(self):
        from backend.core.execution_context import LocalCapExceededError
        err = LocalCapExceededError(
            owner="phase_preflight", phase="preflight",
            deadline_mono=2000.0, remaining_at_entry=50.0,
            local_cap=5.0, effective_timeout=5.0,
            elapsed=5.0, timeout_origin="local_cap",
        )
        assert isinstance(err, TimeoutError)
        assert err.timeout_origin == "local_cap"

    def test_external_cancellation_error(self):
        from backend.core.execution_context import (
            ExternalCancellationError, CancellationCause,
        )
        err = ExternalCancellationError(
            cause=CancellationCause.OWNER_SHUTDOWN,
            scope_id="scope-123",
            detail="Supervisor shutting down",
        )
        assert not isinstance(err, TimeoutError)
        assert err.cause == CancellationCause.OWNER_SHUTDOWN

    def test_budget_exhausted_has_all_metadata(self):
        from backend.core.execution_context import BudgetExhaustedError
        err = BudgetExhaustedError(
            owner="svc_cloudsql", phase="enterprise",
            deadline_mono=500.0, remaining_at_entry=2.0,
            local_cap=30.0, effective_timeout=2.0,
            elapsed=2.0, timeout_origin="budget",
        )
        assert err.remaining_at_entry == 2.0
        assert err.effective_timeout == 2.0


class TestFeatureFlags:
    """Verify feature flags read from environment."""

    def test_budget_enforce_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_BUDGET_ENFORCE", raising=False)
        # Force reimport to pick up env change
        import importlib
        import backend.core.execution_context as mod
        importlib.reload(mod)
        assert mod.BUDGET_ENFORCE is True  # Design: defaults to True

    def test_budget_enforce_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BUDGET_ENFORCE", "false")
        import importlib
        import backend.core.execution_context as mod
        importlib.reload(mod)
        assert mod.BUDGET_ENFORCE is False

    def test_budget_shadow_default_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_BUDGET_SHADOW", raising=False)
        import importlib
        import backend.core.execution_context as mod
        importlib.reload(mod)
        assert mod.BUDGET_SHADOW is False

    def test_budget_shadow_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BUDGET_SHADOW", "1")
        import importlib
        import backend.core.execution_context as mod
        importlib.reload(mod)
        assert mod.BUDGET_SHADOW is True


class TestEnums:
    """Verify enum members exist and have expected values."""

    def test_cancellation_cause_members(self):
        from backend.core.execution_context import CancellationCause
        assert hasattr(CancellationCause, "BUDGET_EXHAUSTED")
        assert hasattr(CancellationCause, "OWNER_SHUTDOWN")
        assert hasattr(CancellationCause, "DEPENDENCY_LOST")
        assert hasattr(CancellationCause, "MANUAL_CANCEL")

    def test_root_reason_members(self):
        from backend.core.execution_context import RootReason
        assert hasattr(RootReason, "DETACHED_BACKGROUND")
        assert hasattr(RootReason, "RECOVERY_WORKER")
        assert hasattr(RootReason, "USER_JOB")

    def test_request_kind_members(self):
        from backend.core.execution_context import RequestKind
        assert hasattr(RequestKind, "STARTUP")
        assert hasattr(RequestKind, "RUNTIME")
        assert hasattr(RequestKind, "RECOVERY")
        assert hasattr(RequestKind, "BACKGROUND")

    def test_criticality_members(self):
        from backend.core.execution_context import Criticality
        assert hasattr(Criticality, "CRITICAL")
        assert hasattr(Criticality, "HIGH")
        assert hasattr(Criticality, "NORMAL")
        assert hasattr(Criticality, "LOW")


class TestCancelScope:
    """Verify CancelScope is write-once and thread-safe."""

    def test_cancel_scope_write_once(self):
        from backend.core.execution_context import (
            CancelScopeHandle, CancellationCause,
        )
        handle = CancelScopeHandle(owner_id="test")
        first = handle.set_cause(
            CancellationCause.BUDGET_EXHAUSTED, "deadline hit"
        )
        second = handle.set_cause(
            CancellationCause.OWNER_SHUTDOWN, "shutdown"
        )
        assert first is True
        assert second is False
        assert handle.scope.cause == CancellationCause.BUDGET_EXHAUSTED

    def test_cancel_scope_initially_none(self):
        from backend.core.execution_context import CancelScopeHandle
        handle = CancelScopeHandle(owner_id="test")
        assert handle.scope is None

    def test_cancel_scope_thread_safe(self):
        from backend.core.execution_context import (
            CancelScopeHandle, CancellationCause,
        )
        handle = CancelScopeHandle(owner_id="test")
        results = []

        def try_set(cause, detail):
            results.append(handle.set_cause(cause, detail))

        threads = [
            threading.Thread(
                target=try_set,
                args=(CancellationCause.BUDGET_EXHAUSTED, f"t{i}"),
            )
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 9

    def test_cancel_scope_frozen(self):
        from backend.core.execution_context import (
            CancelScopeHandle, CancellationCause,
        )
        handle = CancelScopeHandle(owner_id="test")
        handle.set_cause(CancellationCause.MANUAL_CANCEL, "user")
        scope = handle.scope
        # CancelScope is a frozen dataclass — mutation must raise
        with pytest.raises(AttributeError):
            scope.cause = CancellationCause.OWNER_SHUTDOWN

    def test_cancel_scope_detail_preserved(self):
        from backend.core.execution_context import (
            CancelScopeHandle, CancellationCause,
        )
        handle = CancelScopeHandle(owner_id="myowner")
        handle.set_cause(CancellationCause.DEPENDENCY_LOST, "db gone")
        scope = handle.scope
        assert scope.detail == "db gone"
        assert scope.owner_id == "myowner"
        assert scope.cause == CancellationCause.DEPENDENCY_LOST
        assert isinstance(scope.set_at_mono, float)
        assert scope.scope_id  # non-empty string

    def test_cancel_scope_scope_id_unique(self):
        from backend.core.execution_context import (
            CancelScopeHandle, CancellationCause,
        )
        ids = set()
        for i in range(20):
            h = CancelScopeHandle(owner_id=f"owner-{i}")
            h.set_cause(CancellationCause.BUDGET_EXHAUSTED, "test")
            ids.add(h.scope.scope_id)
        assert len(ids) == 20  # all unique


class TestErrorMessages:
    """Verify error string representations are informative."""

    def test_budget_exhausted_str(self):
        from backend.core.execution_context import BudgetExhaustedError
        err = BudgetExhaustedError(
            owner="phase_trinity", phase="trinity",
            deadline_mono=1000.0, remaining_at_entry=0.0,
            local_cap=60.0, effective_timeout=0.0,
            elapsed=120.0, timeout_origin="budget",
        )
        msg = str(err)
        assert "phase_trinity" in msg
        assert "budget" in msg.lower() or "exhausted" in msg.lower()

    def test_local_cap_exceeded_str(self):
        from backend.core.execution_context import LocalCapExceededError
        err = LocalCapExceededError(
            owner="svc_cloudsql", phase="enterprise",
            deadline_mono=2000.0, remaining_at_entry=50.0,
            local_cap=5.0, effective_timeout=5.0,
            elapsed=5.0, timeout_origin="local_cap",
        )
        msg = str(err)
        assert "svc_cloudsql" in msg

    def test_external_cancellation_str(self):
        from backend.core.execution_context import (
            ExternalCancellationError, CancellationCause,
        )
        err = ExternalCancellationError(
            cause=CancellationCause.OWNER_SHUTDOWN,
            scope_id="scope-abc",
            detail="Supervisor shutting down",
        )
        msg = str(err)
        assert "scope-abc" in msg or "OWNER_SHUTDOWN" in msg
