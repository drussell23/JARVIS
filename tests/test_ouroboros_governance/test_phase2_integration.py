# tests/test_ouroboros_governance/test_phase2_integration.py
"""Phase 2 integration tests — Go/No-Go criteria verification.

Tests verify acceptance criteria from design doc section 4
(Phase 2A and Phase 2B Go/No-Go).
"""

import asyncio
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    PressureLevel,
)
from backend.core.ouroboros.governance.degradation import (
    DegradationController,
    DegradationMode,
)
from backend.core.ouroboros.governance.routing_policy import (
    RoutingPolicy,
    RoutingDecision,
    TaskCategory,
)
from backend.core.ouroboros.governance.multi_file_engine import (
    MultiFileChangeEngine,
    MultiFileChangeRequest,
)
from backend.core.ouroboros.governance.change_engine import ChangePhase
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.lock_manager import GovernanceLockManager
from backend.core.ouroboros.governance.break_glass import BreakGlassManager


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def a():\n    return 1\n")
    (src / "b.py").write_text("def b():\n    return 2\n")
    (src / "c.py").write_text("def c():\n    return 3\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


# ---------------------------------------------------------------------------
# Phase 2A: Hybrid Routing Go/No-Go
# ---------------------------------------------------------------------------


class TestHybridRoutingGoNoGo:
    def test_cpu_spike_routes_heavy_to_gcp(self):
        """CPU spike -> heavy task routed to GCP within policy."""
        policy = RoutingPolicy()
        snap = ResourceSnapshot(
            ram_percent=50.0, cpu_percent=85.0,
            event_loop_latency_ms=5.0, disk_io_busy=False,
        )
        decision = policy.route(
            TaskCategory.MULTI_FILE_ANALYSIS, snap,
            DegradationMode.REDUCED_AUTONOMY, gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_event_loop_latency_sheds_background(self):
        """Event loop latency > 40ms p95 -> elevated pressure."""
        snap = ResourceSnapshot(
            ram_percent=50.0, cpu_percent=40.0,
            event_loop_latency_ms=45.0, disk_io_busy=False,
        )
        assert snap.overall_pressure >= PressureLevel.ELEVATED

    def test_gcp_unavailable_queues_heavy_continues_local(self):
        """GCP unavailable -> heavy tasks queued, safe_auto continues local."""
        policy = RoutingPolicy()
        snap = ResourceSnapshot(
            ram_percent=50.0, cpu_percent=40.0,
            event_loop_latency_ms=5.0, disk_io_busy=False,
        )
        heavy = policy.route(
            TaskCategory.CROSS_REPO_PLANNING, snap,
            DegradationMode.REDUCED_AUTONOMY, gcp_available=False,
        )
        light = policy.route(
            TaskCategory.SINGLE_FILE_FIX, snap,
            DegradationMode.REDUCED_AUTONOMY, gcp_available=False,
        )
        assert heavy == RoutingDecision.QUEUE
        assert light == RoutingDecision.LOCAL


# ---------------------------------------------------------------------------
# Phase 2A: Degradation Mode Go/No-Go
# ---------------------------------------------------------------------------


class TestDegradationGoNoGo:
    @pytest.mark.asyncio
    async def test_all_four_modes_reachable(self):
        """All 4 degradation modes reachable via test triggers."""
        ctrl = DegradationController()
        assert ctrl.mode == DegradationMode.FULL_AUTONOMY

        # Elevated -> REDUCED
        await ctrl.evaluate(ResourceSnapshot(82.0, 40.0, 5.0, False))
        assert ctrl.mode == DegradationMode.REDUCED_AUTONOMY

        # Critical -> READ_ONLY
        await ctrl.evaluate(ResourceSnapshot(87.0, 85.0, 5.0, False))
        assert ctrl.mode == DegradationMode.READ_ONLY_PLANNING

        # Emergency -> STOP
        await ctrl.evaluate(ResourceSnapshot(95.0, 40.0, 5.0, False))
        assert ctrl.mode == DegradationMode.EMERGENCY_STOP

    @pytest.mark.asyncio
    async def test_full_to_reduced_to_readonly_to_stop(self):
        """FULL -> REDUCED -> READ_ONLY -> EMERGENCY_STOP transitions tested."""
        ctrl = DegradationController()
        transitions = []

        t = await ctrl.evaluate(ResourceSnapshot(82.0, 40.0, 5.0, False))
        transitions.append(t)
        t = await ctrl.evaluate(ResourceSnapshot(87.0, 85.0, 5.0, False))
        transitions.append(t)
        t = await ctrl.evaluate(ResourceSnapshot(95.0, 40.0, 5.0, False))
        transitions.append(t)

        assert all(t is not None for t in transitions)
        assert transitions[0].to_mode == DegradationMode.REDUCED_AUTONOMY
        assert transitions[1].to_mode == DegradationMode.READ_ONLY_PLANNING
        assert transitions[2].to_mode == DegradationMode.EMERGENCY_STOP

    @pytest.mark.asyncio
    async def test_emergency_stop_requires_explicit_reset(self):
        """Recovery from EMERGENCY_STOP requires explicit re-enable."""
        ctrl = DegradationController()
        await ctrl.evaluate(ResourceSnapshot(95.0, 40.0, 5.0, False))
        assert ctrl.mode == DegradationMode.EMERGENCY_STOP

        # Normal pressure does NOT auto-recover
        await ctrl.evaluate(ResourceSnapshot(30.0, 20.0, 1.0, False))
        assert ctrl.mode == DegradationMode.EMERGENCY_STOP

        # Explicit reset works
        await ctrl.explicit_reset()
        assert ctrl.mode == DegradationMode.FULL_AUTONOMY

    def test_gcp_routing_cost_guardrail(self):
        """GCP routing stays under configured daily budget cap."""
        policy = RoutingPolicy()
        # Blow the budget
        policy.cost_guardrail.record_gcp_usage(999.0)
        assert policy.cost_guardrail.over_budget is True

        decision = policy.route(
            TaskCategory.CANDIDATE_GENERATION,
            ResourceSnapshot(50.0, 40.0, 5.0, False),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.QUEUE


# ---------------------------------------------------------------------------
# Phase 2B: Multi-File Go/No-Go
# ---------------------------------------------------------------------------


class TestMultiFileGoNoGo:
    @pytest.mark.asyncio
    async def test_multi_file_all_applied_or_all_rolled_back(
        self, project, ledger
    ):
        """Multi-file change: all files updated atomically or all rolled back."""
        comm = CommProtocol(transports=[LogTransport()])
        engine = MultiFileChangeEngine(
            project_root=project, ledger=ledger, comm=comm,
        )

        # Success case: all applied
        request = MultiFileChangeRequest(
            goal="Update all",
            files={
                project / "src" / "a.py": "def a():\n    return 10\n",
                project / "src" / "b.py": "def b():\n    return 20\n",
            },
            profile=OperationProfile(
                files_affected=[Path("src/a.py"), Path("src/b.py")],
                change_type=ChangeType.MODIFY, blast_radius=2,
                crosses_repo_boundary=False, touches_security_surface=False,
                touches_supervisor=False, test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)
        assert result.success is True
        assert (project / "src" / "a.py").read_text() == "def a():\n    return 10\n"
        assert (project / "src" / "b.py").read_text() == "def b():\n    return 20\n"

    @pytest.mark.asyncio
    async def test_multi_file_rollback_all_on_verify_failure(
        self, project, ledger
    ):
        """Partial multi-file apply never happens — all rolled back."""
        original_a = (project / "src" / "a.py").read_text()
        original_b = (project / "src" / "b.py").read_text()

        comm = CommProtocol(transports=[LogTransport()])
        engine = MultiFileChangeEngine(
            project_root=project, ledger=ledger, comm=comm,
        )
        request = MultiFileChangeRequest(
            goal="Fail verify",
            files={
                project / "src" / "a.py": "def a():\n    return 100\n",
                project / "src" / "b.py": "def b():\n    return 200\n",
            },
            profile=OperationProfile(
                files_affected=[Path("src/a.py"), Path("src/b.py")],
                change_type=ChangeType.MODIFY, blast_radius=2,
                crosses_repo_boundary=False, touches_security_surface=False,
                touches_supervisor=False, test_scope_confidence=0.9,
            ),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await engine.execute(request)
        assert result.rolled_back is True
        assert (project / "src" / "a.py").read_text() == original_a
        assert (project / "src" / "b.py").read_text() == original_b

    @pytest.mark.asyncio
    async def test_learning_feedback_with_op_id(self, project, ledger):
        """Ledger records op_id correlation for learning feedback."""
        comm = CommProtocol(transports=[LogTransport()])
        engine = MultiFileChangeEngine(
            project_root=project, ledger=ledger, comm=comm,
        )
        request = MultiFileChangeRequest(
            goal="Track op_id",
            files={
                project / "src" / "a.py": "def a():\n    return 42\n",
            },
            profile=OperationProfile(
                files_affected=[Path("src/a.py")],
                change_type=ChangeType.MODIFY, blast_radius=1,
                crosses_repo_boundary=False, touches_security_surface=False,
                touches_supervisor=False, test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)
        assert result.op_id.startswith("op-")
        history = await ledger.get_history(result.op_id)
        assert len(history) > 0
