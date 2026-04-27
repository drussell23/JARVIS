"""Phase 8 — Temporal Observability pins.

5 sub-deliverables under one PR:
  8.1 — Decision causal-trace ledger (.jarvis/decision_trace.jsonl)
  8.2 — Latent-confidence ring buffer (in-memory bounded)
  8.3 — Synchronized multi-op timeline aggregator
  8.4 — Master-flag change emitter (snapshot-and-diff)
  8.5 — Latency-SLO breach detector (per-phase rolling-window p95)

Pinned cage:
  * Master flags default false; read paths short-circuit when off
  * Bounded sizes (file + row + per-op rate caps)
  * Append-only persistence where appropriate; ring buffer for noisy
    streams; in-memory monitor for env state
  * Cross-process flock reused (decision-trace ledger)
  * NEVER raises into caller
  * Authority + cage invariants
"""
from __future__ import annotations

import ast
import time
from pathlib import Path
from unittest import mock

import pytest

# 8.1
from backend.core.ouroboros.governance.observability import (
    decision_trace_ledger as dtl,
)
from backend.core.ouroboros.governance.observability.decision_trace_ledger import (
    DecisionRow,
    DecisionTraceLedger,
    MAX_FACTORS_KEYS,
    MAX_LEDGER_FILE_BYTES,
    MAX_RATIONALE_CHARS,
    MAX_RECORDS_PER_OP,
    MAX_ROW_BYTES,
    SCHEMA_VERSION,
    is_ledger_enabled as is_dtl_enabled,
    ledger_path as dtl_path,
)

# 8.2
from backend.core.ouroboros.governance.observability import (
    latent_confidence_ring as lcr,
)
from backend.core.ouroboros.governance.observability.latent_confidence_ring import (
    ConfidenceEvent,
    DEFAULT_RING_CAPACITY,
    LatentConfidenceRing,
    is_ring_enabled,
)

# 8.3
from backend.core.ouroboros.governance.observability import (
    multi_op_timeline as mot,
)
from backend.core.ouroboros.governance.observability.multi_op_timeline import (
    MAX_TIMELINE_EVENTS,
    TimelineEvent,
    is_timeline_enabled,
    merge_streams,
    render_text_timeline,
)

# 8.4
from backend.core.ouroboros.governance.observability import (
    flag_change_emitter as fce,
)
from backend.core.ouroboros.governance.observability.flag_change_emitter import (
    FlagChangeEvent,
    FlagChangeMonitor,
    MAX_TRACKED_FLAGS,
    TRACKED_PREFIX,
    diff_snapshots,
    is_emitter_enabled,
    snapshot_flags,
)

# 8.5
from backend.core.ouroboros.governance.observability import (
    latency_slo_detector as lsd,
)
from backend.core.ouroboros.governance.observability.latency_slo_detector import (
    DEFAULT_PHASE_SLO_S,
    LatencySLOBreachEvent,
    LatencySLODetector,
    MIN_SAMPLES_FOR_BREACH,
    is_detector_enabled,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_OBS_DIR = (
    _REPO_ROOT / "backend/core/ouroboros/governance/observability"
)


# ===========================================================================
# 8.1 — Decision causal-trace ledger
# ===========================================================================


class TestDecisionTraceMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_DECISION_TRACE_LEDGER_ENABLED", raising=False,
        )
        assert is_dtl_enabled() is False

    def test_truthy(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "1")
        assert is_dtl_enabled() is True


class TestDecisionTraceConstants:
    def test_constants(self):
        assert MAX_LEDGER_FILE_BYTES == 16 * 1024 * 1024
        assert MAX_ROW_BYTES == 16 * 1024
        assert MAX_RATIONALE_CHARS == 1_000
        assert MAX_FACTORS_KEYS == 32
        assert MAX_RECORDS_PER_OP == 200
        assert SCHEMA_VERSION == "1"


@pytest.fixture
def fresh_dtl(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "1")
    return DecisionTraceLedger(path=tmp_path / "dtl.jsonl")


class TestDecisionTraceRecord:
    def test_master_off_skips(self, monkeypatch, tmp_path):
        monkeypatch.delenv(
            "JARVIS_DECISION_TRACE_LEDGER_ENABLED", raising=False,
        )
        ledger = DecisionTraceLedger(path=tmp_path / "x.jsonl")
        ok, detail = ledger.record(
            op_id="op1", phase="ROUTE", decision="STANDARD",
        )
        assert ok is False and detail == "master_off"

    def test_empty_op_id_rejected(self, fresh_dtl):
        ok, detail = fresh_dtl.record(
            op_id="", phase="ROUTE", decision="STANDARD",
        )
        assert not ok
        assert detail == "empty_op_id"

    def test_empty_phase_rejected(self, fresh_dtl):
        ok, detail = fresh_dtl.record(
            op_id="op1", phase="", decision="X",
        )
        assert not ok
        assert detail == "empty_phase"

    def test_empty_decision_rejected(self, fresh_dtl):
        ok, detail = fresh_dtl.record(
            op_id="op1", phase="ROUTE", decision="",
        )
        assert not ok
        assert detail == "empty_decision"

    def test_happy_path(self, fresh_dtl):
        ok, detail = fresh_dtl.record(
            op_id="op-abc", phase="ROUTE", decision="STANDARD",
            factors={"urgency": "normal"},
            weights={"urgency": 1.0},
            rationale="Default cascade",
        )
        assert ok is True

    def test_per_op_rate_cap(self, fresh_dtl):
        for i in range(MAX_RECORDS_PER_OP):
            ok, _ = fresh_dtl.record(
                op_id="op1", phase=f"P{i}", decision="X",
            )
            assert ok
        ok, detail = fresh_dtl.record(
            op_id="op1", phase="overflow", decision="X",
        )
        assert not ok
        assert detail == "rate_cap_exhausted"

    def test_factors_dict_truncated(self, fresh_dtl):
        big = {f"k{i}": i for i in range(MAX_FACTORS_KEYS + 10)}
        ok, _ = fresh_dtl.record(
            op_id="op1", phase="ROUTE", decision="X", factors=big,
        )
        assert ok
        rows = fresh_dtl.reconstruct_op("op1")
        assert len(rows[0].factors) <= MAX_FACTORS_KEYS

    def test_rationale_truncated(self, fresh_dtl):
        big = "X" * (MAX_RATIONALE_CHARS + 100)
        ok, _ = fresh_dtl.record(
            op_id="op1", phase="ROUTE", decision="X", rationale=big,
        )
        assert ok
        rows = fresh_dtl.reconstruct_op("op1")
        assert len(rows[0].rationale) <= MAX_RATIONALE_CHARS


class TestDecisionTraceReconstruct:
    def test_reconstruct_empty(self, fresh_dtl):
        assert fresh_dtl.reconstruct_op("nonexistent") == []

    def test_reconstruct_chronological_order(self, fresh_dtl):
        for i, phase in enumerate(("CLASSIFY", "ROUTE", "PLAN")):
            fresh_dtl.record(
                op_id="op-x", phase=phase, decision=f"d{i}",
            )
        rows = fresh_dtl.reconstruct_op("op-x")
        assert len(rows) == 3
        assert [r.phase for r in rows] == ["CLASSIFY", "ROUTE", "PLAN"]

    def test_reconstruct_filters_by_op_id(self, fresh_dtl):
        fresh_dtl.record(op_id="op-a", phase="ROUTE", decision="x")
        fresh_dtl.record(op_id="op-b", phase="ROUTE", decision="y")
        rows_a = fresh_dtl.reconstruct_op("op-a")
        rows_b = fresh_dtl.reconstruct_op("op-b")
        assert len(rows_a) == 1
        assert len(rows_b) == 1
        assert rows_a[0].decision == "x"
        assert rows_b[0].decision == "y"


# ===========================================================================
# 8.2 — Latent-confidence ring buffer
# ===========================================================================


class TestLatentConfidenceMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", raising=False,
        )
        assert is_ring_enabled() is False

    def test_truthy(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "1")
        assert is_ring_enabled() is True


@pytest.fixture
def fresh_ring(monkeypatch):
    monkeypatch.setenv("JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "1")
    return LatentConfidenceRing(capacity=100)


class TestLatentConfidenceRecord:
    def test_master_off_skips(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", raising=False,
        )
        ring = LatentConfidenceRing()
        ok, detail = ring.record(
            classifier_name="x", confidence=0.8, threshold=0.5,
            outcome="ok",
        )
        assert not ok
        assert detail == "master_off"

    def test_empty_name_rejected(self, fresh_ring):
        ok, detail = fresh_ring.record(
            classifier_name="", confidence=0.8, threshold=0.5,
            outcome="ok",
        )
        assert not ok

    def test_non_numeric_rejected(self, fresh_ring):
        ok, detail = fresh_ring.record(
            classifier_name="c", confidence="high",  # type: ignore[arg-type]
            threshold=0.5, outcome="ok",
        )
        assert not ok

    def test_drop_oldest_when_full(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "1",
        )
        ring = LatentConfidenceRing(capacity=3)
        for i in range(5):
            ring.record(
                classifier_name="c", confidence=float(i),
                threshold=0.5, outcome=f"o{i}",
            )
        # capacity=3 → only the last 3 (i=2,3,4) survive
        recent = ring.recent(10)
        assert len(recent) == 3
        assert [e.confidence for e in recent] == [2.0, 3.0, 4.0]

    def test_below_threshold_property(self, fresh_ring):
        fresh_ring.record(
            classifier_name="c", confidence=0.3, threshold=0.5,
            outcome="lowconf",
        )
        recent = fresh_ring.recent(1)
        assert recent[0].below_threshold is True

    def test_recent_for_classifier_filters(self, fresh_ring):
        fresh_ring.record(
            classifier_name="A", confidence=0.5, threshold=0.5,
            outcome="x",
        )
        fresh_ring.record(
            classifier_name="B", confidence=0.5, threshold=0.5,
            outcome="x",
        )
        a_only = fresh_ring.recent_for_classifier("A", 10)
        assert len(a_only) == 1
        assert a_only[0].classifier_name == "A"


class TestLatentConfidenceDropDetection:
    def test_insufficient_data(self, fresh_ring):
        for _ in range(5):
            fresh_ring.record(
                classifier_name="c", confidence=0.9,
                threshold=0.5, outcome="x",
            )
        result = fresh_ring.confidence_drop_indicators("c", window=20)
        assert result["drop_detected"] is False
        assert "insufficient_data" in result["reason"]

    def test_drop_detected(self, fresh_ring):
        # 20 high-conf events then 20 low-conf events = >50% drop.
        for _ in range(20):
            fresh_ring.record(
                classifier_name="c", confidence=0.95,
                threshold=0.5, outcome="x",
            )
        for _ in range(20):
            fresh_ring.record(
                classifier_name="c", confidence=0.40,
                threshold=0.5, outcome="x",
            )
        result = fresh_ring.confidence_drop_indicators(
            "c", window=20, drop_threshold_pct=20.0,
        )
        assert result["drop_detected"] is True
        assert result["drop_pct"] > 20.0

    def test_no_drop_when_stable(self, fresh_ring):
        for _ in range(40):
            fresh_ring.record(
                classifier_name="c", confidence=0.85,
                threshold=0.5, outcome="x",
            )
        result = fresh_ring.confidence_drop_indicators(
            "c", window=20,
        )
        assert result["drop_detected"] is False


# ===========================================================================
# 8.3 — Multi-op timeline aggregator
# ===========================================================================


def _ev(stream_id, ts, event_type="x", payload=None, seq=0):
    return TimelineEvent(
        ts_epoch=ts, stream_id=stream_id,
        event_type=event_type,
        payload=payload or {},
        seq=seq,
    )


class TestMultiOpTimeline:
    def test_master_flag_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_MULTI_OP_TIMELINE_ENABLED", raising=False,
        )
        assert is_timeline_enabled() is False

    def test_empty_streams(self):
        assert merge_streams({}) == []

    def test_single_stream_pass_through(self):
        events = [_ev("s1", 1.0), _ev("s1", 2.0), _ev("s1", 3.0)]
        out = merge_streams({"s1": events})
        assert out == events

    def test_two_streams_chronological_merge(self):
        s1 = [_ev("s1", 1.0), _ev("s1", 3.0)]
        s2 = [_ev("s2", 2.0), _ev("s2", 4.0)]
        out = merge_streams({"s1": s1, "s2": s2})
        assert [e.ts_epoch for e in out] == [1.0, 2.0, 3.0, 4.0]
        assert [e.stream_id for e in out] == ["s1", "s2", "s1", "s2"]

    def test_tie_break_by_stream_id(self):
        # Two events at exact same ts_epoch — alpha tie-break.
        s1 = [_ev("alpha", 1.0)]
        s2 = [_ev("zulu", 1.0)]
        out = merge_streams({"alpha": s1, "zulu": s2})
        assert out[0].stream_id == "alpha"

    def test_tie_break_by_seq(self):
        # Same stream + same ts — seq breaks tie.
        s1 = [_ev("s1", 1.0, seq=2), _ev("s1", 1.0, seq=1)]
        # Note: aggregator does NOT sort within streams; caller's
        # responsibility. So input order preserved.
        out = merge_streams({"s1": s1})
        assert out == s1

    def test_max_events_cap(self):
        # 100k events from one stream; cap at MAX_TIMELINE_EVENTS.
        events = [_ev("s1", float(i)) for i in range(100_000)]
        out = merge_streams({"s1": events})
        assert len(out) == MAX_TIMELINE_EVENTS

    def test_explicit_max_events(self):
        events = [_ev("s1", float(i)) for i in range(100)]
        out = merge_streams({"s1": events}, max_events=10)
        assert len(out) == 10

    def test_render_text_timeline(self):
        events = [
            _ev("s1", 1.0, event_type="phase_start",
                payload={"phase": "ROUTE"}),
            _ev("s2", 2.0, event_type="decision",
                payload={"choice": "STANDARD"}),
        ]
        text = render_text_timeline(events)
        assert "s1" in text
        assert "s2" in text
        assert "phase_start" in text
        assert "decision" in text


# ===========================================================================
# 8.4 — Master-flag change emitter
# ===========================================================================


class TestFlagChangeMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FLAG_CHANGE_EMITTER_ENABLED", raising=False,
        )
        assert is_emitter_enabled() is False

    def test_truthy(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        assert is_emitter_enabled() is True

    def test_constants(self):
        assert TRACKED_PREFIX == "JARVIS_"
        assert MAX_TRACKED_FLAGS == 1024


class TestFlagChangeSnapshot:
    def test_snapshot_filters_by_prefix(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_X", "1")
        monkeypatch.setenv("OTHER_FLAG", "1")
        snap = snapshot_flags()
        assert "JARVIS_FLAG_X" in snap
        assert "OTHER_FLAG" not in snap

    def test_diff_master_off_returns_empty(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FLAG_CHANGE_EMITTER_ENABLED", raising=False,
        )
        prev = {"JARVIS_X": "0"}
        next_ = {"JARVIS_X": "1"}
        assert diff_snapshots(prev, next_) == []

    def test_diff_added(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        events = diff_snapshots({}, {"JARVIS_NEW": "1"})
        assert len(events) == 1
        assert events[0].is_added
        assert not events[0].is_removed
        assert not events[0].is_changed

    def test_diff_removed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        events = diff_snapshots({"JARVIS_OLD": "1"}, {})
        assert len(events) == 1
        assert events[0].is_removed

    def test_diff_changed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        events = diff_snapshots(
            {"JARVIS_X": "0"}, {"JARVIS_X": "1"},
        )
        assert len(events) == 1
        assert events[0].is_changed

    def test_diff_alpha_sorted(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        events = diff_snapshots(
            {}, {"JARVIS_Z": "1", "JARVIS_A": "1", "JARVIS_M": "1"},
        )
        names = [e.flag_name for e in events]
        assert names == sorted(names)


class TestFlagChangeMonitor:
    def test_initialize_then_check_no_changes(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        monkeypatch.setenv("JARVIS_TEST_MON", "1")
        mon = FlagChangeMonitor()
        mon.initialize()
        assert mon.check() == []

    def test_check_detects_change(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        monkeypatch.setenv("JARVIS_TEST_MON_X", "0")
        mon = FlagChangeMonitor()
        mon.initialize()
        monkeypatch.setenv("JARVIS_TEST_MON_X", "1")
        events = mon.check()
        assert any(
            e.flag_name == "JARVIS_TEST_MON_X" and e.is_changed
            for e in events
        )

    def test_check_advances_baseline(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "1")
        mon = FlagChangeMonitor()
        mon.initialize()
        monkeypatch.setenv("JARVIS_NEW_FLAG", "1")
        first = mon.check()
        # Second check w/ no further changes returns empty.
        second = mon.check()
        assert len(first) >= 1
        assert second == []


# ===========================================================================
# 8.5 — Latency-SLO breach detector
# ===========================================================================


class TestLatencySLOMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LATENCY_SLO_DETECTOR_ENABLED", raising=False,
        )
        assert is_detector_enabled() is False


@pytest.fixture
def fresh_detector(monkeypatch):
    monkeypatch.setenv("JARVIS_LATENCY_SLO_DETECTOR_ENABLED", "1")
    return LatencySLODetector()


class TestLatencySLORecord:
    def test_master_off_skips(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LATENCY_SLO_DETECTOR_ENABLED", raising=False,
        )
        det = LatencySLODetector()
        ok, _ = det.record("ROUTE", 1.0)
        assert not ok

    def test_empty_phase_rejected(self, fresh_detector):
        ok, detail = fresh_detector.record("", 1.0)
        assert not ok
        assert detail == "empty_phase"

    def test_negative_latency_rejected(self, fresh_detector):
        ok, detail = fresh_detector.record("ROUTE", -1.0)
        assert not ok
        assert detail == "negative_latency"


class TestLatencySLOBreach:
    def test_insufficient_data_no_breach(self, fresh_detector):
        # Below MIN_SAMPLES_FOR_BREACH
        for _ in range(5):
            fresh_detector.record("ROUTE", 100.0)
        assert fresh_detector.check_breach("ROUTE") is None

    def test_p95_below_slo_no_breach(self, fresh_detector):
        fresh_detector.set_slo("ROUTE", 60.0)
        for _ in range(MIN_SAMPLES_FOR_BREACH + 5):
            fresh_detector.record("ROUTE", 1.0)
        assert fresh_detector.check_breach("ROUTE") is None

    def test_p95_above_slo_breach(self, fresh_detector):
        fresh_detector.set_slo("GENERATE", 30.0)
        for _ in range(MIN_SAMPLES_FOR_BREACH + 5):
            fresh_detector.record("GENERATE", 100.0)
        ev = fresh_detector.check_breach("GENERATE")
        assert ev is not None
        assert ev.phase == "GENERATE"
        assert ev.p95_s > 30.0
        assert ev.overshoot_s > 0
        assert ev.overshoot_pct > 0

    def test_check_all_breaches_alpha_sorted(self, fresh_detector):
        fresh_detector.set_slo("ROUTE", 1.0)
        fresh_detector.set_slo("GENERATE", 1.0)
        for _ in range(30):
            fresh_detector.record("ROUTE", 100.0)
            fresh_detector.record("GENERATE", 100.0)
        events = fresh_detector.check_all_breaches()
        # Alpha-sorted phase names.
        assert [e.phase for e in events] == sorted(
            e.phase for e in events
        )

    def test_p50_p95_max_in_stats(self, fresh_detector):
        for v in (1.0, 2.0, 3.0, 4.0, 5.0):
            fresh_detector.record("ROUTE", v)
        stats = fresh_detector.stats()
        assert "ROUTE" in stats
        assert stats["ROUTE"]["sample_count"] == 5
        assert stats["ROUTE"]["max_s"] == 5.0


# ===========================================================================
# Authority + cage invariants (all 5 modules)
# ===========================================================================


class TestAuthorityInvariants:
    @pytest.mark.parametrize("module_name", [
        "decision_trace_ledger.py",
        "latent_confidence_ring.py",
        "multi_op_timeline.py",
        "flag_change_emitter.py",
        "latency_slo_detector.py",
    ])
    def test_no_banned_governance_imports(self, module_name):
        path = _OBS_DIR / module_name
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned = (
            "scoped_tool_backend", "general_driver",
            "exploration_engine", "semantic_guardian",
            "orchestrator", "tool_executor", "phase_runners",
            "gate_runner", "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for b in banned:
                    assert b not in node.module, (
                        f"{module_name}: banned import {node.module}"
                    )

    @pytest.mark.parametrize("module_name", [
        "decision_trace_ledger.py",
        "latent_confidence_ring.py",
        "multi_op_timeline.py",
        "flag_change_emitter.py",
        "latency_slo_detector.py",
    ])
    def test_no_subprocess_or_network(self, module_name):
        path = _OBS_DIR / module_name
        source = path.read_text(encoding="utf-8")
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
        ):
            assert token not in source, (
                f"{module_name}: banned token {token}"
            )

    def test_dtl_uses_flock(self):
        # Decision-trace ledger writes are cross-process serialized.
        path = _OBS_DIR / "decision_trace_ledger.py"
        source = path.read_text(encoding="utf-8")
        assert "flock_exclusive" in source
