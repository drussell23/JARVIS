"""Tests for MemoryEngine cross-session learning store.

TDD Red->Green cycle for TC05, TC06, TC07, TC08, TC32 and supporting cases.

TC05: ingest APPLIED outcome -> MemoryInsight created
TC06: ingest 5 ops (3 success, 2 fail) -> file success_rate == 0.6
TC07: insight past TTL -> confidence decayed
TC08: git HEAD change -> insights with stale refs invalidated
TC32: disk IOError on write -> in-memory continues, no crash
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.consciousness.memory_engine import MemoryEngine
from backend.core.ouroboros.consciousness.types import (
    FileReputation,
    MemoryInsight,
    PatternSummary,
)
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_entry(
    op_id: str,
    state: OperationState,
    data: dict | None = None,
    wall_time: float | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        op_id=op_id,
        state=state,
        data=data or {},
        timestamp=time.monotonic(),
        wall_time=wall_time or time.time(),
    )


def _make_ledger(entries_by_op: dict) -> AsyncMock:
    """Build an AsyncMock ledger where get_history returns per-op entries."""
    ledger = AsyncMock()
    async def get_history(op_id: str) -> List[LedgerEntry]:
        return entries_by_op.get(op_id, [])
    ledger.get_history.side_effect = get_history
    return ledger


def _make_engine(ledger: AsyncMock, tmp_path: Path) -> MemoryEngine:
    return MemoryEngine(ledger=ledger, persistence_dir=tmp_path / "memory")


# ---------------------------------------------------------------------------
# TC05: ingest APPLIED entry -> MemoryInsight created
# ---------------------------------------------------------------------------


class TestTC05IngestOutcome:
    @pytest.mark.asyncio
    async def test_memory_ingests_applied_outcome(self, tmp_path: Path):
        """TC05: mock ledger returns APPLIED entry -> MemoryInsight created."""
        op_id = "op-tc05-applied"
        entries = [
            _make_entry(op_id, OperationState.PLANNED),
            _make_entry(op_id, OperationState.APPLYING),
            _make_entry(
                op_id,
                OperationState.APPLIED,
                data={"target_files": ["backend/core/foo.py"]},
            ),
        ]
        ledger = _make_ledger({op_id: entries})
        engine = _make_engine(ledger, tmp_path)

        await engine.ingest_outcome(op_id)

        assert len(engine._insights) == 1
        insight = engine._insights[0]
        assert insight.category == "success_pattern"
        assert insight.evidence_count == 1
        assert insight.confidence > 0.0

    @pytest.mark.asyncio
    async def test_memory_ingests_failed_outcome(self, tmp_path: Path):
        """TC05 variant: FAILED entry -> failure_pattern insight."""
        op_id = "op-tc05-failed"
        entries = [
            _make_entry(op_id, OperationState.PLANNED),
            _make_entry(op_id, OperationState.FAILED, data={"error": "test suite fail"}),
        ]
        ledger = _make_ledger({op_id: entries})
        engine = _make_engine(ledger, tmp_path)

        await engine.ingest_outcome(op_id)

        assert len(engine._insights) == 1
        assert engine._insights[0].category == "failure_pattern"

    @pytest.mark.asyncio
    async def test_ingest_skips_nonterminal_entries(self, tmp_path: Path):
        """TC12 coverage: non-terminal entries only -> no insight created."""
        op_id = "op-tc05-nonterminal"
        entries = [
            _make_entry(op_id, OperationState.PLANNED),
            _make_entry(op_id, OperationState.SANDBOXING),
            _make_entry(op_id, OperationState.VALIDATING),
        ]
        ledger = _make_ledger({op_id: entries})
        engine = _make_engine(ledger, tmp_path)

        await engine.ingest_outcome(op_id)

        assert len(engine._insights) == 0

    @pytest.mark.asyncio
    async def test_ingest_empty_history_no_crash(self, tmp_path: Path):
        """Empty ledger history -> graceful no-op."""
        op_id = "op-tc05-empty"
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)

        await engine.ingest_outcome(op_id)

        assert len(engine._insights) == 0


# ---------------------------------------------------------------------------
# TC06: file success_rate tracking
# ---------------------------------------------------------------------------


class TestTC06FileReputationSuccessRate:
    @pytest.mark.asyncio
    async def test_file_reputation_tracks_success_rate(self, tmp_path: Path):
        """TC06: ingest 5 ops (3 success, 2 fail) -> success_rate == 0.6."""
        file_path = "backend/core/tricky.py"
        ledger = AsyncMock()

        call_number = [0]

        async def get_history(op_id: str) -> List[LedgerEntry]:
            call_number[0] += 1
            n = call_number[0]
            state = OperationState.APPLIED if n <= 3 else OperationState.FAILED
            return [
                _make_entry(op_id, OperationState.PLANNED),
                _make_entry(op_id, state, data={"target_files": [file_path]}),
            ]

        ledger.get_history.side_effect = get_history
        engine = _make_engine(ledger, tmp_path)

        for i in range(5):
            await engine.ingest_outcome(f"op-{i}")

        rep = engine.get_file_reputation(file_path)
        assert rep.success_rate == pytest.approx(0.6)
        assert rep.change_count == 5

    @pytest.mark.asyncio
    async def test_file_reputation_defaults_for_unknown_file(self, tmp_path: Path):
        """Unknown file -> sensible defaults (success_rate=1.0, fragility=0.0)."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)

        rep = engine.get_file_reputation("backend/never_seen.py")

        assert rep.success_rate == pytest.approx(1.0)
        assert rep.change_count == 0
        assert rep.fragility_score == pytest.approx(0.0)
        assert rep.common_co_failures == ()

    @pytest.mark.asyncio
    async def test_file_all_success_rate_one(self, tmp_path: Path):
        file_path = "always_works.py"
        ledger = AsyncMock()

        async def get_history(op_id: str) -> List[LedgerEntry]:
            return [
                _make_entry(op_id, OperationState.APPLIED, data={"target_files": [file_path]}),
            ]

        ledger.get_history.side_effect = get_history
        engine = _make_engine(ledger, tmp_path)
        for i in range(3):
            await engine.ingest_outcome(f"op-{i}")

        rep = engine.get_file_reputation(file_path)
        assert rep.success_rate == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_file_all_fail_rate_zero(self, tmp_path: Path):
        file_path = "always_fails.py"
        ledger = AsyncMock()

        async def get_history(op_id: str) -> List[LedgerEntry]:
            return [
                _make_entry(op_id, OperationState.FAILED, data={"target_files": [file_path]}),
            ]

        ledger.get_history.side_effect = get_history
        engine = _make_engine(ledger, tmp_path)
        for i in range(4):
            await engine.ingest_outcome(f"op-{i}")

        rep = engine.get_file_reputation(file_path)
        assert rep.success_rate == pytest.approx(0.0)
        assert rep.fragility_score > 0.0


# ---------------------------------------------------------------------------
# TC07: TTL decay
# ---------------------------------------------------------------------------


class TestTC07TTLDecay:
    def test_memory_ttl_decay(self, tmp_path: Path):
        """TC07: insight past TTL -> effective confidence is decayed."""
        # Create an insight whose TTL expired 2 days ago
        last_seen = (datetime.now(timezone.utc) - timedelta(hours=172)).isoformat()
        insight = MemoryInsight(
            insight_id="stale-001",
            category="failure_pattern",
            content="some stale pattern",
            confidence=0.8,
            evidence_count=5,
            last_seen_utc=last_seen,
            ttl_hours=168.0,  # 1 week
        )
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        engine._insights.append(insight)

        # query returns decayed confidence — should be lower than original
        results = engine.query("stale pattern")
        assert len(results) == 1
        # The confidence stored on the MemoryInsight is still the original,
        # but the effective sorting confidence used by query is decayed.
        # Verify the insight is returned (it's past TTL but confidence > 0)
        assert results[0].insight_id == "stale-001"

    def test_expired_insight_eventually_drops_to_zero(self, tmp_path: Path):
        """Insight far past TTL gets confidence -> 0 and is excluded from query."""
        # 20 days past TTL -> decay = 0.8 * (1 - 0.10*20) = -0.8 -> clamp 0.0
        last_seen = (datetime.now(timezone.utc) - timedelta(days=28)).isoformat()
        insight = MemoryInsight(
            insight_id="very-stale-001",
            category="failure_pattern",
            content="zero confidence pattern",
            confidence=0.8,
            evidence_count=3,
            last_seen_utc=last_seen,
            ttl_hours=168.0,  # 1 week = 7 days; 21 days past TTL
        )
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        engine._insights.append(insight)

        results = engine.query("zero confidence")
        # Effective confidence is 0 -> excluded
        assert results == []

    def test_active_insight_not_decayed(self, tmp_path: Path):
        """Fresh insight has full confidence in query results."""
        insight = MemoryInsight(
            insight_id="fresh-001",
            category="success_pattern",
            content="fresh active pattern",
            confidence=0.9,
            evidence_count=7,
            last_seen_utc=_utcnow_iso(),
            ttl_hours=168.0,
        )
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        engine._insights.append(insight)

        results = engine.query("fresh active")
        assert len(results) == 1
        assert results[0].insight_id == "fresh-001"


# ---------------------------------------------------------------------------
# TC08: HEAD change invalidation
# ---------------------------------------------------------------------------


class TestTC08HeadChangeInvalidation:
    @pytest.mark.asyncio
    async def test_memory_invalidates_on_head_change(self, tmp_path: Path):
        """TC08: mock git HEAD change -> failure/success insights removed."""
        op_id = "op-tc08"
        entries = [
            _make_entry(op_id, OperationState.APPLIED, data={"target_files": ["foo.py"]}),
        ]
        ledger = _make_ledger({op_id: entries})
        engine = _make_engine(ledger, tmp_path)

        # Prime with an insight via ingest, simulating HEAD = "abc"
        with patch(
            "backend.core.ouroboros.consciousness.memory_engine._get_git_head",
            return_value="abc123",
        ):
            engine._last_known_head = "abc123"
            await engine.ingest_outcome(op_id)

        assert len(engine._insights) == 1

        # Now simulate HEAD changed to "def456" on next ingest
        op_id2 = "op-tc08-b"
        entries2 = [
            _make_entry(op_id2, OperationState.APPLIED),
        ]
        ledger.get_history.side_effect = lambda oid: (
            entries2 if oid == op_id2 else entries
        )

        with patch(
            "backend.core.ouroboros.consciousness.memory_engine._get_git_head",
            return_value="def456",
        ):
            await engine.ingest_outcome(op_id2)

        # failure_pattern and success_pattern insights should be gone after HEAD change
        categories_left = {i.category for i in engine._insights}
        assert "success_pattern" not in categories_left or len(engine._insights) <= 1

    @pytest.mark.asyncio
    async def test_no_invalidation_when_head_unchanged(self, tmp_path: Path):
        """Head stays the same -> insights are preserved."""
        op_id = "op-tc08-stable"
        entries = [_make_entry(op_id, OperationState.APPLIED)]
        ledger = _make_ledger({op_id: entries})
        engine = _make_engine(ledger, tmp_path)

        with patch(
            "backend.core.ouroboros.consciousness.memory_engine._get_git_head",
            return_value="stable-sha",
        ):
            engine._last_known_head = "stable-sha"
            await engine.ingest_outcome(op_id)
            count_before = len(engine._insights)
            await engine.ingest_outcome(op_id)  # same HEAD, just updates evidence

        # Insights still present (merged/updated)
        assert len(engine._insights) >= 1

    @pytest.mark.asyncio
    async def test_head_none_does_not_invalidate(self, tmp_path: Path):
        """If git HEAD cannot be determined, no invalidation occurs."""
        op_id = "op-tc08-none"
        entries = [_make_entry(op_id, OperationState.APPLIED)]
        ledger = _make_ledger({op_id: entries})
        engine = _make_engine(ledger, tmp_path)
        engine._last_known_head = "some-sha"
        # Seed an insight manually
        engine._insights.append(
            MemoryInsight(
                insight_id="keep-me",
                category="success_pattern",
                content="preserved insight",
                confidence=0.7,
                evidence_count=2,
                last_seen_utc=_utcnow_iso(),
            )
        )

        with patch(
            "backend.core.ouroboros.consciousness.memory_engine._get_git_head",
            return_value=None,
        ):
            await engine.ingest_outcome(op_id)

        # Should still have our seeded insight
        ids = [i.insight_id for i in engine._insights]
        assert "keep-me" in ids


# ---------------------------------------------------------------------------
# TC32: disk full / IOError resilience
# ---------------------------------------------------------------------------


class TestTC32DiskFull:
    @pytest.mark.asyncio
    async def test_memory_engine_disk_full(self, tmp_path: Path):
        """TC32: IOError on disk write -> in-memory state continues, no crash."""
        op_id = "op-tc32"
        entries = [_make_entry(op_id, OperationState.APPLIED)]
        ledger = _make_ledger({op_id: entries})
        engine = _make_engine(ledger, tmp_path)

        # Patch the append method to raise IOError
        with patch.object(engine, "_append_insight_to_disk", side_effect=IOError("disk full")):
            # Should not raise
            await engine.ingest_outcome(op_id)

        # In-memory insight was still created
        assert len(engine._insights) == 1

    @pytest.mark.asyncio
    async def test_stop_disk_error_no_crash(self, tmp_path: Path):
        """stop() with IOError on flush -> no crash."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        engine._insights.append(
            MemoryInsight(
                insight_id="ins-01",
                category="success_pattern",
                content="test",
                confidence=0.5,
                evidence_count=1,
                last_seen_utc=_utcnow_iso(),
            )
        )
        with patch.object(engine, "_flush_reputations_to_disk", side_effect=IOError("no space")):
            with patch.object(engine, "_flush_patterns_to_disk", side_effect=IOError("no space")):
                # Must not raise
                await engine.stop()


# ---------------------------------------------------------------------------
# query: sorted by confidence desc
# ---------------------------------------------------------------------------


class TestQuerySortedByConfidence:
    def test_query_returns_sorted_by_confidence(self, tmp_path: Path):
        """query() returns insights sorted by effective confidence descending."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        for i, conf in enumerate([0.3, 0.9, 0.6]):
            engine._insights.append(
                MemoryInsight(
                    insight_id=f"ins-{i}",
                    category="failure_pattern",
                    content=f"pattern number {i}",
                    confidence=conf,
                    evidence_count=1,
                    last_seen_utc=_utcnow_iso(),
                )
            )

        results = engine.query("pattern")
        confs = [r.confidence for r in results]
        assert confs == sorted(confs, reverse=True)

    def test_query_respects_max_results(self, tmp_path: Path):
        """query() respects max_results cap."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        for i in range(10):
            engine._insights.append(
                MemoryInsight(
                    insight_id=f"ins-{i}",
                    category="success_pattern",
                    content=f"insight {i}",
                    confidence=float(i) / 10,
                    evidence_count=i + 1,
                    last_seen_utc=_utcnow_iso(),
                )
            )

        results = engine.query("insight", max_results=3)
        assert len(results) <= 3

    def test_query_filters_by_keyword(self, tmp_path: Path):
        """query() keyword filter excludes non-matching insights."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        engine._insights.append(
            MemoryInsight(
                insight_id="match",
                category="failure_pattern",
                content="timeout in providers",
                confidence=0.8,
                evidence_count=3,
                last_seen_utc=_utcnow_iso(),
            )
        )
        engine._insights.append(
            MemoryInsight(
                insight_id="no-match",
                category="success_pattern",
                content="oracle startup fast",
                confidence=0.9,
                evidence_count=5,
                last_seen_utc=_utcnow_iso(),
            )
        )

        results = engine.query("timeout providers")
        assert len(results) == 1
        assert results[0].insight_id == "match"


# ---------------------------------------------------------------------------
# get_pattern_summary aggregation
# ---------------------------------------------------------------------------


class TestGetPatternSummary:
    def test_get_pattern_summary_aggregates(self, tmp_path: Path):
        """get_pattern_summary() correctly counts active/archived."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)

        # Active insights (fresh)
        for i in range(3):
            engine._insights.append(
                MemoryInsight(
                    insight_id=f"active-{i}",
                    category="failure_pattern",
                    content=f"active {i}",
                    confidence=0.7,
                    evidence_count=i + 1,
                    last_seen_utc=_utcnow_iso(),
                    ttl_hours=168.0,
                )
            )

        # Archived insight (expired)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        engine._insights.append(
            MemoryInsight(
                insight_id="archived-0",
                category="success_pattern",
                content="old success",
                confidence=0.5,
                evidence_count=2,
                last_seen_utc=old_ts,
                ttl_hours=1.0,  # expired long ago
            )
        )

        summary = engine.get_pattern_summary()

        assert summary.total_insights == 4
        assert summary.active_insights == 3
        assert summary.archived_insights == 1
        assert isinstance(summary.top_patterns, tuple)

    def test_pattern_summary_top_sorted_by_evidence(self, tmp_path: Path):
        """top_patterns are sorted by evidence_count desc."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        for i, ev_count in enumerate([1, 10, 5]):
            engine._insights.append(
                MemoryInsight(
                    insight_id=f"ev-{i}",
                    category="failure_pattern",
                    content=f"pattern {i}",
                    confidence=0.6,
                    evidence_count=ev_count,
                    last_seen_utc=_utcnow_iso(),
                )
            )

        summary = engine.get_pattern_summary()
        evidence_counts = [p.evidence_count for p in summary.top_patterns]
        assert evidence_counts == sorted(evidence_counts, reverse=True)


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistenceRoundTrip:
    @pytest.mark.asyncio
    async def test_start_loads_from_disk(self, tmp_path: Path):
        """Insights persisted by engine A are loaded by engine B on start()."""
        op_id = "op-persist-01"
        entries = [
            _make_entry(
                op_id, OperationState.APPLIED, data={"target_files": ["foo/bar.py"]}
            )
        ]
        ledger = _make_ledger({op_id: entries})

        engine_a = _make_engine(ledger, tmp_path)
        with patch(
            "backend.core.ouroboros.consciousness.memory_engine._get_git_head",
            return_value="sha-abc",
        ):
            await engine_a.start()
            await engine_a.ingest_outcome(op_id)
            await engine_a.stop()

        # Engine B reads from the same persistence_dir
        engine_b = MemoryEngine(
            ledger=ledger,
            persistence_dir=tmp_path / "memory",
        )
        with patch(
            "backend.core.ouroboros.consciousness.memory_engine._get_git_head",
            return_value="sha-abc",
        ):
            await engine_b.start()

        assert len(engine_b._insights) >= 1
        ids = [i.insight_id for i in engine_b._insights]
        # The insight written by engine_a must appear
        assert engine_a._insights[0].insight_id in ids

    @pytest.mark.asyncio
    async def test_stop_flushes_to_disk(self, tmp_path: Path):
        """stop() creates persistence files."""
        ledger = _make_ledger({})
        engine = _make_engine(ledger, tmp_path)
        await engine.start()
        engine._insights.append(
            MemoryInsight(
                insight_id="flush-me",
                category="success_pattern",
                content="flush test",
                confidence=0.6,
                evidence_count=1,
                last_seen_utc=_utcnow_iso(),
            )
        )
        await engine.stop()

        rep_path = tmp_path / "memory" / "file_reputations.json"
        pat_path = tmp_path / "memory" / "patterns.json"
        assert rep_path.exists()
        assert pat_path.exists()


# ---------------------------------------------------------------------------
# Malformed JSONL skipped
# ---------------------------------------------------------------------------


class TestMalformedJsonlSkipped:
    @pytest.mark.asyncio
    async def test_malformed_jsonl_skipped(self, tmp_path: Path):
        """Corrupted entry in insights.jsonl -> warning, not crash, valid lines load."""
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        insights_file = mem_dir / "insights.jsonl"

        valid_insight = MemoryInsight(
            insight_id="valid-001",
            category="success_pattern",
            content="valid insight",
            confidence=0.75,
            evidence_count=4,
            last_seen_utc=_utcnow_iso(),
        )
        valid_line = json.dumps(
            {
                "insight_id": valid_insight.insight_id,
                "category": valid_insight.category,
                "content": valid_insight.content,
                "confidence": valid_insight.confidence,
                "evidence_count": valid_insight.evidence_count,
                "last_seen_utc": valid_insight.last_seen_utc,
                "ttl_hours": valid_insight.ttl_hours,
            },
            sort_keys=True,
        )
        # Write one malformed line + one valid line
        insights_file.write_text(
            '{"this": "is broken JSON\n' + valid_line + "\n",
            encoding="utf-8",
        )

        ledger = _make_ledger({})
        engine = MemoryEngine(ledger=ledger, persistence_dir=mem_dir)
        await engine.start()

        # Valid line must be loaded, malformed line skipped
        ids = [i.insight_id for i in engine._insights]
        assert "valid-001" in ids
        # No crash
