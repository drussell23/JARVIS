"""Typed transcript blocks: the cockpit as a component tree (2026-10-09).

Live blocks re-render in place; final blocks freeze exactly once. Frames carry
full state, so duplicates, stale revisions and out-of-order delivery are
harmless; ``final`` is terminal. Pinned here against the real protocol, store,
canvas, bridge and production transport.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from io import StringIO
from pathlib import Path

import pytest

from backend.core.ouroboros.ui.block_protocol import (
    Block, BlockClock, FINAL, encode_frame, fit, frame_limit_bytes,
)
from backend.core.ouroboros.ui.block_store import BlockStore, block_lines


def _store():
    frozen, views = [], []
    store = BlockStore(on_freeze=frozen.append, on_live=lambda bs: views.append(bs))
    return store, frozen, views


# ------------------------------------------------------------------ protocol


def test_wire_round_trip_and_shape_validation():
    b = BlockClock().open("tool", op_id="op", lines=("a", "b"), meta={"k": 1})
    assert Block.from_wire(b.to_wire()) == b
    for junk in (None, 1, {}, {"id": "x", "state": "weird"}, {"id": "x", "state": "live", "lines": 3}):
        assert Block.from_wire(junk) is None


def test_final_is_terminal_and_revisions_are_monotonic():
    clock = BlockClock()
    b = clock.open("tool")
    r2 = clock.revise(b, lines=("x",))
    done = clock.revise(r2, state=FINAL)
    assert (b.rev, r2.rev, done.rev) == (1, 2, 3)
    assert clock.revise(done, lines=("y",)) is done


def test_an_oversized_block_fits_the_frame_budget_in_both_forms():
    big = BlockClock().open("diff", state=FINAL, lines=tuple("x" * 200 for _ in range(5000)))
    budget = 64 * 1024
    data = encode_frame(big, limit=budget)
    assert data is not None and len(data) <= budget and data.endswith(b"\n")
    fitted = fit(big, limit=budget)
    assert any("elided" in ln for ln in fitted.lines) and fitted.lines[0] == big.lines[0]


def test_frame_limit_is_env_tunable_with_a_floor(monkeypatch):
    monkeypatch.setenv("JARVIS_ATTACH_FRAME_MAX_BYTES", "10")
    assert frame_limit_bytes() == 64 * 1024
    monkeypatch.setenv("JARVIS_ATTACH_FRAME_MAX_BYTES", str(8 * 1024 * 1024))
    assert frame_limit_bytes() == 8 * 1024 * 1024


# ------------------------------------------------------------------ store


def test_out_of_order_duplicates_and_late_live_frames_are_harmless():
    clock = BlockClock()
    b1 = clock.open("tool", lines=("start",))
    b2 = clock.revise(b1, lines=("mid",))
    b3 = clock.revise(b2, lines=("done",), state=FINAL)
    store, frozen, _ = _store()
    for frame in (b2, b1, b2, b3, b2, b1):
        store.apply(frame)
    assert [f.lines for f in frozen] == [("done",)]
    assert store.live() == ()


def test_live_blocks_are_ordered_by_creation_not_arrival():
    clock = BlockClock()
    first, second = clock.open("tool"), clock.open("tool")
    store, _, _ = _store()
    store.apply(second)
    store.apply(first)
    assert [b.id for b in store.live()] == [first.id, second.id]


def test_live_blocks_are_bounded_and_expire(monkeypatch):
    monkeypatch.setenv("JARVIS_COCKPIT_LIVE_BLOCKS_MAX", "2")
    now = [0.0]
    frozen = []
    store = BlockStore(on_freeze=frozen.append, clock=lambda: now[0])
    clock = BlockClock()
    blocks = [clock.open("tool") for _ in range(3)]
    for b in blocks:
        store.apply(b)
    assert len(store.live()) == 2 and frozen[0].id == blocks[0].id
    monkeypatch.setenv("JARVIS_COCKPIT_LIVE_BLOCK_TTL_S", "5")
    now[0] = 100.0
    assert store.expire() == 2 and store.live() == ()
    assert all(f.meta.get("interrupted") for f in frozen)


def test_abandon_freezes_what_was_live_as_interrupted():
    store, frozen, _ = _store()
    store.apply(BlockClock().open("tool", lines=("● Bash(pytest)",)))
    assert store.abandon() == 1
    assert "interrupted" in block_lines(frozen[0])[-1]


def test_a_faulty_sink_never_wedges_intake():
    def boom(_):
        raise RuntimeError("renderer fault")
    store = BlockStore(on_freeze=boom, on_live=boom)
    clock = BlockClock()
    b = clock.open("tool")
    assert store.apply(b) is True
    assert store.apply(clock.revise(b, state=FINAL)) is True


# ------------------------------------------------------------------ canvas


def test_live_region_renders_after_history_and_freezes_in_place():
    from backend.core.ouroboros.battle_test.bipartite_layout import BipartiteLayout
    canvas = BipartiteLayout()
    clock = BlockClock()
    store = BlockStore(
        on_freeze=lambda b: [canvas.push_raw(ln) for ln in block_lines(b)],
        on_live=lambda bs: canvas.set_live("blocks", [ln for b in bs for ln in block_lines(b)]))
    canvas.push_raw("  ● Read(a.py)")
    b = clock.open("tool", lines=("  ● Search(x)",))
    store.apply(b)
    live_view = canvas._transcript()
    assert live_view[-2:] == ("", "  ● Search(x)")
    store.apply(clock.revise(b, lines=("  ● Search(x)  1ms",), state=FINAL))
    assert canvas._transcript()[-2:] == ("", "  ● Search(x)  1ms")
    assert canvas._live_lines() == []


def test_the_streaming_tail_is_actually_drawn():
    """It went to the buffer's pending slot, which the canvas never read."""
    from backend.core.ouroboros.battle_test.bipartite_layout import BipartiteLayout
    canvas = BipartiteLayout()
    canvas.set_streaming_tail("partial output")
    assert canvas._transcript()[-1] == "partial output"
    canvas.set_streaming_tail("")
    assert "partial output" not in canvas._transcript()


# ------------------------------------------------------------------ bridge


def test_bridge_routes_by_declared_capability_and_replays_live(monkeypatch):
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge, CockpitAttachClient,
    )
    monkeypatch.setattr(os, "get_terminal_size", lambda *a, **k: os.terminal_size((120, 40)))

    async def run():
        sock = Path(tempfile.mkdtemp()) / "a.sock"
        bridge = CockpitAttachBridge(path=sock)
        assert await bridge.start()
        clock = BlockClock()
        early = clock.open("tool", lines=("  ● Read(a.py)",))
        bridge.publish_block(early)
        store, frozen, _ = _store()
        legacy: list = []
        modern = CockpitAttachClient(path=sock, on_block=store.apply,
                                     on_markup=lambda t, *a: None)
        old = CockpitAttachClient(path=sock, on_markup=lambda t, *a: legacy.append(t))
        assert await modern.connect() and await old.connect()
        modern.send_caps()
        old.send_caps()
        await asyncio.sleep(0.3)
        assert [b.id for b in store.live()] == [early.id]
        bridge.publish_block(clock.revise(early, lines=("  ● Read(a.py)  1ms",), state=FINAL))
        await asyncio.sleep(0.3)
        await bridge.stop()
        await modern.close()
        await old.close()
        return frozen, legacy

    frozen, legacy = asyncio.run(run())
    assert len(frozen) == 1 and frozen[0].lines == ("  ● Read(a.py)  1ms",)
    assert [t.strip() for t in legacy] == ["● Read(a.py)  1ms"]     # final lines only


def test_an_oversized_frame_skips_without_detaching():
    from backend.core.ouroboros.battle_test.cockpit_attach import CockpitAttachClient

    async def run():
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b'{"type":"line","text":"' + b"x" * 200 + b'"}\n')
        reader.feed_data(b'{"type":"line","text":"ok"}\n')
        reader.feed_eof()
        seen: list = []
        client = CockpitAttachClient(on_line=seen.append)
        client._reader = reader
        await client._read_loop()
        return seen

    assert asyncio.run(run()) == ["ok"]


# ------------------------------------------------------------------ transport


def test_production_transport_emits_a_tool_as_one_live_then_final_block():
    from rich.console import Console
    from backend.core.ouroboros.governance.claude_style_transport import ClaudeStyleTransport
    tr = ClaudeStyleTransport(console=Console(file=StringIO(), width=100))
    blocks: list = []
    tr.block_sink = blocks.append
    start = {"tool_name": "read_file", "tool_args_summary": "a.py", "round_index": 0,
             "tool_starting": True}
    done = {"tool_name": "read_file", "tool_args_summary": "a.py", "round_index": 0,
            "result_preview": "# a.py  (lines 1-3 of 3)\n1: x", "status": "success",
            "duration_ms": 4.0}
    tr._handle_heartbeat("op-1", start)
    tr._handle_heartbeat("op-1", done)
    assert len(blocks) == 2 and blocks[0].id == blocks[1].id
    assert not blocks[0].final and blocks[1].final
    assert any("Read 3 lines" in ln for ln in blocks[1].lines)


def test_production_transport_renders_the_apply_diff_once_and_no_false_death():
    from rich.console import Console
    from backend.core.ouroboros.governance.claude_style_transport import ClaudeStyleTransport
    out: list = []
    tr = ClaudeStyleTransport(console=Console(file=StringIO(), width=100))
    tr.markup_mirror = out.append
    tr._handle_intent("op-1", {"goal": "g", "target_files": ["t.py"], "risk_tier": "SAFE_AUTO"})
    tr._handle_intent("op-1", {"goal": "g", "target_files": ["t.py"], "risk_tier": "SAFE_AUTO"})
    diff = "--- /dev/null\n+++ b/t.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    tr._handle_heartbeat("op-1", {"phase": "APPLY", "target_file": "t.py"})
    tr._handle_heartbeat("op-1", {"phase": "APPLY", "target_file": "t.py", "diff_text": diff})
    tr._handle_decision("op-1", {"outcome": "applied"})
    tr._handle_postmortem("op-1", {"root_cause": "none", "failed_phase": None})
    from rich.text import Text
    plain = [Text.from_markup(ln).plain for ln in out]
    assert sum("Update(t.py)" in p for p in plain) == 1
    assert any("Added 2 lines" in p for p in plain)
    assert sum("g" in p and p.lstrip().startswith(("●", "*")) for p in plain if "Update" not in p) <= 1
    assert not any("postmortem" in p for p in plain)
