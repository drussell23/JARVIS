"""HiveEmitter — the zero-authority telemetry emission edge (Phase 12, Hive Step 2).

The silent actors (MCP tools, web capabilities, voice, the 5 core contexts,
ghost-hands actuation, the frame pipeline, the memory engine) are silent because
they emit to NO fabric at all. This module is the ONE process-local emission
edge they call — a bounded, fire-and-forget, sanitized sink the HiveAggregator
drains as its third source.

Design invariants:
  * ZERO AUTHORITY (mandate 1): pure telemetry. Nothing subscribes commands from
    it; the aggregator only *reads* ``out_queue``. It cannot block, retry, or
    influence an actor — ``emit()`` is synchronous, non-blocking, and NEVER
    raises. A failed emit is a dropped stat, never a failed action.
  * EDGE-LEVEL DEBOUNCER (mandate: 50 granular events in 200ms → ONE semantic
    envelope): coalescing happens HERE, at the edge where the semantics are
    known, keyed by (actor_id, intent). Windows are adaptive — a stormy key
    widens its window (AIMD-style) up to a ceiling; quiet keys decay back.
    ``flush()`` closes a window early on sequence completion.
  * SANITIZED AT THE EDGE: summaries pass credential-shape redaction
    (``conversation_bridge.redact_secrets``) + control-char strip / length cap
    (``secure_logging.sanitize_for_log``) BEFORE entering the queue. ``detail``
    accepts only scalar values (strings sanitized) — payload bodies are
    structurally unable to ride along.
  * THREAD-SAFE: mirrors ``cockpit_attach.publish_line`` — same-loop fast path,
    ``call_soon_threadsafe`` marshal from worker threads (OCR executor, STT
    executor, the SCK capture thread). All debouncer state is loop-confined, so
    there are no locks.

Everything is env-tunable (no hardcoding); master ``JARVIS_HIVE_EMITTERS_ENABLED``
(default true — this edge has no authority to gate).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

from backend.api.hive_envelope import ActorEnvelope

logger = logging.getLogger("Jarvis.HiveEmitter")

_MASTER_ENV = "JARVIS_HIVE_EMITTERS_ENABLED"


def hive_emitters_enabled() -> bool:
    return os.environ.get(_MASTER_ENV, "true").strip().lower() == "true"


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "")
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "")
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


# --- sanitization (reused, lazy, cached — mandate 3: no duplicate regexes) ---

_sanitizers: Optional[Tuple[Callable, Callable]] = None


def _get_sanitizers() -> Tuple[Callable, Callable]:
    """(sanitize_for_log, redact_secrets) — resolved once, degrade to identity."""
    global _sanitizers
    if _sanitizers is None:
        try:
            from backend.core.secure_logging import sanitize_for_log
        except Exception:  # noqa: BLE001
            sanitize_for_log = lambda v, max_len=200: str(v)[:max_len]  # noqa: E731
        try:
            from backend.core.ouroboros.governance.conversation_bridge import (
                redact_secrets,
            )
        except Exception:  # noqa: BLE001
            redact_secrets = lambda t: (t, 0)  # noqa: E731
        _sanitizers = (sanitize_for_log, redact_secrets)
    return _sanitizers


def _clean_text(text: str, max_len: int) -> str:
    sanitize_for_log, redact_secrets = _get_sanitizers()
    try:
        redacted, _ = redact_secrets(str(text))
        return sanitize_for_log(redacted, max_len=max_len)
    except Exception:  # noqa: BLE001
        return str(text)[:max_len]


def _clean_detail(detail: Optional[Dict[str, Any]], max_len: int) -> Dict[str, Any]:
    """Scalars only — payload bodies are structurally excluded from the feed."""
    out: Dict[str, Any] = {}
    if not detail:
        return out
    for k, v in list(detail.items())[:16]:
        try:
            if isinstance(v, bool) or isinstance(v, (int, float)):
                out[str(k)[:48]] = v
            elif isinstance(v, str):
                out[str(k)[:48]] = _clean_text(v, max_len)
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------------------------------------------------------------------
# Edge-Level Debouncer
# ---------------------------------------------------------------------------

class _Window:
    __slots__ = ("first_ts", "last_ts", "count", "item", "handle",
                 "worst_severity")

    def __init__(self, item: Any, severity: str) -> None:
        now = time.time()
        self.first_ts = now
        self.last_ts = now
        self.count = 1
        self.item = item
        self.handle: Optional[asyncio.TimerHandle] = None
        self.worst_severity = severity


_SEV_RANK = {"info": 0, "success": 1, "warn": 2, "error": 3}


def _actor_kwargs_finalizer(kwargs: Dict[str, Any], count: int, span_ms: float,
                            first_ts: float, severity: str) -> Any:
    """Default finalizer: build ONE ActorEnvelope from the first event's
    kwargs, amended with the burst arithmetic."""
    kw = dict(kwargs)
    if count > 1:
        kw["action_summary"] = (
            f"{kw['action_summary']} (×{count} in {span_ms:.0f}ms)")
    kw["severity"] = severity
    return ActorEnvelope(coalesced_n=count, span_ms=span_ms, ts=first_ts, **kw)


class EdgeDebouncer:
    """Per-(actor_id, intent) coalescing windows with adaptive width.

    Loop-confined: every mutation happens on the bound event loop (the emitter
    marshals cross-thread callers), so no locks. A window opens on the first
    event of a key, folds subsequent events, and flushes ONE item when the
    window timer fires or ``flush()`` closes it early (sequence completion).

    Adaptive width (AIMD-flavored): a window that closes at/over the high-water
    count doubles the key's next window (up to the ceiling); a window that
    closes with a single event halves it (down to the base). Storms compress
    harder; sparse keys stay near-realtime.

    Generic over the folded item via the injectable ``finalizer(first_item,
    count, span_ms, first_ts, worst_severity) -> obj|None``: the emitter folds
    raw emit kwargs into an ActorEnvelope (default), the aggregator folds
    already-cast bus envelopes (bus-storm compression) — ONE window/adaptive
    implementation for both (mandate 3).
    """

    def __init__(self, sink: Callable[[Any], None],
                 finalizer: Optional[Callable] = None) -> None:
        self._sink = sink
        self._finalizer = finalizer or _actor_kwargs_finalizer
        self._windows: Dict[Tuple[str, str], _Window] = {}
        self._widths: Dict[Tuple[str, str], float] = {}
        self.base_ms = _env_float("JARVIS_HIVE_DEBOUNCE_WINDOW_MS", 200.0)
        self.max_ms = _env_float("JARVIS_HIVE_DEBOUNCE_MAX_MS", 2000.0)
        self.high_water = _env_int("JARVIS_HIVE_DEBOUNCE_HIGHWATER", 25)
        self.stats = {"opened": 0, "folded": 0, "flushed": 0}

    def accept(self, key: Tuple[str, str], item: Any,
               severity: str = "info") -> None:
        """Fold one event into its key's window (opening one if needed).
        The FIRST item is the semantic anchor; later ones bump count/severity."""
        win = self._windows.get(key)
        if win is not None:
            win.count += 1
            win.last_ts = time.time()
            if _SEV_RANK.get(severity, 0) > _SEV_RANK.get(win.worst_severity, 0):
                win.worst_severity = severity
            self.stats["folded"] += 1
            return
        win = _Window(item, severity)
        self._windows[key] = win
        self.stats["opened"] += 1
        width = self._widths.get(key, self.base_ms) / 1000.0
        try:
            loop = asyncio.get_running_loop()
            win.handle = loop.call_later(width, self._close, key)
        except RuntimeError:
            # No loop (sync/unit context) — degrade to immediate pass-through.
            self._close(key)

    def flush(self, key: Tuple[str, str]) -> None:
        """Sequence completion — close the key's window NOW."""
        self._close(key)

    def flush_all(self) -> None:
        for key in list(self._windows):
            self._close(key)

    def _close(self, key: Tuple[str, str]) -> None:
        win = self._windows.pop(key, None)
        if win is None:
            return
        if win.handle is not None:
            win.handle.cancel()
        # adapt this key's next window
        cur = self._widths.get(key, self.base_ms)
        if win.count >= self.high_water:
            self._widths[key] = min(cur * 2, self.max_ms)
        elif win.count <= 1:
            self._widths[key] = max(cur / 2, self.base_ms)
        span_ms = max(0.0, (win.last_ts - win.first_ts) * 1000.0)
        try:
            out = self._finalizer(win.item, win.count, span_ms,
                                  win.first_ts, win.worst_severity)
            if out is not None:
                self._sink(out)
                self.stats["flushed"] += 1
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# HiveEmitter
# ---------------------------------------------------------------------------

class HiveEmitter:
    """The process-local emission edge. ``emit()`` is safe from any thread and
    NEVER raises; the aggregator drains ``out_queue`` read-only."""

    def __init__(self) -> None:
        self.out_queue: "asyncio.Queue[ActorEnvelope]" = asyncio.Queue(
            maxsize=_env_int("JARVIS_HIVE_EMITTER_QUEUE_MAX", 2048))
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._debouncer = EdgeDebouncer(self._enqueue)
        self._summary_max = _env_int("JARVIS_HIVE_EMIT_SUMMARY_MAX", 240)
        self._detail_max = _env_int("JARVIS_HIVE_EMIT_DETAIL_MAX", 120)
        self.stats = {"emitted": 0, "dropped_full": 0, "dropped_no_loop": 0,
                      "dropped_disabled": 0}

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Capture the event loop once (cockpit_attach idiom). Called by the
        aggregator host at relay start; emits before binding self-bind when the
        caller is already on a running loop."""
        if loop is not None:
            self._loop = loop
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    # -- the ONE public entry point every shim calls ------------------------

    def emit(
        self,
        *,
        actor_id: str,
        subsystem: str,
        intent: str,
        summary: str,
        severity: str = "info",
        trace_id: str = "—",
        detail: Optional[Dict[str, Any]] = None,
        coalesce: bool = False,
    ) -> None:
        """Fire-and-forget from ANY thread. NEVER raises, NEVER blocks."""
        try:
            if not hive_emitters_enabled():
                self.stats["dropped_disabled"] += 1
                return
            kwargs = dict(
                actor_id=str(actor_id)[:64],
                subsystem=str(subsystem)[:32],
                intent=str(intent)[:64],
                action_summary=_clean_text(summary, self._summary_max),
                trace_id=str(trace_id)[:64],
                severity=severity if severity in _SEV_RANK else "info",
                detail=_clean_detail(detail, self._detail_max),
                source_fabric="actor_edge",
            )
            self._marshal(self._accept, kwargs, coalesce)
        except Exception:  # noqa: BLE001
            pass

    def flush(self, actor_id: str, intent: str) -> None:
        """Sequence-completion flush for a coalescing key. Thread-safe."""
        try:
            self._marshal(self._debouncer.flush, (str(actor_id)[:64], str(intent)[:64]))
        except Exception:  # noqa: BLE001
            pass

    # -- internals ----------------------------------------------------------

    def _marshal(self, fn: Callable, *args: Any) -> None:
        """Same-loop fast path vs cross-thread call_soon_threadsafe (mirrors
        cockpit_attach.publish_line)."""
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (loop is None or running is loop):
            if loop is None:
                self._loop = running     # lazy self-bind on first loop emit
            fn(*args)
            return
        if loop is None or loop.is_closed():
            self.stats["dropped_no_loop"] += 1
            return
        loop.call_soon_threadsafe(fn, *args)

    def _accept(self, kwargs: Dict[str, Any], coalesce: bool) -> None:
        if coalesce:
            self._debouncer.accept((kwargs["actor_id"], kwargs["intent"]),
                                   kwargs, severity=kwargs.get("severity", "info"))
            return
        try:
            env = ActorEnvelope(**kwargs)
        except Exception:  # noqa: BLE001
            return
        self._enqueue(env)

    def _enqueue(self, env: ActorEnvelope) -> None:
        try:
            self.out_queue.put_nowait(env)
            self.stats["emitted"] += 1
        except asyncio.QueueFull:
            try:
                self.out_queue.get_nowait()
                self.out_queue.put_nowait(env)
                self.stats["emitted"] += 1
            except Exception:  # noqa: BLE001
                pass
            self.stats["dropped_full"] += 1


# ---------------------------------------------------------------------------
# Module-level default emitter — what the shims lazy-import
# ---------------------------------------------------------------------------

_default: Optional[HiveEmitter] = None


def get_default_emitter() -> HiveEmitter:
    global _default
    if _default is None:
        _default = HiveEmitter()
    return _default


def hive_emit(**kwargs: Any) -> None:
    """The one-liner every shim calls: ``hive_emit(actor_id=..., ...)``.
    NEVER raises; no-op when the master flag is off."""
    try:
        get_default_emitter().emit(**kwargs)
    except Exception:  # noqa: BLE001
        pass


def hive_flush(actor_id: str, intent: str) -> None:
    try:
        get_default_emitter().flush(actor_id, intent)
    except Exception:  # noqa: BLE001
        pass


def register_flags(registry: Any) -> None:
    """Declare this family in the FlagRegistry (repo discoverability idiom).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
        for spec in (
            FlagSpec(name=_MASTER_ENV, type=FlagType.BOOL, default=True,
                     category=Category.OBSERVABILITY, source_file="backend/api/hive_emitter.py",
                     example="JARVIS_HIVE_EMITTERS_ENABLED=false",
                     description="Master switch for the silent-actor hive emission edge"),
            FlagSpec(name="JARVIS_HIVE_DEBOUNCE_WINDOW_MS", type=FlagType.FLOAT, default=200.0,
                     category=Category.OBSERVABILITY, source_file="backend/api/hive_emitter.py",
                     example="JARVIS_HIVE_DEBOUNCE_WINDOW_MS=500",
                     description="Edge debouncer base coalescing window (ms)"),
            FlagSpec(name="JARVIS_HIVE_DEBOUNCE_MAX_MS", type=FlagType.FLOAT, default=2000.0,
                     category=Category.OBSERVABILITY, source_file="backend/api/hive_emitter.py",
                     example="JARVIS_HIVE_DEBOUNCE_MAX_MS=5000",
                     description="Adaptive window ceiling under sustained storms (ms)"),
            FlagSpec(name="JARVIS_HIVE_DEBOUNCE_HIGHWATER", type=FlagType.INT, default=25,
                     category=Category.OBSERVABILITY, source_file="backend/api/hive_emitter.py",
                     example="JARVIS_HIVE_DEBOUNCE_HIGHWATER=50",
                     description="Window count at/over which a key's window widens"),
            FlagSpec(name="JARVIS_HIVE_EMITTER_QUEUE_MAX", type=FlagType.INT, default=2048,
                     category=Category.OBSERVABILITY, source_file="backend/api/hive_emitter.py",
                     example="JARVIS_HIVE_EMITTER_QUEUE_MAX=4096",
                     description="Bounded emitter queue size (drop-oldest)"),
        ):
            registry.register(spec)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "HiveEmitter", "EdgeDebouncer", "hive_emit", "hive_flush",
    "get_default_emitter", "hive_emitters_enabled", "register_flags",
]
