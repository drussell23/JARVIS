"""HiveEmitter + EdgeDebouncer — the silent-actor emission edge (Hive Step 2)."""
from __future__ import annotations

import asyncio
import threading

import pytest

from backend.api.hive_emitter import (
    EdgeDebouncer, HiveEmitter, get_default_emitter, hive_emitters_enabled,
)
from backend.api.hive_envelope import ActorEnvelope


def _mk(intent: str = "actions:test", summary: str = "click in 'test'",
        **kw) -> dict:
    base = dict(actor_id="ghost_hands.orchestrator", subsystem="actuation",
                intent=intent, summary=summary, trace_id="test", **kw)
    return base


# ---------------------------------------------------------------------------
# THE mandate test: 50 ghost-hand events in 200ms → exactly ONE envelope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fifty_events_in_200ms_yield_exactly_one_envelope(monkeypatch):
    monkeypatch.setenv("JARVIS_HIVE_DEBOUNCE_WINDOW_MS", "200")
    em = HiveEmitter()
    em.bind_loop()
    for i in range(50):
        em.emit(**_mk(), coalesce=True)
        await asyncio.sleep(0.2 / 60)  # 50 events spread inside ~170ms
    await asyncio.sleep(0.4)           # let the window close
    out = []
    while not em.out_queue.empty():
        out.append(em.out_queue.get_nowait())
    assert len(out) == 1, f"expected ONE coalesced envelope, got {len(out)}"
    env = out[0]
    assert isinstance(env, ActorEnvelope)
    assert env.coalesced_n == 50
    assert "×50" in env.action_summary
    assert env.span_ms > 0


@pytest.mark.asyncio
async def test_sequence_flush_closes_window_early(monkeypatch):
    monkeypatch.setenv("JARVIS_HIVE_DEBOUNCE_WINDOW_MS", "5000")  # long window
    em = HiveEmitter()
    em.bind_loop()
    for _ in range(7):
        em.emit(**_mk(), coalesce=True)
    em.flush("ghost_hands.orchestrator", "actions:test")   # task completed
    await asyncio.sleep(0.05)
    out = []
    while not em.out_queue.empty():
        out.append(em.out_queue.get_nowait())
    assert len(out) == 1 and out[0].coalesced_n == 7


@pytest.mark.asyncio
async def test_adaptive_window_widens_under_storm_and_decays(monkeypatch):
    monkeypatch.setenv("JARVIS_HIVE_DEBOUNCE_WINDOW_MS", "20")
    monkeypatch.setenv("JARVIS_HIVE_DEBOUNCE_MAX_MS", "160")
    monkeypatch.setenv("JARVIS_HIVE_DEBOUNCE_HIGHWATER", "5")
    sunk = []
    deb = EdgeDebouncer(sunk.append)
    key = ("a", "i")
    # storm: 6 events (>= high water) → window should double after close
    for _ in range(6):
        deb.accept(key, dict(actor_id="a", subsystem="s", intent="i",
                             action_summary="x", trace_id="t",
                             severity="info", detail={},
                             source_fabric="actor_edge"))
    deb.flush(key)
    assert deb._widths[key] == 40.0    # 20 → 40 (doubled)
    # quiet: single-event window → decays back toward base
    deb.accept(key, dict(actor_id="a", subsystem="s", intent="i",
                         action_summary="x", trace_id="t",
                         severity="info", detail={},
                         source_fabric="actor_edge"))
    deb.flush(key)
    assert deb._widths[key] == 20.0    # 40 → 20 (halved, floored at base)


@pytest.mark.asyncio
async def test_worst_severity_wins_in_a_window():
    sunk = []
    deb = EdgeDebouncer(sunk.append)
    key = ("a", "i")
    base = dict(actor_id="a", subsystem="s", intent="i", action_summary="x",
                trace_id="t", detail={}, source_fabric="actor_edge")
    deb.accept(key, dict(base, severity="info"))
    deb.accept(key, dict(base, severity="error"))
    deb.accept(key, dict(base, severity="info"))
    deb.flush(key)
    assert len(sunk) == 1 and sunk[0].severity == "error"


# ---------------------------------------------------------------------------
# thread-safety: emits from a worker thread marshal onto the bound loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_from_worker_thread_is_marshalled():
    em = HiveEmitter()
    em.bind_loop()

    def _worker():
        for _ in range(5):
            em.emit(**_mk(intent="stt", summary="final transcript: 42 chars"))

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    await asyncio.sleep(0.1)           # let call_soon_threadsafe callbacks run
    assert em.out_queue.qsize() == 5
    assert em.stats["dropped_no_loop"] == 0


# ---------------------------------------------------------------------------
# sanitization at the edge: secrets redacted, control chars stripped,
# payload bodies structurally excluded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_is_redacted_and_capped():
    em = HiveEmitter()
    em.bind_loop()
    em.emit(**_mk(summary="key sk-" + "a" * 30 + " leaked\x00\x1b " + "z" * 500))
    await asyncio.sleep(0.02)
    env = em.out_queue.get_nowait()
    assert "sk-" + "a" * 30 not in env.action_summary   # credential shape gone
    assert "\x00" not in env.action_summary             # control chars gone
    assert len(env.action_summary) <= 240               # capped


@pytest.mark.asyncio
async def test_detail_accepts_scalars_only():
    em = HiveEmitter()
    em.bind_loop()
    em.emit(**_mk(detail={"ok": True, "n": 3, "s": "fine",
                          "payload": {"body": "SECRET"},
                          "blob": [1, 2, 3]}))
    await asyncio.sleep(0.02)
    env = em.out_queue.get_nowait()
    assert env.detail == {"ok": True, "n": 3, "s": "fine"}  # dict/list dropped


# ---------------------------------------------------------------------------
# master switch + fail-soft posture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_master_switch_no_ops(monkeypatch):
    monkeypatch.setenv("JARVIS_HIVE_EMITTERS_ENABLED", "false")
    assert not hive_emitters_enabled()
    em = HiveEmitter()
    em.bind_loop()
    em.emit(**_mk())
    await asyncio.sleep(0.02)
    assert em.out_queue.empty()
    assert em.stats["dropped_disabled"] == 1


def test_emit_without_any_loop_never_raises():
    em = HiveEmitter()             # never bound, called from sync context
    em.emit(**_mk())               # must not raise
    assert em.stats["dropped_no_loop"] == 1


@pytest.mark.asyncio
async def test_default_emitter_is_process_singleton():
    assert get_default_emitter() is get_default_emitter()
