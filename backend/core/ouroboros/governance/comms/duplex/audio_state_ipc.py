"""Stateful IPC Audio Pub/Sub Bridge — supervisor audio plane → ov CLI.

Architecture decision (operator-signed 2026-07-18): the daemonized
``unified_supervisor`` retains ABSOLUTE ownership of the audio hardware
plane (mic, speakers, CoreAudio/ALSA locks). The ephemeral ``ov``
cockpit never binds ``karen_duplex`` or VAD listeners — it subscribes to
the supervisor's audio STATE over a Unix Domain Socket and renders.

Protocol (newline-delimited JSON over UDS, schema ``audio_ipc.v1``):

  * On accept, the server's FIRST line is the **state-reconciliation
    handshake**: current voice-state booleans, the ACTIVE utterance
    (id + accumulated ``text_so_far``) if Karen or the operator is
    mid-sentence, and a bounded ring of recent events. A CLI opened
    while Karen is mid-sentence renders the ongoing transcript
    immediately — no ghosting, no fresh prompt required.
  * Thereafter the server streams events:
      ``{"type":"event","kind":"VAD_ACTIVE"|"VAD_INACTIVE"|
         "TTS_GENERATING"|"AUDIO_PLAYING"|"AUDIO_IDLE","ts":...}``
      ``{"type":"transcript","role":"user"|"karen","chunk":"...",
         "final":bool,"utterance_id":"u-...","ts":...}``

Degradation contract (mandate 4): the CLIENT side is strictly
non-blocking — ``connect()`` is bounded by a short timeout and returns
``False`` on a missing / refused / dead socket, so the ``ov`` UI loop
cleanly stays in text-only mode. The SERVER side never lets one slow or
dead subscriber block a publish (per-client fail-drop).

Authority invariant: pure presentation telemetry. No audio capture, no
mutation surface, no policy imports. Socket is chmod 0600 (same-user
only). Master ``JARVIS_AUDIO_IPC_ENABLED`` (default on — §7 Absolute
Observability; the socket exports state, never grants control).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Set

logger = logging.getLogger(__name__)

AUDIO_IPC_SCHEMA_VERSION = "audio_ipc.v1"

_TRUTHY = ("1", "true", "yes", "on")

#: Closed event-kind vocabulary (state machine below keys off these).
EVENT_VAD_ACTIVE = "VAD_ACTIVE"
EVENT_VAD_INACTIVE = "VAD_INACTIVE"
EVENT_TTS_GENERATING = "TTS_GENERATING"
EVENT_AUDIO_PLAYING = "AUDIO_PLAYING"
EVENT_AUDIO_IDLE = "AUDIO_IDLE"
EVENT_KINDS = (
    EVENT_VAD_ACTIVE, EVENT_VAD_INACTIVE, EVENT_TTS_GENERATING,
    EVENT_AUDIO_PLAYING, EVENT_AUDIO_IDLE,
)


def audio_ipc_enabled() -> bool:
    """Master gate — default ON (read-only state export). NEVER raises."""
    return os.environ.get(
        "JARVIS_AUDIO_IPC_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def socket_path() -> Path:
    """``JARVIS_AUDIO_IPC_SOCKET`` (default ``.jarvis/audio_state.sock``)."""
    return Path(os.environ.get(
        "JARVIS_AUDIO_IPC_SOCKET", ".jarvis/audio_state.sock",
    ))


def _recent_cap() -> int:
    try:
        return max(8, int(os.environ.get("JARVIS_AUDIO_IPC_RECENT", "100")))
    except (TypeError, ValueError):
        return 100


def _connect_timeout_s() -> float:
    try:
        return max(0.05, float(os.environ.get(
            "JARVIS_AUDIO_IPC_CONNECT_TIMEOUT_S", "0.5",
        )))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# Server — lives in the unified_supervisor process (audio-plane owner)
# ---------------------------------------------------------------------------


class AudioStateBroadcaster:
    """The supervisor-side pub/sub hub.

    Holds the authoritative presentation-state (VAD / TTS / playback
    booleans), the ACTIVE utterance accumulation, and a bounded ring of
    recent messages for late-joiner replay. Every publish is O(clients);
    a slow or dead client is dropped, never awaited inline.
    """

    def __init__(self, *, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else socket_path()
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=_recent_cap())
        self._state: Dict[str, bool] = {
            "vad_active": False,
            "tts_generating": False,
            "audio_playing": False,
        }
        self._utterance: Optional[Dict[str, Any]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- lifecycle ----

    async def start(self) -> bool:
        """Bind the UDS (removing any stale socket file). Returns False
        (never raises) when binding fails — the supervisor keeps running
        without the export surface."""
        try:
            self._loop = asyncio.get_running_loop()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                pass
            self._server = await asyncio.start_unix_server(
                self._on_client, path=str(self._path),
            )
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            logger.info("[AudioIPC] broadcaster bound at %s", self._path)
            return True
        except Exception as exc:  # noqa: BLE001 — export is optional
            logger.warning("[AudioIPC] broadcaster bind failed: %s", exc)
            self._server = None
            return False

    async def stop(self) -> None:
        """Close server + clients, unlink the socket. NEVER raises."""
        try:
            if self._server is not None:
                self._server.close()
                try:
                    await self._server.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                self._server = None
            for w in list(self._clients):
                self._drop_client(w)
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                pass
        except Exception:  # noqa: BLE001
            logger.debug("[AudioIPC] stop degraded", exc_info=True)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---- publish surface (supervisor-side hooks call these) ----

    def publish_event(self, kind: str) -> None:
        """Publish one state event + update the state machine. Safe from
        any thread (marshalled to the bound loop). NEVER raises."""
        try:
            if kind not in EVENT_KINDS:
                return
            msg = {"type": "event", "kind": kind, "ts": time.time()}
            self._apply_state(kind)
            self._enqueue(msg)
        except Exception:  # noqa: BLE001
            logger.debug("[AudioIPC] publish_event degraded", exc_info=True)

    def publish_vad(self, active: bool) -> None:
        """Edge-coalesced VAD publisher — repeated same-state calls are
        dropped (the per-frame consumer may fire at 50Hz). NEVER raises."""
        try:
            if bool(active) == self._state["vad_active"]:
                return
            self.publish_event(
                EVENT_VAD_ACTIVE if active else EVENT_VAD_INACTIVE,
            )
        except Exception:  # noqa: BLE001
            pass

    def publish_transcript(
        self,
        role: str,
        chunk: str,
        *,
        final: bool = False,
        utterance_id: Optional[str] = None,
    ) -> str:
        """Publish a transcript chunk, accumulating the ACTIVE utterance
        for late-joiner reconciliation. Returns the utterance id. A
        ``final`` chunk seals the utterance into the recent ring and
        clears the active slot. NEVER raises."""
        uid = utterance_id or ""
        try:
            role = str(role or "unknown")
            chunk = str(chunk or "")
            if (
                self._utterance is not None
                and (utterance_id in (None, self._utterance.get("utterance_id")))
                and self._utterance.get("role") == role
            ):
                uid = str(self._utterance["utterance_id"])
                self._utterance["text_so_far"] = (
                    str(self._utterance.get("text_so_far", "")) + chunk
                )
            else:
                uid = utterance_id or f"u-{uuid.uuid4().hex[:12]}"
                self._utterance = {
                    "utterance_id": uid,
                    "role": role,
                    "text_so_far": chunk,
                    "started_unix": time.time(),
                }
            msg = {
                "type": "transcript",
                "role": role,
                "chunk": chunk,
                "final": bool(final),
                "utterance_id": uid,
                "ts": time.time(),
            }
            if final:
                self._utterance = None
            self._enqueue(msg)
            return uid
        except Exception:  # noqa: BLE001
            logger.debug("[AudioIPC] publish_transcript degraded", exc_info=True)
            return uid

    # ---- internals ----

    def _apply_state(self, kind: str) -> None:
        if kind == EVENT_VAD_ACTIVE:
            self._state["vad_active"] = True
        elif kind == EVENT_VAD_INACTIVE:
            self._state["vad_active"] = False
        elif kind == EVENT_TTS_GENERATING:
            self._state["tts_generating"] = True
        elif kind == EVENT_AUDIO_PLAYING:
            self._state["tts_generating"] = False
            self._state["audio_playing"] = True
        elif kind == EVENT_AUDIO_IDLE:
            self._state["tts_generating"] = False
            self._state["audio_playing"] = False

    def _enqueue(self, msg: Dict[str, Any]) -> None:
        self._recent.append(msg)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._broadcast(msg)
        else:
            loop.call_soon_threadsafe(self._broadcast, msg)

    def _broadcast(self, msg: Dict[str, Any]) -> None:
        if not self._clients:
            return
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        for w in list(self._clients):
            try:
                if w.is_closing():
                    self._drop_client(w)
                    continue
                w.write(data)
                # No inline await — asyncio buffers; a client that never
                # drains eventually errors on write and gets dropped.
            except Exception:  # noqa: BLE001
                self._drop_client(w)

    def _drop_client(self, w: asyncio.StreamWriter) -> None:
        self._clients.discard(w)
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass

    def _handshake_payload(self) -> Dict[str, Any]:
        return {
            "type": "handshake",
            "schema_version": AUDIO_IPC_SCHEMA_VERSION,
            "ts": time.time(),
            "state": dict(self._state),
            "active_utterance": (
                dict(self._utterance) if self._utterance is not None else None
            ),
            "recent": list(self._recent),
        }

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            handshake = (
                json.dumps(self._handshake_payload(), separators=(",", ":"))
                + "\n"
            ).encode()
            writer.write(handshake)
            await writer.drain()
            self._clients.add(writer)
            # Hold the connection open; EOF (client gone) unregisters.
            while True:
                data = await reader.read(4096)
                if not data:
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._drop_client(writer)


# ---------------------------------------------------------------------------
# Client — lives in the ov cockpit process (render-only subscriber)
# ---------------------------------------------------------------------------


class AudioStateClient:
    """Non-blocking UDS subscriber for the ov cockpit.

    ``connect()`` is bounded by a short timeout and NEVER raises — a
    missing/refused/dead socket returns ``False`` and the CLI stays in
    text-only mode with zero UI-loop impact. On success the FIRST frame
    is the reconciliation handshake (delivered to ``on_handshake``);
    subsequent frames stream to ``on_message``.
    """

    def __init__(
        self,
        *,
        on_handshake: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        path: Optional[Path] = None,
    ) -> None:
        self._path = Path(path) if path is not None else socket_path()
        self._on_handshake = on_handshake or (lambda _m: None)
        self._on_message = on_message or (lambda _m: None)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self.connected: bool = False

    async def connect(self) -> bool:
        """Bounded connect + handshake consume. Returns False on ANY
        failure (text-only degrade). NEVER raises, NEVER hangs — both
        the connect and the handshake read share one deadline."""
        timeout = _connect_timeout_s()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path=str(self._path)),
                timeout=timeout,
            )
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=timeout,
            )
            if not line:
                await self.close()
                return False
            handshake = json.loads(line)
            if handshake.get("type") == "handshake":
                self._safe(self._on_handshake, handshake)
            else:
                self._safe(self._on_message, handshake)
            self._read_task = asyncio.get_running_loop().create_task(
                self._read_loop(),
            )
            self.connected = True
            return True
        except Exception:  # noqa: BLE001 — dead daemon = text-only mode
            logger.debug(
                "[AudioIPC] connect degraded (text-only mode)", exc_info=True,
            )
            await self.close()
            return False

    async def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue          # one malformed frame never kills the feed
                self._safe(self._on_message, msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.connected = False

    async def close(self) -> None:
        """Idempotent teardown. NEVER raises."""
        self.connected = False
        task = self._read_task
        self._read_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        w = self._writer
        self._writer = None
        self._reader = None
        if w is not None:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _safe(cb: Callable[[Dict[str, Any]], None], msg: Dict[str, Any]) -> None:
        try:
            cb(msg)
        except Exception:  # noqa: BLE001 — render sink never kills the feed
            logger.debug("[AudioIPC] client callback degraded", exc_info=True)


__all__ = [
    "AUDIO_IPC_SCHEMA_VERSION",
    "AudioStateBroadcaster",
    "AudioStateClient",
    "EVENT_AUDIO_IDLE",
    "EVENT_AUDIO_PLAYING",
    "EVENT_KINDS",
    "EVENT_TTS_GENERATING",
    "EVENT_VAD_ACTIVE",
    "EVENT_VAD_INACTIVE",
    "audio_ipc_enabled",
    "socket_path",
]
