"""Karen voice channel — the spoken half of the tool-call preamble.

The tool-narration channel (``tool_narration.py``) already surfaces a
one-sentence "WHY" line above the SerpentFlow spinner at tool-call time.
This module gives that sentence a *voice*: Karen, the macOS TTS persona
Ouroboros speaks through. The result is a running audio commentary as
the organism explores — something Claude Code cannot do at all because
it has no long-lived audio stack.

Design goals
------------
1.  **Sync-in, async-out.** ``speak()`` is called from Venom's synchronous
    tool-dispatch hook. It enqueues to an :class:`asyncio.Queue` drained
    by a lazy background worker, so the tool loop never blocks on TTS.

2.  **Rate limiting.** The model can fire 3-5 tool calls per round and
    the ToolExecutor already dedupes per ``(op_id, round)`` pair — but if
    *multiple ops* overlap the tool loop, Karen could still get spammed.
    A min-gap clock (``JARVIS_KAREN_MIN_GAP_S``, default 3.0s) gates every
    dequeued message so Karen never speaks on top of herself.

3.  **Drop-oldest shedding.** The queue is small (maxsize 8); when full,
    the oldest pending preamble is shed so the *next* utterance is
    always the *most relevant* one. This mirrors the pattern in
    ``VoiceNarrator`` (see voice_narrator.py:31).

4.  **Fault isolation.** Every failure path logs at DEBUG and returns.
    A broken audio device, a missing ``safe_say`` module, or a
    ``RuntimeError`` from the event loop must *never* propagate up into
    the tool loop — if Karen can't speak, the organism keeps thinking.

5.  **Env-driven kill switches.** Two layers:
       * ``OUROBOROS_NARRATOR_ENABLED`` — master switch shared with
         ``VoiceNarrator``. When false, Karen is a no-op.
       * ``JARVIS_KAREN_TOOL_VOICE_ENABLED`` — sub-switch that lets
         operators silence *tool* preambles while keeping phase
         narration (INTENT/DECISION/POSTMORTEM) on, or vice-versa.

6.  **Lazy imports.** ``safe_say`` lives inside
    ``backend.core.supervisor.unified_voice_orchestrator`` which pulls
    the full audio stack (CoreAudio, pyttsx, model cache) on import.
    We defer the import until the first *dequeued* message so headless
    runs, unit tests, and CI never touch the audio stack at all.

Manifesto compliance
--------------------
* §3 *Asynchronous tendrils* — sync callback schedules, never blocks.
* §4 *Synthetic soul* — the organism has a continuous audible presence.
* §7 *Absolute observability* — every tool round the model narrates
  reaches a human ear, not just a log file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers (duplicated here from tool_narration.py so this module has
# zero import-time dependencies on the rest of the governance tree —
# essential because the tool loop imports *this* lazily, and we want
# every env read to happen at construction time, not at import time).
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, val)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, val)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KarenConfig:
    """Immutable runtime configuration for :class:`KarenPreambleVoice`.

    Every tunable reads an env var with a safe default. Frozen so the
    config cannot be mutated out from under a running worker — rebuild
    the channel if you need different settings.
    """

    #: Master kill switch (shared with VoiceNarrator). False → no-op.
    master_enabled: bool = field(
        default_factory=lambda: _env_bool("OUROBOROS_NARRATOR_ENABLED", True)
    )
    #: Sub-switch specific to tool-call preambles. False → no-op even
    #: when the master is on, so operators can silence Karen's tool
    #: narration while keeping phase narration active.
    enabled: bool = field(
        default_factory=lambda: _env_bool("JARVIS_KAREN_TOOL_VOICE_ENABLED", True)
    )
    #: Minimum gap between consecutive utterances in seconds. Enforced
    #: at dequeue time — if the previous utterance finished less than
    #: ``min_gap_s`` ago, the dequeued message is dropped silently so
    #: Karen never speaks on top of herself.
    min_gap_s: float = field(
        default_factory=lambda: _env_float("JARVIS_KAREN_MIN_GAP_S", 3.0, minimum=0.0)
    )
    #: Maximum characters in the spoken text. Longer preambles are
    #: truncated with an ellipsis so macOS ``say`` doesn't monologue a
    #: paragraph. Set to 0 to disable truncation.
    max_chars: int = field(
        default_factory=lambda: _env_int("JARVIS_KAREN_MAX_CHARS", 140, minimum=0)
    )
    #: macOS voice name passed to ``safe_say``. "Karen" is Ouroboros'
    #: canonical voice (Australian female, matches the VoiceNarrator
    #: default). Override with ``JARVIS_KAREN_VOICE`` — useful when the
    #: user has a different voice installed or for A/B testing.
    voice: str = field(
        default_factory=lambda: os.environ.get("JARVIS_KAREN_VOICE", "Karen")
    )
    #: Speech rate (words per minute). 180 is slightly faster than the
    #: safe_say default (175) — punchy for short tool-round narration.
    rate_wpm: int = field(
        default_factory=lambda: _env_int("JARVIS_KAREN_RATE", 180, minimum=80)
    )
    #: Queue capacity. Small by design — we only need the *most recent*
    #: preamble when the tool loop fires fast, not a backlog of stale
    #: rationales. Older messages are shed DROP_OLDEST.
    queue_maxsize: int = field(
        default_factory=lambda: _env_int("JARVIS_KAREN_QUEUE_MAXSIZE", 8, minimum=1)
    )
    #: Whether the TTS call itself should block. False = fire-and-forget
    #: at the safe_say layer; the drain worker proceeds to the next
    #: message immediately. Keep False unless you're debugging timing.
    wait: bool = field(
        default_factory=lambda: _env_bool("JARVIS_KAREN_WAIT", False)
    )
    #: Skip safe_say's global dedup. Preambles are always unique per
    #: tool round, so the dedup check would either be a no-op or (worse)
    #: false-positive us when two runs emit similar sentences. True by
    #: default — set to false if Karen starts repeating identical text.
    skip_dedup: bool = field(
        default_factory=lambda: _env_bool("JARVIS_KAREN_SKIP_DEDUP", True)
    )


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class KarenPreambleVoice:
    """Sync-to-async speaker channel for tool-call preambles.

    Construction is cheap — no audio stack is touched until the first
    *dequeued* message. The caller pattern is::

        karen = KarenPreambleVoice()           # cheap, no side effects
        karen.speak("Checking cascade wiring") # enqueue, returns immediately
        # ... tool round executes, Karen speaks in the background ...

    Concurrency model
    -----------------
    * ``speak()`` is synchronous and uses :meth:`asyncio.Queue.put_nowait`.
      When the queue is full, the *oldest* message is shed with
      ``get_nowait()``; the new message always makes it in.
    * ``_drain_loop`` is a long-lived asyncio task created lazily via
      :meth:`asyncio.get_running_loop().create_task` on first ``speak()``
      inside a running loop. If there's no loop (headless tests), the
      enqueue is silently dropped.
    * The drain loop dequeues, enforces the min-gap clock, lazy-imports
      ``safe_say``, and awaits the TTS call. Every step is wrapped in
      ``try/except`` so one failure can't stall the loop.

    Telemetry
    ---------
    * ``speak_count`` — preambles successfully handed to ``safe_say``.
    * ``shed_count`` — messages dropped due to queue overflow.
    * ``rate_limited_count`` — messages dropped by the min-gap clock.
    * ``failure_count`` — exceptions raised inside the drain loop.
    All counters are plain ints readable at any time — no locking
    required because only the drain loop mutates them.
    """

    def __init__(
        self,
        *,
        config: Optional[KarenConfig] = None,
        say_fn_override: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config = config or KarenConfig()

        # Lazy-imported safe_say, or an override injected for tests. The
        # override path is how the smoke test in /tmp/claude exercises
        # this class without pulling CoreAudio.
        self._say_fn: Optional[Callable[..., Any]] = say_fn_override
        self._say_fn_loaded: bool = say_fn_override is not None

        # Bounded queue + lazy drain task (same pattern as VoiceNarrator).
        self._queue: Optional["asyncio.Queue[str]"] = None
        self._drain_task: Optional["asyncio.Task[None]"] = None

        # Telemetry counters — drain-loop-owned, no lock needed.
        self.speak_count: int = 0
        self.shed_count: int = 0
        self.rate_limited_count: int = 0
        self.failure_count: int = 0

        # Monotonic timestamp of the last *dispatched* (not queued) utterance.
        # -inf means "first message always passes the min-gap check".
        self._last_spoken_at: float = float("-inf")

        # Log disabled state exactly once so operators know why the CLI
        # is silent without enabling DEBUG.
        if not self.is_enabled:
            reasons = []
            if not self._config.master_enabled:
                reasons.append("OUROBOROS_NARRATOR_ENABLED=false")
            if not self._config.enabled:
                reasons.append("JARVIS_KAREN_TOOL_VOICE_ENABLED=false")
            logger.info(
                "[KarenPreambleVoice] DISABLED (%s)",
                ", ".join(reasons) or "unknown",
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def config(self) -> KarenConfig:
        return self._config

    @property
    def is_enabled(self) -> bool:
        """True iff both master and sub switches are on."""
        return self._config.master_enabled and self._config.enabled

    def speak(self, text: str) -> None:
        """Enqueue one preamble for background narration.

        Safe to call from any sync context. Returns immediately. Never
        raises — every failure path logs at DEBUG and returns silently.
        """
        if not self.is_enabled:
            return
        if not text:
            return

        # Truncate + normalise here so the queue never holds a string
        # that will embarrass us later (stray newline, 2 KB rant, etc).
        text = self._normalise(text)
        if not text:
            return

        # We need a running event loop to create tasks. In headless
        # tests / REPLs there isn't one — drop the message and move on.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[KarenPreambleVoice] no running loop — dropping preamble: %s",
                text[:40],
            )
            return

        # Lazy queue construction — happens exactly once, inside the
        # running loop, so the queue is bound to the correct loop.
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._config.queue_maxsize)

        # DROP_OLDEST shedding: when full, evict the stalest message so
        # the *current* tool round's rationale gets priority.
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self.shed_count += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            # Race: another task drained then filled. Count the drop
            # so operators see the pressure in telemetry.
            self.shed_count += 1
            return

        # Ensure the drain worker is running on *this* loop.
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = loop.create_task(self._drain_loop())

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time telemetry snapshot.

        Matches the shape :class:`ClaudeProvider.get_cascade_telemetry`
        uses so operators can grep for counters uniformly across
        governance subsystems.
        """
        return {
            "enabled": self.is_enabled,
            "speak_count": self.speak_count,
            "shed_count": self.shed_count,
            "rate_limited_count": self.rate_limited_count,
            "failure_count": self.failure_count,
            "queue_size": self._queue.qsize() if self._queue is not None else 0,
            "config": {
                "voice": self._config.voice,
                "rate_wpm": self._config.rate_wpm,
                "min_gap_s": self._config.min_gap_s,
                "max_chars": self._config.max_chars,
                "queue_maxsize": self._config.queue_maxsize,
                "wait": self._config.wait,
                "skip_dedup": self._config.skip_dedup,
            },
        }

    async def shutdown(self) -> None:
        """Cancel the drain worker and drop pending messages.

        Called from graceful-shutdown paths. Never raises — cancellation
        of an already-finished or never-started worker is a no-op.
        """
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
        self._drain_task = None
        # Drain any leftover messages so the queue doesn't pin memory
        # between re-initialisations in long test runs.
        if self._queue is not None:
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _normalise(self, text: str) -> str:
        """Collapse whitespace and truncate at ``max_chars``.

        Duplicated from ``tool_narration.ToolNarrationChannel.emit``'s
        normalisation step because callers can reach Karen directly
        without passing through the narration channel (e.g. tests).
        """
        cleaned = " ".join(text.split())
        if not cleaned:
            return ""
        cap = self._config.max_chars
        if cap and len(cleaned) > cap:
            cleaned = cleaned[:cap].rstrip() + "…"
        return cleaned

    async def _drain_loop(self) -> None:
        """Background worker: dequeue, rate-limit, speak, repeat.

        One message at a time. Never parallel — Karen has one mouth.
        """
        assert self._queue is not None
        while True:
            try:
                text = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._speak_one(text)
            except Exception:
                self.failure_count += 1
                logger.debug(
                    "[KarenPreambleVoice] drain error for text=%r",
                    text[:40],
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

    async def _speak_one(self, text: str) -> None:
        """Execute one TTS call with rate limiting and lazy import.

        Three early-exits:
        1. Min-gap clock hasn't elapsed → rate-limited, drop silently.
        2. ``safe_say`` couldn't be resolved → disable channel, drop.
        3. ``safe_say`` raised → log + increment failure_count.
        """
        now = time.monotonic()
        if (now - self._last_spoken_at) < self._config.min_gap_s:
            self.rate_limited_count += 1
            return

        say_fn = self._resolve_say_fn()
        if say_fn is None:
            # Permanent disable — don't retry the import on every call.
            return

        try:
            result = say_fn(
                text,
                voice=self._config.voice,
                rate=self._config.rate_wpm,
                wait=self._config.wait,
                skip_dedup=self._config.skip_dedup,
                source="ouroboros_preamble",
            )
            # safe_say is async; a test override may return bool directly.
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            self.failure_count += 1
            logger.debug(
                "[KarenPreambleVoice] say_fn failed for text=%r",
                text[:40],
                exc_info=True,
            )
            return

        # Count dispatch, not success — safe_say's dedup/gate may drop
        # the utterance internally, but from our perspective we handed
        # it off cleanly.
        self.speak_count += 1
        self._last_spoken_at = time.monotonic()

    def _resolve_say_fn(self) -> Optional[Callable[..., Any]]:
        """Lazy-import the canonical ``safe_say`` entry point.

        Cached on the instance — a successful import is persistent, a
        failed import sets ``self._say_fn = None`` permanently so we
        don't retry on every tool round. The import is deliberately
        scoped to this method (not module level) so test environments,
        CI runners, and API-only deployments never trigger the full
        CoreAudio / voice model load path.
        """
        if self._say_fn_loaded:
            return self._say_fn
        try:
            from backend.core.supervisor.unified_voice_orchestrator import safe_say
            self._say_fn = safe_say
        except Exception:
            logger.debug(
                "[KarenPreambleVoice] safe_say unavailable — disabling channel",
                exc_info=True,
            )
            self._say_fn = None
        self._say_fn_loaded = True
        return self._say_fn
