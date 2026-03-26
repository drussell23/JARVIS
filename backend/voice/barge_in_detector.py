"""
Barge-In Detector — Interrupt JARVIS mid-speech by speaking.

When Derek talks while JARVIS is speaking, this module:
1. Detects voice activity via energy threshold (VAD)
2. Kills the afplay TTS process immediately (SIGTERM → SIGKILL)
3. Re-enables audio capture
4. Routes interrupted speech through ConversationManager

All subprocess calls argv-based. Energy detection via sounddevice.

Boundary Principle:
  Deterministic: VAD energy threshold, process kill, gate state.
  No model inference for detection — just RMS energy comparison.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("JARVIS_BARGE_IN_ENABLED", "true").lower() in ("true", "1", "yes")
_ENERGY_THRESHOLD = float(os.environ.get("JARVIS_BARGE_IN_ENERGY_THRESHOLD", "0.02"))
_MIN_TTS_S = float(os.environ.get("JARVIS_BARGE_IN_MIN_TTS_S", "0.5"))
_COOLDOWN_S = float(os.environ.get("JARVIS_BARGE_IN_COOLDOWN_S", "0.3"))


class BargeInDetector:
    """Detects when Derek speaks during TTS and interrupts JARVIS.

    Monitors audio capture energy every 50ms during TTS playback.
    Energy above threshold → kill TTS, notify ConversationManager.
    """

    def __init__(
        self,
        on_barge_in: Optional[Callable[[str], Coroutine]] = None,
        speech_gate: Optional[Any] = None,
    ) -> None:
        self._on_barge_in = on_barge_in
        self._speech_gate = speech_gate
        self._tts_active = False
        self._tts_proc: Optional[asyncio.subprocess.Process] = None
        self._tts_start: float = 0.0
        self._monitor: Optional[asyncio.Task] = None
        self._barge_count: int = 0
        self._last_barge: float = 0.0

    @property
    def is_tts_active(self) -> bool:
        return self._tts_active

    async def start_tts(self, process: asyncio.subprocess.Process) -> None:
        """Register TTS process for barge-in monitoring."""
        if not _ENABLED:
            return
        self._tts_proc = process
        self._tts_active = True
        self._tts_start = time.time()
        self._monitor = asyncio.create_task(
            self._monitor_loop(), name="barge_in_monitor",
        )

    async def stop_tts(self) -> None:
        """Called when TTS finishes normally."""
        self._tts_active = False
        self._tts_proc = None
        if self._monitor and not self._monitor.done():
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass

    async def force_interrupt(self) -> None:
        """Force-interrupt TTS from external trigger."""
        if self._tts_active:
            await self._kill_tts()

    async def _monitor_loop(self) -> None:
        """Check audio energy every 50ms during TTS."""
        try:
            while self._tts_active:
                if (time.time() - self._tts_start) < _MIN_TTS_S:
                    await asyncio.sleep(0.05)
                    continue
                energy = self._get_capture_energy()
                if energy > _ENERGY_THRESHOLD:
                    logger.info("[BargeIn] Detected! energy=%.4f", energy)
                    await self._handle_barge_in()
                    return
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    def _get_capture_energy(self) -> float:
        """Get current audio capture RMS energy. Deterministic."""
        try:
            import sounddevice as sd
            import numpy as np
            rec = sd.rec(int(0.05 * 16000), samplerate=16000,
                         channels=1, dtype="float32", blocking=True)
            if rec is not None:
                return float(np.sqrt(np.mean(rec ** 2)))
        except Exception:
            pass
        return 0.0

    async def _handle_barge_in(self) -> None:
        """Kill TTS, pause, notify manager."""
        self._barge_count += 1
        self._last_barge = time.time()
        await self._kill_tts()
        await asyncio.sleep(_COOLDOWN_S)
        if self._on_barge_in:
            try:
                await self._on_barge_in("__barge_in__")
            except Exception:
                pass
        logger.info("[BargeIn] Interrupted (total: %d)", self._barge_count)

    async def _kill_tts(self) -> None:
        """Kill TTS process. SIGTERM then SIGKILL. Argv-based pkill fallback."""
        self._tts_active = False
        if self._tts_proc:
            try:
                self._tts_proc.terminate()
                try:
                    await asyncio.wait_for(self._tts_proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    self._tts_proc.kill()
                    await self._tts_proc.wait()
            except ProcessLookupError:
                pass
            finally:
                self._tts_proc = None

        # Belt + suspenders: kill lingering afplay (argv, no shell)
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill", "-f", "afplay",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except Exception:
            pass

        if self._speech_gate:
            try:
                self._speech_gate.release()
            except Exception:
                pass

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": _ENABLED, "tts_active": self._tts_active,
            "barge_count": self._barge_count, "last_barge": self._last_barge,
            "threshold": _ENERGY_THRESHOLD,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Enhanced safe_say with barge-in support
# ═══════════════════════════════════════════════════════════════════════════

_global_detector: Optional[BargeInDetector] = None


def get_barge_in_detector() -> BargeInDetector:
    global _global_detector
    if _global_detector is None:
        _global_detector = BargeInDetector()
    return _global_detector


async def safe_say_with_barge_in(
    text: str, voice: str = "Daniel", rate: int = 200,
) -> bool:
    """TTS with barge-in support. Returns True if completed, False if interrupted."""
    import tempfile

    detector = get_barge_in_detector()

    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Render to file (argv, no shell)
        render = await asyncio.create_subprocess_exec(
            "say", "-v", voice, "-r", str(rate), "-o", tmp_path, text,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(render.communicate(), timeout=30.0)
        if render.returncode != 0:
            return False

        # Play with barge-in monitoring (argv, no shell)
        play = await asyncio.create_subprocess_exec(
            "afplay", tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await detector.start_tts(play)
        try:
            await play.wait()
            completed = play.returncode == 0
        except asyncio.CancelledError:
            completed = False
        await detector.stop_tts()
        await asyncio.sleep(0.15)
        return completed

    except Exception:
        return False
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass
