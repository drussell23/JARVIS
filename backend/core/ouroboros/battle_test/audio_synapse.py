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
from typing import Any, Callable, Optional, Dict

logger = logging.getLogger("Ouroboros.AudioSynapse")

#: VoiceState.value → attach-protocol AUDIO_STATES member.
_FSM_MAP = {
    "listening": "LISTENING",
    "user_speaking": "HEARING",
    "karen_speaking": "SPEAKING",
    "thinking": "THINKING",
}


#: Supervisor event kinds → attach-protocol AUDIO_STATES (the remote
#: lane's morphing vocabulary; IDLE while armed reads LISTENING).
_EVENT_MAP = {
    "VAD_ACTIVE": "HEARING",
    "VAD_INACTIVE": "LISTENING",
    "TTS_GENERATING": "THINKING",
    "AUDIO_PLAYING": "SPEAKING",
    "AUDIO_IDLE": "LISTENING",
}


def _poll_interval_s() -> float:
    try:
        raw = float(os.environ.get("JARVIS_AUDIO_SYNAPSE_POLL_S", "0.15"))
    except (TypeError, ValueError):
        raw = 0.15
    return max(0.05, min(1.0, raw))


def broker_enabled() -> bool:
    """``JARVIS_AUDIO_BROKER_ENABLED`` (default on) — the cross-process
    lane is fail-soft: no supervisor socket → honest UNAVAILABLE, same
    as before the broker existed. NEVER raises."""
    return os.environ.get(
        "JARVIS_AUDIO_BROKER_ENABLED", "1",
    ).strip().lower() in ("1", "true", "yes", "on")


class RemoteAudioLease:
    """Daemon-side half of the Tri-State broker: negotiate the audio
    lease with the supervisor over the EXISTING ``audio_state_ipc``
    transport, heartbeat while armed, and map the supervisor's
    hardware FSM events into attach-protocol states.

    Strictly a broker (mandate 1): no audio bytes, no hardware, no
    duplex import. If the supervisor vanishes mid-conversation the
    read loop ends → ``OFFLINE`` is published and the heartbeat task
    reaps itself; the supervisor's own drop-release disarms the mic.
    """

    def __init__(
        self,
        publish: Callable[[str], None],
        publish_transcript: Optional[Callable[..., None]] = None,
    ) -> None:
        self._publish = publish
        #: Optional sink for recognised speech. Absent, transcripts are
        #: simply not forwarded and voice behaves exactly as before.
        self._publish_transcript = publish_transcript
        #: utterance_id → (accumulated_text, seq). The WIRE carries deltas
        #: (`chunk`); a cockpit needs the accumulated sentence, because it
        #: replaces a span rather than appending to one. Accumulating here
        #: keeps that in ONE place instead of in every consumer.
        #:
        #: `seq` is added here too: the duplex protocol has no ordering field,
        #: and without one a partial delivered after its own final would
        #: rewind the sentence in the operator's prompt.
        self._utterances: Dict[str, Any] = {}
        self._client: Any = None
        self._hb_task: Optional[asyncio.Task] = None
        self._ttl_s: float = 5.0
        self._granted = asyncio.Event()
        self._denied_reason: Optional[str] = None
        self.active: bool = False

    async def acquire(self, *, preempt: bool = False, ptt: bool = False) -> bool:
        """Connect + negotiate. ``preempt`` sends ``acquire_preempt``
        (FORCE_WAKE — revokes an incumbent terminal); ``ptt`` opens an
        ephemeral push-to-talk hold (always preempting; closed by
        :meth:`ptt_end`). False (with UNAVAILABLE published by the
        CALLER, keeping one honesty seam) when the supervisor is
        absent, refuses, or times out. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (  # noqa: E501,PLC0415
                AudioStateClient,
                lease_ttl_s,
            )
            self._ttl_s = lease_ttl_s()
            client = AudioStateClient(on_message=self._on_message)
            if not await client.connect():
                return False
            self._client = client
            cmd = "ptt_start" if ptt else (
                "acquire_preempt" if preempt else "acquire"
            )
            if not client.send_lease(cmd):
                await client.close()
                self._client = None
                return False
            try:
                await asyncio.wait_for(
                    self._granted.wait(), timeout=self._ttl_s,
                )
            except asyncio.TimeoutError:
                await self.release()
                return False
            if self._denied_reason is not None:
                await self.release()
                return False
            self.active = True
            self._hb_task = asyncio.get_running_loop().create_task(
                self._heartbeat_loop(),
            )
            self._publish_safe("LISTENING")
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] remote acquire degraded", exc_info=True)
            await self.release()
            return False

    def _on_transcript(self, msg: dict) -> None:
        """Accumulate a chunk and forward the sentence so far. NEVER raises.

        Karen's own speech is forwarded with its role intact and refused by
        the composer — she is talking, not dictating, and her words have no
        business in the operator's prompt.
        """
        try:
            sink = self._publish_transcript
            if sink is None:
                return
            uid = str(msg.get("utterance_id", "") or "").strip()
            if not uid:
                return
            chunk = str(msg.get("chunk", "") or "")
            final = bool(msg.get("final", False))
            state = self._utterances.get(uid) or {"text": "", "seq": 0}
            state["text"] = (state["text"] + chunk)[:4000]
            state["seq"] += 1
            self._utterances[uid] = state
            if final:
                self._utterances.pop(uid, None)
            # Bounded on EVERY chunk, not only on a final. A dropped final or
            # a crashed recogniser produces utterances that never complete,
            # and evicting only on completion means those accumulate for the
            # life of the daemon. Oldest first: an utterance still being
            # spoken is the one worth keeping.
            while len(self._utterances) > 8:
                self._utterances.pop(next(iter(self._utterances)), None)
            sink(uid, state["text"], final=final,
                 role=str(msg.get("role", "user") or "user"),
                 seq=state["seq"])
        except Exception:  # noqa: BLE001 — never break the audio FSM
            logger.debug("[AudioSynapse] transcript degraded", exc_info=True)

    def _on_message(self, msg: dict) -> None:
        try:
            mtype = msg.get("type")
            if mtype == "transcript":
                self._on_transcript(msg)
                return
            if mtype == "lease":
                if msg.get("granted"):
                    ttl = msg.get("ttl_s")
                    if isinstance(ttl, (int, float)) and ttl > 0:
                        self._ttl_s = float(ttl)
                    self._denied_reason = None
                    self._granted.set()
                else:
                    reason = str(msg.get("reason", "denied"))
                    if reason in ("expired", "held"):
                        self._denied_reason = reason
                        self._granted.set()
                    if reason == "expired" and self.active:
                        # Supervisor expired us (our heartbeats
                        # stalled) — mirror its fail-safe honestly.
                        self.active = False
                        self._publish_safe("OFFLINE")
                    elif reason == "preempted" and self.active:
                        # Revoked over the heartbeat return channel:
                        # another terminal asserted FORCE_WAKE/PTT.
                        # Morph honestly; do NOT auto-re-acquire (that
                        # would be two terminals fighting a mic war).
                        self.active = False
                        self._publish_safe("HELD")
                    elif reason == "hw_fault" and self.active:
                        # The audio device vanished under the lease —
                        # supervisor already disarmed; render truth.
                        self.active = False
                        self._publish_safe("UNAVAILABLE")
            elif mtype == "event" and self.active:
                kind = str(msg.get("kind", ""))
                if kind == "HW_FAULT":
                    self.active = False
                    self._publish_safe("UNAVAILABLE")
                    return
                state = _EVENT_MAP.get(kind)
                if state is not None:
                    self._publish_safe(state)
        except Exception:  # noqa: BLE001
            pass

    async def _heartbeat_loop(self) -> None:
        """TTL/3 cadence — three misses inside one deadline window
        would be needed to lose a healthy lease. Ends itself (and
        reports OFFLINE) the moment the transport dies."""
        try:
            while self.active:
                client = self._client
                if client is None or not client.connected:
                    break
                client.send_lease("heartbeat")
                await asyncio.sleep(self._ttl_s / 3.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self.active:
                self.active = False
                self._publish_safe("OFFLINE")

    def flush(self) -> bool:
        """Ducking: halt the supervisor's outbound audio NOW (holder-
        only server-side). Non-blocking. NEVER raises."""
        try:
            client = self._client
            if client is None or not self.active:
                return False
            return bool(client.send_lease("flush"))
        except Exception:  # noqa: BLE001
            return False

    def ptt_end(self) -> bool:
        """Close an ephemeral push-to-talk hold (full release server-
        side). NEVER raises."""
        try:
            client = self._client
            if client is None:
                return False
            return bool(client.send_lease("ptt_end"))
        except Exception:  # noqa: BLE001
            return False

    async def release(self) -> None:
        """Release + teardown. Idempotent; NEVER raises."""
        self.active = False
        task = self._hb_task
        self._hb_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.send_lease("release")
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    def _publish_safe(self, state: str) -> None:
        try:
            self._publish(state)
        except Exception:  # noqa: BLE001
            pass


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
        publish_transcript: Optional[Callable[..., None]] = None,
        *,
        handle_resolver: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._publish = publish
        #: Threaded down to the lease, which is where duplex messages
        #: actually arrive. Optional throughout: without it, transcripts are
        #: simply not forwarded and voice behaves exactly as before.
        self._publish_transcript = publish_transcript
        self._resolve = handle_resolver or self._default_resolver
        self._watch_task: Optional[asyncio.Task] = None
        self._armed = False
        self._remote: Optional[RemoteAudioLease] = None

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
            elif cmd == "force_wake":
                await self._wake(preempt=True)
            elif cmd == "sleep":
                await self._sleep()
            elif cmd == "barge":
                await self._barge()
            elif cmd == "ptt":
                await self._wake(preempt=True, ptt=True)
            elif cmd == "ptt_stop":
                remote = self._remote
                if remote is not None and remote.ptt_end():
                    self._remote = None
                    self._armed = False
                    self._safe_publish("OFFLINE")
            elif cmd == "flush":
                await self._flush()
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] cmd degraded", exc_info=True)

    async def _wake(self, *, preempt: bool = False, ptt: bool = False) -> None:
        handle = self._resolve()
        if handle is None or preempt or ptt:
            # No LOCAL duplex — the supervisor owns the hardware plane.
            # Tri-State broker path: negotiate a cross-process audio
            # lease over audio_state_ipc (preempt/ptt are inherently
            # cross-process verbs — floor arbitration lives at the
            # supervisor's lease table). Only when the supervisor is
            # absent/refusing does the TUI see UNAVAILABLE — honesty,
            # never a fake LISTENING.
            if broker_enabled():
                remote = RemoteAudioLease(
                    self._publish, self._publish_transcript,
                )
                if await remote.acquire(preempt=preempt, ptt=ptt):
                    self._remote = remote
                    self._armed = True
                    return
            if handle is None:
                self._safe_publish("UNAVAILABLE")
                return
            # Preempt/ptt asked but no supervisor: fall through to the
            # local handle (single-terminal case — nothing to preempt).
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
        remote = self._remote
        self._remote = None
        if remote is not None:
            await remote.release()
            self._safe_publish("OFFLINE")
            return
        handle = self._resolve()
        if handle is not None:
            try:
                await handle.stop()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[AudioSynapse] duplex stop degraded", exc_info=True,
                )
        self._safe_publish("OFFLINE")

    async def _flush(self) -> None:
        """Ducking — halt outbound audio wherever the lease lives:
        remote lease → wire flush; local handle → the arbiter's own
        flush seam. NEVER raises."""
        remote = self._remote
        if remote is not None:
            remote.flush()
            return
        handle = self._resolve()
        arbiter = getattr(handle, "arbiter", None)
        flush = getattr(arbiter, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:  # noqa: BLE001
                logger.debug("[AudioSynapse] local flush degraded", exc_info=True)

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
        """Teardown — disarm + reap watch AND remote lease (the
        supervisor's drop-release is the backstop, but a clean daemon
        exit releases explicitly). NEVER raises."""
        try:
            self._armed = False
            await self._stop_watch()
            remote = self._remote
            self._remote = None
            if remote is not None:
                await remote.release()
        except Exception:  # noqa: BLE001
            pass

    def _safe_publish(self, state: str) -> None:
        try:
            self._publish(state)
        except Exception:  # noqa: BLE001
            logger.debug("[AudioSynapse] publish degraded", exc_info=True)


__all__ = ["AudioVisualSynapse", "RemoteAudioLease", "broker_enabled"]
