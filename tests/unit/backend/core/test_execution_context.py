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


class TestExecutionContext:
    """Verify ExecutionContext dataclass and ContextVar query functions."""

    def test_context_is_frozen(self):
        from backend.core.execution_context import ExecutionContext, CancelScopeHandle
        ctx = ExecutionContext(
            deadline_mono=time.monotonic() + 60.0, trace_id="test",
            owner_id="test", cancel_scope=CancelScopeHandle(owner_id="test"),
            mode_snapshot="normal",
        )
        with pytest.raises(AttributeError):
            ctx.deadline_mono = 999.0

    def test_context_uses_monotonic_clock(self):
        from backend.core.execution_context import ExecutionContext, CancelScopeHandle
        before = time.monotonic()
        ctx = ExecutionContext(
            deadline_mono=time.monotonic() + 60.0, trace_id="test",
            owner_id="test", cancel_scope=CancelScopeHandle(owner_id="test"),
            mode_snapshot="normal",
        )
        after = time.monotonic()
        assert before <= ctx.created_at_mono <= after

    def test_context_parent_chain(self):
        from backend.core.execution_context import ExecutionContext, CancelScopeHandle
        parent = ExecutionContext(
            deadline_mono=time.monotonic() + 60.0, trace_id="parent",
            owner_id="phase_preflight", cancel_scope=CancelScopeHandle(owner_id="p"),
            mode_snapshot="normal",
        )
        child = ExecutionContext(
            deadline_mono=time.monotonic() + 30.0, trace_id="parent",
            owner_id="svc_cloudsql", cancel_scope=CancelScopeHandle(owner_id="s"),
            mode_snapshot="normal", parent_ctx=parent,
        )
        assert child.parent_ctx is parent
        assert child.parent_ctx.owner_id == "phase_preflight"

    def test_contextvar_default_is_none(self):
        from backend.core.execution_context import current_context
        assert current_context() is None

    def test_remaining_budget_none_when_no_context(self):
        from backend.core.execution_context import remaining_budget
        assert remaining_budget() is None

    def test_remaining_property(self):
        from backend.core.execution_context import ExecutionContext, CancelScopeHandle
        ctx = ExecutionContext(
            deadline_mono=time.monotonic() + 10.0, trace_id="test",
            owner_id="test", cancel_scope=CancelScopeHandle(owner_id="test"),
            mode_snapshot="normal",
        )
        assert 9.0 < ctx.remaining < 10.1


class TestExecutionBudget:
    """Verify execution_budget() context manager behavior."""

    @pytest.mark.asyncio
    async def test_budget_sets_context(self):
        from backend.core.execution_context import execution_budget, current_context
        assert current_context() is None
        async with execution_budget("test_owner", 60.0) as ctx:
            assert current_context() is ctx
            assert ctx.owner_id == "test_owner"
            assert ctx.remaining > 50.0
        assert current_context() is None

    @pytest.mark.asyncio
    async def test_budget_shrinks_with_nesting(self):
        from backend.core.execution_context import execution_budget
        async with execution_budget("parent", 60.0) as parent_ctx:
            async with execution_budget("child", 30.0) as child_ctx:
                assert child_ctx.deadline_mono <= parent_ctx.deadline_mono
                assert child_ctx.parent_ctx is parent_ctx

    @pytest.mark.asyncio
    async def test_budget_never_extends(self):
        from backend.core.execution_context import execution_budget
        async with execution_budget("parent", 10.0) as parent_ctx:
            async with execution_budget("child", 60.0) as child_ctx:
                assert abs(child_ctx.deadline_mono - parent_ctx.deadline_mono) < 0.1

    @pytest.mark.asyncio
    async def test_root_scope_creates_fresh_deadline(self):
        from backend.core.execution_context import execution_budget, RootReason
        async with execution_budget("parent", 10.0) as parent_ctx:
            async with execution_budget(
                "supervisor", 120.0, root=True, root_reason=RootReason.RECOVERY_WORKER,
            ) as child_ctx:
                assert child_ctx.deadline_mono > parent_ctx.deadline_mono
                assert child_ctx.root_reason == RootReason.RECOVERY_WORKER

    @pytest.mark.asyncio
    async def test_root_scope_requires_reason(self):
        from backend.core.execution_context import execution_budget
        with pytest.raises(ValueError, match="root_reason"):
            async with execution_budget("supervisor", 60.0, root=True):
                pass

    @pytest.mark.asyncio
    async def test_root_scope_blocked_for_unauthorized_owner(self):
        from backend.core.execution_context import execution_budget, RootReason
        with pytest.raises(ValueError, match="not authorized"):
            async with execution_budget(
                "random_service", 60.0, root=True, root_reason=RootReason.DETACHED_BACKGROUND,
            ):
                pass

    @pytest.mark.asyncio
    async def test_context_leak_prevention(self):
        from backend.core.execution_context import execution_budget, current_context
        try:
            async with execution_budget("test", 60.0):
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        assert current_context() is None

    @pytest.mark.asyncio
    async def test_context_leak_prevention_nested(self):
        from backend.core.execution_context import execution_budget, current_context
        async with execution_budget("outer", 60.0) as outer:
            try:
                async with execution_budget("inner", 30.0):
                    raise RuntimeError("inner failure")
            except RuntimeError:
                pass
            assert current_context() is outer
        assert current_context() is None

    @pytest.mark.asyncio
    async def test_phase_fields_propagated(self):
        from backend.core.execution_context import execution_budget, Criticality, RequestKind
        async with execution_budget(
            "phase_preflight", 90.0, phase_id="1", phase_name="preflight",
            priority=Criticality.CRITICAL, request_kind=RequestKind.STARTUP, tags={"zone": "5"},
        ) as ctx:
            assert ctx.phase_id == "1"
            assert ctx.phase_name == "preflight"
            assert ctx.priority == Criticality.CRITICAL
            assert ctx.tags["zone"] == "5"


class TestBudgetAwareWaitFor:
    """Verify budget_aware_wait_for() timeout and error behavior."""

    @pytest.mark.asyncio
    async def test_completes_within_budget(self):
        from backend.core.execution_context import execution_budget, budget_aware_wait_for

        async def fast_op():
            await asyncio.sleep(0.01)
            return "done"

        async with execution_budget("test", 5.0):
            result = await budget_aware_wait_for(fast_op(), local_cap=2.0, label="fast_op")
        assert result == "done"

    @pytest.mark.asyncio
    async def test_local_cap_exceeded_raises_typed_error(self):
        from backend.core.execution_context import execution_budget, budget_aware_wait_for, LocalCapExceededError

        async def slow_op():
            await asyncio.sleep(10.0)

        async with execution_budget("test", 60.0):
            with pytest.raises(LocalCapExceededError) as exc_info:
                await budget_aware_wait_for(slow_op(), local_cap=0.1, label="slow_op")
            assert exc_info.value.timeout_origin == "local_cap"

    @pytest.mark.asyncio
    async def test_budget_exhausted_raises_typed_error(self):
        from backend.core.execution_context import execution_budget, budget_aware_wait_for, BudgetExhaustedError

        async def slow_op():
            await asyncio.sleep(10.0)

        async with execution_budget("test", 0.15):
            with pytest.raises(BudgetExhaustedError) as exc_info:
                await budget_aware_wait_for(slow_op(), local_cap=5.0, label="slow_op")
            assert exc_info.value.timeout_origin == "budget"

    @pytest.mark.asyncio
    async def test_no_budget_no_cap_fails_closed(self):
        from backend.core.execution_context import budget_aware_wait_for

        async def some_op():
            return "done"

        with pytest.raises(RuntimeError, match="No budget and no local_cap"):
            await budget_aware_wait_for(some_op(), local_cap=0.0, label="test")

    @pytest.mark.asyncio
    async def test_no_budget_with_cap_uses_local(self):
        from backend.core.execution_context import budget_aware_wait_for

        async def fast_op():
            await asyncio.sleep(0.01)
            return "ok"

        result = await budget_aware_wait_for(fast_op(), local_cap=5.0, label="unscoped")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_effective_timeout_is_min(self):
        from backend.core.execution_context import execution_budget, budget_aware_wait_for, LocalCapExceededError

        async def slow_op():
            await asyncio.sleep(10.0)

        async with execution_budget("test", 0.5):
            with pytest.raises(LocalCapExceededError):
                await budget_aware_wait_for(slow_op(), local_cap=0.1, label="test")

    @pytest.mark.asyncio
    async def test_shadow_mode_logs_without_enforcing(self):
        import backend.core.execution_context as ec
        original_enforce = ec.BUDGET_ENFORCE
        original_shadow = ec.BUDGET_SHADOW
        try:
            ec.BUDGET_ENFORCE = False
            ec.BUDGET_SHADOW = True
            from backend.core.execution_context import execution_budget, budget_aware_wait_for

            async def fast_op():
                await asyncio.sleep(0.01)
                return "ok"

            async with execution_budget("test", 0.05):
                result = await budget_aware_wait_for(fast_op(), local_cap=5.0, label="shadow_test")
            assert result == "ok"
        finally:
            ec.BUDGET_ENFORCE = original_enforce
            ec.BUDGET_SHADOW = original_shadow


class TestExceptionBridging:
    """Verify bridge_timeout_error() produces correct typed errors."""

    def test_bridge_with_budget_context(self):
        from backend.core.execution_context import bridge_timeout_error, BudgetExhaustedError
        err = bridge_timeout_error(
            asyncio.TimeoutError(), label="test", remaining_at_entry=0.0,
            local_cap=30.0, owner="phase_test", phase="test",
        )
        assert isinstance(err, BudgetExhaustedError)

    def test_bridge_with_remaining_budget(self):
        from backend.core.execution_context import bridge_timeout_error, LocalCapExceededError
        err = bridge_timeout_error(
            asyncio.TimeoutError(), label="test", remaining_at_entry=20.0,
            local_cap=5.0, owner="phase_test", phase="test",
        )
        assert isinstance(err, LocalCapExceededError)
