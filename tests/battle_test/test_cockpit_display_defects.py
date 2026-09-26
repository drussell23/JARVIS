"""Four cockpit display defects measured live on 2026-09-26, pinned at their roots.

Recorded from a real `ov` in a 200x50 tmux pane (session bt-2026-09-26-074849):

1. The thinking line printed ``[dim]· Synthesizing… (2s · ↓ 309 tokens)[/dim]``
   verbatim, 68 times in ten minutes. The stream strip draws ``str`` rows as
   ANSI; the producer handed it a Rich MARKUP string.
2. The status bar read ``landed 0 · 0m in`` beneath "sentinel landed … 06b35c990f"
   although the daemon had logged ``[Landed] #1``. The heartbeat's hand-typed
   serializer listed 13 of the snapshot's 21 fields.
3. ``[✓] Generated 125 tokens via Claude`` on the local lane, with $0 of Claude
   credit. One process-wide stream slot credited the lane whose stream opened
   FIRST for the op (a failed Claude attempt), and the receipt reached cockpits
   that the elected per-op transport already serves.
4. ``IDLE`` through whole generations. The builder asked the loop's runtime
   entries (``LoopRuntimeContext``) for ``phase`` / ``phase_entered_at``, which
   only ``OperationContext`` has, so every live op looked phase-less.
"""
from __future__ import annotations

import contextlib
import io
import math
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest


# --------------------------------------------------------------------------
# 1. Styled rows reach the strip as ANSI, raw text passes through untouched
# --------------------------------------------------------------------------

def _row_lines(rows):
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        dynamic_row_lines,
    )
    return dynamic_row_lines(rows)


def test_a_str_row_is_drawn_byte_identical():
    """The strip also carries raw tool output; its brackets are content."""
    raw = "  [1/3] a[0] = b[dim] \x1b[2mpre-escaped\x1b[0m"
    assert _row_lines([raw]) == [raw]


def test_a_rich_Text_row_is_resolved_to_ANSI_not_printed_as_tags():
    from rich.text import Text
    [line] = _row_lines([Text("· Weaving… (3s)", style="dim")])
    assert "[dim]" not in line and "[/dim]" not in line
    assert "\x1b[" in line and "· Weaving… (3s)" in line


def test_a_multiline_renderable_claims_every_row_it_draws():
    from rich.text import Text
    assert len(_row_lines([Text("one\ntwo\nthree")])) == 3


def test_one_bad_row_never_blanks_the_strip():
    class _Boom:
        def __rich__(self):
            raise RuntimeError("render fault")
    lines = _row_lines(["kept", None, _Boom(), "also kept"])
    assert lines[0] == "kept" and lines[-1] == "also kept"


def test_the_real_thinking_line_carries_no_markup_through_the_strip():
    import time

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
    ui._stream_op = "op-display-defects"
    ui._stream_started = time.monotonic() - 3
    ui._stream_tokens = 309
    row = ui._thinking_line()
    assert not isinstance(row, str) or row == ""
    [line] = _row_lines([row])
    assert "[dim]" not in line and "[/" not in line
    assert "309 tokens" in line


def test_theme_tokens_resolve_through_the_shared_translator():
    """A scratch Console has no theme; `muted` used to vanish silently."""
    from backend.core.ouroboros.ui.markup_ansi import markup_to_ansi
    assert "\x1b[" in markup_to_ansi("[muted]x[/muted]")


# --------------------------------------------------------------------------
# 2. Every snapshot field crosses the bridge
# --------------------------------------------------------------------------

def _non_default_snapshot():
    import dataclasses

    from backend.core.ouroboros.battle_test.status_line import StatusSnapshot
    values = {}
    for f in dataclasses.fields(StatusSnapshot):
        d = f.default
        if isinstance(d, bool):
            values[f.name] = not d
        elif isinstance(d, int):
            values[f.name] = d + 7
        elif isinstance(d, float):
            values[f.name] = d + 1.5
        elif isinstance(d, str):
            values[f.name] = f"v-{f.name}"
        else:                                   # Optional[...] = None
            values[f.name] = 42.0
    return StatusSnapshot(**values)


def test_the_payload_round_trips_EVERY_field():
    """Derived, not hand-typed: a field added to the snapshot cannot be
    dropped on the wire without this failing."""
    from backend.core.ouroboros.battle_test.status_line import (
        payload_to_snapshot, snapshot_to_payload,
    )
    snap = _non_default_snapshot()
    assert payload_to_snapshot(snapshot_to_payload(snap)) == snap


def test_the_measured_landed_reading_survives():
    from backend.core.ouroboros.battle_test.status_line import (
        StatusSnapshot, payload_to_snapshot, snapshot_to_payload,
    )
    snap = StatusSnapshot(landed_total=1, landed_uptime_s=540.0)
    back = payload_to_snapshot(snapshot_to_payload(snap))
    assert back.landed_total == 1 and back.landed_uptime_s == 540.0


def test_a_non_finite_float_degrades_to_its_default_not_the_frame():
    import json

    from backend.core.ouroboros.battle_test.status_line import (
        StatusSnapshot, snapshot_to_payload,
    )
    payload = snapshot_to_payload(StatusSnapshot(cost_spent_usd=math.nan,
                                                 landed_total=2))
    json.dumps(payload, allow_nan=False)           # strict JSON
    assert payload["cost_spent_usd"] == 0.0
    assert payload["landed_total"] == 2


def test_the_bridge_status_provider_uses_the_one_serializer():
    """The harness's second hand-typed copy is what drifted first."""
    import ast
    from pathlib import Path

    src = Path("backend/core/ouroboros/battle_test/harness.py").read_text(
        encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_status_provider")
    called = {getattr(c.func, "id", getattr(c.func, "attr", ""))
              for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "snapshot_to_payload" in called
    assert not any(isinstance(n, ast.Dict) and len(n.keys) > 3
                   for n in ast.walk(fn))


# --------------------------------------------------------------------------
# 3. The receipt credits the lane that produced the tokens, on the right surface
# --------------------------------------------------------------------------

@pytest.fixture()
def flow():
    from rich.console import Console

    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    sf = SerpentFlow(session_id="t", branch_name="b")
    sf._local_buf = io.StringIO()
    sf.console = Console(file=sf._local_buf, force_terminal=False, width=160,
                         color_system=None)
    sf._mirrored = []
    sf.markup_mirror = sf._mirrored.append
    return sf


@pytest.fixture()
def no_election():
    from backend.core.ouroboros.battle_test import stream_renderer as sr
    prior = sr._INFLIGHT_PUBLISHER
    sr.set_inflight_publisher(None)
    yield sr
    sr.set_inflight_publisher(prior)


def _receipts(lines):
    return [str(x) for x in lines if "Generated" in str(x)]


def test_a_fallback_lane_gets_the_credit_not_the_failed_one(flow, no_election):
    """THE measured case: Claude opened a stream, failed, the local lane
    served the same op."""
    flow.show_streaming_start("claude", op_id="op-a")
    flow.show_streaming_start("local", op_id="op-a")        # lane switch
    for _ in range(125):
        flow.show_streaming_token("x", op_id="op-a")
    flow.show_streaming_end("op-a")
    [receipt] = _receipts(flow._mirrored)
    assert "125 tokens" in receipt and "local" in receipt
    assert "Claude" not in receipt


def test_the_measured_case_through_the_UNSCOPED_legacy_calls(flow, no_election):
    """Same sequence, no op_id on tokens or end — the calls every caller made
    before. Failed on the old code with exactly the live receipt."""
    flow.show_streaming_start(provider="claude", op_id="op-a")
    flow.show_streaming_start(provider="local", op_id="op-a")
    for _ in range(125):
        flow.show_streaming_token("x")
    flow.show_streaming_end()
    [receipt] = _receipts(flow._mirrored)
    assert "Claude" not in receipt and "local" in receipt


def test_concurrent_ops_do_not_clobber_each_other(flow, no_election):
    flow.show_streaming_start("doubleword", op_id="op-a")
    flow.show_streaming_start("local", op_id="op-b")
    for i in range(10):
        flow.show_streaming_token("x", op_id="op-a" if i % 2 else "op-b")
    flow.show_streaming_token("x", op_id="op-b")
    flow.show_streaming_end("op-a")
    flow.show_streaming_end("op-b")
    receipts = _receipts(flow._mirrored)
    assert any("5 tokens" in r and "DW" in r for r in receipts)
    assert any("6 tokens" in r and "local" in r for r in receipts)


def test_a_duplicate_end_never_closes_another_ops_stream(flow, no_election):
    """Two render paths deliver the same PHASE_END. The second names a
    closed stream and must not finish op-b's generation early."""
    flow.show_streaming_start("local", op_id="op-a")
    flow.show_streaming_start("local", op_id="op-b")
    flow.show_streaming_token("x", op_id="op-a")
    flow.show_streaming_token("x", op_id="op-b")
    flow.show_streaming_end("op-a")
    flow.show_streaming_end("op-a")                 # the duplicate
    assert "op-b" in flow._stream_tallies
    flow.show_streaming_token("x", op_id="op-b")
    flow.show_streaming_end("op-b")
    assert any("2 tokens" in r for r in _receipts(flow._mirrored))


def test_an_elected_transport_keeps_the_receipt_off_the_cockpit(flow, no_election):
    """The cockpit already has the thinking indicator + op recap; the local
    console keeps its receipt."""
    no_election.set_inflight_publisher("claude_style_transport")
    flow.show_streaming_start("local", op_id="op-a")
    flow.show_streaming_token("x", op_id="op-a")
    flow.show_streaming_end("op-a")
    assert _receipts(flow._mirrored) == []
    assert "Generated 1 tokens" in flow._local_buf.getvalue()


def test_teardown_closes_every_open_stream(flow, no_election):
    import asyncio

    for op in ("op-a", "op-b"):
        flow.show_streaming_start("local", op_id=op)
        flow.show_streaming_token("x", op_id=op)
    asyncio.run(flow.stop())
    assert flow._stream_tallies == {}
    assert len(_receipts(flow._mirrored)) == 2


def test_an_op_that_never_ended_its_stream_does_not_leak(flow, no_election):
    flow.op_started("op-a", "g", ["a.py"], "SAFE_AUTO", sensor="s")
    flow.show_streaming_start("claude", op_id="op-a")
    flow.op_completed("op-a", files_changed=[], cost_usd=0.0)
    assert "op-a" not in flow._stream_tallies


class _Narrow:
    """A renderer with the older signatures (many test doubles)."""
    def __init__(self):
        self.calls = []

    def show_streaming_token(self, token):
        self.calls.append(("token", token))

    def show_streaming_end(self):
        self.calls.append(("end",))


class _RaisesInside:
    def __init__(self):
        self.calls = 0

    def show_streaming_end(self, op_id=""):
        self.calls += 1
        raise TypeError("inside the renderer")


def _event(kind, **kw):
    return SimpleNamespace(kind=kind, op_id="op-a", content="", **kw)


def test_the_backend_scopes_calls_only_for_renderers_that_can_take_it():
    from backend.core.ouroboros.governance.render_backends import (
        SerpentFlowBackend,
    )
    narrow = _Narrow()
    backend = SerpentFlowBackend(narrow)
    backend._call_scoped("show_streaming_token", "t", op_id="op-a")
    backend._call_scoped("show_streaming_end", op_id="op-a")
    assert narrow.calls == [("token", "t"), ("end",)]


def test_a_TypeError_inside_the_renderer_is_not_retried_as_a_second_call():
    from backend.core.ouroboros.governance.render_backends import (
        SerpentFlowBackend,
    )
    r = _RaisesInside()
    with pytest.raises(TypeError):
        SerpentFlowBackend(r)._call_scoped("show_streaming_end", op_id="op-a")
    assert r.calls == 1


# --------------------------------------------------------------------------
# 4. The live pipeline phase reaches the status line for every op
# --------------------------------------------------------------------------

def _runtime_ctx(op_id):
    from backend.core.ouroboros.governance.contracts.fsm_contract import (
        LoopRuntimeContext,
    )
    return LoopRuntimeContext(op_id=op_id)


def test_the_runtime_entry_records_a_transition_as_phase_and_progress():
    ctx = _runtime_ctx("op-a")
    before = ctx.last_activity_at_utc
    at = datetime.now(timezone.utc)
    ctx.observe_pipeline_phase("GENERATE", at=at)
    assert (ctx.pipeline_phase, ctx.pipeline_phase_entered_at) == ("GENERATE", at)
    assert ctx.last_activity_at_utc >= before


def test_the_status_line_reads_the_phase_off_the_loops_runtime_entries():
    """THE measured case: a pool op mid-GENERATE used to read IDLE."""
    from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
    ctx = _runtime_ctx("op-gen")
    ctx.observe_pipeline_phase("GENERATE")
    gls = SimpleNamespace(_fsm_contexts={"op-gen": ctx}, _active_ops={"op-gen"})
    snap = StatusLineBuilder(governed_loop_service=gls).snapshot()
    assert snap.phase == "GENERATE"
    assert snap.primary_op_id == "op-gen"


def test_a_duck_typed_context_is_not_mistaken_for_a_phase():
    """A MagicMock answers `pipeline_phase` with a truthy stand-in; taking it
    rendered a mock repr as the phase. The OperationContext names still win."""
    from unittest.mock import MagicMock

    from backend.core.ouroboros.battle_test.status_line import _live_phase
    ctx = MagicMock()
    ctx.phase = SimpleNamespace(name="VALIDATE")
    ctx.phase_entered_at = datetime.now(timezone.utc)
    label, entered = _live_phase(ctx)
    assert label == "VALIDATE" and entered is ctx.phase_entered_at


def test_an_op_with_no_observed_transition_still_falls_back_honestly():
    from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
    gls = SimpleNamespace(_fsm_contexts={"op-new": _runtime_ctx("op-new")},
                          _active_ops={"op-new"})
    assert StatusLineBuilder(governed_loop_service=gls).snapshot().phase == "IDLE"


def test_the_loop_mirrors_transitions_from_the_one_choke_point():
    """Registered as a bound method; removable by equality (a bound method
    is a new object on every access, so identity could never remove it)."""
    from backend.core.ouroboros.governance import op_context as oc
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopService,
    )
    svc = GovernedLoopService.__new__(GovernedLoopService)
    svc._fsm_contexts = {"op-a": _runtime_ctx("op-a")}
    oc.register_phase_transition_observer(svc._on_pipeline_phase)
    try:
        oc._notify_phase_transition("op-a", "VALIDATE")
        oc._notify_phase_transition("op-unknown", "VALIDATE")   # not in flight
        assert svc._fsm_contexts["op-a"].pipeline_phase == "VALIDATE"
    finally:
        assert oc.unregister_phase_transition_observer(svc._on_pipeline_phase)
    assert svc._on_pipeline_phase not in oc._PHASE_TRANSITION_OBSERVERS
