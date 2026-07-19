"""Audio-Visual Synapse — IPC remote control for the karen_duplex plane.

Operator authorization 2026-07-18: the ``karen_duplex`` audio engine was
isolated in the daemon with no orchestration bridge — the attached TUI
could neither arm the VAD/mic loop nor see the audio FSM. This module is
the missing synapse, and it is strictly an ADAPTER (mandate 3, DRY): it
holds NO audio logic. It composes two solved mechanisms:

  * :func:`~backend.core.ouroboros.governance.comms.duplex.
    karen_duplex_factory.get_default_karen` — the process-wide duplex
    handle mounted by ``audio_pipeline_bootstrap`` (start / stop /
    barge-in are ITS methods; we only call them).
  * ``CockpitAttachBridge.publish_audio_state`` — the v2 attach-protocol
    downstream lane (edge-coalesced broadcast + hydration retention).

State mapping (arbiter ``VoiceState`` → attach ``AUDIO_STATES``)::

    listening       → LISTENING     karen_speaking → SPEAKING
    user_speaking   → HEARING       thinking       → THINKING

The arbiter exposes no observer hook, so the synapse watches the FSM
with a bounded edge-coalescing poll (``JARVIS_AUDIO_SYNAPSE_POLL_S``,
default 0.15s — instant to a human eye) rather than rewriting the
arbiter to push. Only EDGES are published; a steady state costs zero
frames on the wire.

Bulletproof contract (mandate 4): every public method is fail-soft. A
missing duplex handle answers ``UNAVAILABLE`` (the TUI renders honesty,
not a hang); a dead watch task is contained; ``stop()`` never raises.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger("Ouroboros.AudioSynapse")

#: VoiceState.value → attach-protocol AUDIO_STATES member.
_FSM_MAP = {
    "listening": "LISTENING",
    "user_speaking": "HEARING",
    "karen_speaking": "SPEAKING",
    "thinking": "THINKING",
}


def _poll_interval_s() -> float:
    try:
        raw = float(os.environ.get("JARVIS_AUDIO_SYNAPSE_POLL_S", "0.15"))
    except (TypeError, ValueError):
        raw = 0.15
    return max(0.05, min(1.0, raw))


class AudioVisualSynapse:
    """Remote-control adapter: attach-protocol audio commands in,
    audio-FSM state frames out.

    ``publish`` is the injected downstream lane (production:
    ``bridge.publish_audio_state``). ``handle_resolver`` is injected for
    tests; production default is ``get_default_karen``.
    """

    def __init__(
        self,
        publish: Callable[[str], None],
        *,
        handle_resolver: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._publish = publish
        self._resolve = handle_resolver or self._default_resolver
        self._watch_task: Optional[asyncio.Task] = None
        self._armed = False

    @staticmethod
    def _default_resolver() -> Any:
        try:
            from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (  # noqa: E501,PLC0415
                get_default_karen,
            )
            return get_default_karen()
        except Exception:  # noqa: BLE001
            return None

    @property
    def armed(self) -> bool:
        return self._armed

    # ---- the upstream command surface (bridge on_audio sink) ----

    async def handle_cmd(self, cmd: str) -> None:
        """Execute one attach-protocol audio command. NEVER raises."""
        try:
            cmd = str(cmd or "").strip().lower()
            if cmd == "wake":
                await self._wake()
            elif cmd == "sleep":
                await self._sleep()
            elif cmd == "barge":
                await self._barge()
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] cmd degraded", exc_info=True)

    async def _wake(self) -> None:
        handle = self._resolve()
        if handle is None:
            # Honest surface: this process has no mounted duplex (the
            # supervisor owns the hardware plane) — the TUI must render
            # that truth, never a fake LISTENING.
            self._safe_publish("UNAVAILABLE")
            return
        try:
            await handle.start()
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] duplex start degraded", exc_info=True)
            self._safe_publish("UNAVAILABLE")
            return
        self._armed = True
        self._safe_publish(self._current_state(handle) or "LISTENING")
        self._start_watch()

    async def _sleep(self) -> None:
        self._armed = False
        await self._stop_watch()
        handle = self._resolve()
        if handle is not None:
            try:
                await handle.stop()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[AudioSynapse] duplex stop degraded", exc_info=True,
                )
        self._safe_publish("OFFLINE")

    async def _barge(self) -> None:
        """Operator barge-in from the TUI — the text-plane equivalent
        of speaking over Karen; routes to the arbiter's OWN interrupt
        seam (no new interrupt logic)."""
        handle = self._resolve()
        arbiter = getattr(handle, "arbiter", None)
        if arbiter is None:
            return
        try:
            await arbiter.on_user_speech_start()
            await arbiter.on_user_speech_end()
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] barge degraded", exc_info=True)

    # ---- the downstream FSM watch (edge-coalesced poll) ----

    def _current_state(self, handle: Any) -> Optional[str]:
        try:
            raw = getattr(getattr(handle, "arbiter", None), "state", None)
            value = getattr(raw, "value", raw)
            return _FSM_MAP.get(str(value or "").strip().lower())
        except Exception:  # noqa: BLE001
            return None

    def _start_watch(self) -> None:
        if self._watch_task is not None and not self._watch_task.done():
            return
        try:
            self._watch_task = asyncio.get_running_loop().create_task(
                self._watch_loop(),
            )
        except RuntimeError:
            self._watch_task = None

    async def _watch_loop(self) -> None:
        interval = _poll_interval_s()
        last: Optional[str] = None
        try:
            while self._armed:
                handle = self._resolve()
                if handle is None:
                    break
                state = self._current_state(handle)
                if state is not None and state != last:
                    last = state
                    self._safe_publish(state)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] watch degraded", exc_info=True)

    async def _stop_watch(self) -> None:
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def stop(self) -> None:
        """Teardown — disarm + reap the watch. NEVER raises."""
        try:
            self._armed = False
            await self._stop_watch()
        except Exception:  # noqa: BLE001
            pass

    def _safe_publish(self, state: str) -> None:
        try:
            self._publish(state)
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] publish degraded", exc_info=True)


__all__ = ["AudioVisualSynapse"]
