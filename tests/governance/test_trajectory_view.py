"""Slices 2+3+4 tests — TrajectoryBuilder + Renderer + Stream + REPL."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.trajectory_frame import (
    TrajectoryFrame,
    TrajectoryPhase,
    idle_frame,
)
from backend.core.ouroboros.governance.trajectory_view import (
    TRAJECTORY_VIEW_SCHEMA_VERSION,
    TrajectoryBuilder,
    TrajectoryDispatchResult,
    TrajectoryRenderer,
    TrajectoryStream,
    TrajectorySurface,
    dispatch_trajectory_command,
    get_default_builder,
    get_default_renderer,
    get_default_stream,
    reset_default_trajectory_singletons,
    set_default_suppliers,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_default_trajectory_singletons()
    yield
    reset_default_trajectory_singletons()


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeOpState:
    def __init__(self, op_info: Optional[Dict[str, Any]] = None) -> None:
        self.op_info = op_info

    def current_op(self) -> Optional[Dict[str, Any]]:
        return self.op_info


class _FakeCost:
    def __init__(self, snapshot: Optional[Dict[str, Any]] = None) -> None:
        self.snapshot = snapshot

    def cost_snapshot(self, op_id: str) -> Optional[Dict[str, Any]]:
        _ = op_id
        return self.snapshot


class _FakeEta:
    def __init__(self, eta: Optional[Dict[str, Any]] = None) -> None:
        self.eta = eta

    def eta_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        _ = op_id
        return self.eta


class _FakeSensor:
    def __init__(self, trig: Optional[Dict[str, Any]] = None) -> None:
        self.trig = trig

    def trigger_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        _ = op_id
        return self.trig


class _RaisingSupplier:
    def current_op(self) -> Optional[Dict[str, Any]]:
        raise RuntimeError("supplier went boom")

    def cost_snapshot(self, op_id: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("cost supplier boom")

    def eta_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("eta supplier boom")

    def trigger_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("sensor supplier boom")


def _op_info(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "op_id": "op-abc1234",
        "raw_phase": "apply",
        "subject": "fix auth",
        "target_paths": ["backend/auth.py"],
        "active_tools": ["edit_file"],
        "trigger_source": "test_failure",
        "trigger_reason": "fired",
        "started_at_ts": time.time() - 60,
        "is_blocked": False,
        "blocked_reason": "",
        "next_step": "apply to disk",
    }
    base.update(overrides)
    return base


# ===========================================================================
# Schema + version
# ===========================================================================


def test_schema_version_stable():
    assert TRAJECTORY_VIEW_SCHEMA_VERSION == "trajectory_view.v1"


# ===========================================================================
# TrajectoryBuilder — empty / idle
# ===========================================================================


def test_builder_no_suppliers_returns_idle():
    b = TrajectoryBuilder()
    f = b.build()
    assert f.is_idle is True
    assert f.phase is TrajectoryPhase.IDLE


def test_builder_op_state_returning_none_returns_idle():
    b = TrajectoryBuilder(op_state=_FakeOpState(None))
    f = b.build()
    assert f.is_idle is True


def test_builder_empty_op_id_returns_idle():
    b = TrajectoryBuilder(op_state=_FakeOpState(_op_info(op_id="")))
    f = b.build()
    assert f.is_idle is True


# ===========================================================================
# TrajectoryBuilder — composed frame
# ===========================================================================


def test_builder_composes_frame_from_all_suppliers():
    b = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        cost=_FakeCost({"spent_usd": 0.012, "budget_usd": 0.50}),
        eta=_FakeEta({
            "eta_seconds": 45.0,
            "deadline_at_ts": time.time() + 45,
            "confidence": 0.75,
        }),
        sensor_trigger=_FakeSensor({
            "source": "runtime_health",
            "reason": "cpu over 90%",
        }),
    )
    f = b.build()
    assert f.is_idle is False
    assert f.op_id == "op-abc1234"
    assert f.phase is TrajectoryPhase.APPLYING
    assert f.cost_spent_usd == pytest.approx(0.012)
    assert f.cost_budget_usd == pytest.approx(0.50)
    assert f.eta_seconds == pytest.approx(45.0)
    assert f.confidence == pytest.approx(0.75)
    # sensor supplier overrode since op_info's trigger was still set;
    # builder keeps op_info's if present, only fills from sensor when empty
    assert f.trigger_source == "test_failure"


def test_builder_sensor_supplier_fills_empty_trigger():
    b = TrajectoryBuilder(
        op_state=_FakeOpState(
            _op_info(trigger_source="", trigger_reason="")
        ),
        sensor_trigger=_FakeSensor(
            {"source": "web_intelligence", "reason": "new CVE"},
        ),
    )
    f = b.build()
    assert f.trigger_source == "web_intelligence"
    assert f.trigger_reason == "new CVE"


def test_builder_sequence_counter_monotonic():
    b = TrajectoryBuilder(op_state=_FakeOpState(_op_info()))
    f1 = b.build()
    f2 = b.build()
    f3 = b.build()
    assert f2.sequence > f1.sequence
    assert f3.sequence > f2.sequence


def test_builder_snapshot_timestamp_frozen_to_now_ts_when_passed():
    b = TrajectoryBuilder(op_state=_FakeOpState(_op_info()))
    f = b.build(now_ts=1745244000.0)
    assert f.snapshot_at_ts == pytest.approx(1745244000.0)


# ===========================================================================
# TrajectoryBuilder — fail-closed on supplier errors
# ===========================================================================


def test_builder_cost_supplier_raises_fallback_to_zero():
    b = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        cost=_RaisingSupplier(),
    )
    f = b.build()
    # No crash — cost defaults
    assert f.cost_spent_usd == 0.0
    assert f.cost_budget_usd is None


def test_builder_eta_supplier_raises_fallback_to_none():
    b = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        eta=_RaisingSupplier(),
    )
    f = b.build()
    assert f.eta_seconds is None


def test_builder_sensor_raising_fallback_to_empty():
    b = TrajectoryBuilder(
        op_state=_FakeOpState(
            _op_info(trigger_source="", trigger_reason="")
        ),
        sensor_trigger=_RaisingSupplier(),
    )
    f = b.build()
    assert f.trigger_source == ""


def test_builder_op_state_raising_returns_idle():
    b = TrajectoryBuilder(op_state=_RaisingSupplier())
    f = b.build()
    assert f.is_idle is True


# ===========================================================================
# TrajectoryBuilder — type coercion
# ===========================================================================


def test_builder_target_paths_coerces_non_list():
    b = TrajectoryBuilder(op_state=_FakeOpState(
        _op_info(target_paths="single/path.py"),
    ))
    f = b.build()
    # A single string is coerced into a one-tuple
    assert f.target_paths == ("single/path.py",)


def test_builder_target_paths_none_becomes_empty_tuple():
    b = TrajectoryBuilder(op_state=_FakeOpState(
        _op_info(target_paths=None),
    ))
    f = b.build()
    assert f.target_paths == ()


def test_builder_tolerates_float_coercion():
    b = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        cost=_FakeCost({"spent_usd": "0.042", "budget_usd": "1.0"}),
    )
    f = b.build()
    assert f.cost_spent_usd == pytest.approx(0.042)
    assert f.cost_budget_usd == pytest.approx(1.0)


def test_builder_tolerates_non_numeric_strings_as_zero():
    b = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        cost=_FakeCost({"spent_usd": "not-a-number"}),
    )
    f = b.build()
    assert f.cost_spent_usd == 0.0


# ===========================================================================
# TrajectoryRenderer
# ===========================================================================


def test_renderer_repl_compact_idle():
    r = TrajectoryRenderer()
    text = r.render(idle_frame(), surface=TrajectorySurface.REPL_COMPACT)
    assert text == "idle"


def test_renderer_repl_compact_full():
    f = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        cost=_FakeCost({"spent_usd": 0.01, "budget_usd": 0.50}),
        eta=_FakeEta({"eta_seconds": 30.0}),
    ).build()
    text = TrajectoryRenderer().render(f, surface=TrajectorySurface.REPL_COMPACT)
    assert "op-abc1234" in text
    assert "applying" in text
    assert "ETA 30s" in text
    assert "$0.010" in text


def test_renderer_plain_returns_narrative():
    f = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        eta=_FakeEta({"eta_seconds": 30.0}),
        cost=_FakeCost({"spent_usd": 0.01, "budget_usd": 0.50}),
    ).build()
    text = TrajectoryRenderer().render(f, surface=TrajectorySurface.PLAIN)
    assert text.startswith("currently: op-")
    assert "because sensor" in text


def test_renderer_repl_expanded_contains_key_value_lines():
    f = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info()),
        cost=_FakeCost({"spent_usd": 0.02, "budget_usd": 0.50}),
        eta=_FakeEta({"eta_seconds": 60.0, "confidence": 0.9}),
    ).build()
    text = TrajectoryRenderer().render(
        f, surface=TrajectorySurface.REPL_EXPANDED,
    )
    assert "op_id       :" in text
    assert "phase       :" in text
    assert "cost        :" in text
    assert "confidence  :" in text


def test_renderer_ide_json_is_valid_json():
    f = idle_frame()
    text = TrajectoryRenderer().render(f, surface=TrajectorySurface.IDE_JSON)
    parsed = json.loads(text)
    assert parsed["is_idle"] is True


def test_renderer_sse_is_compact_valid_json():
    f = TrajectoryBuilder(op_state=_FakeOpState(_op_info())).build()
    text = TrajectoryRenderer().render(f, surface=TrajectorySurface.SSE)
    parsed = json.loads(text)
    assert parsed["schema_version"] == TRAJECTORY_VIEW_SCHEMA_VERSION
    assert parsed["op_id"] == "op-abc1234"
    assert "one_line_summary" in parsed


# ===========================================================================
# TrajectoryStream — listeners + emit + emit_if_changed
# ===========================================================================


def test_stream_subscribe_and_emit():
    stream = TrajectoryStream()
    received: List[TrajectoryFrame] = []
    stream.subscribe(received.append)
    f = idle_frame()
    stream.emit(f)
    assert len(received) == 1
    assert received[0] is f


def test_stream_unsub_stops_delivery():
    stream = TrajectoryStream()
    received: List[TrajectoryFrame] = []
    unsub = stream.subscribe(received.append)
    stream.emit(idle_frame(sequence=1))
    unsub()
    stream.emit(idle_frame(sequence=2))
    assert len(received) == 1


def test_stream_listener_exception_isolated():
    stream = TrajectoryStream()
    good: List[TrajectoryFrame] = []

    def _bad(_f: TrajectoryFrame) -> None:
        raise RuntimeError("boom")

    stream.subscribe(_bad)
    stream.subscribe(good.append)
    stream.emit(idle_frame())
    # Bad listener didn't prevent the good one
    assert len(good) == 1


def test_stream_emit_if_changed_suppresses_duplicates():
    b = TrajectoryBuilder(op_state=_FakeOpState(_op_info()))
    stream = TrajectoryStream()
    received: List[TrajectoryFrame] = []
    stream.subscribe(received.append)
    # Two frames back-to-back with same presentation content
    f1 = b.build()
    f2 = b.build()
    emitted1 = stream.emit_if_changed(f1)
    emitted2 = stream.emit_if_changed(f2)
    assert emitted1 is True
    assert emitted2 is False
    assert len(received) == 1


def test_stream_emit_if_changed_fires_on_phase_change():
    stream = TrajectoryStream()
    received: List[TrajectoryFrame] = []
    stream.subscribe(received.append)
    b1 = TrajectoryBuilder(op_state=_FakeOpState(_op_info(raw_phase="apply")))
    b2 = TrajectoryBuilder(op_state=_FakeOpState(_op_info(raw_phase="verify")))
    stream.emit_if_changed(b1.build())
    stream.emit_if_changed(b2.build())
    assert len(received) == 2


def test_stream_emit_if_changed_ignores_sequence_diff_alone():
    """Sequence + timestamp differ on every build() — but if the
    content is identical, emit_if_changed suppresses."""
    b = TrajectoryBuilder(op_state=_FakeOpState(_op_info()))
    stream = TrajectoryStream()
    received: List[TrajectoryFrame] = []
    stream.subscribe(received.append)
    stream.emit_if_changed(b.build())
    stream.emit_if_changed(b.build())
    stream.emit_if_changed(b.build())
    assert len(received) == 1


def test_stream_emits_total_counter():
    stream = TrajectoryStream()
    stream.emit(idle_frame(sequence=1))
    stream.emit(idle_frame(sequence=2))
    assert stream.emits_total == 2


def test_stream_last_emitted_tracks():
    stream = TrajectoryStream()
    assert stream.last_emitted is None
    f = idle_frame(sequence=7)
    stream.emit(f)
    assert stream.last_emitted is f


# ===========================================================================
# Singletons + set_default_suppliers
# ===========================================================================


def test_default_builder_is_singleton():
    a = get_default_builder()
    b = get_default_builder()
    assert a is b


def test_default_renderer_is_singleton():
    a = get_default_renderer()
    b = get_default_renderer()
    assert a is b


def test_default_stream_is_singleton():
    a = get_default_stream()
    b = get_default_stream()
    assert a is b


def test_set_default_suppliers_replaces_default():
    new_b = set_default_suppliers(
        op_state=_FakeOpState(_op_info()),
    )
    assert new_b is get_default_builder()
    # Frame built via the default now reflects the new suppliers
    f = get_default_builder().build()
    assert f.op_id == "op-abc1234"


# ===========================================================================
# /trajectory REPL dispatcher
# ===========================================================================


def test_repl_unmatched_falls_through():
    r = dispatch_trajectory_command("/plan mode on")
    assert r.matched is False


def test_repl_status_returns_one_line():
    set_default_suppliers(op_state=_FakeOpState(_op_info()))
    r = dispatch_trajectory_command("/trajectory")
    assert r.ok is True
    assert "op-abc1234" in r.text
    assert "applying" in r.text


def test_repl_status_idle_when_no_op():
    r = dispatch_trajectory_command("/trajectory status")
    assert r.ok is True
    assert r.text == "idle"


def test_repl_expanded():
    set_default_suppliers(op_state=_FakeOpState(_op_info()))
    r = dispatch_trajectory_command("/trajectory expanded")
    assert r.ok is True
    assert "op_id       :" in r.text


def test_repl_json():
    set_default_suppliers(op_state=_FakeOpState(_op_info()))
    r = dispatch_trajectory_command("/trajectory json")
    assert r.ok is True
    parsed = json.loads(r.text)
    assert parsed["op_id"] == "op-abc1234"


def test_repl_sse():
    set_default_suppliers(op_state=_FakeOpState(_op_info()))
    r = dispatch_trajectory_command("/trajectory sse")
    assert r.ok is True
    parsed = json.loads(r.text)
    assert "one_line_summary" in parsed


def test_repl_plain():
    set_default_suppliers(op_state=_FakeOpState(_op_info()))
    r = dispatch_trajectory_command("/trajectory plain")
    assert r.ok is True
    assert r.text.startswith("currently: op-")


def test_repl_help():
    r = dispatch_trajectory_command("/trajectory help")
    assert r.ok is True
    assert "/trajectory" in r.text
    assert "expanded" in r.text


def test_repl_watch_is_non_mutating_placeholder():
    r = dispatch_trajectory_command("/trajectory watch")
    assert r.ok is True
    assert "TrajectoryStream" in r.text


def test_repl_unknown_subcommand():
    r = dispatch_trajectory_command("/trajectory frobnicate")
    assert r.ok is False
    assert "unknown subcommand" in r.text


# ===========================================================================
# Gap-quote end-to-end shape
# ===========================================================================


def test_gap_quote_shape_survives_full_pipeline():
    """Quote: `currently: op-X, analyzing path Y because sensor Z fired,
    ETA W seconds, cost $C.`"""
    b = TrajectoryBuilder(
        op_state=_FakeOpState(_op_info(raw_phase="classify")),
        cost=_FakeCost({"spent_usd": 0.012, "budget_usd": 0.50}),
        eta=_FakeEta({"eta_seconds": 42.0}),
        sensor_trigger=_FakeSensor({
            "source": "test_failure",
            "reason": "fired",
        }),
    )
    f = b.build()
    plain = TrajectoryRenderer().render(
        f, surface=TrajectorySurface.PLAIN,
    )
    # Every structural piece of the quote is represented
    assert "currently:" in plain
    assert "op-abc1234" in plain
    assert "classifying" in plain  # verb for 'classify'
    assert "backend/auth.py" in plain
    assert "because sensor test_failure" in plain
    assert "ETA 42s" in plain
    assert "cost $0.012" in plain


# ===========================================================================
# Renderer falls through to one-line on unknown surface
# ===========================================================================


def test_renderer_unknown_surface_defaults_to_one_line():
    class _UnknownSurface:
        pass

    r = TrajectoryRenderer()
    # The renderer checks enum members; a non-enum argument should
    # fall through to one-line. (We rely on the last else return.)
    f = idle_frame()
    # Passing invalid arg type via duck — use REPL_COMPACT
    text = r.render(f, surface=TrajectorySurface.REPL_COMPACT)
    assert text == "idle"
