"""Tests for the OpportunityMiner auto-ack lane (Task #69 — C1+C3 fix).

Background
----------
Battle test bt-2026-04-12-005521 / bt-2026-04-12-025527 showed every miner
batch landing at ``pending_ack`` and never reaching the priority queue. The
empirical risk-tier audit (run against ``RiskEngine.classify`` with profiles
built the same way ``Orchestrator._build_profile`` builds them) showed:

  N=1 backend/foo.py    → SAFE_AUTO       (silent auto-apply!)
  N=2 backend/foo+bar   → NOTIFY_APPLY    (yellow / 5s preview)
  N=3+                  → APPROVAL_REQUIRED (orange — safe)

So Option B (drop ``requires_human_ack`` on miner batches) was rejected:
1- and 2-file batches would auto-apply silently. Option A — a sensor-driven
auto-ack lane via ``router.acknowledge()`` with hard guards — was chosen,
with ``MIN_FILES=3`` matching the classifier's ``too_many_files`` boundary
and the lane defaulting OFF.

These tests pin the guards, the ``router.acknowledge()`` evidence merge,
and the cycle_summary integration so the lane cannot widen its own scope
in a future refactor.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    opportunity_miner_sensor,
)
from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (  # noqa: F401
    OpportunityMinerSensor,
)
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Records ingest + acknowledge calls. Mimics the real router contract."""

    def __init__(
        self,
        *,
        ingest_result: str = "pending_ack",
        acknowledge_returns: bool = True,
    ) -> None:
        self._ingest_result = ingest_result
        self._acknowledge_returns = acknowledge_returns
        self.ingested: List[Any] = []
        self.acknowledged: List[Tuple[str, Optional[dict]]] = []

    async def ingest(self, envelope: Any) -> str:
        self.ingested.append(envelope)
        return self._ingest_result

    async def acknowledge(
        self,
        idempotency_key: str,
        *,
        extra_evidence: Optional[dict] = None,
    ) -> bool:
        self.acknowledged.append((idempotency_key, extra_evidence))
        return self._acknowledge_returns


class _FakeGraph:
    def __init__(self, graph_id: str = "g_test") -> None:
        self.graph_id = graph_id


class _FakeBatch:
    """Duck-typed CoalescedBatch."""

    def __init__(
        self,
        *,
        target_files: Tuple[str, ...],
        graph_id: str = "g_test_001",
        submitted: bool = False,
    ) -> None:
        self.graph = _FakeGraph(graph_id)
        self.target_files = target_files
        self.description = (
            f"Coalesced refactor of {len(target_files)} test candidate(s): "
            f"{target_files[0]}"
        )
        self.confidence = 0.6
        self.envelope_evidence = {
            "coalesced_graph": True,
            "strategy": "complexity",
            "unit_count": len(target_files),
            "graph_id": graph_id,
        }
        self.submitted_to_scheduler = submitted


def _make_files(n: int, prefix: str = "backend/core/m") -> Tuple[str, ...]:
    return tuple(f"{prefix}{i}.py" for i in range(n))


# ---------------------------------------------------------------------------
# Module reload fixture (for env-var driven constants)
# ---------------------------------------------------------------------------


@pytest.fixture
def lane_module(monkeypatch: pytest.MonkeyPatch):
    """Reload the sensor module so env-var changes take effect.

    The lane constants (``_AUTO_ACK_LANE_ENABLED``, etc.) are read at import
    time, so each test that needs a non-default value reloads the module
    after monkeypatching the env. Restored at teardown.
    """
    yield
    # Restore defaults by clearing env + reloading
    for var in (
        "JARVIS_MINER_AUTO_ACK_LANE",
        "JARVIS_MINER_AUTO_ACK_MIN_FILES",
        "JARVIS_MINER_AUTO_ACK_MAX_FILES",
    ):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(opportunity_miner_sensor)


def _enable_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_MINER_AUTO_ACK_LANE", "true")
    importlib.reload(opportunity_miner_sensor)


# ---------------------------------------------------------------------------
# _check_auto_ack_lane: pure unit tests on the guard predicate
# ---------------------------------------------------------------------------


class TestCheckAutoAckLane:
    def test_lane_disabled_by_default(self):
        """The lane MUST be off out of the box (safety default)."""
        importlib.reload(opportunity_miner_sensor)
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(
            ("backend/a.py", "backend/b.py", "backend/c.py")
        )
        assert ok is False
        assert reason == "lane_disabled"

    def test_below_min_files_blocked(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        """2-file batches must NEVER use the lane (would route NOTIFY_APPLY)."""
        _enable_lane(monkeypatch)
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(
            ("backend/a.py", "backend/b.py")
        )
        assert ok is False
        assert reason == "below_min_files"

    def test_min_files_three_passes(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        """3 files matches too_many_files boundary → APPROVAL_REQUIRED → safe."""
        _enable_lane(monkeypatch)
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(
            ("backend/a.py", "backend/b.py", "backend/c.py")
        )
        assert ok is True
        assert reason == "ok"

    def test_max_files_eight_passes(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = _make_files(8)
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is True
        assert reason == "ok"

    def test_above_max_files_blocked(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = _make_files(9)
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "above_max_files"

    def test_scripts_path_not_allowed(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        """v1 explicitly excludes scripts/ — broader blast radius than backend/."""
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "backend/b.py", "scripts/migrate.py")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "path_not_allowed"

    def test_arbitrary_path_not_allowed(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "backend/b.py", "frontend/app.tsx")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "path_not_allowed"

    def test_supervisor_fragment_blocked(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "backend/b.py", "backend/unified_supervisor.py")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "forbidden_fragment"

    def test_auth_fragment_blocked(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "backend/b.py", "backend/auth/login.py")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "forbidden_fragment"

    def test_credential_fragment_blocked(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "backend/b.py", "backend/services/credentials_loader.py")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "forbidden_fragment"

    def test_token_fragment_blocked(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "backend/b.py", "backend/api/token_refresh.py")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "forbidden_fragment"

    def test_dot_env_fragment_blocked(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "backend/b.py", "backend/config/.env_loader.py")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is False
        assert reason == "forbidden_fragment"

    def test_tests_prefix_allowed(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        files = ("backend/a.py", "tests/fixtures/b.py", "tests/fixtures/c.py")
        ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(files)
        assert ok is True
        assert reason == "ok"


# ---------------------------------------------------------------------------
# FORBIDDEN_PATH integration via the global provider hook
# ---------------------------------------------------------------------------


class TestForbiddenPathProvider:

    def test_provider_hit_blocks_lane(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        """A FORBIDDEN_PATH substring registered via the global provider must block."""
        _enable_lane(monkeypatch)
        from backend.core.ouroboros.governance import user_preference_memory

        def fake_provider() -> List[str]:
            return ["backend/sensitive/"]

        user_preference_memory.register_protected_path_provider(fake_provider)
        try:
            ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(
                ("backend/a.py", "backend/b.py", "backend/sensitive/data.py")
            )
            assert ok is False
            assert reason == "forbidden_user_pref"
        finally:
            user_preference_memory.register_protected_path_provider(None)

    def test_provider_miss_allows_lane(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        _enable_lane(monkeypatch)
        from backend.core.ouroboros.governance import user_preference_memory

        def fake_provider() -> List[str]:
            return ["backend/totally_unrelated/"]

        user_preference_memory.register_protected_path_provider(fake_provider)
        try:
            ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(
                ("backend/a.py", "backend/b.py", "backend/c.py")
            )
            assert ok is True
            assert reason == "ok"
        finally:
            user_preference_memory.register_protected_path_provider(None)

    def test_provider_raising_is_fault_isolated(self, lane_module, monkeypatch: pytest.MonkeyPatch):
        """A broken provider must not break the lane (defense in depth)."""
        _enable_lane(monkeypatch)
        from backend.core.ouroboros.governance import user_preference_memory

        def broken_provider() -> List[str]:
            raise RuntimeError("provider exploded")

        user_preference_memory.register_protected_path_provider(broken_provider)
        try:
            ok, reason = opportunity_miner_sensor.OpportunityMinerSensor._check_auto_ack_lane(
                ("backend/a.py", "backend/b.py", "backend/c.py")
            )
            # Provider failed → fall through → still ok
            assert ok is True
            assert reason == "ok"
        finally:
            user_preference_memory.register_protected_path_provider(None)


# ---------------------------------------------------------------------------
# _ingest_coalesced_batch + lane integration with the FakeRouter
# ---------------------------------------------------------------------------


class TestIngestCoalescedBatchLaneIntegration:

    @pytest.mark.asyncio
    async def test_lane_off_pending_ack_only(self, tmp_path: Path):
        """Default config: pending_ack stays parked, no acknowledge call."""
        importlib.reload(opportunity_miner_sensor)
        # Use the freshly-reloaded sensor class
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter(ingest_result="pending_ack")
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        batch = _FakeBatch(target_files=_make_files(3))
        counters = opportunity_miner_sensor._CycleCounters(mined=10, eligible=10, selected=3)
        await sensor._ingest_coalesced_batch(batch, [], "complexity", counters)

        assert counters.pending_ack == 1
        assert counters.enqueued == 0
        assert counters.auto_acked == 0
        assert router.acknowledged == []  # lane never fired

    @pytest.mark.asyncio
    async def test_lane_on_n3_rescues_to_enqueued(
        self, tmp_path: Path, lane_module, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_lane(monkeypatch)
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter(ingest_result="pending_ack", acknowledge_returns=True)
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        batch = _FakeBatch(target_files=_make_files(3), graph_id="g_n3")
        counters = opportunity_miner_sensor._CycleCounters(mined=10, eligible=10, selected=3)
        await sensor._ingest_coalesced_batch(batch, [], "complexity", counters)

        assert counters.pending_ack == 1   # first ingest hit the gate
        assert counters.enqueued == 1      # lane re-ingested successfully
        assert counters.auto_acked == 1    # the rescue itself
        assert len(router.acknowledged) == 1
        idempotency_key, extra_evidence = router.acknowledged[0]
        assert idempotency_key  # non-empty
        assert extra_evidence is not None
        assert extra_evidence["auto_acked"] is True
        assert extra_evidence["auto_ack_reason"] == "miner_graph_lane"
        assert extra_evidence["auto_ack_graph_id"] == "g_n3"
        assert extra_evidence["auto_ack_file_count"] == 3

    @pytest.mark.asyncio
    async def test_lane_on_n2_stays_pending_ack(
        self, tmp_path: Path, lane_module, monkeypatch: pytest.MonkeyPatch,
    ):
        """2-file graphs (min_units=2) must NOT be rescued — documented intentional."""
        _enable_lane(monkeypatch)
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter(ingest_result="pending_ack")
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        batch = _FakeBatch(target_files=_make_files(2))
        counters = opportunity_miner_sensor._CycleCounters(mined=10, eligible=10, selected=2)
        await sensor._ingest_coalesced_batch(batch, [], "complexity", counters)

        assert counters.pending_ack == 1
        assert counters.enqueued == 0
        assert counters.auto_acked == 0
        assert router.acknowledged == []

    @pytest.mark.asyncio
    async def test_lane_on_n8_rescues(
        self, tmp_path: Path, lane_module, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_lane(monkeypatch)
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter(ingest_result="pending_ack", acknowledge_returns=True)
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        batch = _FakeBatch(target_files=_make_files(8), graph_id="g_n8")
        counters = opportunity_miner_sensor._CycleCounters(mined=20, eligible=20, selected=8)
        await sensor._ingest_coalesced_batch(batch, [], "complexity", counters)

        assert counters.pending_ack == 1
        assert counters.enqueued == 1
        assert counters.auto_acked == 1
        n8_extra = router.acknowledged[0][1]
        assert n8_extra is not None
        assert n8_extra["auto_ack_file_count"] == 8

    @pytest.mark.asyncio
    async def test_lane_on_n9_stays_pending_ack(
        self, tmp_path: Path, lane_module, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_lane(monkeypatch)
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter(ingest_result="pending_ack")
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        batch = _FakeBatch(target_files=_make_files(9))
        counters = opportunity_miner_sensor._CycleCounters(mined=20, eligible=20, selected=9)
        await sensor._ingest_coalesced_batch(batch, [], "complexity", counters)

        assert counters.pending_ack == 1
        assert counters.enqueued == 0
        assert counters.auto_acked == 0
        assert router.acknowledged == []

    @pytest.mark.asyncio
    async def test_lane_on_supervisor_path_blocked(
        self, tmp_path: Path, lane_module, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_lane(monkeypatch)
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter(ingest_result="pending_ack")
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        batch = _FakeBatch(
            target_files=("backend/a.py", "backend/b.py", "backend/unified_supervisor.py"),
        )
        counters = opportunity_miner_sensor._CycleCounters(mined=10, eligible=10, selected=3)
        await sensor._ingest_coalesced_batch(batch, [], "complexity", counters)

        assert counters.pending_ack == 1
        assert counters.auto_acked == 0
        assert router.acknowledged == []

    @pytest.mark.asyncio
    async def test_lane_reingest_failure_does_not_crash(
        self, tmp_path: Path, lane_module, monkeypatch: pytest.MonkeyPatch,
    ):
        """If router.acknowledge() returns False (re-ingest failed), the lane logs and moves on."""
        _enable_lane(monkeypatch)
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter(ingest_result="pending_ack", acknowledge_returns=False)
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        batch = _FakeBatch(target_files=_make_files(3))
        counters = opportunity_miner_sensor._CycleCounters(mined=10, eligible=10, selected=3)
        result = await sensor._ingest_coalesced_batch(batch, [], "complexity", counters)

        assert counters.pending_ack == 1
        assert counters.enqueued == 0
        assert counters.auto_acked == 0
        assert len(router.acknowledged) == 1  # lane DID try
        # And the function should still return something coherent
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# cycle_summary integration: the new auto_acked key
# ---------------------------------------------------------------------------


class TestCycleSummaryAutoAckedKey:

    def test_summary_includes_auto_acked_key(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        importlib.reload(opportunity_miner_sensor)
        SensorCls = opportunity_miner_sensor.OpportunityMinerSensor
        router = _FakeRouter()
        sensor = SensorCls(
            repo_root=tmp_path,
            router=router,
            scan_paths=["backend"],
            repo="jarvis",
            max_candidates_per_scan=10,
        )
        counters = opportunity_miner_sensor._CycleCounters(
            mined=5, eligible=4, selected=3,
            graph_built=1, graph_submitted=1,
            enqueued=1, pending_ack=1, auto_acked=1,
        )
        with caplog.at_level(logging.INFO, logger=opportunity_miner_sensor.__name__):
            sensor._emit_cycle_summary(counters, "complexity")

        # Single line, contains auto_acked=1
        summary_lines = [r for r in caplog.records if "cycle_summary" in r.getMessage()]
        assert len(summary_lines) == 1
        msg = summary_lines[0].getMessage()
        assert "auto_acked=1" in msg
        # Sanity: full key set still present
        for key in (
            "mined=", "eligible=", "selected=",
            "graph_built=", "graph_submitted=",
            "enqueued=", "pending_ack=", "queued_behind=",
            "deduplicated=", "backpressure=", "auto_acked=",
        ):
            assert key in msg


# ---------------------------------------------------------------------------
# Integration: real UnifiedIntakeRouter.acknowledge() with extra_evidence
# ---------------------------------------------------------------------------


class TestRouterAcknowledgeExtraEvidence:
    """Higher-fidelity test that the real router preserves extra_evidence
    through the re-ingest path. Anchors the API contract the lane depends on.
    """

    @pytest.mark.asyncio
    async def test_extra_evidence_merges_into_queued_envelope(self, tmp_path: Path):
        gls = MagicMock()
        gls.submit = MagicMock()
        config = IntakeRouterConfig(project_root=tmp_path)
        router = UnifiedIntakeRouter(gls=gls, config=config)
        await router.start()
        try:
            env = make_envelope(
                source="ai_miner",
                description="test miner batch",
                target_files=("backend/a.py", "backend/b.py", "backend/c.py"),
                repo="jarvis",
                confidence=0.5,
                urgency="low",
                evidence={"coalesced_graph": True, "strategy": "complexity"},
                requires_human_ack=True,
            )
            result = await router.ingest(env)
            assert result == "pending_ack"
            assert router.pending_ack_count() == 1

            # Acknowledge with extra evidence
            extra = {
                "auto_acked": True,
                "auto_ack_reason": "miner_graph_lane",
                "auto_ack_graph_id": "g_real",
                "auto_ack_file_count": 3,
            }
            released = await router.acknowledge(
                env.idempotency_key, extra_evidence=extra,
            )
            assert released is True
            assert router.pending_ack_count() == 0

            # Pull the queued envelope off the priority queue and assert
            # the merged evidence survived.
            queue = router._queue  # type: ignore[attr-defined]
            assert queue.qsize() == 1
            _priority, _ts, queued = queue.get_nowait()
            assert queued.requires_human_ack is False  # cleared
            assert queued.evidence["coalesced_graph"] is True  # original survived
            assert queued.evidence["strategy"] == "complexity"
            assert queued.evidence["auto_acked"] is True
            assert queued.evidence["auto_ack_reason"] == "miner_graph_lane"
            assert queued.evidence["auto_ack_graph_id"] == "g_real"
            assert queued.evidence["auto_ack_file_count"] == 3
        finally:
            await router.stop()

    @pytest.mark.asyncio
    async def test_acknowledge_without_extra_evidence_unchanged(self, tmp_path: Path):
        """Backwards compat: acknowledge() without extra_evidence still works."""
        gls = MagicMock()
        gls.submit = MagicMock()
        config = IntakeRouterConfig(project_root=tmp_path)
        router = UnifiedIntakeRouter(gls=gls, config=config)
        await router.start()
        try:
            env = make_envelope(
                source="ai_miner",
                description="legacy ack path",
                target_files=("backend/a.py",),
                repo="jarvis",
                confidence=0.5,
                urgency="low",
                evidence={"signature": "legacy"},
                requires_human_ack=True,
            )
            await router.ingest(env)
            released = await router.acknowledge(env.idempotency_key)
            assert released is True
            queue = router._queue  # type: ignore[attr-defined]
            assert queue.qsize() == 1
            _p, _t, queued = queue.get_nowait()
            assert queued.evidence["signature"] == "legacy"
            assert "auto_acked" not in queued.evidence
        finally:
            await router.stop()
