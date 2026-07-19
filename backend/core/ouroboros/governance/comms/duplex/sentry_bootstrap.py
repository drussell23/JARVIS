"""Sentry bootstrap — the three final wiring points of the ambient loop.

Operator authorization 2026-07-19:

  1. **Total fail-closed gate**: ``JARVIS_PASSIVE_SENTRY_ENABLED``
     (default OFF) is checked BEFORE any sentry import — threads,
     deques, and pyobjc runloops stay completely uninstantiated when
     the flag is down.
  2. **Shared-Channel Audio Concurrency**: capture allocation goes
     through :class:`DeferredCaptureAllocator` — a device-busy
     collision is classified DETERMINISTICALLY (PortAudio -9985/-9986
     device errors, POSIX EBUSY/EACCES) and transitions the sentry to
     ``DEFERRED_CAPTURE``: bounded async retries on the loop, the
     primary FSM never blocks, never panics. Non-busy faults fail the
     allocation permanently (no blind retry of a real bug).
  3. **Biometric gate**: :class:`BiometricGateAdapter` wraps the
     EXISTING ``voice_authentication_layer`` (VBIA) — zero rewritten
     biometric code. The stitched pre-roll window is the primary
     verification target; a score in the boundary band invokes the
     PAVA drift matrix over the next three sub-frames. Sentry
     semantics INVERT tier-1's permissive BYPASSED: no adapter mounted
     means NOBODY wakes the organism.

DRY: the wake lease is the EXACT ``RemoteAudioLease`` pathway the CLI
``wake`` command uses — the sentry is a headless client of the same
Tri-State broker.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.SentryBootstrap")

_TRUTHY = ("1", "true", "yes", "on")

STATE_ACTIVE = "ACTIVE"
STATE_DEFERRED_CAPTURE = "DEFERRED_CAPTURE"
STATE_FAILED = "FAILED"


def sentry_enabled() -> bool:
    """The TOTAL gate — consulted before any sentry import. Default
    OFF. NEVER raises."""
    return os.environ.get(
        "JARVIS_PASSIVE_SENTRY_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Shared-channel capture — deterministic device-busy handling
# ---------------------------------------------------------------------------

#: PortAudio device-contention codes (paDeviceUnavailable / paTimedOut
#: family) + POSIX busy/permission errnos. A closed classification —
#: anything else is a REAL fault and is not retried.
_PA_BUSY_CODES = (-9985, -9986, -9988, -9996)
_POSIX_BUSY = (errno.EBUSY, errno.EACCES, errno.EAGAIN)


def classify_capture_error(exc: BaseException) -> str:
    """``busy`` (another OS consumer holds the device — retryable) or
    ``fault`` (a genuine bug — never blind-retried). Deterministic on
    exception TYPE + code, no message pattern-matching where a code
    exists. NEVER raises."""
    try:
        code = getattr(exc, "errno", None)
        if code in _POSIX_BUSY:
            return "busy"
        pa = getattr(exc, "args", ())
        for a in pa:
            if isinstance(a, int) and a in _PA_BUSY_CODES:
                return "busy"
        name = type(exc).__name__
        if name == "PortAudioError":
            # PortAudio hides the code in args[1] sometimes; the
            # device-contention family is the ONLY retryable class.
            text = str(exc).lower()
            if "busy" in text or "unavailable" in text or "timed out" in text:
                return "busy"
        return "fault"
    except Exception:  # noqa: BLE001
        return "fault"


class DeferredCaptureAllocator:
    """Owns the capture-allocation lifecycle. ``ensure()`` tries the
    injected opener; a busy-classified failure transitions to
    DEFERRED_CAPTURE and schedules bounded async retries — the caller
    (the supervisor FSM) returns immediately and keeps orchestrating.
    NEVER raises out of any public method."""

    def __init__(
        self,
        opener: Callable[[], Any],
        *,
        retry_interval_s: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self._opener = opener
        try:
            self._interval = retry_interval_s if retry_interval_s is not None \
                else max(1.0, float(os.environ.get(
                    "JARVIS_SENTRY_CAPTURE_RETRY_S", "10",
                )))
        except (TypeError, ValueError):
            self._interval = 10.0
        try:
            self._max_retries = max_retries if max_retries is not None \
                else max(1, int(os.environ.get(
                    "JARVIS_SENTRY_CAPTURE_MAX_RETRIES", "30",
                )))
        except (TypeError, ValueError):
            self._max_retries = 30
        self.state = STATE_DEFERRED_CAPTURE
        self.handle: Any = None
        self._retry_task: Optional[asyncio.Task] = None
        self.stats: Dict[str, int] = {"attempts": 0, "busy": 0, "faults": 0}

    def ensure(self) -> str:
        """One allocation attempt now; busy → schedule the deferred
        loop. Returns the resulting state. NEVER raises, NEVER
        blocks."""
        state = self._attempt()
        if state == STATE_DEFERRED_CAPTURE and self._retry_task is None:
            try:
                self._retry_task = asyncio.get_running_loop().create_task(
                    self._retry_loop(),
                )
            except RuntimeError:
                pass                    # no loop (sync probe) — caller re-ensures
        return self.state

    def _attempt(self) -> str:
        self.stats["attempts"] += 1
        try:
            self.handle = self._opener()
            self.state = STATE_ACTIVE
            return self.state
        except Exception as exc:  # noqa: BLE001 — classified, not padded
            kind = classify_capture_error(exc)
            if kind == "busy":
                self.stats["busy"] += 1
                self.state = STATE_DEFERRED_CAPTURE
                logger.info(
                    "[Sentry] capture device busy — DEFERRED_CAPTURE "
                    "(retry every %.0fs)", self._interval,
                )
            else:
                self.stats["faults"] += 1
                self.state = STATE_FAILED
                logger.warning("[Sentry] capture fault (no retry): %r", exc)
            return self.state

    async def _retry_loop(self) -> None:
        try:
            retries = 0
            while self.state == STATE_DEFERRED_CAPTURE \
                    and retries < self._max_retries:
                await asyncio.sleep(self._interval)
                retries += 1
                if self._attempt() == STATE_ACTIVE:
                    logger.info(
                        "[Sentry] capture allocated after %d deferred "
                        "retries", retries,
                    )
                    return
            if self.state != STATE_ACTIVE:
                self.state = STATE_FAILED
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.state = STATE_FAILED

    async def stop(self) -> None:
        task = self._retry_task
        self._retry_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Biometric gate — VBIA primary + PAVA drift on the boundary band
# ---------------------------------------------------------------------------


def _boundary_margin() -> float:
    try:
        return max(0.0, min(0.5, float(os.environ.get(
            "JARVIS_SENTRY_VBIA_BOUNDARY_MARGIN", "0.08",
        ))))
    except (TypeError, ValueError):
        return 0.08


def _vbia_threshold() -> float:
    try:
        return max(0.1, min(0.99, float(os.environ.get(
            "JARVIS_TIER1_VBIA_THRESHOLD", "0.70",
        ))))
    except (TypeError, ValueError):
        return 0.70


class BiometricGateAdapter:
    """Composes the EXISTING biometric stack — no rewritten scoring.

    ``scorer(window) -> (confidence: float, is_owner: bool)`` is the
    injected seam; production default routes through
    ``VoiceAuthenticationLayer.verify_for_tier1`` with the stitched
    window in context. Decision law:

      * conf ≥ threshold + margin           → PASS (clear)
      * conf <  threshold − margin          → FAIL (clear)
      * boundary band                       → PAVA drift: score the
        window's three trailing sub-frames; PASS only if the trend is
        non-degrading AND the mean clears the threshold.
      * scorer unavailable / error          → FAIL (sentry inverts
        tier-1's permissive BYPASSED — fail CLOSED).
    """

    def __init__(
        self,
        scorer: Optional[
            Callable[[Any], Awaitable[tuple]]
        ] = None,
    ) -> None:
        self._scorer = scorer or self._default_scorer
        self.stats: Dict[str, int] = {
            "clear_pass": 0, "clear_fail": 0, "pava_evals": 0,
            "pava_pass": 0, "pava_fail": 0, "errors": 0,
        }

    @staticmethod
    async def _default_scorer(window: Any) -> tuple:
        from backend.core.voice_authentication_layer import (  # noqa: PLC0415
            AuthResult,
            VoiceAuthenticationLayer,
        )
        layer = VoiceAuthenticationLayer()
        result = await layer.verify_for_tier1(
            "sentry_wake",
            context={"audio_window": window, "source": "passive_sentry"},
        )
        if result.result != AuthResult.PASSED:
            # BYPASSED / FAILED / ERROR all read as no-confidence in
            # sentry semantics — fail closed.
            return 0.0, False
        return float(result.confidence), bool(result.is_owner)

    async def verify(self, window: Any) -> bool:
        """The sentry's injected verifier. NEVER raises."""
        try:
            # Dynamic Acoustic Normalization pre-filter (E2E
            # Sovereignty 2026-07-19): strip the static room/channel
            # response before ANY scoring — shifting acoustics stop
            # masquerading as identity drift.
            try:
                from .biometric_evolution import normalize_acoustics  # noqa: E501,PLC0415
                window = normalize_acoustics(window)
            except Exception:  # noqa: BLE001
                pass
            conf, is_owner = await self._scorer(window)
            thr, margin = _vbia_threshold(), _boundary_margin()
            if conf >= thr + margin and is_owner:
                self.stats["clear_pass"] += 1
                # Rolling Biometric Evolution: a HIGH-confidence pass
                # teaches the profile (slow EMA in the enrollment's
                # native x-vector space; strict tensor guards inside).
                try:
                    from .biometric_evolution import evolve_if_confident  # noqa: E501,PLC0415
                    emb = getattr(getattr(self, "scorer_ref", None), "last_embedding", None) or getattr(self, "last_embedding", None)
                    if emb is not None and evolve_if_confident(conf, emb):
                        self.stats["evolutions"] = (
                            self.stats.get("evolutions", 0) + 1
                        )
                except Exception:  # noqa: BLE001
                    pass
                return True
            if conf < thr - margin or not is_owner:
                self.stats["clear_fail"] += 1
                return False
            # Boundary band → PAVA acoustic-drift evaluation.
            self.stats["pava_evals"] += 1
            ok = await self._pava_drift(window, thr)
            self.stats["pava_pass" if ok else "pava_fail"] += 1
            return ok
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return False                       # biometric fault = closed

    async def _pava_drift(self, window: Any, thr: float) -> bool:
        """Score the three trailing sub-frames; a non-degrading trend
        whose mean clears the threshold survives the band."""
        try:
            import numpy as np  # noqa: PLC0415
            x = np.asarray(window, dtype=np.float32).reshape(-1)
            if x.size < 3:
                return False
            thirds = np.array_split(x[-min(x.size, 3 * 8000):], 3)
            scores = []
            for frame in thirds:
                conf, _owner = await self._scorer(frame)
                scores.append(float(conf))
            non_degrading = all(
                scores[i + 1] >= scores[i] - 1e-3
                for i in range(len(scores) - 1)
            )
            return non_degrading and (sum(scores) / len(scores)) >= thr
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# The mount — called from the supervisor's audio bootstrap
# ---------------------------------------------------------------------------


def mount_passive_sentry(
    *,
    broadcaster: Any,
    mic_register: Callable[[Callable[[Any], None]], Any],
    scorer: Optional[Callable[[Any], Awaitable[tuple]]] = None,
) -> Optional[Any]:
    """Wire the full ambient loop. Returns the sentry, or ``None``
    when the total gate is down (NOTHING was imported or allocated).
    NEVER raises.

    * ``broadcaster``  — the AudioStateBroadcaster; its
      ``publish_event`` is wrapped so ``AUDIO_PLAYING``/``AUDIO_IDLE``
      drive the Acoustic-Mirage blanking (DRY — the same event the
      attach plane already consumes).
    * ``mic_register`` — audio-bus consumer registration (the SHARED
      capture plane); wrapped in the DeferredCaptureAllocator.
    """
    if not sentry_enabled():
        return None                            # total gate: zero imports
    try:
        from .passive_sentry import (  # noqa: PLC0415
            PassiveSentry,
            SFSpeechWindowSession,
        )
        from backend.core.ouroboros.battle_test.audio_synapse import (  # noqa: E501,PLC0415
            RemoteAudioLease,
        )

        # Production x-vector scorer (final integration 2026-07-19):
        # async encoder load — the mic arms NOW, wake-words landing
        # mid-warmup queue in the Deferred-Evaluation lane. Fail-soft
        # to the injected scorer (tests) or fail-closed default.
        _xvec = None
        if scorer is None:
            try:
                from .biometric_scorer import XVectorScorer  # noqa: PLC0415
                _xvec = XVectorScorer()
                await_start = getattr(_xvec, "start_loading", None)
                if await_start is not None:
                    import asyncio as _aio  # noqa: PLC0415
                    try:
                        _aio.get_running_loop().create_task(await_start())
                    except RuntimeError:
                        _xvec = None
                scorer = _xvec.verify if _xvec is not None else None
            except Exception:  # noqa: BLE001
                _xvec = None
        gate_adapter = BiometricGateAdapter(scorer)
        if _xvec is not None:
            # Rolling-Evolution seam: the adapter reads the scorer's
            # freshest embedding after a clear pass.
            gate_adapter.last_embedding = None

            class _EmbeddingMirror:
                def __get__(self, obj, owner=None):
                    return _xvec.last_embedding
            gate_adapter.scorer_ref = _xvec

        async def _lease() -> bool:
            # DRY: the EXACT CLI-wake pathway — the sentry is a
            # headless Tri-State broker client.
            lease = RemoteAudioLease(lambda _s: None)
            return await lease.acquire()

        def _session_factory() -> Any:
            try:
                return SFSpeechWindowSession()
            except Exception:  # noqa: BLE001
                return None

        sentry = PassiveSentry(
            session_factory=_session_factory,
            verifier=gate_adapter.verify,
            lease_acquirer=_lease,
        )
        sentry.biometrics = gate_adapter       # telemetry surface

        # ---- Acoustic-Mirage wiring (wrap, don't rewrite) ----
        original_publish = broadcaster.publish_event

        def _publish_with_blanking(kind: str) -> None:
            original_publish(kind)
            if kind == "AUDIO_PLAYING":
                sentry.notify_playback(True)
            elif kind == "AUDIO_IDLE":
                sentry.notify_playback(False)

        broadcaster.publish_event = _publish_with_blanking

        # ---- Shared-channel capture with deferral ----
        def _open_capture() -> Any:
            return mic_register(sentry.feed_chunk)

        sentry.capture = DeferredCaptureAllocator(_open_capture)
        sentry.capture.ensure()

        # Monolith fold (E2E Sovereignty): the recognition callbacks
        # land on the MAIN runloop — pump it from the supervisor's own
        # asyncio loop (main thread) so the loop needs NO scratchpad
        # host and survives native launchd ignition.
        async def _pump_loop() -> None:
            from .passive_sentry import SFSpeechWindowSession  # noqa: PLC0415
            while True:
                SFSpeechWindowSession.pump_main_runloop(0.0)
                await asyncio.sleep(0.03)

        try:
            sentry.pump_task = asyncio.get_running_loop().create_task(
                _pump_loop(),
            )
        except RuntimeError:
            sentry.pump_task = None
        logger.info(
            "[Sentry] mounted (capture=%s)", sentry.capture.state,
        )
        return sentry
    except Exception:  # noqa: BLE001
        logger.warning("[Sentry] mount degraded", exc_info=True)
        return None


__all__ = [
    "BiometricGateAdapter",
    "DeferredCaptureAllocator",
    "STATE_ACTIVE",
    "STATE_DEFERRED_CAPTURE",
    "STATE_FAILED",
    "classify_capture_error",
    "mount_passive_sentry",
    "sentry_enabled",
]
