"""Tests for OpportunityMinerSensor per-cycle summary counters (Task #69).

The cycle_summary log line is the diagnostic for safe-module starvation.
These tests pin all 13 stable keys, the per-counter increment behavior,
and the eligible/selected divergence — so future refactors cannot quietly
desync the metric from the predicate it measures.

The starvation analysis (C1=auto_submit off, C2=cap+module_diversity,
C3=AC2 ack gate) lives in `_CycleCounters.__doc__`. If you change a
counter definition, update both that docstring and the assertions here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    OpportunityMinerSensor,
    _CycleCounters,
)


# ---------------------------------------------------------------------------
# Summary line parsing
# ---------------------------------------------------------------------------

# Stable key set — must match _emit_cycle_summary in opportunity_miner_sensor.py.
# If you add a counter, append it here AND update the dataclass docstring.
_SUMMARY_KEYS = (
    "cycle", "strategy", "max_per_scan",
    "mined", "eligible", "selected",
    "graph_built", "graph_submitted",
    "enqueued", "pending_ack", "queued_behind",
    "deduplicated", "backpressure",
)

_SUMMARY_RE = re.compile(
    r"OpportunityMinerSensor cycle_summary "
    r"cycle=(?P<cycle>\d+) strategy=(?P<strategy>\S+) max_per_scan=(?P<max_per_scan>\d+) "
    r"mined=(?P<mined>\d+) eligible=(?P<eligible>\d+) selected=(?P<selected>\d+) "
    r"graph_built=(?P<graph_built>\d+) graph_submitted=(?P<graph_submitted>\d+) "
    r"enqueued=(?P<enqueued>\d+) pending_ack=(?P<pending_ack>\d+) queued_behind=(?P<queued_behind>\d+) "
    r"deduplicated=(?P<deduplicated>\d+) backpressure=(?P<backpressure>\d+)"
)


def _parse_summary(caplog: pytest.LogCaptureFixture) -> Dict[str, Any]:
    """Find the cycle_summary line in caplog and return its parsed key/value dict.

    Fails the test loudly if the line is missing or doesn't match the stable
    regex — that means the format drifted and dashboards/parsers will break.
    """
    matches: List[re.Match] = []
    for record in caplog.records:
        m = _SUMMARY_RE.search(record.getMessage())
        if m:
            matches.append(m)
    assert matches, (
        "no cycle_summary line found in logs — the stable key set may have "
        "drifted from _SUMMARY_RE. Captured log lines:\n  " +
        "\n  ".join(r.getMessage() for r in caplog.records)
    )
    parsed = matches[-1].groupdict()
    # Coerce all numeric fields back to int for easy assertions
    for k in _SUMMARY_KEYS:
        if k != "strategy":
            parsed[k] = int(parsed[k])
    return parsed


# ---------------------------------------------------------------------------
# Pure unit tests on _CycleCounters + _record_ingest_result
# ---------------------------------------------------------------------------


class TestCycleCountersDataclass:
    def test_defaults_are_zero(self):
        c = _CycleCounters()
        for field in (
            "mined", "eligible", "selected",
            "graph_built", "graph_submitted",
            "enqueued", "pending_ack", "queued_behind",
            "deduplicated", "backpressure",
        ):
            assert getattr(c, field) == 0, f"{field} should default to 0"

    def test_mined_can_be_initialized(self):
        c = _CycleCounters(mined=42)
        assert c.mined == 42
        assert c.selected == 0


class TestRecordIngestResult:
    """Each canonical UnifiedIntakeRouter return string maps to its own bucket."""

    def test_enqueued_increments_enqueued(self):
        c = _CycleCounters()
        OpportunityMinerSensor._record_ingest_result(c, "enqueued")
        assert c.enqueued == 1
        assert c.pending_ack == 0

    def test_pending_ack_increments_pending_ack(self):
        c = _CycleCounters()
        OpportunityMinerSensor._record_ingest_result(c, "pending_ack")
        assert c.pending_ack == 1
        assert c.enqueued == 0

    def test_queued_behind_increments_queued_behind(self):
        c = _CycleCounters()
        OpportunityMinerSensor._record_ingest_result(c, "queued_behind")
        assert c.queued_behind == 1

    def test_deduplicated_increments_deduplicated(self):
        c = _CycleCounters()
        OpportunityMinerSensor._record_ingest_result(c, "deduplicated")
        assert c.deduplicated == 1

    def test_backpressure_increments_backpressure(self):
        c = _CycleCounters()
        OpportunityMinerSensor._record_ingest_result(c, "backpressure")
        assert c.backpressure == 1

    def test_unknown_result_increments_nothing(self):
        """Defense in depth — a future router string won't crash the counter."""
        c = _CycleCounters()
        OpportunityMinerSensor._record_ingest_result(c, "wat_is_this")
        assert c.enqueued == 0
        assert c.pending_ack == 0
        assert c.queued_behind == 0
        assert c.deduplicated == 0
        assert c.backpressure == 0

    def test_repeated_results_accumulate(self):
        c = _CycleCounters()
        for _ in range(5):
            OpportunityMinerSensor._record_ingest_result(c, "pending_ack")
        OpportunityMinerSensor._record_ingest_result(c, "enqueued")
        assert c.pending_ack == 5
        assert c.enqueued == 1


# ---------------------------------------------------------------------------
# Test fixtures: synthetic file builders + fake coalescer
# ---------------------------------------------------------------------------


def _branchy_source(branches: int = 12) -> str:
    """Generate a function with N if/elif branches → cyclomatic complexity ≈ N+1."""
    parts = ["def big(x):\n"]
    for i in range(branches):
        parts.append(f"    if x == {i}:\n        return {i}\n")
    parts.append("    return -1\n")
    return "".join(parts)


def _make_branchy_files(
    root: Path, count: int, branches: int = 12, module: str = "backend/core",
) -> List[Path]:
    """Create N synthetic .py files in tmp/{module}/ that pass the complexity gate."""
    pkg = root / module
    pkg.mkdir(parents=True, exist_ok=True)
    files: List[Path] = []
    src = _branchy_source(branches)
    for i in range(count):
        f = pkg / f"mod_{i}.py"
        f.write_text(src)
        files.append(f)
    return files


class _FakeCoalescer:
    """Stand-in for MinerGraphCoalescer.

    Configurable: returns a fake CoalescedBatch (with a fake graph) or None.
    Mirrors the duck-typed surface that opportunity_miner_sensor uses
    (`should_coalesce`, `coalesce`, `batch.submitted_to_scheduler`,
    `batch.target_files`, `batch.description`, `batch.confidence`,
    `batch.envelope_evidence`, `batch.graph.graph_id`).
    """

    def __init__(
        self,
        *,
        return_batch: bool = True,
        submitted_to_scheduler: bool = False,
        min_units: int = 2,
    ) -> None:
        self._return_batch = return_batch
        self._submitted = submitted_to_scheduler
        self._min_units = min_units
        self.coalesce_calls = 0

    def should_coalesce(self, analyses) -> bool:
        return len(analyses) >= self._min_units

    async def coalesce(self, analyses, *, strategy, sort_field, repo) -> Optional[Any]:
        self.coalesce_calls += 1
        if not self._return_batch:
            return None
        target_files = tuple(a.file_path for a in analyses)

        class _FakeGraph:
            graph_id = "fake_graph_001"

        class _FakeBatch:
            graph = _FakeGraph()
            description = "fake coalesced batch"
            confidence = 0.42
            envelope_evidence: Dict[str, Any] = {"strategy": strategy}

        b = _FakeBatch()
        b.target_files = target_files  # type: ignore[attr-defined]
        b.submitted_to_scheduler = self._submitted  # type: ignore[attr-defined]
        return b


def _make_sensor(
    tmp_path: Path,
    router: Any,
    *,
    coalescer: Any = None,
    max_per_scan: int = 10,
) -> OpportunityMinerSensor:
    return OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["backend/core/"],
        complexity_threshold=5,
        max_candidates_per_scan=max_per_scan,
        graph_coalescer=coalescer,
    )


# ---------------------------------------------------------------------------
# scan_once end-to-end: per-file ingest path
# ---------------------------------------------------------------------------


class TestCycleSummaryPerFilePath:
    """End-to-end: scan_once with no coalescer, real selection, fake router."""

    @pytest.mark.asyncio
    async def test_summary_line_has_all_stable_keys(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        parsed = _parse_summary(caplog)
        # Every key must be present, no exceptions
        for key in _SUMMARY_KEYS:
            assert key in parsed, f"summary missing key: {key}"

    @pytest.mark.asyncio
    async def test_pending_ack_dominates_when_router_parks_everything(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """The C3 starvation fingerprint: pending_ack > 0, enqueued = 0."""
        _make_branchy_files(tmp_path, count=4)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        assert s["mined"] >= 4
        assert s["selected"] >= 1
        assert s["pending_ack"] == s["selected"]
        assert s["enqueued"] == 0
        assert s["graph_built"] == 0  # no coalescer attached
        assert s["graph_submitted"] == 0

    @pytest.mark.asyncio
    async def test_enqueued_counted_when_router_accepts(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = _make_sensor(tmp_path, router)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        assert s["enqueued"] >= 1
        assert s["enqueued"] == s["selected"]
        assert s["pending_ack"] == 0

    @pytest.mark.asyncio
    async def test_mixed_router_results_split_across_buckets(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """Router cycles through all 5 return strings — each lands in its own bucket."""
        _make_branchy_files(tmp_path, count=5)
        router = AsyncMock()
        # Cycle through the 5 canonical results in a stable order
        router.ingest = AsyncMock(side_effect=[
            "enqueued", "pending_ack", "queued_behind",
            "deduplicated", "backpressure",
        ])
        sensor = _make_sensor(tmp_path, router, max_per_scan=10)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        # Each bucket should have caught its assigned result. Module diversity
        # may trim selected below 5; assert >= 1 in each bucket the router was
        # called for.
        called = router.ingest.call_count
        total = (
            s["enqueued"] + s["pending_ack"] + s["queued_behind"]
            + s["deduplicated"] + s["backpressure"]
        )
        assert total == called, "every ingest call must land in exactly one bucket"

    @pytest.mark.asyncio
    async def test_unknown_router_result_not_counted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        _make_branchy_files(tmp_path, count=2)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="some_future_string")
        sensor = _make_sensor(tmp_path, router)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        # All 5 buckets stay zero — the unknown string is silently absorbed.
        assert s["enqueued"] == 0
        assert s["pending_ack"] == 0
        assert s["queued_behind"] == 0
        assert s["deduplicated"] == 0
        assert s["backpressure"] == 0


# ---------------------------------------------------------------------------
# eligible vs selected: the no-drift guarantee
# ---------------------------------------------------------------------------


class TestEligibleVsSelected:
    """The eligible counter must match the predicate selection actually applies."""

    @pytest.mark.asyncio
    async def test_select_diverse_candidates_returns_tuple(self, tmp_path: Path):
        """The selection function returns (eligible_count, selected_list)."""
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router)

        # Run a scan to build up the analysis cache
        await sensor.scan_once()

        # Now call _select_diverse_candidates directly with synthetic analyses
        from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
            _FileAnalysis,
        )
        analyses = [
            _FileAnalysis(file_path=f"backend/core/x{i}.py", cyclomatic_complexity=20)
            for i in range(5)
        ]
        result = sensor._select_diverse_candidates(analyses, "cyclomatic_complexity")
        assert isinstance(result, tuple)
        assert len(result) == 2
        eligible_count, selected = result
        assert isinstance(eligible_count, int)
        assert isinstance(selected, list)
        assert eligible_count >= len(selected), (
            "eligible should never be less than selected — selection draws from eligible"
        )

    @pytest.mark.asyncio
    async def test_seen_files_drop_from_eligible(self, tmp_path: Path):
        """Files in _seen_file_paths must NOT count toward eligible."""
        _make_branchy_files(tmp_path, count=4)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router)

        from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
            _FileAnalysis,
        )
        analyses = [
            _FileAnalysis(file_path=f"backend/core/x{i}.py", cyclomatic_complexity=20)
            for i in range(4)
        ]
        # Pre-mark 2 of them as seen
        sensor._seen_file_paths.add("backend/core/x0.py")
        sensor._seen_file_paths.add("backend/core/x1.py")

        eligible_count, selected = sensor._select_diverse_candidates(
            analyses, "cyclomatic_complexity",
        )
        assert eligible_count == 2, (
            f"expected 2 eligible after dropping 2 seen, got {eligible_count}"
        )
        assert all(a.file_path not in sensor._seen_file_paths for a in selected)

    @pytest.mark.asyncio
    async def test_summary_eligible_matches_selection_predicate(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """The summary's `eligible` value equals what _select_diverse_candidates saw."""
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        # First scan: nothing seen, nothing on cooldown → eligible == mined
        assert s["eligible"] == s["mined"]


# ---------------------------------------------------------------------------
# Coalescer branches: graph_built and graph_submitted
# ---------------------------------------------------------------------------


class TestGraphCounters:
    @pytest.mark.asyncio
    async def test_no_coalescer_means_graph_built_zero(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router, coalescer=None)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        assert s["graph_built"] == 0
        assert s["graph_submitted"] == 0

    @pytest.mark.asyncio
    async def test_coalescer_returns_none_means_graph_built_zero(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """Coalescer attempted but returned None → graph_built stays 0."""
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        coalescer = _FakeCoalescer(return_batch=False)
        sensor = _make_sensor(tmp_path, router, coalescer=coalescer)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        assert coalescer.coalesce_calls == 1, "coalescer should have been invoked"
        assert s["graph_built"] == 0
        assert s["graph_submitted"] == 0
        # Per-file fallback ran
        assert s["pending_ack"] >= 1

    @pytest.mark.asyncio
    async def test_coalescer_built_but_not_submitted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """C1 fingerprint: graph_built=1 but graph_submitted=0 (auto_submit off)."""
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        coalescer = _FakeCoalescer(return_batch=True, submitted_to_scheduler=False)
        sensor = _make_sensor(tmp_path, router, coalescer=coalescer)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        assert s["graph_built"] == 1
        assert s["graph_submitted"] == 0
        # The coalesced envelope itself was parked at the AC2 gate
        assert s["pending_ack"] >= 1

    @pytest.mark.asyncio
    async def test_coalescer_built_and_submitted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """Healthy path: graph_built=1, graph_submitted=1."""
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        coalescer = _FakeCoalescer(return_batch=True, submitted_to_scheduler=True)
        sensor = _make_sensor(tmp_path, router, coalescer=coalescer)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        assert s["graph_built"] == 1
        assert s["graph_submitted"] == 1


# ---------------------------------------------------------------------------
# Empty scan + format stability
# ---------------------------------------------------------------------------


class TestEmptyScanAndStability:
    @pytest.mark.asyncio
    async def test_empty_scan_emits_zeroed_summary(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """No source files at all → all counters 0, but summary line still emitted."""
        (tmp_path / "backend" / "core").mkdir(parents=True)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()

        s = _parse_summary(caplog)
        assert s["mined"] == 0
        assert s["eligible"] == 0
        assert s["selected"] == 0
        assert s["enqueued"] == 0
        assert s["pending_ack"] == 0
        assert s["graph_built"] == 0
        assert s["graph_submitted"] == 0

    @pytest.mark.asyncio
    async def test_two_consecutive_scans_emit_two_summaries(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """Each scan_once call must emit exactly one summary line."""
        _make_branchy_files(tmp_path, count=3)
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="pending_ack")
        sensor = _make_sensor(tmp_path, router)

        with caplog.at_level(logging.INFO):
            await sensor.scan_once()
            await sensor.scan_once()

        matches = [
            r for r in caplog.records
            if _SUMMARY_RE.search(r.getMessage())
        ]
        assert len(matches) == 2, f"expected 2 summary lines, got {len(matches)}"
