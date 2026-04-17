"""StreamRenderer tests — token-level operator-visible streaming.

Covers the four architectural mandates for the Ouroboros GENERATE-phase
streaming UX fix:

1. **Async isolation** — ``on_token`` never blocks the provider stream;
   queue-full drops rather than blocking; dedicated consumer task
   drives Live updates.
2. **Syntax-aware render** — Rich.Markdown handles partial code-fence
   regions gracefully.
3. **Kill switch** — ``JARVIS_UI_STREAMING_ENABLED=0`` disables entirely
   (no Live, no consumer, no INFO line emitted).
4. **Observability anchor** — ``[StreamRender]`` INFO emits TTFT + TPS.

Plus: lifecycle invariants (idempotent start/end, reusable across ops),
provider-seam regression (AST canary that providers wire the renderer).
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

from backend.core.ouroboros.battle_test import stream_renderer as sr
from backend.core.ouroboros.battle_test.stream_renderer import (
    StreamRenderer,
    get_stream_renderer,
    register_stream_renderer,
    reset_stream_renderer,
    streaming_enabled,
)


@pytest.fixture(autouse=True)
def _reset_renderer(monkeypatch):
    """Clear env + singleton between cases so tests don't leak state."""
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_UI_STREAMING_"):
            monkeypatch.delenv(key, raising=False)
    reset_stream_renderer()
    yield
    reset_stream_renderer()


# ---------------------------------------------------------------------------
# (1) Env gate — JARVIS_UI_STREAMING_ENABLED
# ---------------------------------------------------------------------------


def test_env_gate_default_on():
    assert streaming_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "  0  "])
def test_env_gate_off_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_UI_STREAMING_ENABLED", value)
    assert streaming_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "  1  "])
def test_env_gate_on_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_UI_STREAMING_ENABLED", value)
    assert streaming_enabled() is True


# ---------------------------------------------------------------------------
# (2) Singleton registration
# ---------------------------------------------------------------------------


def test_default_singleton_is_none():
    assert get_stream_renderer() is None


def test_register_and_reset_singleton():
    r = StreamRenderer()
    register_stream_renderer(r)
    assert get_stream_renderer() is r
    reset_stream_renderer()
    assert get_stream_renderer() is None


# ---------------------------------------------------------------------------
# (3) Lifecycle without a running loop — graceful no-op
# ---------------------------------------------------------------------------


def test_start_without_running_loop_is_noop():
    """Sync context (no loop) — renderer must not raise; stays inactive."""
    r = StreamRenderer()
    r.start("op-sync", "claude")
    assert r.active is False
    # on_token silently ignored.
    r.on_token("abc")
    assert r.token_count == 0
    r.end()  # idempotent no-op


def test_on_token_before_start_is_noop():
    r = StreamRenderer()
    r.on_token("early")
    assert r.token_count == 0
    assert r.buffer == ""


def test_end_before_start_is_noop():
    r = StreamRenderer()
    r.end()  # should not raise


# ---------------------------------------------------------------------------
# (4) Disabled-gate lifecycle — no-op start, no-op token, no INFO
# ---------------------------------------------------------------------------


def test_start_with_gate_off_is_noop(monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_UI_STREAMING_ENABLED", "0")

    async def _run():
        r = StreamRenderer()
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-disabled", "claude")
            assert r.active is False
            r.on_token("ignored")
            r.end()
            assert r.token_count == 0
        # No INFO line emitted when disabled — only the DEBUG log.
        infos = [
            rec for rec in caplog.records
            if rec.levelno == logging.INFO
            and rec.name == "Ouroboros.StreamRenderer"
        ]
        assert infos == []

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (5) Async lifecycle — happy path metrics
# ---------------------------------------------------------------------------


def test_happy_path_lifecycle_counts_tokens_and_emits_info(caplog):
    async def _run():
        r = StreamRenderer()
        # No console so Live stays None — consumer still drains queue.
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-happy", "claude")
            assert r.active is True
            # Feed a handful of tokens with small delays so the consumer
            # gets time to drain + batch.
            for chunk in ["Hello ", "world", "\n", "```python\n", "x = 1\n", "```"]:
                r.on_token(chunk)
            # Yield the loop so the consumer task can batch + update.
            await asyncio.sleep(0.08)
            # Capture pre-end state for assertions — end() clears counters
            # so the instance can be reused across ops.
            pre_end_count = r.token_count
            pre_end_dropped = r.dropped_count
            pre_end_buffer = r.buffer
            r.end()
        # Every token counted (queue is 256-deep; no drops expected).
        assert pre_end_count == 6
        assert pre_end_dropped == 0
        assert "Hello world" in pre_end_buffer
        assert "```python" in pre_end_buffer
        # INFO line contract: required keys present, TTFT ≥ 0, TPS ≥ 0.
        infos = [
            rec for rec in caplog.records
            if rec.levelno == logging.INFO
            and rec.name == "Ouroboros.StreamRenderer"
        ]
        assert len(infos) == 1
        msg = infos[0].getMessage()
        assert "[StreamRender]" in msg
        assert "op=op-happy" in msg
        assert "provider=claude" in msg
        assert "tokens=6" in msg
        assert "dropped=0" in msg
        assert "first_token_ms=" in msg
        assert "total_ms=" in msg
        assert "tps=" in msg

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (6) TTFT + TPS arithmetic — metric correctness
# ---------------------------------------------------------------------------


def test_ttft_and_tps_are_populated(caplog):
    async def _run():
        r = StreamRenderer()
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-metrics", "claude")
            # Delay before first token so TTFT is measurably > 0.
            await asyncio.sleep(0.03)
            r.on_token("first")
            for _ in range(9):
                r.on_token("x")
            await asyncio.sleep(0.05)
            r.end()
        line = caplog.records[-1].getMessage()
        # Parse the INFO line for the metrics.
        parts = dict(kv.split("=") for kv in line.split() if "=" in kv and not kv.startswith("["))
        ttft_ms = int(parts["first_token_ms"])
        total_ms = int(parts["total_ms"])
        tokens = int(parts["tokens"])
        tps = float(parts["tps"])
        assert ttft_ms >= 10  # we slept ~30ms before first token
        assert total_ms > ttft_ms
        assert tokens == 10
        # tps ≈ tokens / total_s; loose bound because scheduler jitter.
        assert tps > 0.0
        assert tps < 10_000.0  # sanity — not garbage from div-by-zero

    asyncio.run(_run())


def test_no_tokens_emits_info_with_ttft_negative_one(caplog):
    """Start → end with zero on_token calls: TTFT sentinel is -1 so
    downstream parsers can distinguish 'never got a token' from a fast
    first token at 0ms."""
    async def _run():
        r = StreamRenderer()
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-empty", "claude")
            await asyncio.sleep(0.02)
            r.end()
        line = caplog.records[-1].getMessage()
        assert "first_token_ms=-1" in line
        assert "tokens=0" in line
        assert "tps=0.0" in line

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (7) Async isolation — on_token is O(1) non-blocking; queue-full drops
# ---------------------------------------------------------------------------


def test_queue_full_drops_tokens_without_blocking(monkeypatch, caplog):
    """Simulate a render-lag scenario: shrink the queue to 4 slots,
    never yield to the consumer, then flood on_token. Producer must
    not block; dropped_count reflects the overflow; consumer never
    ran so token_count stays at whatever made it into the queue."""
    monkeypatch.setenv("JARVIS_UI_STREAMING_QUEUE_MAX", "4")
    # Reimport to pick up the new queue size (module-level constant).
    import importlib
    from backend.core.ouroboros.battle_test import stream_renderer as _sr
    importlib.reload(_sr)

    async def _run():
        r = _sr.StreamRenderer()
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-overflow", "claude")
            # Fire 100 tokens in a tight synchronous burst — no awaits,
            # so the consumer never runs. Queue is 4-deep; 96 must drop.
            for i in range(100):
                r.on_token(f"tok{i}")
            # Give the consumer one tick to drain the 4 queued tokens.
            await asyncio.sleep(0.05)
            pre_end_count = r.token_count
            pre_end_dropped = r.dropped_count
            r.end()
        # After-the-fact: consumer picked up the 4 that fit.
        assert pre_end_count <= 4
        assert pre_end_dropped >= 96
        # Also verify the INFO line reports the drop count.
        line = caplog.records[-1].getMessage()
        assert "dropped=" in line
        # Extract dropped count from INFO line to cross-check.
        parts = dict(
            kv.split("=") for kv in line.split()
            if "=" in kv and not kv.startswith("[")
        )
        assert int(parts["dropped"]) >= 96

    asyncio.run(_run())


def test_on_token_returns_synchronously_without_loop_yield():
    """Producer must NOT await anywhere inside on_token. Test by
    calling it from sync code inside an async context — if it awaited,
    we'd get a RuntimeWarning or the call would never return."""
    async def _run():
        r = StreamRenderer()
        r.start("op-sync-check", "claude")
        # A hundred sync calls in a single event-loop tick.
        for _ in range(100):
            r.on_token("x")
        # If on_token awaited internally, we'd never get here without
        # yielding. We reached this line without any await — contract met.
        r.end()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (8) Idempotency — end() is safe to call twice; start-while-active rolls
# ---------------------------------------------------------------------------


def test_end_is_idempotent(caplog):
    async def _run():
        r = StreamRenderer()
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-1", "claude")
            r.on_token("x")
            await asyncio.sleep(0.03)
            r.end()
            # Second end() is a no-op — no second INFO line.
            r.end()
        infos = [
            rec for rec in caplog.records
            if rec.levelno == logging.INFO
            and rec.name == "Ouroboros.StreamRenderer"
        ]
        assert len(infos) == 1

    asyncio.run(_run())


def test_start_while_active_ends_prior_session(caplog):
    """Defensive: if caller forgets to end() before starting a new op,
    the prior session is closed cleanly (one INFO line) before the
    new one opens (second INFO line on its own end())."""
    async def _run():
        r = StreamRenderer()
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-A", "claude")
            r.on_token("a")
            await asyncio.sleep(0.03)
            # Don't call end() — start a new op while active.
            r.start("op-B", "claude")
            r.on_token("b")
            await asyncio.sleep(0.03)
            r.end()
        infos = [
            rec for rec in caplog.records
            if rec.levelno == logging.INFO
            and rec.name == "Ouroboros.StreamRenderer"
        ]
        # Two INFO lines: one for each session's end.
        assert len(infos) == 2
        assert "op=op-A" in infos[0].getMessage()
        assert "op=op-B" in infos[1].getMessage()

    asyncio.run(_run())


def test_renderer_is_reusable_across_ops(caplog):
    """One renderer instance handles multiple sequential sessions
    without state bleed. token_count / buffer reset between ops."""
    async def _run():
        r = StreamRenderer()
        with caplog.at_level(logging.INFO, logger="Ouroboros.StreamRenderer"):
            r.start("op-first", "claude")
            r.on_token("111")
            await asyncio.sleep(0.03)
            r.end()
            assert r.token_count == 0  # cleared on end()
            assert r.buffer == ""

            r.start("op-second", "claude")
            r.on_token("222")
            await asyncio.sleep(0.03)
            r.end()

        infos = [
            rec for rec in caplog.records
            if rec.levelno == logging.INFO
            and rec.name == "Ouroboros.StreamRenderer"
        ]
        assert len(infos) == 2
        assert "op=op-first" in infos[0].getMessage()
        assert "op=op-second" in infos[1].getMessage()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (9) Syntax-aware render — Markdown handles partial fenced blocks
# ---------------------------------------------------------------------------


def test_partial_markdown_fence_does_not_raise():
    """When the buffer contains an unclosed ```python ... fence, the
    Markdown widget construction must not raise. This is the exact
    mid-stream state we'll be rendering at 60fps."""
    from rich.markdown import Markdown
    partial = "Explanation before code:\n\n```python\ndef foo():\n    return 42"
    # Construction + equality of renderable = enough to confirm no raise.
    md = Markdown(partial)
    assert md is not None


def test_markdown_fence_sealed_cleanly_on_close():
    """After the closing ``` arrives, Markdown still constructs fine.
    Covers the post-batch final-flush case."""
    from rich.markdown import Markdown
    complete = "```python\ndef foo():\n    return 42\n```\n\nEnd."
    md = Markdown(complete)
    assert md is not None


# ---------------------------------------------------------------------------
# (10) Provider-seam regression — AST canary that providers.py wires
#      the renderer into the stream callback chain
# ---------------------------------------------------------------------------


def test_providers_wire_get_stream_renderer_into_stream_callback():
    """Static check: ``providers.py`` must reference
    ``get_stream_renderer`` inside the Claude streaming dispatch path,
    else a refactor could silently bypass the operator terminal (the
    stream_callback would fall back to None and the ``create()`` path
    would be taken — no tokens flowing, back to spinner).
    """
    import ast
    from pathlib import Path

    providers_path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/providers.py"
    )
    assert providers_path.is_file(), (
        f"providers.py not at expected path: {providers_path}"
    )
    src = providers_path.read_text(encoding="utf-8")
    # Cheap string check first — catches the most common deletion.
    assert "get_stream_renderer" in src, (
        "providers.py no longer references get_stream_renderer — "
        "operator streaming will silently regress to spinner for "
        "non-tool-loop GENERATE calls."
    )
    # AST check: confirm it's actually CALLED, not just imported or
    # mentioned in a comment.
    tree = ast.parse(src)
    called = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "get_stream_renderer":
            called = True
            break
        if isinstance(func, ast.Attribute) and func.attr == "get_stream_renderer":
            called = True
            break
    assert called, (
        "get_stream_renderer is referenced but never invoked in providers.py"
    )
