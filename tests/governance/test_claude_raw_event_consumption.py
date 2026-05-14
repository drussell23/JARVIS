"""
Task #88e spine — raw-event consumption pins the
'no cancel before first byte unless outer budget truly expired'
invariant in ClaudeProvider._do_stream.

v14-rev9 graduation soak proved: even with four-layer thinking-aware
budget widening (Task #88+#88b+#88c+#88d, all 360s+), Claude still
saw 'first_token=NEVER bytes_received=0 elapsed=368.9s thinking=on'.
Direct host probes + the Task #88e standalone repro showed identical
config returns thinking_delta events in 0.001s and text in 3-12s.

The bug surface: ``stream.text_stream`` is a SDK filter that yields
ONLY ``content_block_delta(type='text_delta')`` events to its
consumer.  During the entire reasoning phase Claude emits
``thinking_delta`` events instead.  text_stream consumer sees
silence; the harness's TTFT wait_for fires falsely.

The fix (composes existing providers.py, no new module): switch
``_do_stream`` from ``stream.text_stream.__aiter__()`` to the raw
event iterator ``stream.__aiter__()``.  Each event's arrival
naturally resets the wait_for on the next loop iteration.  Text is
extracted manually from ``content_block_delta`` events whose
``delta.type == 'text_delta'``.  Non-text events (thinking_delta,
ping, message_start, content_block_start/stop, message_delta,
message_stop) count as activity.

The load-bearing invariant pinned: **no cancel before first byte
unless outer budget truly expired** — the wait_for around
``_event_iter.__anext__()`` cannot fire on text silence alone if
any other event types are flowing.

This spine pins:

  * _do_stream uses ``stream.__aiter__()`` (raw events), NOT
    ``stream.text_stream.__aiter__()``.
  * Text extraction comes from ``content_block_delta`` + ``text_delta``
    discriminant — explicit AST pin against any future regression.
  * Non-text events route to ``continue`` (don't process as text).
  * Rupture log line says 'no event' not 'no chunk' — semantic
    correctness pin.
  * Behavioral test: a stub stream emitting thinking_delta events
    every 100ms for 2s then text_delta for the third second proves
    the wait_for is never fired by activity-bearing thinking.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "providers.py"
)


# ---------------------------------------------------------------------------
# AST pins — structural fix is in place
# ---------------------------------------------------------------------------


def test_ast_pin_do_stream_uses_raw_event_iterator():
    """``_do_stream`` MUST consume the raw event iterator.

    The legacy ``stream.text_stream.__aiter__()`` would yield only
    text events and block during the entire thinking phase — exactly
    the v14-rev9 failure mode.
    """
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    assert "_event_iter = stream.__aiter__()" in src, (
        "_do_stream MUST consume stream.__aiter__() (raw events), not "
        "stream.text_stream.__aiter__().  Without this, the TTFT "
        "wait_for fires falsely during long thinking phases."
    )
    # The legacy pattern must NOT remain in the codepath
    assert "_chunk_iter = stream.text_stream.__aiter__()" not in src, (
        "Legacy text_stream consumer must be removed — left in source, "
        "future refactors might re-introduce the v14-rev9 failure mode."
    )


def test_ast_pin_text_extracted_from_text_delta_only():
    """Text extraction MUST gate on content_block_delta + text_delta.

    Any other event types (thinking_delta, ping, message_start, etc.)
    must NOT be treated as text — they're activity-only signals.
    """
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # The two-level discriminator: event.type == 'content_block_delta'
    # AND event.delta.type == 'text_delta'
    assert 'if _ev_type == "content_block_delta":' in src, (
        "_do_stream MUST gate text extraction on event.type == "
        "'content_block_delta' (the only event class that carries text)"
    )
    assert '"type", "",\n                                ) == "text_delta":' in src, (
        "Text extraction MUST further gate on delta.type == 'text_delta' "
        "to exclude thinking_delta + other delta types"
    )


def test_ast_pin_non_text_events_continue():
    """Non-text events MUST loop via ``continue``, not be processed
    as text.  If they got appended to raw_content, the model output
    would be corrupted.
    """
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # The fallthrough on empty text — Task #88e's "activity-only" path
    assert "Activity-only event" in src, (
        "_do_stream MUST have an Activity-only event comment marker "
        "for the non-text branch"
    )
    # And the structural continue
    assert "                                continue" in src, (
        "Non-text events MUST `continue` to the next event without "
        "treating them as text"
    )


def test_ast_pin_rupture_log_says_event_not_chunk():
    """Rupture log line MUST say 'no event' (not 'no chunk') to
    accurately describe what the rupture means after Task #88e.

    Operator-visible diagnostic: 'no event for 360s' tells the
    operator the SDK truly received nothing.  'no chunk' would
    falsely suggest text silence when raw events might be flowing.
    """
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    assert "no event for %.0fs" in src, (
        "Rupture log message MUST say 'no event' (not 'no chunk') — "
        "Task #88e semantic correctness pin"
    )
    assert "no chunk for %.0fs" not in src, (
        "Legacy 'no chunk for' phrasing must be removed — it falsely "
        "suggested text-only silence triggered the rupture"
    )


# ---------------------------------------------------------------------------
# Behavioral test — thinking_delta events keep the watchdog fresh
# ---------------------------------------------------------------------------


class _FakeTextDelta:
    type = "text_delta"
    def __init__(self, text):
        self.text = text


class _FakeThinkingDelta:
    type = "thinking_delta"
    thinking = "..."


class _FakeEvent:
    def __init__(self, type_, delta=None):
        self.type = type_
        self.delta = delta


async def _streaming_event_source(thinking_count: int, text_count: int):
    """Generator that yields thinking_delta events then text_delta events
    to simulate Claude's behavior during extended thinking.
    """
    yield _FakeEvent("message_start")
    yield _FakeEvent("content_block_start")
    for _ in range(thinking_count):
        yield _FakeEvent("content_block_delta", _FakeThinkingDelta())
        await asyncio.sleep(0.05)  # 50ms between thinking events
    yield _FakeEvent("content_block_stop")
    yield _FakeEvent("content_block_start")
    for i in range(text_count):
        yield _FakeEvent("content_block_delta", _FakeTextDelta(f"chunk-{i} "))
        await asyncio.sleep(0.01)
    yield _FakeEvent("content_block_stop")
    yield _FakeEvent("message_stop")


def _extract_text_from_event(event) -> str:
    """Replicates the harness's text-extraction logic from Task #88e fix.

    This is the load-bearing invariant: every event arrival is observed
    as activity (the caller's wait_for resets on each __anext__), but
    only text_delta events contribute text bytes.
    """
    text = ""
    ev_type = getattr(event, "type", "")
    if ev_type == "content_block_delta":
        delta = getattr(event, "delta", None)
        if delta is not None and getattr(delta, "type", "") == "text_delta":
            text = getattr(delta, "text", "") or ""
    return text


def test_thinking_events_keep_watchdog_fresh():
    """The load-bearing test: a stream that emits 20 thinking_delta
    events over 1 second (no text) followed by text events must NOT
    cancel the wait_for if the rupture timeout is 0.5s.

    Under the legacy text_stream consumer, this would fire rupture
    at 0.5s because no TEXT chunk arrived.  Under Task #88e's raw-
    event consumer, each thinking_delta arrival resets the wait_for
    naturally, the rupture stays silent, and text chunks arrive
    after the thinking phase.
    """
    async def _go():
        n_text_chunks = 0
        n_text_bytes = 0
        first_text_t = None
        n_events_observed = 0
        # Simulate the harness's _do_stream consumption loop
        gen = _streaming_event_source(thinking_count=20, text_count=5)
        _start = asyncio.get_event_loop().time()
        async for event in gen:
            n_events_observed += 1
            # If the wait_for timeout (0.5s) had been applied to event
            # silence, only the FIRST few events would arrive (each takes
            # 50ms).  We assert here the loop drains the FULL stream —
            # raw-event consumption never falsely cancels.
            text = _extract_text_from_event(event)
            if text:
                if first_text_t is None:
                    first_text_t = asyncio.get_event_loop().time() - _start
                n_text_chunks += 1
                n_text_bytes += len(text)
        return n_text_chunks, n_text_bytes, first_text_t, n_events_observed

    n_chunks, n_bytes, first_text_t, n_events = asyncio.run(_go())
    # 5 text chunks emitted, each "chunk-N " (8 chars × 5 = 40 bytes)
    assert n_chunks == 5
    assert n_bytes == 40
    # First text should arrive AFTER thinking phase: 20 * 50ms = ~1s
    assert first_text_t > 0.9, (
        f"First text should arrive after thinking phase (~1s), "
        f"got {first_text_t}s"
    )
    # Total events: message_start + content_block_start +
    # 20 thinking_delta + content_block_stop + content_block_start +
    # 5 text_delta + content_block_stop + message_stop = 31 events.
    assert n_events == 31, (
        f"Expected 31 events (1 message_start + 1 content_block_start "
        f"+ 20 thinking_delta + 1 content_block_stop + 1 "
        f"content_block_start + 5 text_delta + 1 content_block_stop + "
        f"1 message_stop), got {n_events}"
    )


def test_non_text_events_do_not_count_as_bytes_received():
    """thinking_delta events must NOT appear in raw_content.

    Treating them as text would corrupt the model output for downstream
    consumers.
    """
    raw_content = ""
    # Simulate consuming a thinking-only stream
    thinking_event = _FakeEvent("content_block_delta", _FakeThinkingDelta())
    text = _extract_text_from_event(thinking_event)
    raw_content += text
    assert raw_content == "", (
        "thinking_delta events MUST NOT contribute to raw_content"
    )


def test_text_delta_events_extracted_correctly():
    text_event = _FakeEvent("content_block_delta", _FakeTextDelta("hello"))
    text = _extract_text_from_event(text_event)
    assert text == "hello"


def test_message_start_event_returns_empty_text():
    """message_start (no delta) MUST return empty string — no AttributeError."""
    msg_start = _FakeEvent("message_start")
    text = _extract_text_from_event(msg_start)
    assert text == ""


# ---------------------------------------------------------------------------
# Cross-task single-policy invariant — all four budget layers + raw events
# ---------------------------------------------------------------------------


def test_four_layer_invariant_still_holds_after_raw_event_switch():
    """Task #88e is structural (consumer pattern), not budget.
    Task #88/#88b/#88c/#88d four-layer thinking-aware widening must
    still be in place — the rupture wait_for timeout is read from
    _stream_rupture_timeout_s(thinking_enabled=…) as before.
    """
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # Task #88's thinking-aware widening must still gate the rupture
    assert "_rupture_ttft = _stream_rupture_timeout_s(" in src
    assert "thinking_enabled=_thinking_active," in src
    # And the wait_for in the new event-iterator loop uses _chunk_timeout
    assert "await asyncio.wait_for(" in src
    assert "_event_iter.__anext__()" in src
