"""Audio level pump — raw frames in, normalized floats out, at a capped rate.

The decoupling contract
-----------------------
Audio capture produces thousands of samples per second. The UI needs a *number*,
roughly 20 times a second. Everything about this module exists to enforce that
asymmetry:

* **Only a float crosses the boundary.** Raw frames never reach the UI layer or
  the event broker — the pump computes RMS at the source and yields a single
  normalized value. Publishing frames would put audio buffers into a bounded
  event queue and evict real telemetry under backpressure.
* **Rate-capped by construction.** A hard minimum interval between publishes
  (default 20 FPS) means a 48kHz source cannot saturate the event loop. Levels
  arriving faster than the cap are *coalesced*: the newest wins, because a stale
  amplitude has no value — this is a monitor, not a ledger.
* **Off the UI path.** RMS runs where the samples arrive (the capture callback
  or a worker), never inside a repaint. The pump's only UI contact is an
  ``invalidate`` thunk — the identical zero-flicker hook the reactive theme
  uses.
* **Silence is still information.** A transition INTO silence publishes once so
  the scope can draw a flat baseline, then goes quiet. Without that, a stalled
  capture and a genuinely silent room look identical on screen.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Sequence

from backend.core.ouroboros.ui.audio_scope import (
    AdaptiveNormalizer,
    AudioPlane,
    BrailleScope,
    rms,
)

logger = logging.getLogger(__name__)

EVENT_AUDIO_LEVEL = "audio_level_changed"
EVENT_MIC_STATE = "mic_state_changed"


def pump_enabled() -> bool:
    return os.environ.get(
        "JARVIS_AUDIO_PUMP_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _max_fps() -> float:
    """Publish ceiling. 20 FPS is under the human flicker-fusion threshold for
    amplitude perception while costing ~20 events/s instead of thousands."""
    try:
        return max(1.0, float(os.environ.get("JARVIS_AUDIO_PUMP_FPS", "20")))
    except (TypeError, ValueError):
        return 20.0


class AudioLevelPump:
    """Coalescing, rate-capped bridge from audio frames to UI + broker.

    Thread-safe: ``feed`` is designed to be called from a capture callback on a
    non-async thread, while the UI reads the scope from the event loop."""

    def __init__(
        self,
        *,
        scope: Optional[BrailleScope] = None,
        invalidate: Optional[Callable[[], None]] = None,
        publish: Optional[Callable[[str, str, dict], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        max_fps: Optional[float] = None,
        normalizer: Optional[AdaptiveNormalizer] = None,
    ) -> None:
        self._scope = scope if scope is not None else BrailleScope()
        self._invalidate = invalidate
        self._publish = publish
        self._clock = clock or time.monotonic
        self._min_interval = 1.0 / float(max_fps if max_fps else _max_fps())
        self._norm = normalizer if normalizer is not None else AdaptiveNormalizer()
        # -inf, NOT 0.0: with a monotonic clock near zero, 0.0 made the
        # very first sample look 'too soon' and coalesced it away, so the
        # pump swallowed its own first frame.
        self._last_publish = float("-inf")
        self._was_silent = True
        self._lock = threading.Lock()
        # Observability
        self.published = 0
        self.coalesced = 0

    # -- properties -----------------------------------------------------

    @property
    def scope(self) -> BrailleScope:
        return self._scope

    # -- ingest ---------------------------------------------------------

    def feed_frames(
        self, samples: Sequence[float], *, plane: AudioPlane = AudioPlane.USER,
    ) -> Optional[float]:
        """Called from the capture side with raw normalized (-1..1) samples.

        RMS happens HERE — at the source, off the UI path. Returns the published
        normalized level, or None when coalesced away."""
        return self.feed_level(self._norm.normalize(rms(samples)), plane=plane)

    def feed_level(
        self, level: float, *, plane: AudioPlane = AudioPlane.USER,
    ) -> Optional[float]:
        """Called with an already-normalized 0..1 level. NEVER raises."""
        if not pump_enabled():
            return None
        try:
            lvl = min(1.0, max(0.0, float(level)))
        except (TypeError, ValueError):
            return None

        now = self._clock()
        with self._lock:
            silent = lvl <= 0.0
            # A transition INTO silence always publishes once, so the scope can
            # settle to a flat baseline instead of freezing on the last peak.
            edge_to_silence = silent and not self._was_silent
            due = (now - self._last_publish) >= self._min_interval
            if not due and not edge_to_silence:
                self.coalesced += 1
                return None
            self._last_publish = now
            self._was_silent = silent
            self.published += 1

        self._scope.set_plane(plane)
        self._scope.push(lvl, normalized=True)

        if self._publish is not None:
            try:
                self._publish(EVENT_AUDIO_LEVEL, "audio", {
                    "level": round(lvl, 4),
                    "plane": plane.value,
                })
            except Exception:  # noqa: BLE001 — telemetry never breaks capture
                logger.debug("[AudioPump] publish failed", exc_info=True)

        # Repaint on every ACCEPTED sample. No extra condition on plane change:
        # set_plane already happened above, so this same repaint carries both the
        # new waveform and the new colour in one frame.
        if self._invalidate is not None:
            try:
                self._invalidate()
            except Exception:  # noqa: BLE001
                logger.debug("[AudioPump] invalidate failed", exc_info=True)
        return lvl

    # -- mic state ------------------------------------------------------

    def publish_mic_state(self, state: str, *, reason: str = "") -> None:
        """Broadcast a latch transition. Separate from level events so a
        consumer can subscribe to state without draining 20 FPS of amplitude."""
        if self._publish is None:
            return
        try:
            self._publish(EVENT_MIC_STATE, "audio", {
                "state": str(state), "reason": str(reason),
            })
        except Exception:  # noqa: BLE001
            logger.debug("[AudioPump] mic-state publish failed", exc_info=True)

    def on_event(self, payload: dict) -> Optional[float]:
        """Consume an ``audio_level_changed`` payload from the broker.

        Lets a SEPARATE process (the ``ov`` attach client) drive its own scope
        from the daemon's stream — the two-process split the cockpit already
        has. Unknown planes degrade to IDLE rather than raising."""
        try:
            data = dict(payload or {})
            lvl = float(data.get("level", 0.0) or 0.0)
            try:
                plane = AudioPlane(str(data.get("plane", "idle")))
            except ValueError:
                plane = AudioPlane.IDLE
            self._scope.set_plane(plane)
            self._scope.push(min(1.0, max(0.0, lvl)), normalized=True)
            if self._invalidate is not None:
                try:
                    self._invalidate()
                except Exception:  # noqa: BLE001
                    pass
            return lvl
        except Exception:  # noqa: BLE001
            return None


def default_publisher() -> Optional[Callable[[str, str, dict], Any]]:
    """The existing broker publisher, or None when unavailable (DRY — no second
    event path)."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_task_event,
        )
        return publish_task_event
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "EVENT_AUDIO_LEVEL",
    "EVENT_MIC_STATE",
    "AudioLevelPump",
    "default_publisher",
    "pump_enabled",
]
