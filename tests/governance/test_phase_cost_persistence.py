"""Tests for Slice 3 persistence — SessionRecorder <- CostGovernor <- SessionRecord round-trip."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.battle_test.session_recorder import (
    SessionRecorder,
)
from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
    register_finalize_observer,
    reset_finalize_observers,
)
from backend.core.ouroboros.governance.session_record import (
    parse_session_dir,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_observers():
    reset_finalize_observers()
    yield
    reset_finalize_observers()


def _governor() -> CostGovernor:
    return CostGovernor(
        config=CostGovernorConfig(
            enabled=True, baseline_usd=1.0,
            max_cap_usd=100.0, min_cap_usd=0.01,
        ),
    )


# ===========================================================================
# Observer registry
# ===========================================================================


def test_register_finalize_observer_receives_events():
    events = []

    def _obs(op_id: str, summary: Dict[str, Any]):
        events.append((op_id, summary))

    register_finalize_observer(_obs)
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude", phase="GENERATE")
    g.finish("op-1")
    assert len(events) == 1
    op_id, summary = events[0]
    assert op_id == "op-1"
    assert summary["phase_totals"]["GENERATE"] == pytest.approx(0.10)


def test_register_finalize_observer_unsub_stops_events():
    events = []
    unsub = register_finalize_observer(lambda op, s: events.append(op))
    unsub()
    g = _governor()
    g.start("op-x", route="standard", complexity="light")
    g.charge("op-x", 0.01, "claude")
    g.finish("op-x")
    assert events == []


def test_register_finalize_observer_dedups():
    events = []
    obs = lambda op, s: events.append(op)
    register_finalize_observer(obs)
    register_finalize_observer(obs)  # second call is a no-op
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude")
    g.finish("op-1")
    assert events == ["op-1"]


def test_observer_exception_does_not_break_finalize():
    def _boom(op, s):
        raise RuntimeError("observer exploded")
    register_finalize_observer(_boom)
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude")
    # finalize must still return the summary despite the raising observer
    summary = g.finish("op-1")
    assert summary is not None
    assert summary["cumulative_usd"] == pytest.approx(0.10)


def test_multiple_observers_all_receive_event():
    a, b = [], []
    register_finalize_observer(lambda op, s: a.append(op))
    register_finalize_observer(lambda op, s: b.append(op))
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.05, "claude")
    g.finish("op-1")
    assert a == ["op-1"]
    assert b == ["op-1"]


# ===========================================================================
# SessionRecorder receives + aggregates finalize events
# ===========================================================================


def test_recorder_captures_cost_finalize():
    recorder = SessionRecorder(session_id="bt-cost-1")
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.20, "claude", phase="GENERATE")
    g.charge("op-1", 0.10, "claude", phase="VERIFY")
    g.finish("op-1")
    recorder.detach_cost_finalize_observer()
    assert recorder.cost_by_op_phase == {
        "op-1": {
            "GENERATE": pytest.approx(0.20),
            "VERIFY": pytest.approx(0.10),
        },
    }


def test_recorder_captures_multiple_ops():
    recorder = SessionRecorder(session_id="bt-cost-2")
    g = _governor()
    g.start("op-a", route="standard", complexity="light")
    g.start("op-b", route="complex", complexity="heavy_code")
    g.charge("op-a", 0.10, "claude", phase="GENERATE")
    g.charge("op-b", 0.50, "claude", phase="GENERATE")
    g.charge("op-b", 0.20, "claude", phase="VERIFY")
    g.finish("op-a")
    g.finish("op-b")
    recorder.detach_cost_finalize_observer()
    assert set(recorder.cost_by_op_phase.keys()) == {"op-a", "op-b"}
    assert recorder.cost_by_op_phase["op-b"]["VERIFY"] == pytest.approx(0.20)


def test_recorder_finalize_is_idempotent_to_detach():
    recorder = SessionRecorder(session_id="bt-cost-3")
    recorder.detach_cost_finalize_observer()
    recorder.detach_cost_finalize_observer()  # safe to call twice


# ===========================================================================
# save_summary() emits cost_by_phase + cost_by_op_phase
# ===========================================================================


def _minimal_save_args(**overrides):
    defaults = dict(
        stop_reason="complete",
        duration_s=10.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={},
        convergence_state="IMPROVING",
        convergence_slope=0.0,
        convergence_r2=0.0,
    )
    defaults.update(overrides)
    return defaults


def test_save_summary_emits_cost_by_phase_when_observed(tmp_path: Path):
    recorder = SessionRecorder(session_id="bt-emit-1")
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.40, "claude", phase="GENERATE")
    g.charge("op-1", 0.15, "doubleword", phase="VERIFY")
    g.finish("op-1")
    recorder.save_summary(output_dir=tmp_path, **_minimal_save_args())
    raw = json.loads((tmp_path / "summary.json").read_text())
    assert "cost_by_phase" in raw
    assert raw["cost_by_phase"]["GENERATE"] == pytest.approx(0.40)
    assert raw["cost_by_phase"]["VERIFY"] == pytest.approx(0.15)
    assert raw["cost_by_op_phase"]["op-1"]["GENERATE"] == pytest.approx(0.40)


def test_save_summary_omits_cost_keys_when_no_ops_observed(tmp_path: Path):
    """Sessions that never emitted a finalize event (pre-Slice-2
    runs, mock sessions) produce no cost_by_phase key — preserves
    backward-compat with consumers that would break on the new key."""
    recorder = SessionRecorder(session_id="bt-empty")
    recorder.save_summary(output_dir=tmp_path, **_minimal_save_args())
    raw = json.loads((tmp_path / "summary.json").read_text())
    assert "cost_by_phase" not in raw
    assert "cost_by_op_phase" not in raw


def test_save_summary_emits_provider_matrix(tmp_path: Path):
    recorder = SessionRecorder(session_id="bt-prov-1")
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.30, "claude", phase="GENERATE")
    g.charge("op-1", 0.05, "doubleword", phase="GENERATE")
    g.finish("op-1")
    recorder.save_summary(output_dir=tmp_path, **_minimal_save_args())
    raw = json.loads((tmp_path / "summary.json").read_text())
    assert raw["cost_by_op_phase_provider"]["op-1"]["GENERATE"] == {
        "claude": pytest.approx(0.30),
        "doubleword": pytest.approx(0.05),
    }


def test_save_summary_emits_unknown_phase_when_observed(tmp_path: Path):
    recorder = SessionRecorder(session_id="bt-unk-1")
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.10, "claude")  # no phase
    g.finish("op-1")
    recorder.save_summary(output_dir=tmp_path, **_minimal_save_args())
    raw = json.loads((tmp_path / "summary.json").read_text())
    assert "cost_unknown_phase_by_op" in raw
    assert raw["cost_unknown_phase_by_op"]["op-1"] == pytest.approx(0.10)


# ===========================================================================
# SessionRecord parser round-trip
# ===========================================================================


def test_session_record_parses_cost_by_phase(tmp_path: Path):
    session_dir = tmp_path / "bt-round-1"
    session_dir.mkdir()
    (session_dir / "summary.json").write_text(json.dumps({
        "stop_reason": "complete",
        "stats": {"ops_total": 1, "ops_applied": 1},
        "cost_by_phase": {
            "GENERATE": 0.40, "VALIDATE": 0.10, "VERIFY": 0.30,
        },
        "cost_by_op_phase": {
            "op-1": {"GENERATE": 0.40, "VERIFY": 0.30},
            "op-2": {"VALIDATE": 0.10},
        },
    }))
    rec = parse_session_dir(session_dir)
    assert rec.cost_by_phase == {
        "GENERATE": 0.40, "VALIDATE": 0.10, "VERIFY": 0.30,
    }
    assert rec.cost_by_op_phase["op-1"]["GENERATE"] == pytest.approx(0.40)
    assert rec.cost_by_op_phase["op-2"]["VALIDATE"] == pytest.approx(0.10)


def test_session_record_empty_when_keys_missing(tmp_path: Path):
    """Back-compat: pre-Slice-3 summaries load cleanly with empty
    cost-by-phase dicts."""
    session_dir = tmp_path / "bt-legacy"
    session_dir.mkdir()
    (session_dir / "summary.json").write_text(json.dumps({
        "stop_reason": "complete",
        "stats": {"ops_total": 1, "ops_applied": 1},
    }))
    rec = parse_session_dir(session_dir)
    assert rec.cost_by_phase == {}
    assert rec.cost_by_op_phase == {}


def test_session_record_rejects_malformed_cost_values(tmp_path: Path):
    session_dir = tmp_path / "bt-bad"
    session_dir.mkdir()
    (session_dir / "summary.json").write_text(json.dumps({
        "cost_by_phase": {
            "GENERATE": 0.40,
            "BOGUS": "not a number",
            "NEGATIVE": -0.10,
            "": 0.10,  # empty key
            "ZERO": 0.0,
        },
    }))
    rec = parse_session_dir(session_dir)
    assert rec.cost_by_phase == {"GENERATE": 0.40}


def test_session_record_rejects_non_mapping_cost_by_phase(tmp_path: Path):
    session_dir = tmp_path / "bt-notdict"
    session_dir.mkdir()
    (session_dir / "summary.json").write_text(json.dumps({
        "cost_by_phase": ["not", "a", "mapping"],
    }))
    rec = parse_session_dir(session_dir)
    assert rec.cost_by_phase == {}


def test_session_record_projection_includes_cost_fields():
    from backend.core.ouroboros.governance.session_record import SessionRecord
    rec = SessionRecord(
        session_id="bt-proj",
        cost_by_phase={"GENERATE": 0.50},
        cost_by_op_phase={"op-1": {"GENERATE": 0.50}},
    )
    p = rec.project()
    assert p["cost_by_phase"] == {"GENERATE": 0.50}
    assert p["cost_by_op_phase"] == {"op-1": {"GENERATE": 0.50}}
    assert p["has_phase_cost_data"] is True


def test_session_record_projection_flags_no_data_when_absent():
    from backend.core.ouroboros.governance.session_record import SessionRecord
    rec = SessionRecord(session_id="bt-empty-rec")
    p = rec.project()
    assert p["has_phase_cost_data"] is False


# ===========================================================================
# End-to-end: governor -> recorder -> summary.json -> record parse
# ===========================================================================


def test_end_to_end_round_trip(tmp_path: Path):
    """Full flow:
       governor charges -> recorder observes -> save_summary -> parse_session_dir.
    """
    session_dir = tmp_path / "bt-e2e"
    recorder = SessionRecorder(session_id="bt-e2e")
    g = _governor()
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.12, "claude", phase="CLASSIFY")
    g.charge("op-1", 0.60, "claude", phase="GENERATE")
    g.charge("op-1", 0.08, "claude", phase="VERIFY")
    g.finish("op-1")
    recorder.save_summary(output_dir=session_dir, **_minimal_save_args())
    rec = parse_session_dir(session_dir)
    assert rec.cost_by_phase["GENERATE"] == pytest.approx(0.60)
    assert rec.cost_by_op_phase["op-1"]["CLASSIFY"] == pytest.approx(0.12)
    assert rec.cost_by_op_phase["op-1"]["VERIFY"] == pytest.approx(0.08)
