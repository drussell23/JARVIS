# backend/tests/test_gcp_operation_poller.py
"""Hermetic tests for GCPOperationPoller — no GCP network calls."""
from __future__ import annotations
import asyncio
import dataclasses
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Helpers — fake GCP Operation objects
# ---------------------------------------------------------------------------

def _make_op(
    name: str = "operation-1234",
    status: str = "RUNNING",        # PENDING | RUNNING | DONE | ABORTING
    error: Any = None,
    zone_url: str = "https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b",
    self_link: str = "",
    region_url: str = "",
) -> MagicMock:
    op = MagicMock()
    op.name = name
    op.status = status
    op.error = error
    op.zone = zone_url
    op.region = region_url
    op.self_link = self_link or f"{zone_url}/operations/{name}"
    return op


# ---------------------------------------------------------------------------
# Task 1: OperationScope
# ---------------------------------------------------------------------------

class TestOperationScope:
    def test_extracts_zone_from_zone_url(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b")
        scope = OperationScope.from_operation(op, fallback_project="proj")
        assert scope.zone == "us-central1-b"
        assert scope.project == "proj"
        assert scope.scope_type == "zonal"

    def test_extracts_project_from_self_link(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="",
            self_link="https://www.googleapis.com/compute/v1/projects/other-proj/zones/us-east1-b/operations/op-1",
        )
        scope = OperationScope.from_operation(op, fallback_project="fallback")
        assert scope.project == "other-proj"
        assert scope.zone == "us-east1-b"

    def test_uses_fallback_project_when_self_link_absent(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b",
            self_link="",
        )
        scope = OperationScope.from_operation(op, fallback_project="fallback-proj")
        assert scope.project == "proj"  # extracted from zone url, not fallback

    def test_raises_contract_error_when_no_scope(self):
        from backend.core.gcp_operation_poller import OperationScope, ScopeContractError
        op = _make_op(zone_url="", self_link="", region_url="")
        with pytest.raises(ScopeContractError):
            OperationScope.from_operation(op, fallback_project="proj")

    def test_zone_mismatch_regression_no_config_zone_fallback(self):
        """Old path: poller used config.zone even when op was in a different zone.
        New contract: scope ALWAYS comes from the operation object."""
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-east1-c")
        scope = OperationScope.from_operation(op, fallback_project="proj")
        # The scope must reflect the operation's actual zone, not any external config
        assert scope.zone == "us-east1-c"
        # There is no "config zone" parameter to from_operation — the old path is simply gone

    def test_extracts_region_from_region_url(self):
        """Regional operations (e.g. MIG, forwarding rules) must produce scope_type=regional."""
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="",
            region_url="https://www.googleapis.com/compute/v1/projects/proj/regions/us-central1",
            self_link="https://www.googleapis.com/compute/v1/projects/proj/regions/us-central1/operations/op-r",
        )
        scope = OperationScope.from_operation(op, fallback_project="proj")
        assert scope.scope_type == "regional"
        assert scope.region == "us-central1"
        assert scope.zone is None
        assert scope.project == "proj"

    def test_global_scope_from_self_link(self):
        """Global operations (e.g. global URL map changes) must produce scope_type=global."""
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="",
            region_url="",
            self_link="https://www.googleapis.com/compute/v1/projects/proj/global/operations/op-g",
        )
        scope = OperationScope.from_operation(op, fallback_project="proj")
        assert scope.scope_type == "global"
        assert scope.zone is None
        assert scope.region is None


# ---------------------------------------------------------------------------
# Task 2: OperationRecord + OperationLifecycleRegistry
# ---------------------------------------------------------------------------

class TestOperationLifecycleRegistry:
    @pytest.fixture
    def tmp_registry(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry
        return OperationLifecycleRegistry(
            persist_path=tmp_path / "ops.json",
            supervisor_epoch=1,
        )

    @pytest.fixture
    def sample_scope(self):
        from backend.core.gcp_operation_poller import OperationScope
        return OperationScope(project="proj", zone="us-central1-b", region=None, scope_type="zonal")

    @pytest.mark.asyncio
    async def test_register_creates_record(self, tmp_registry, sample_scope):
        op = _make_op()
        record = await tmp_registry.register(op, instance_name="vm-1", action="start",
                                              correlation_id="corr-1")
        assert record.operation_id == "operation-1234"
        assert record.action == "start"
        assert record.instance_name == "vm-1"
        assert record.terminal_state is None  # still in-flight

    @pytest.mark.asyncio
    async def test_update_terminal_succeeds_with_current_epoch(self, tmp_registry, sample_scope):
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op()
        record = await tmp_registry.register(op, instance_name="vm-1", action="start",
                                              correlation_id="corr-1")
        await tmp_registry.update_terminal(record.operation_id, "success",
                                           TerminalReason.OP_DONE_SUCCESS, epoch=1)
        updated = tmp_registry.get(record.operation_id)
        assert updated.terminal_state == "success"
        assert updated.terminal_reason == TerminalReason.OP_DONE_SUCCESS

    @pytest.mark.asyncio
    async def test_update_terminal_rejected_stale_epoch(self, tmp_registry):
        from backend.core.gcp_operation_poller import TerminalReason, SplitBrainFenceError
        op = _make_op()
        record = await tmp_registry.register(op, instance_name="vm-1", action="start",
                                              correlation_id="corr-1")
        with pytest.raises(SplitBrainFenceError):
            # epoch 0 < registry epoch 1 → rejected
            await tmp_registry.update_terminal(record.operation_id, "success",
                                               TerminalReason.OP_DONE_SUCCESS, epoch=0)
        # Record must NOT be mutated
        assert tmp_registry.get(record.operation_id).terminal_state is None

    @pytest.mark.asyncio
    async def test_persist_and_reload(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry, TerminalReason
        reg1 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=1)
        op = _make_op()
        record = await reg1.register(op, instance_name="vm-1", action="start",
                                     correlation_id="corr-1")
        await reg1.persist()

        reg2 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=2)
        await reg2.load()
        loaded = reg2.get(record.operation_id)
        assert loaded is not None
        assert loaded.instance_name == "vm-1"

    @pytest.mark.asyncio
    async def test_pruning_removes_completed_before_inflight(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry, TerminalReason
        reg = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json",
                                         supervisor_epoch=1, max_entries=3)
        # Register 3 ops — 2 completed, 1 in-flight
        for i in range(2):
            op = _make_op(name=f"op-done-{i}",
                          zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
            r = await reg.register(op, instance_name=f"vm-{i}", action="start",
                                   correlation_id=str(i))
            await reg.update_terminal(r.operation_id, "success",
                                      TerminalReason.OP_DONE_SUCCESS, epoch=1)
        op_live = _make_op(name="op-inflight",
                           zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        await reg.register(op_live, instance_name="vm-live", action="start",
                           correlation_id="live")

        # Add a 4th op — should trigger pruning of completed entries first
        op_new = _make_op(name="op-new",
                          zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        await reg.register(op_new, instance_name="vm-new", action="start",
                           correlation_id="new")
        # In-flight record must survive pruning
        assert reg.get("op-inflight") is not None
        # op-new must be registered
        assert reg.get("op-new") is not None

    @pytest.mark.asyncio
    async def test_stale_op_from_prior_session_reconciled(self, tmp_path):
        """Orphaned in-flight record from prior session is closed on startup reconciliation."""
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry, TerminalReason
        # Session 1: register op, crash without closing
        reg1 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=1)
        op = _make_op(name="op-orphan",
                      zone_url="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        await reg1.register(op, instance_name="vm-orphan", action="start",
                            correlation_id="c-orphan")
        await reg1.persist()

        # Session 2: load registry; reconcile with mock instance describer
        reg2 = OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=2)
        await reg2.load()

        async def mock_describe(instance_name: str, zone: str):
            return "RUNNING"  # VM is now running → start op succeeded

        events = []
        await reg2.reconcile_orphans(
            describe_fn=mock_describe,
            emit_fn=lambda name, payload: events.append((name, payload)),
        )
        record = reg2.get("op-orphan")
        assert record.terminal_state == "success"
        assert record.terminal_reason == TerminalReason.NOT_FOUND_CORRELATED
        assert any(e[0] == "orphan_recovered" for e in events)


# ---------------------------------------------------------------------------
# Task 3: GCPOperationPoller
# ---------------------------------------------------------------------------

def _make_done_op(name="op-1", error=None):
    op = _make_op(name=name, status="DONE")
    op.error = error
    if error:
        err_mock = MagicMock()
        err_mock.errors = [MagicMock(message="some GCP error")]
        op.error = err_mock
    return op


class TestGCPOperationPoller:
    @pytest.fixture
    def tmp_registry(self, tmp_path):
        from backend.core.gcp_operation_poller import OperationLifecycleRegistry
        return OperationLifecycleRegistry(persist_path=tmp_path / "ops.json", supervisor_epoch=1)

    @pytest.fixture
    def ops_client(self):
        """Mock zone operations client."""
        return MagicMock()

    def _make_poller(self, ops_client, registry, postcondition=None, max_retries=3,
                     timeout=10.0):
        from backend.core.gcp_operation_poller import GCPOperationPoller
        return GCPOperationPoller(
            operations_client=ops_client,
            registry=registry,
            project="proj",
            postcondition=postcondition,
            max_retries=max_retries,
            base_backoff=0.0,
            max_backoff=0.0,
            timeout=timeout,
        )

    @pytest.mark.asyncio
    async def test_op_done_on_first_poll(self, tmp_registry, ops_client):
        op = _make_done_op()
        poller = self._make_poller(ops_client, tmp_registry)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is True
        from backend.core.gcp_operation_poller import TerminalReason
        assert result.reason == TerminalReason.OP_DONE_SUCCESS

    @pytest.mark.asyncio
    async def test_op_aborting_is_failure(self, tmp_registry, ops_client):
        op = _make_op(status="ABORTING")
        # The client refresh also returns ABORTING
        ops_client.get = MagicMock(return_value=_make_op(status="ABORTING"))
        poller = self._make_poller(ops_client, tmp_registry)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        from backend.core.gcp_operation_poller import TerminalReason
        assert result.reason == TerminalReason.OP_DONE_FAILURE

    @pytest.mark.asyncio
    async def test_404_correlated_success(self, tmp_registry, ops_client):
        """404 with registry match + postcondition=True → terminal success."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.NotFound("op gone"))
        postcond = AsyncMock(return_value=True)
        poller = self._make_poller(ops_client, tmp_registry, postcondition=postcond)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is True
        assert result.reason == TerminalReason.NOT_FOUND_CORRELATED

    @pytest.mark.asyncio
    async def test_404_scope_correct_zone_used(self, tmp_registry, ops_client):
        """Regression: NEW code uses op.zone for polling → no zone mismatch bug."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        # Op was created in us-east1-c (NOT config.zone=us-central1-b)
        op = _make_op(
            status="RUNNING",
            zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-east1-c",
        )
        ops_client.get = MagicMock(side_effect=gex.NotFound("op gone"))
        postcond = AsyncMock(return_value=True)
        poller = self._make_poller(ops_client, tmp_registry, postcondition=postcond)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        # With correct zone-aware polling, 404 is correlated success (postcondition passes)
        assert result.success is True
        assert result.reason == TerminalReason.NOT_FOUND_CORRELATED
        # Verify it polled the correct zone, not config.zone
        call_kwargs = ops_client.get.call_args_list[0][1]
        assert call_kwargs.get("zone") == "us-east1-c"

    @pytest.mark.asyncio
    async def test_404_no_postcondition_terminal_unknown(self, tmp_registry, ops_client):
        """404 with registry match but no postcondition → terminal unknown."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.NotFound("op gone"))
        poller = self._make_poller(ops_client, tmp_registry, postcondition=None)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        assert result.reason == TerminalReason.NOT_FOUND_NO_POSTCONDITION

    @pytest.mark.asyncio
    async def test_permission_denied_immediate_failure(self, tmp_registry, ops_client):
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.Forbidden("no permission"))
        poller = self._make_poller(ops_client, tmp_registry)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        assert result.reason == TerminalReason.PERMISSION_DENIED
        # Must NOT have retried
        assert ops_client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_transient_retry_bounded(self, tmp_registry, ops_client):
        """503 retries with backoff; stops at max_retries and returns RETRY_BUDGET_EXHAUSTED."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import TerminalReason
        op = _make_op(status="RUNNING")
        ops_client.get = MagicMock(side_effect=gex.ServiceUnavailable("GCP is down"))
        poller = self._make_poller(ops_client, tmp_registry, max_retries=3)
        result = await poller.wait(op, action="start", instance_name="vm-1",
                                   correlation_id="c-1")
        assert result.success is False
        assert result.reason == TerminalReason.RETRY_BUDGET_EXHAUSTED
        assert ops_client.get.call_count == 4  # initial + 3 retries

    @pytest.mark.asyncio
    async def test_concurrent_dedup_single_poll_loop(self, tmp_registry, ops_client):
        """N concurrent waiters share exactly 1 poll loop (get called once)."""
        from backend.core.gcp_operation_poller import TerminalReason, GCPOperationPoller, reset_operation_registry
        reset_operation_registry()
        op = _make_done_op()
        ops_client.get = MagicMock(return_value=op)
        poller = self._make_poller(ops_client, tmp_registry)
        # 5 concurrent waiters
        results = await asyncio.gather(*[
            poller.wait(op, action="start", instance_name="vm-1",
                        correlation_id=f"c-{i}")
            for i in range(5)
        ])
        assert all(r.success for r in results)
        # Already DONE on first check — get() called 0 times (fast path)
        assert ops_client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_cancellation_no_task_leak(self, tmp_registry, ops_client):
        """Cancelled waiter leaves no orphan entry in _active_pollers."""
        import google.api_core.exceptions as gex
        from backend.core.gcp_operation_poller import GCPOperationPoller, reset_operation_registry
        reset_operation_registry()
        op = _make_op(status="RUNNING")

        # Make ops_client.get block by sleeping (simulates slow GCP API)
        async def slow_get(**kw):
            await asyncio.sleep(100)
        ops_client.get = MagicMock(wraps=lambda **kw: None)

        poller = self._make_poller(ops_client, tmp_registry, timeout=100.0)

        task = asyncio.create_task(
            poller.wait(op, action="start", instance_name="vm-1", correlation_id="c-1")
        )
        await asyncio.sleep(0)  # yield to let task start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # After cancellation, the op_id must be cleaned up from _active_pollers
        assert op.name not in GCPOperationPoller._active_pollers
