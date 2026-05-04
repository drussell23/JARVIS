"""Operator-visible token streaming renderer for Ouroboros GENERATE.

Closes the "spinner for 2 minutes while Claude generates" UX gap: tokens
arrive on the operator terminal in real-time via a Rich ``Live`` + ``Markdown``
widget, syntax-highlighted as they stream.

Architectural mandates (§ each enforced):

1. **Async isolation** — the token callback is O(1) non-blocking (enqueues
   via ``put_nowait``, drops on overflow rather than blocking the provider's
   stream). A dedicated consumer task batches at ~16ms cadence (60fps) and
   drives ``Live.update``, so terminal rendering lag cannot starve the
   inference I/O stream.
2. **Syntax-aware rendering** — the buffer is rendered via Rich's
   ``Markdown`` widget, which handles partial fenced code blocks gracefully
   (unclosed ```` ```python ```` renders as syntax-highlighted code-in-
   progress and seals cleanly when the closing fence arrives).
3. **Kill switch** — ``JARVIS_UI_STREAMING_ENABLED=1`` (default on). Flip to
   ``0`` for overnight batches where terminal UI overhead isn't wanted.
   When off, ``start()`` is a no-op and ``on_token()`` silently discards.
4. **Observability anchor** — on ``end()``, emits a single INFO line:
   ``[StreamRender] op=X provider=Y tokens=N dropped=D first_token_ms=T
   total_ms=M tps=P``. TTFT + TPS turn the UI widget into a provider-
   health telemetry sensor.

Authority invariant: this module mutates only the terminal presentation
layer. It never reads or writes ``ctx``, never touches Iron Gate,
UrgencyRouter, risk tier, policy engine, FORBIDDEN_PATH, ToolExecutor
protected-path checks, or approval gating.

Module-level singleton (``register_stream_renderer`` / ``get_stream_renderer``
/ ``reset_stream_renderer``) matches the pattern already used by
``OpsDigestObserver`` and ``LastSessionSummary``: the harness registers on
boot, providers look up at stream time, tests reset between cases.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.StreamRenderer")

_STREAMING_ENV_VAR = "JARVIS_UI_STREAMING_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def streaming_enabled() -> bool:
    """Env gate read. Default: ON (``1``). Flip to ``0`` for batch mode."""
    return os.environ.get(_STREAMING_ENV_VAR, "1").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Tunables — env-overridable without code churn
# ---------------------------------------------------------------------------

# Maximum tokens held in the producer→consumer queue. Overflow drops
# incoming tokens (dropped_count tracked) rather than blocking the provider.
# 256 sized for ~5s of buffering at 50 tok/s — plenty of headroom under
# normal render load, and the dropped_count is surfaced in the INFO line
# so tuning is empirical.
_QUEUE_MAX = int(os.environ.get("JARVIS_UI_STREAMING_QUEUE_MAX", "256"))

# Batch interval — target render cadence. 16ms ≈ 60fps.
_BATCH_INTERVAL_S = float(os.environ.get("JARVIS_UI_STREAMING_BATCH_MS", "16")) / 1000.0

# Rich Live refresh_per_second — internal widget refresh rate. Decoupled
# from our batch cadence; Rich handles its own render throttling on a
# background thread so .update() is essentially pointer-swap cheap.
_LIVE_REFRESH_HZ = int(os.environ.get("JARVIS_UI_STREAMING_LIVE_REFRESH_HZ", "30"))

# Sliding-window cap on the markdown re-parse buffer (Manifesto §3).
# Rich.Markdown re-parses the full string on every .update(); at the 16ms
# batch cadence over a 16k-token stream that's O(N²) work where N is
# accumulated chars. Slicing to the tail keeps the per-render cost O(1)
# in the stream length — Rich Live only displays the visible viewport
# anyway, so nothing above the slice would have rendered to the terminal.
_RENDER_TAIL_CHARS = int(os.environ.get("JARVIS_UI_STREAMING_RENDER_TAIL_CHARS", "4096"))


# ---------------------------------------------------------------------------
# StreamRenderer — per-session operator-visible token stream
# ---------------------------------------------------------------------------


class StreamRenderer:
    """Async-isolated token renderer for GENERATE phase.

    Lifecycle: ``start(op_id, provider)`` → many ``on_token(text)`` calls
    → ``end()``. Each lifecycle yields exactly one INFO line on ``end()``
    with TTFT + TPS metrics.

    Thread/coroutine model:
      - ``on_token`` runs on the provider's stream coroutine. Non-blocking
        enqueue via ``put_nowait``; on ``QueueFull`` it drops and
        increments ``dropped_count``. Never awaits.
      - ``start`` spawns a dedicated consumer task on the currently
        running loop; the consumer batches at ``_BATCH_INTERVAL_S`` and
        calls ``Live.update``. Rich's Live does the actual terminal
        render on its own thread, so ``update`` is cheap.
      - ``end`` cancels the consumer, flushes the final buffer, stops
        Live, and emits the observability INFO line.

    When ``streaming_enabled()`` is False at ``start`` time, the
    renderer becomes a no-op: no Live, no consumer, no INFO line (DEBUG
    line only). ``on_token`` calls fall through silently.

    RenderBackend conformance (Slice 2 of the RenderConductor arc): the
    ``name`` / ``notify`` / ``flush`` / ``shutdown`` methods below let
    this renderer plug into ``RenderConductor`` as a backend. Conductor
    events route to the same internal queue as the legacy ``on_token``
    entry point — no logic duplication. The legacy API stays functional
    for back-compat; both paths converge on the queue.
    """

    # RenderBackend Protocol — Slice 2 of the RenderConductor arc.
    name: str = "stream_renderer"

    def __init__(self, console: Optional[Any] = None) -> None:
        self._console = console
        self._queue: Optional[asyncio.Queue] = None
        self._buffer: str = ""
        self._live: Optional[Any] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._active: bool = False
        self._op_id: str = ""
        self._provider: str = ""
        self._start_mono: float = 0.0
        self._first_token_mono: Optional[float] = None
        self._token_count: int = 0
        self._dropped_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, op_id: str, provider: str = "") -> None:
        """Begin a streaming session. Idempotent: safe to call mid-stream
        (ends any prior session cleanly first). No-op when the env gate
        is off.

        Safe to call from any coroutine on the asyncio loop; if no loop
        is running (e.g. unit test without loop), falls back to no-op
        gracefully — this preserves the "renderer optional" contract.
        """
        # Idempotency: if already active, close prior session first.
        if self._active:
            self.end()

        if not streaming_enabled():
            logger.debug(
                "[StreamRender] op=%s streaming disabled via %s — no-op",
                op_id, _STREAMING_ENV_VAR,
            )
            return

        # Reset per-session state.
        self._op_id = op_id
        self._provider = provider or ""
        self._buffer = ""
        self._token_count = 0
        self._dropped_count = 0
        self._first_token_mono = None
        self._start_mono = time.monotonic()

        # Obtain the running loop. The queue MUST be bound to the same
        # loop that will run the consumer, else put_nowait raises.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — renderer degrades to no-op. This is the
            # headless / non-async caller path; streaming wouldn't work
            # anyway since the provider is async.
            logger.debug(
                "[StreamRender] op=%s no running loop — renderer is no-op",
                op_id,
            )
            return

        self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)

        # Enforce the TTY contract (Manifesto §3). Headless / sandbox / CI
        # runs must bypass the Rich Markdown re-parse path entirely — a
        # cosmetic UI renderer cannot be permitted to block the async
        # event loop running the Claude stream. The consumer task still
        # drains the queue so token_count and the final INFO line stay
        # accurate; only the visible Live widget is skipped.
        #
        # REPL coordination (2026-05-03): the same log-only branch is
        # also taken when a SerpentREPL is active. Rich.Live writes via
        # direct cursor manipulation that bypasses ``patch_stdout`` and
        # clobbers the input prompt under concurrent output. Operators
        # retain the [StreamRender] INFO line at end-of-stream (token
        # count + duration + drops) so observability is preserved;
        # only the per-token visible widget goes away.
        try:
            from backend.core.ouroboros.battle_test.serpent_flow import (
                is_repl_active,
            )
            _repl_active = is_repl_active()
        except Exception:
            _repl_active = False
        if not sys.stdout.isatty() or _repl_active:
            _why = "non-TTY stdout" if not sys.stdout.isatty() else "REPL active"
            logger.debug(
                "[StreamRender] op=%s %s — Live skipped, log-only stream",
                op_id, _why,
            )
            self._live = None
        else:
            # Try to open a Rich Live widget. On any failure (no console, no
            # Rich, terminal misbehaves), degrade to log-only streaming — the
            # consumer still drains the queue and emits the INFO line at end.
            try:
                from rich.live import Live
                from rich.markdown import Markdown

                self._live = Live(
                    Markdown(""),
                    console=self._console,
                    transient=False,
                    refresh_per_second=_LIVE_REFRESH_HZ,
                )
                self._live.start()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[StreamRender] op=%s Rich.Live unavailable; log-only stream",
                    op_id, exc_info=True,
                )
                self._live = None

        self._consumer_task = loop.create_task(self._consume())
        self._active = True

    def on_token(self, text: str) -> None:
        """Non-blocking token ingress.

        Hot path: called once per token from the provider's stream
        coroutine. Must complete in O(1) — no awaits, no synchronous
        render, no I/O. Drops on queue overflow (rare) rather than
        blocking the producer. Overflow count surfaces in the INFO line.
        """
        if not self._active or not text:
            return
        if self._first_token_mono is None:
            self._first_token_mono = time.monotonic()
        q = self._queue
        if q is None:
            return
        try:
            q.put_nowait(text)
        except asyncio.QueueFull:
            self._dropped_count += 1

    def end(self) -> None:
        """Finalize the stream: cancel the consumer, flush, stop Live,
        emit observability INFO line. Idempotent."""
        if not self._active:
            return
        self._active = False

        # Cancel consumer and wait for it to flush remaining batch.
        task = self._consumer_task
        self._consumer_task = None
        if task is not None and not task.done():
            task.cancel()
            # Best-effort: schedule a small drain. We're not awaiting
            # here (end is sync) — the task's CancelledError handler
            # does a final buffer flush before it exits.

        # Stop the Live widget after giving the consumer one last
        # synchronous drain opportunity.
        self._drain_remaining_sync()

        if self._live is not None:
            try:
                from rich.markdown import Markdown
                # Final render: tail slice only. Rich Live's viewport shows
                # at most the visible terminal area, so re-parsing the full
                # buffer would pay O(N) cost to render content that never
                # reaches pixels.
                self._live.update(Markdown(self._buffer[-_RENDER_TAIL_CHARS:]))  # type: ignore[attr-defined]
                self._live.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[StreamRender] op=%s Live.stop failed", self._op_id,
                    exc_info=True,
                )
            self._live = None

        # Observability INFO — single line, grep-able, metrics-first.
        total_s = time.monotonic() - self._start_mono
        ttft_ms = (
            int((self._first_token_mono - self._start_mono) * 1000)
            if self._first_token_mono is not None
            else -1
        )
        tps = (self._token_count / total_s) if total_s > 0.0 else 0.0
        logger.info(
            "[StreamRender] op=%s provider=%s tokens=%d dropped=%d "
            "first_token_ms=%d total_ms=%d tps=%.1f",
            self._op_id, self._provider, self._token_count,
            self._dropped_count, ttft_ms, int(total_s * 1000), tps,
        )

        # Clear state so the renderer instance is reusable across ops.
        self._queue = None
        self._buffer = ""
        self._op_id = ""
        self._provider = ""
        self._first_token_mono = None
        self._token_count = 0
        self._dropped_count = 0

    # ------------------------------------------------------------------
    # RenderBackend Protocol — Slice 2 of RenderConductor arc.
    # Routes RenderEvents to the same internal pipeline as legacy
    # ``on_token`` / ``start`` / ``end``. Both legacy callers and
    # conductor-routed events converge on the queue — no duplication.
    # ------------------------------------------------------------------

    def notify(self, event: Any) -> None:
        """Consume a RenderEvent from the conductor.

        Maps event.kind → existing internal method:
          * REASONING_TOKEN  → on_token(content)
          * PHASE_BEGIN      → start(op_id, provider) (provider read from
                                event.metadata.provider, fallback to "")
          * PHASE_END        → end()
          * BACKEND_RESET    → end() (idempotent finalizer)
          * other kinds      → no-op (this renderer surfaces only the
                                token-stream lifecycle; other regions
                                are owned by SerpentFlow / OuroborosTUI)

        NEVER raises — defensive everywhere. Lazy import of EventKind
        keeps stream_renderer free of a hard import on the conductor
        primitive (the conductor module imports stream_renderer at boot
        time, not the other way around — preserves dependency direction).
        """
        if event is None:
            return
        try:
            kind = getattr(event, "kind", None)
            kind_value = getattr(kind, "value", None) or str(kind or "")
            if kind_value == "REASONING_TOKEN":
                content = getattr(event, "content", "") or ""
                if content:
                    self.on_token(content)
                return
            if kind_value == "PHASE_BEGIN":
                op_id = getattr(event, "op_id", None) or ""
                metadata = getattr(event, "metadata", None) or {}
                provider = ""
                try:
                    provider = str(metadata.get("provider", ""))
                except Exception:  # noqa: BLE001 — defensive
                    provider = ""
                if op_id:
                    self.start(op_id, provider)
                return
            if kind_value in ("PHASE_END", "BACKEND_RESET"):
                self.end()
                return
            # Other event kinds are not surfaced by this renderer.
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[StreamRender] notify(event) failed", exc_info=True,
            )

    def flush(self) -> None:
        """Drain any pending tokens. Reuses the existing sync drain path."""
        try:
            self._drain_remaining_sync()
            self._render_buffer_safe()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug("[StreamRender] flush failed", exc_info=True)

    def shutdown(self) -> None:
        """Tear down the active session if any. Idempotent — wraps end()."""
        try:
            self.end()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug("[StreamRender] shutdown failed", exc_info=True)

    # ------------------------------------------------------------------
    # Introspection (for tests + debugging)
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def token_count(self) -> int:
        return self._token_count

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def buffer(self) -> str:
        return self._buffer

    # ------------------------------------------------------------------
    # Consumer task — batches + renders
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Drain the queue at ~60fps cadence. Runs as a dedicated task.

        Pattern: wait up to ``_BATCH_INTERVAL_S`` for the next chunk,
        then drain anything else already queued (non-blocking), then
        flush the accumulated batch to the Live widget. This coalesces
        burst arrivals into a single render and never blocks if no
        tokens arrive — the timeout path just returns and loops.
        """
        pending: list = []
        last_render = time.monotonic()
        q = self._queue
        if q is None:
            return
        try:
            while True:
                # Compute a timeout that rounds down to the next render
                # boundary so we render at predictable cadence.
                elapsed = time.monotonic() - last_render
                timeout = max(0.001, _BATCH_INTERVAL_S - elapsed)
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=timeout)
                    pending.append(chunk)
                    self._token_count += 1
                except asyncio.TimeoutError:
                    pass

                # Opportunistic drain: pull anything already queued
                # without awaiting. Coalesces bursts into one render.
                while True:
                    try:
                        pending.append(q.get_nowait())
                        self._token_count += 1
                    except asyncio.QueueEmpty:
                        break

                # Flush if we have content and the batch interval elapsed.
                now = time.monotonic()
                if pending and (now - last_render) >= _BATCH_INTERVAL_S:
                    self._buffer += "".join(pending)
                    pending.clear()
                    self._render_buffer_safe()
                    last_render = now
        except asyncio.CancelledError:
            # Final flush on cancellation (end() path). Any pending
            # chunks from the last interval land in the terminal before
            # Live is stopped.
            if pending:
                self._buffer += "".join(pending)
                pending.clear()
                self._render_buffer_safe()
            raise

    def _render_buffer_safe(self) -> None:
        """Swap the Markdown renderable on Live. Rich handles the
        actual terminal write on its background thread.

        Buffer is sliced to ``_RENDER_TAIL_CHARS`` to bound per-render
        parser work — prevents O(N²) event-loop pressure when a 16k-token
        stream re-parses a growing buffer on every 16ms batch tick.
        """
        if self._live is None:
            return
        try:
            from rich.markdown import Markdown
            self._live.update(Markdown(self._buffer[-_RENDER_TAIL_CHARS:]))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.debug(
                "[StreamRender] Live.update failed", exc_info=True,
            )

    def _drain_remaining_sync(self) -> None:
        """Best-effort sync drain used in ``end()`` when the consumer
        task has been cancelled but hasn't fully run its except-block
        yet. Pulls anything still in the queue into the buffer so the
        final Live.update and the token_count metric are accurate.
        """
        q = self._queue
        if q is None:
            return
        while True:
            try:
                chunk = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            except Exception:  # noqa: BLE001
                break
            self._buffer += chunk
            self._token_count += 1


# ---------------------------------------------------------------------------
# Process-global singleton — matches OpsDigestObserver / LastSessionSummary
# ---------------------------------------------------------------------------

_DEFAULT_RENDERER: Optional[StreamRenderer] = None


def register_stream_renderer(renderer: Optional[StreamRenderer]) -> None:
    """Register the process-global renderer.

    Providers consult this on stream-start to know whether an operator
    terminal is watching. Called from the harness after SerpentFlow
    boots. Pass ``None`` to clear (also via ``reset_stream_renderer``).
    """
    global _DEFAULT_RENDERER
    _DEFAULT_RENDERER = renderer


def get_stream_renderer() -> Optional[StreamRenderer]:
    """Return the registered renderer or ``None`` if headless / not wired.

    Providers call this at the start of each streaming request. Return
    value is cached by the caller for the duration of one stream so
    late-registration doesn't split a single op across two modes.
    """
    return _DEFAULT_RENDERER


def reset_stream_renderer() -> None:
    """Clear the process-global singleton. Primarily for tests."""
    global _DEFAULT_RENDERER
    _DEFAULT_RENDERER = None
