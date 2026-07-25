"""Mic → UDS telemetry bridge, and the client-side smoother that consumes it.

Where this sits
---------------
``audio_state_ipc`` records the operator-signed boundary: the daemonized
supervisor owns the audio hardware absolutely; the ``ov`` cockpit is a thin
client that subscribes to audio STATE over a Unix socket and renders. So the
visualizer cannot read the mic — it is in the wrong process — and the amplitude
has to cross the boundary as data.

Three hops, each with a different constraint:

1. **Audio thread** — ``AudioBus.register_mic_consumer`` delivers AEC-cleaned
   16kHz frames and documents that the callback "must be fast and
   non-blocking". So this hop does exactly one thing: hand a zero-copy view to
   the existing :class:`AudioBroadcastTap` mailbox. No RMS, no serialization,
   no socket.
2. **Consumer side** — drains the mailbox, computes RMS, rate-caps to ~20 FPS,
   and publishes over the EXISTING UDS bridge. Off the audio thread entirely.
3. **Client side** — :class:`LevelSmoother` interpolates between received
   samples so the oscilloscope stays fluid even though the transport is
   deliberately lossy.

Why the transport may drop
--------------------------
``StreamWriter.write`` never blocks; it buffers without bound. At 20 FPS a
lagging cockpit would therefore grow the DAEMON's memory indefinitely — the
failure is unbounded queueing, not a deadlock. ``_broadcast_lossy`` reads the
transport's real write-buffer depth and sheds frames above a watermark. For
amplitude that is strictly correct: the newest sample is the only useful one,
so a stale frame is worse than no frame.

State events keep the delivered path. Dropping a ``VAD_ACTIVE`` would corrupt
the client's model; dropping an amplitude sample costs one frame of animation.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def bridge_enabled() -> bool:
    return os.environ.get(
        "JARVIS_MIC_TELEMETRY_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _fps() -> float:
    try:
        return max(1.0, float(os.environ.get("JARVIS_MIC_TELEMETRY_FPS", "20")))
    except (TypeError, ValueError):
        return 20.0


class MicTelemetryBridge:
    """Registers as an AudioBus mic consumer and publishes RMS over the UDS.

    Deliberately split across two threads: ``on_mic_frame`` runs on the audio
    thread and is O(1); ``drain`` runs anywhere else and does the arithmetic
    and the I/O."""

    def __init__(
        self,
        *,
        server: Any = None,
        tap: Any = None,
        clock: Optional[Callable[[], float]] = None,
        fps: Optional[float] = None,
    ) -> None:
        self._server = server
        self._clock = clock or time.monotonic
        self._min_interval = 1.0 / float(fps if fps else _fps())
        self._last_publish = float("-inf")
        self._registered_bus: Any = None
        if tap is not None:
            self._tap = tap
        else:
            from backend.voice.audio_broadcast_tap import get_default_tap
            self._tap = get_default_tap()
        self._unsub: Optional[Callable[[], None]] = None
        self.frames_seen = 0
        self.published = 0
        self.coalesced = 0

    # -- hop 1: the audio thread (MUST stay O(1)) -----------------------

    def on_mic_frame(self, frame: Any) -> None:
        """AudioBus mic consumer. Hands a zero-copy view to the mailbox and
        returns. No RMS here — the callback contract says fast and
        non-blocking, and the audio thread is the one place that must never
        wait on anything."""
        try:
            self.frames_seen += 1
            self._tap.offer(frame, sample_rate=16000)
        except Exception:  # noqa: BLE001 — telemetry NEVER perturbs capture
            pass

    def attach(self, bus: Any) -> bool:
        """Register with a live AudioBus. Returns True iff registered."""
        if not bridge_enabled() or bus is None:
            return False
        try:
            bus.register_mic_consumer(self.on_mic_frame)
            self._registered_bus = bus
            logger.info("[MicTelemetry] attached to AudioBus mic plane")
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[MicTelemetry] attach failed", exc_info=True)
            return False

    def detach(self) -> None:
        bus, self._registered_bus = self._registered_bus, None
        if bus is None:
            return
        try:
            bus.unregister_mic_consumer(self.on_mic_frame)
        except Exception:  # noqa: BLE001
            pass

    # -- hop 2: consumer side (RMS + rate cap + publish) ----------------

    def drain(self, *, plane: str = "user") -> Optional[float]:
        """Reduce the pending frame and publish it. Returns the published
        level, or None when nothing was pending or the rate cap coalesced it.
        Runs OFF the audio thread. NEVER raises."""
        try:
            chunk = self._tap.take()
            if chunk is None:
                return None
            now = self._clock()
            if (now - self._last_publish) < self._min_interval:
                self.coalesced += 1
                return None
            self._last_publish = now

            from backend.core.ouroboros.ui.audio_scope import rms
            level = rms(chunk)
            self.published += 1
            if self._server is not None:
                try:
                    self._server.publish_rms(level, plane)
                except Exception:  # noqa: BLE001
                    logger.debug("[MicTelemetry] publish failed", exc_info=True)
            return level
        except Exception:  # noqa: BLE001
            return None

    def stats(self) -> dict:
        return {
            "frames_seen": self.frames_seen,
            "published": self.published,
            "coalesced": self.coalesced,
        }


# --------------------------------------------------------------------------
# IoC binding
# --------------------------------------------------------------------------

_BRIDGE: Optional["MicTelemetryBridge"] = None


def ensure_attached(server: Any = None) -> Optional["MicTelemetryBridge"]:
    """Idempotently bind the bridge to the live AudioBus singleton.

    Inversion of control WITHOUT touching either collaborator: AudioBus already
    publishes ``get_audio_bus_safe()`` and ``register_mic_consumer``, so the
    bridge pulls its dependency at the moment one exists rather than having it
    pushed in. Nothing is injected into the supervisor's init flow and AudioBus
    is not modified.

    Why lazy rather than a lifecycle subscription: AudioBus exposes NO
    ready-hook — there is no ``on_audio_bus_ready`` to subscribe to, and adding
    one would mean editing the hardware owner to serve a visualizer. So this is
    called from a path that already runs periodically and self-heals: if the
    bus does not exist yet (or was torn down and rebuilt), the next call
    attaches. ``register_mic_consumer`` already de-duplicates, so repeated
    calls are safe.

    Returns the attached bridge, or None when no bus is available yet. NEVER
    raises — a visualizer must not be able to break audio bring-up."""
    global _BRIDGE
    if not bridge_enabled():
        return None
    try:
        from backend.audio.audio_bus import get_audio_bus_safe
        bus = get_audio_bus_safe()
        if bus is None:
            return None
        if _BRIDGE is None:
            _BRIDGE = MicTelemetryBridge(server=server)
        elif server is not None and _BRIDGE._server is None:
            _BRIDGE._server = server          # server arrived after the bus
        if _BRIDGE._registered_bus is not bus:
            # Covers first attach AND re-attach after a bus restart.
            _BRIDGE.detach()
            _BRIDGE.attach(bus)
        return _BRIDGE
    except Exception:  # noqa: BLE001
        return None


def get_bridge() -> Optional["MicTelemetryBridge"]:
    return _BRIDGE


def reset_bridge() -> None:
    """Test seam — drop the singleton so suites cannot bleed into each other."""
    global _BRIDGE
    if _BRIDGE is not None:
        _BRIDGE.detach()
    _BRIDGE = None


def pump_once(server: Any = None, *, plane: str = "user") -> Optional[float]:
    """One attach-and-drain tick. The single call a host loop needs: it binds
    on first use, re-binds after a bus restart, and publishes at most one
    rate-capped sample. NEVER raises."""
    try:
        bridge = ensure_attached(server)
        if bridge is None:
            return None
        return bridge.drain(plane=plane)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# hop 3: client side
# --------------------------------------------------------------------------


def _smoothing() -> float:
    """EMA factor. 0 = frozen, 1 = no smoothing. 0.35 tracks speech onsets
    without visibly stepping when frames are dropped."""
    try:
        return min(1.0, max(0.01, float(
            os.environ.get("JARVIS_MIC_TELEMETRY_SMOOTHING", "0.35"),
        )))
    except (TypeError, ValueError):
        return 0.35


class LevelSmoother:
    """Interpolates between received samples so a lossy transport still looks
    fluid.

    Two mechanisms, because dropped frames and a dead stream need opposite
    treatment:

    * **EMA toward the last received level** — fills the visual gap between
      sparse samples without inventing detail.
    * **Decay toward zero after a silence timeout** — if the daemon stops
      sending entirely (client detached, supervisor died), the meter must fall
      to a flat baseline rather than freeze on the last value. A frozen
      waveform reads as "live and loud" and is the worst possible failure for a
      monitor.
    """

    def __init__(
        self,
        *,
        alpha: Optional[float] = None,
        silence_timeout_s: float = 0.5,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._alpha = alpha if alpha is not None else _smoothing()
        self._timeout = max(0.05, float(silence_timeout_s))
        self._clock = clock or time.monotonic
        self._target = 0.0
        self._current = 0.0
        self._last_rx = float("-inf")

    def observe(self, level: float) -> float:
        """Record a received sample."""
        try:
            self._target = min(1.0, max(0.0, float(level)))
        except (TypeError, ValueError):
            return self._current
        self._last_rx = self._clock()
        return self.value()

    def value(self) -> float:
        """Current interpolated level. Call this per UI frame."""
        now = self._clock()
        target = self._target
        if (now - self._last_rx) > self._timeout:
            # Stream went quiet — fall to baseline instead of freezing.
            target = 0.0
        self._current += (target - self._current) * self._alpha
        if abs(self._current) < 1e-4:
            self._current = 0.0
        return min(1.0, max(0.0, self._current))

    def reset(self) -> None:
        self._target = self._current = 0.0
        self._last_rx = float("-inf")


def parse_rms_frame(msg: Any) -> Optional[tuple]:
    """``(level, plane)`` from a decoded UDS frame, or None if it is not an RMS
    frame. Tolerant by design: an unknown/short payload is simply not ours."""
    try:
        from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
            MSG_RMS_LEVEL,
        )
        if not isinstance(msg, dict) or msg.get("type") != MSG_RMS_LEVEL:
            return None
        level = float(msg.get("level", 0.0) or 0.0)
        plane = str(msg.get("plane", "user") or "user")
        return (min(1.0, max(0.0, level)), plane)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "LevelSmoother",
    "ensure_attached",
    "get_bridge",
    "pump_once",
    "reset_bridge",
    "MicTelemetryBridge",
    "bridge_enabled",
    "parse_rms_frame",
]
