"""
RecoveryAnnouncer — Karen voice for recovery plans.
=====================================================

Slice 3 of the Recovery Guidance + Voice Loop Closure arc. Speaks
:class:`RecoveryPlan` summaries aloud so the operator can run
hands-free.

Design mirrors ``backend/core/ouroboros/governance/comms/karen_voice.py``:

* **Opt-in with existing knob.** Master switch is
  ``OUROBOROS_NARRATOR_ENABLED`` (shared with ``VoiceNarrator`` /
  ``KarenVoice``) so operators flip one flag for every voice surface.
  A sub-switch ``JARVIS_RECOVERY_VOICE_ENABLED`` lets operators
  silence recovery narration while keeping tool / phase narration on,
  or vice versa.
* **Lazy audio import.** The macOS ``say`` binding lives in
  :mod:`backend.core.supervisor.unified_voice_orchestrator` which
  pulls the full audio stack at import time. We defer that import
  until the first dequeued utterance — headless / CI / sandbox runs
  never touch the audio stack at all.
* **Rate-limited queue.** One recovery plan per op is the typical
  shape, but an operator can trigger REPL speak-outs while another
  plan is still narrating. A bounded ``asyncio.Queue`` (drop-oldest)
  keeps the speaker from backlog-ing.
* **Idempotent per plan.** The same ``(op_id, matched_rule)`` pair
  is never spoken twice within the same process lifetime — prevents
  a re-submit from re-announcing the same failure.
* **Fault isolation.** Any audio / TTS / loop failure logs at DEBUG
  and returns. ``announce()`` never propagates exceptions.

Authority boundary
------------------

* §1 read-only — the announcer observes plans; never mutates
  orchestrator, cost governor, or session state.
* §7 fail-closed — a silent announcer never breaks the pipeline.
* §8 observable — every queued / dropped / spoken event logs.
* No imports from orchestrator / policy_engine / iron_gate /
  risk_tier_floor / semantic_guardian / tool_executor /
  candidate_generator / change_engine. Grep-pinned at graduation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

from backend.core.ouroboros.governance.recovery_advisor import (
    RecoveryPlan,
)
from backend.core.ouroboros.governance.recovery_formatter import (
    render_voice,
)

logger = logging.getLogger("Ouroboros.RecoveryAnnouncer")


RECOVERY_ANNOUNCER_SCHEMA_VERSION: str = "recovery_announcer.v1"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, val)


def narrator_enabled() -> bool:
    """Master switch — shared with Karen tool-call + VoiceNarrator surfaces."""
    return _env_bool("OUROBOROS_NARRATOR_ENABLED", True)


def recovery_voice_enabled() -> bool:
    """Sub-switch — recovery announcer specifically.

    Default ``false`` so recovery narration is opt-in; the master
    switch alone doesn't flip it. Operators who want recovery voice
    explicitly set ``JARVIS_RECOVERY_VOICE_ENABLED=true`` alongside
    (or instead of) ``OUROBOROS_NARRATOR_ENABLED``.
    """
    return _env_bool("JARVIS_RECOVERY_VOICE_ENABLED", False)


def is_voice_live() -> bool:
    """Both switches must be true for audio to fire."""
    return narrator_enabled() and recovery_voice_enabled()


# ---------------------------------------------------------------------------
# RecoveryAnnouncer
# ---------------------------------------------------------------------------


# Type alias for the injectable speaker callable — takes text,
# returns an awaitable (to mirror the ``safe_say`` signature).
SpeakerFn = Callable[..., Awaitable[bool]]


class RecoveryAnnouncer:
    """Voice surface for recovery plans.

    Construction is safe in headless / sandbox environments — no
    audio stack is imported. The speaker is lazy-bound on first
    ``_drain`` iteration. Tests inject their own speaker to capture
    spoken text without touching real TTS.
    """

    def __init__(
        self,
        *,
        speaker: Optional[SpeakerFn] = None,
        queue_maxsize: int = 8,
        min_gap_s: Optional[float] = None,
        voice: str = "Karen",
        idempotency_cap: int = 128,
    ) -> None:
        self._speaker = speaker
        self._voice = voice
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._min_gap_s = (
            _env_float("JARVIS_RECOVERY_VOICE_MIN_GAP_S", 3.0, minimum=0.0)
            if min_gap_s is None else float(min_gap_s)
        )
        self._queue: "asyncio.Queue[tuple[str, str]]" = asyncio.Queue(
            maxsize=self._queue_maxsize,
        )
        self._lock = threading.Lock()
        self._spoken: "OrderedDict[str, None]" = OrderedDict()
        self._idempotency_cap = max(16, int(idempotency_cap))
        self._drain_task: Optional["asyncio.Task[None]"] = None
        self._shed_count: int = 0
        self._spoken_count: int = 0
        self._suppressed_count: int = 0
        self._last_spoken_mono: float = float("-inf")

    # --- public API ---------------------------------------------------

    def announce(self, plan: RecoveryPlan) -> bool:
        """Enqueue a plan for narration.

        Returns True iff the plan was accepted into the queue.
        False when:
          * voice is disabled via env flags,
          * the plan is empty,
          * the plan was already announced this process,
          * the queue was full and drop-oldest kicked in.

        Never raises. Safe to call from sync contexts (REPL handlers).
        """
        if plan is None or not plan.has_suggestions:
            return False
        if not is_voice_live():
            with self._lock:
                self._suppressed_count += 1
            return False
        key = self._plan_key(plan)
        with self._lock:
            if key in self._spoken:
                self._suppressed_count += 1
                return False
            self._remember(key)
        text = render_voice(plan)
        if not text:
            return False
        try:
            return self._enqueue(key, text)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[RecoveryAnnouncer] enqueue raise: %s", exc,
            )
            return False

    def announce_text(self, key: str, text: str) -> bool:
        """Bypass-rendering entry point for REPL ``/recover <op> speak``.

        The REPL already renders the plan via ``render_voice`` — this
        lets it push pre-rendered text through the same rate-limiter
        without re-checking idempotency.
        """
        if not is_voice_live():
            return False
        if not text:
            return False
        try:
            return self._enqueue(key or "manual", text)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[RecoveryAnnouncer] enqueue_text raise: %s", exc,
            )
            return False

    def stats(self) -> dict:
        """Snapshot counters for tests + /recover status."""
        with self._lock:
            return {
                "schema_version": RECOVERY_ANNOUNCER_SCHEMA_VERSION,
                "queued": self._queue.qsize(),
                "queue_maxsize": self._queue_maxsize,
                "spoken": self._spoken_count,
                "shed": self._shed_count,
                "suppressed": self._suppressed_count,
                "is_live": is_voice_live(),
                "voice": self._voice,
                "min_gap_s": self._min_gap_s,
                "idempotency_seen": len(self._spoken),
            }

    def reset(self) -> None:
        """Test helper — drop queue + idempotency state."""
        with self._lock:
            # Drain queue
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._spoken.clear()
            self._shed_count = 0
            self._spoken_count = 0
            self._suppressed_count = 0
            self._last_spoken_mono = float("-inf")
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            self._drain_task = None

    # --- internal helpers --------------------------------------------

    @staticmethod
    def _plan_key(plan: RecoveryPlan) -> str:
        return f"{plan.op_id}|{plan.matched_rule}"

    def _remember(self, key: str) -> None:
        """Insert into bounded LRU of spoken keys. Caller holds lock."""
        self._spoken[key] = None
        if len(self._spoken) > self._idempotency_cap:
            self._spoken.popitem(last=False)

    def _enqueue(self, key: str, text: str) -> bool:
        """Push into the bounded queue with drop-oldest."""
        try:
            self._queue.put_nowait((key, text))
        except asyncio.QueueFull:
            # Drop oldest to admit newest — recovery is time-sensitive,
            # the most recent op's plan is usually the right one to hear.
            try:
                self._queue.get_nowait()
                with self._lock:
                    self._shed_count += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait((key, text))
            except asyncio.QueueFull:
                return False
        # Kick off the drain task on first push. Safe to call from
        # sync contexts because we only touch the loop when actually
        # draining.
        self._ensure_drain_task()
        return True

    def _ensure_drain_task(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — caller is fully sync (e.g., headless
            # harness). The queue holds the text; tests drain manually
            # via ``drain_once_for_test``.
            return
        self._drain_task = loop.create_task(self._drain())

    async def _drain(self) -> None:
        """Background worker: dequeue, rate-limit, speak."""
        speaker = await self._resolve_speaker()
        if speaker is None:
            logger.debug("[RecoveryAnnouncer] no speaker available")
            return
        while True:
            try:
                key, text = await self._queue.get()
            except asyncio.CancelledError:
                return
            # Rate-limit: wait until min_gap_s has elapsed since the
            # last utterance.
            now = time.monotonic()
            gap = self._min_gap_s - (now - self._last_spoken_mono)
            if gap > 0:
                try:
                    await asyncio.sleep(gap)
                except asyncio.CancelledError:
                    return
            try:
                await speaker(text, voice=self._voice)
                with self._lock:
                    self._spoken_count += 1
                    self._last_spoken_mono = time.monotonic()
                logger.info(
                    "[RecoveryAnnouncer] spoken key=%s len=%d",
                    key, len(text),
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[RecoveryAnnouncer] speaker raised on key=%s: %s",
                    key, exc,
                )

    async def _resolve_speaker(self) -> Optional[SpeakerFn]:
        if self._speaker is not None:
            return self._speaker
        # Lazy import of the audio stack — deferred so headless
        # environments never touch it.
        try:
            from backend.core.supervisor.unified_voice_orchestrator import (
                safe_say,  # type: ignore[attr-defined]
            )
            return safe_say  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[RecoveryAnnouncer] lazy speaker import failed: %s", exc,
            )
            return None

    async def drain_once_for_test(self) -> Optional[tuple]:
        """Test helper — pull one item from the queue + invoke the
        injected speaker directly.

        Only callable when a speaker was passed at construction. Returns
        the ``(key, text)`` pair actually spoken, or ``None`` if the
        queue is empty.
        """
        if self._speaker is None:
            return None
        try:
            key, text = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        try:
            await self._speaker(text, voice=self._voice)
            with self._lock:
                self._spoken_count += 1
                self._last_spoken_mono = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RecoveryAnnouncer] drain_once raise: %s", exc)
        return (key, text)


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_default_announcer: Optional[RecoveryAnnouncer] = None
_singleton_lock = threading.Lock()


def get_default_announcer() -> RecoveryAnnouncer:
    global _default_announcer
    with _singleton_lock:
        if _default_announcer is None:
            _default_announcer = RecoveryAnnouncer()
        return _default_announcer


def set_default_announcer(announcer: RecoveryAnnouncer) -> None:
    global _default_announcer
    with _singleton_lock:
        _default_announcer = announcer


def reset_default_announcer() -> None:
    global _default_announcer
    with _singleton_lock:
        if _default_announcer is not None:
            try:
                _default_announcer.reset()
            except Exception:  # noqa: BLE001
                pass
        _default_announcer = None


__all__ = [
    "RECOVERY_ANNOUNCER_SCHEMA_VERSION",
    "RecoveryAnnouncer",
    "SpeakerFn",
    "get_default_announcer",
    "is_voice_live",
    "narrator_enabled",
    "recovery_voice_enabled",
    "reset_default_announcer",
    "set_default_announcer",
]
