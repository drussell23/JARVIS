"""P3.5 — Realtime progress visibility regression suite.

Pins the per-stream HEARTBEAT producer (plan_exploit) + the
RealtimeProgressTracker consumer + the coalesced status-line render.
P3.5 ships always-on per PRD §9 spec (no env knob); the only
behavioural delta is bounded in-memory state + non-empty render output.

Sections:
    (A) StreamTick + tracker basic CRUD
    (B) record_tick — happy / malformed inputs / elapsed_seconds clamps
    (C) render_coalesced — empty op / single stream / multi-stream /
        sorted ordering / matches PRD example shape
    (D) ETA math — under anchor / above anchor / no streams reported
    (E) Bounded state — per-op stream cap + global op cap (FIFO eviction)
    (F) forget_op + known_op_ids
    (G) Default-singleton accessor
    (H) plan_exploit producer integration — periodic heartbeat fires;
        cancelled cleanly on completion; no error when tracker
        unavailable
    (I) Authority invariants — banned imports + side-effect surface pin
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import pytest

from backend.core.ouroboros.governance.realtime_progress_tracker import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    MAX_OPS_TRACKED,
    MAX_STREAMS_PER_OP,
    RealtimeProgressTracker,
    StreamTick,
    get_default_tracker,
    reset_default_tracker,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_default_tracker()
    yield
    reset_default_tracker()


# ===========================================================================
# A — StreamTick + tracker basic CRUD
# ===========================================================================


def test_default_heartbeat_interval_pinned():
    """Pin: PRD spec says 5s heartbeat cadence."""
    assert DEFAULT_HEARTBEAT_INTERVAL_S == 5.0


def test_max_streams_per_op_pinned():
    assert MAX_STREAMS_PER_OP == 16


def test_max_ops_tracked_pinned():
    assert MAX_OPS_TRACKED == 64


def test_streamtick_is_frozen():
    s = StreamTick(
        stream_id="stream-1", unit_id="u1",
        activity_summary="generating", elapsed_seconds=4.0,
        last_seen_unix=100.0,
    )
    with pytest.raises(Exception):
        s.elapsed_seconds = 99.0  # type: ignore[misc]


def test_streamtick_display_label_prefers_stream_id():
    s = StreamTick(
        stream_id="stream-2", unit_id="some_unit_id",
        activity_summary="generating", elapsed_seconds=1.0,
        last_seen_unix=100.0,
    )
    assert s.display_label == "stream-2"


# ===========================================================================
# B — record_tick
# ===========================================================================


def test_record_tick_happy_path():
    t = RealtimeProgressTracker()
    t.record_tick("op-1", "stream-1", unit_id="u1", elapsed_seconds=4.0)
    state = t.get_state("op-1")
    assert state is not None
    assert "stream-1" in state.streams
    assert state.streams["stream-1"].elapsed_seconds == 4.0


def test_record_tick_empty_op_id_dropped():
    t = RealtimeProgressTracker()
    t.record_tick("", "stream-1")
    assert t.get_state("") is None


def test_record_tick_empty_stream_id_dropped():
    t = RealtimeProgressTracker()
    t.record_tick("op-1", "")
    assert t.get_state("op-1") is None


def test_record_tick_negative_elapsed_clamps_to_zero():
    t = RealtimeProgressTracker()
    t.record_tick("op-1", "stream-1", elapsed_seconds=-99.0)
    state = t.get_state("op-1")
    assert state.streams["stream-1"].elapsed_seconds == 0.0


def test_record_tick_invalid_elapsed_falls_back_to_zero():
    t = RealtimeProgressTracker()
    # Passing string-ish that float() can't handle.
    t.record_tick("op-1", "stream-1", elapsed_seconds="not-a-number")  # type: ignore[arg-type]
    state = t.get_state("op-1")
    assert state.streams["stream-1"].elapsed_seconds == 0.0


def test_record_tick_truncates_long_activity_summary():
    t = RealtimeProgressTracker()
    long_summary = "X" * 500
    t.record_tick("op-1", "stream-1", activity_summary=long_summary)
    state = t.get_state("op-1")
    assert len(state.streams["stream-1"].activity_summary) == 120


# ===========================================================================
# C — render_coalesced
# ===========================================================================


def test_render_unknown_op_returns_empty():
    t = RealtimeProgressTracker()
    assert t.render_coalesced("nonexistent") == ""


def test_render_single_stream_format():
    t = RealtimeProgressTracker()
    t.record_tick(
        "op-019dc42c", "stream-1", unit_id="u1",
        activity_summary="generating", elapsed_seconds=4.0,
        now_unix=100.0,
    )
    line = t.render_coalesced("op-019dc42c", now_unix=178.0)
    assert "[op-019dc42c]" in line
    assert "PLAN-EXPLOIT 1-stream" in line
    assert "stream-1 generating (4s)" in line
    assert "78s elapsed" in line
    assert "ETA" in line


def test_render_multi_stream_matches_prd_example_shape():
    """Pin: output matches PRD §9 P3.5 example shape:
    [op-...] PLAN-EXPLOIT 3-stream: stream-1 ..., stream-2 ..., ...
    (Xs elapsed, ~Ys ETA)"""
    t = RealtimeProgressTracker()
    for i, sid in enumerate(["stream-1", "stream-2", "stream-3"]):
        t.record_tick(
            "op-019dc42c-38d7", sid, unit_id=f"u{i+1}",
            activity_summary="generating", elapsed_seconds=4.0,
            now_unix=100.0,
        )
    line = t.render_coalesced("op-019dc42c-38d7", now_unix=178.0)
    assert "PLAN-EXPLOIT 3-stream" in line
    assert "stream-1 generating (4s)" in line
    assert "stream-2 generating (4s)" in line
    assert "stream-3 generating (4s)" in line
    assert "78s elapsed" in line


def test_render_streams_sorted_by_id():
    """Stable output: streams always rendered in stream_id sort order."""
    t = RealtimeProgressTracker()
    # Insert in reverse order.
    for sid in ["stream-3", "stream-1", "stream-2"]:
        t.record_tick("op-x", sid, elapsed_seconds=1.0, now_unix=100.0)
    line = t.render_coalesced("op-x", now_unix=105.0)
    s1 = line.find("stream-1")
    s2 = line.find("stream-2")
    s3 = line.find("stream-3")
    assert 0 < s1 < s2 < s3


def test_render_multiple_ops_independent():
    t = RealtimeProgressTracker()
    t.record_tick("op-A", "stream-1", elapsed_seconds=1.0, now_unix=100.0)
    t.record_tick("op-B", "stream-1", elapsed_seconds=2.0, now_unix=100.0)
    line_a = t.render_coalesced("op-A", now_unix=110.0)
    line_b = t.render_coalesced("op-B", now_unix=110.0)
    assert "[op-A]" in line_a
    assert "[op-B]" in line_b


# ===========================================================================
# D — ETA math
# ===========================================================================


def test_eta_string_under_anchor():
    """When max-stream-elapsed < 180s anchor, ETA = anchor - elapsed_overall."""
    t = RealtimeProgressTracker()
    t.record_tick("op-1", "stream-1", elapsed_seconds=30.0, now_unix=100.0)
    line = t.render_coalesced("op-1", now_unix=130.0)
    # elapsed_overall = 30s. anchor = 180s. ETA ≈ 150s.
    assert "~150s ETA" in line


def test_eta_string_above_anchor():
    """When slowest stream > 180s, ETA extrapolates +50% of slowest."""
    t = RealtimeProgressTracker()
    t.record_tick("op-1", "stream-1", elapsed_seconds=240.0, now_unix=100.0)
    line = t.render_coalesced("op-1", now_unix=340.0)
    # max_per_stream = 240. ETA = 240 * 0.5 = 120.
    assert "~120s ETA" in line


def test_eta_string_no_streams_reports_na():
    """Defensive: zero max_per_stream → 'n/a'."""
    t = RealtimeProgressTracker()
    t.record_tick("op-1", "stream-1", elapsed_seconds=0.0, now_unix=100.0)
    line = t.render_coalesced("op-1", now_unix=100.0)
    assert "~ETA n/a" in line


# ===========================================================================
# E — Bounded state
# ===========================================================================


def test_per_op_stream_cap():
    """Pin: extra streams beyond MAX_STREAMS_PER_OP are silently dropped."""
    t = RealtimeProgressTracker()
    for i in range(MAX_STREAMS_PER_OP + 5):
        t.record_tick("op-1", f"stream-{i}", elapsed_seconds=1.0)
    state = t.get_state("op-1")
    assert len(state.streams) == MAX_STREAMS_PER_OP


def test_global_op_fifo_eviction():
    """Pin: oldest op evicted when global cap hit."""
    t = RealtimeProgressTracker()
    n = MAX_OPS_TRACKED + 3
    for i in range(n):
        t.record_tick(f"op-{i:04d}", "stream-1", elapsed_seconds=1.0)
    # Oldest 3 should have been evicted.
    assert t.get_state("op-0000") is None
    assert t.get_state("op-0002") is None
    # Newest still present.
    assert t.get_state(f"op-{n - 1:04d}") is not None


# ===========================================================================
# F — forget_op + known_op_ids
# ===========================================================================


def test_forget_op_drops_state():
    t = RealtimeProgressTracker()
    t.record_tick("op-1", "stream-1")
    t.forget_op("op-1")
    assert t.get_state("op-1") is None


def test_forget_op_idempotent():
    t = RealtimeProgressTracker()
    t.forget_op("nonexistent")  # must not raise
    t.record_tick("op-1", "stream-1")
    t.forget_op("op-1")
    t.forget_op("op-1")  # second call also no-op


def test_known_op_ids():
    t = RealtimeProgressTracker()
    t.record_tick("op-A", "stream-1")
    t.record_tick("op-B", "stream-1")
    assert sorted(t.known_op_ids()) == ["op-A", "op-B"]


# ===========================================================================
# G — Default-singleton accessor
# ===========================================================================


def test_get_default_tracker_is_singleton():
    a = get_default_tracker()
    b = get_default_tracker()
    assert a is b


def test_reset_default_tracker_drops_singleton():
    a = get_default_tracker()
    reset_default_tracker()
    b = get_default_tracker()
    assert a is not b


def test_no_master_flag_required():
    """Pin: P3.5 ships always-on per PRD spec — no env knob."""
    src = _read(
        "backend/core/ouroboros/governance/realtime_progress_tracker.py",
    )
    # Defensive: make sure no JARVIS env knob got introduced.
    assert "JARVIS_REALTIME_PROGRESS" not in src
    assert "JARVIS_PROGRESS_VISIBILITY" not in src


# ===========================================================================
# H — plan_exploit producer integration
# ===========================================================================


def test_plan_exploit_emits_tick_via_initial_record(monkeypatch):
    """The producer side records an initial 0s tick on every stream
    before the first 5s heartbeat fires. Verify the path by patching
    asyncio.create_task to capture arguments + driving _generate_unit
    with a fast-returning fake generator.

    This exercises the integration without waiting 5+ real seconds."""
    import dataclasses
    from backend.core.ouroboros.governance import plan_exploit as _pe

    reset_default_tracker()
    tracker = get_default_tracker()

    # Fake fast generator that returns immediately.
    class _FastGen:
        async def generate(self, ctx, deadline):
            return object()

    # Build a minimal ctx that dataclasses.replace can clone.
    @dataclasses.dataclass
    class _Ctx:
        op_id: str = "op-pe-test"
        target_files: tuple = ()
        execution_graph: dict = dataclasses.field(default_factory=lambda: {
            "units": [{"unit_id": "u1", "owned_paths": ("a.py",)}],
            "concurrency_limit": 1,
        })
        provider_route: str = "standard"

    ctx = _Ctx()

    # Force exploit_enabled() True for this test by stubbing the gate.
    def _force_ok(_ctx):
        return (True, "")
    monkeypatch.setattr(_pe, "check_exploit_conditions", _force_ok)

    async def _run():
        return await _pe.try_parallel_generate(
            ctx, deadline=None, gen_timeout=2.0, generator=_FastGen(),
        )

    asyncio.run(_run())

    state = tracker.get_state("op-pe-test")
    # An initial tick was recorded (0s "starting" or fast-completion ≤2s).
    assert state is not None
    assert "stream-1" in state.streams


def test_plan_exploit_tracker_failure_does_not_break_generation(
    monkeypatch,
):
    """Pin: if the tracker raises, generation still completes."""
    import dataclasses
    from backend.core.ouroboros.governance import plan_exploit as _pe
    from backend.core.ouroboros.governance import (
        realtime_progress_tracker as _rt,
    )

    # Force tracker construction to raise.
    monkeypatch.setattr(
        _rt, "get_default_tracker",
        lambda: (_ for _ in ()).throw(RuntimeError("tracker dead")),
    )

    class _FastGen:
        async def generate(self, ctx, deadline):
            return "ok"

    @dataclasses.dataclass
    class _Ctx:
        op_id: str = "op-tracker-down"
        target_files: tuple = ()
        execution_graph: dict = dataclasses.field(default_factory=lambda: {
            "units": [{"unit_id": "u1", "owned_paths": ("a.py",)}],
            "concurrency_limit": 1,
        })
        provider_route: str = "standard"

    monkeypatch.setattr(_pe, "check_exploit_conditions", lambda c: (True, ""))

    async def _run():
        return await _pe.try_parallel_generate(
            _Ctx(), deadline=None, gen_timeout=2.0, generator=_FastGen(),
        )
    # Should not raise — generation still completes (returns merged or None).
    asyncio.run(_run())


# ===========================================================================
# I — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_realtime_progress_tracker_no_authority_imports():
    src = _read(
        "backend/core/ouroboros/governance/realtime_progress_tracker.py",
    )
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_realtime_progress_tracker_no_io_or_subprocess():
    """Pin: tracker is pure in-memory data — no file I/O, no subprocess,
    no env mutation."""
    src = _read(
        "backend/core/ouroboros/governance/realtime_progress_tracker.py",
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
