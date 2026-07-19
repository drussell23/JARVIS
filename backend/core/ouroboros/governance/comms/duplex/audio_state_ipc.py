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

Tri-State IPC Audio Broker (v2, operator-authorized 2026-07-18): the
transport gains an upstream **lease lane** — the ONE narrow control
surface, and it still never moves audio bytes:

  * ``{"type":"lease","cmd":"acquire"|"release"|"heartbeat"}`` from a
    client negotiates the audio-arming lease. Single-holder; a second
    client is answered ``{"type":"lease","granted":false,
    "reason":"held"}``. Grants carry ``ttl_s`` so the client derives
    its own heartbeat cadence (no hardcoded numbers on the wire's far
    side).
  * **Orphaned-mic protection**: the lease DIES two independent ways —
    (a) the holder's socket drops (daemon SIGKILL = broken pipe →
    instant release), and (b) heartbeats stop while the socket wedges
    open (a monotonic-deadline watchdog sweeps at TTL/2 and expires
    the lease). Either path invokes the injected ``on_lease_change
    (False)`` so the supervisor disarms the hardware and fails safe.
    The watchdog reads ONLY ``time.monotonic()`` + its own deadline —
    never pipeline state (the Slice-47 watchdog-isolation invariant).
  * The supervisor stays sovereign: ``on_lease_change`` is an injected
    callable the BOOTSTRAP owns; this module never imports or touches
    karen_duplex / hardware. Lease grant ≠ hardware promise — the
    supervisor may still refuse in its callback.
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

AUDIO_IPC_SCHEMA_VERSION = "audio_ipc.v2"

#: Closed lease-command vocabulary (the upstream control lane).
LEASE_CMDS = ("acquire", "release", "heartbeat")


def lease_ttl_s() -> float:
    """``JARVIS_AUDIO_LEASE_TTL_S`` (default 5.0, clamped [1, 60]) —
    the heartbeat deadline. A dead daemon releases the mic within one
    TTL. NEVER raises."""
    try:
        raw = float(os.environ.get("JARVIS_AUDIO_LEASE_TTL_S", "5.0"))
    except (TypeError, ValueError):
        raw = 5.0
    return max(1.0, min(60.0, raw))

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

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        on_lease_change: Optional[Callable[[bool], Any]] = None,
    ) -> None:
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
        # ---- lease lane (v2) ----
        # ``on_lease_change(armed)`` is the supervisor's injected
        # arm/disarm seam (sync or async — both awaited safely). The
        # holder is identified by its StreamWriter; the deadline is a
        # raw ``time.monotonic()`` instant (watchdog isolation).
        self._on_lease_change = on_lease_change
        self._lease_holder: Optional[asyncio.StreamWriter] = None
        self._lease_deadline: float = 0.0
        self._lease_watchdog: Optional[asyncio.Task] = None
        self.lease_stats: Dict[str, int] = {
            "acquires": 0, "denials": 0, "releases": 0,
            "expiries": 0, "drop_releases": 0, "heartbeats": 0,
        }

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
            await self._release_lease(reason="server_stop")
            wd = self._lease_watchdog
            self._lease_watchdog = None
            if wd is not None and not wd.done():
                wd.cancel()
                try:
                    await wd
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
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
        # Orphaned-mic protection path (a): the holder's socket died —
        # SIGKILL'd daemon, broken pipe, hard reset. Release the
        # hardware IMMEDIATELY; never wait for the heartbeat expiry.
        if w is self._lease_holder:
            self.lease_stats["drop_releases"] += 1
            self._schedule_lease_release(reason="holder_dropped")
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- lease lane (v2) --------------------------------------------------

    @property
    def lease_held(self) -> bool:
        return self._lease_holder is not None

    def _schedule_lease_release(self, *, reason: str) -> None:
        """Fire-and-forget release from sync contexts (client drop).
        NEVER raises."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._lease_holder = None
            return
        try:
            loop.create_task(self._release_lease(reason=reason))
        except RuntimeError:
            self._lease_holder = None

    async def _release_lease(self, *, reason: str) -> None:
        """Disarm the supervisor's audio plane + clear the holder.
        Idempotent; NEVER raises — the supervisor process must survive
        ANY fault in the disarm callback (mandate 4: no hang, no
        crash, mic never left hot)."""
        if self._lease_holder is None:
            return
        self._lease_holder = None
        self._lease_deadline = 0.0
        logger.info("[AudioIPC] lease released (%s) — audio disarmed", reason)
        await self._invoke_lease_change(False)

    async def _invoke_lease_change(self, armed: bool) -> None:
        cb = self._on_lease_change
        if cb is None:
            return
        try:
            result = cb(armed)
            if asyncio.iscoroutine(result):
                # Bounded: a wedged arm/disarm callback must never hang
                # the broadcaster loop (the supervisor's own timeout
                # discipline applies inside; this is the outer fuse).
                await asyncio.wait_for(result, timeout=lease_ttl_s())
        except asyncio.TimeoutError:
            logger.warning(
                "[AudioIPC] lease callback timed out (armed=%s)", armed,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[AudioIPC] lease callback degraded (armed=%s)",
                armed, exc_info=True,
            )

    async def _handle_lease_frame(
        self, cmd: str, writer: asyncio.StreamWriter,
    ) -> None:
        """One lease negotiation step. Replies only to the requesting
        client (grants are private; STATE stays broadcast)."""
        ttl = lease_ttl_s()
        if cmd == "heartbeat":
            if writer is self._lease_holder:
                self._lease_deadline = time.monotonic() + ttl
                self.lease_stats["heartbeats"] += 1
            return
        if cmd == "release":
            if writer is self._lease_holder:
                self.lease_stats["releases"] += 1
                await self._release_lease(reason="client_release")
                self._reply(writer, {
                    "type": "lease", "granted": False, "reason": "released",
                })
            return
        # acquire
        if self._lease_holder is not None and self._lease_holder is not writer:
            self.lease_stats["denials"] += 1
            self._reply(writer, {
                "type": "lease", "granted": False, "reason": "held",
            })
            return
        first_acquire = self._lease_holder is None
        self._lease_holder = writer
        self._lease_deadline = time.monotonic() + ttl
        self.lease_stats["acquires"] += 1
        if first_acquire:
            await self._invoke_lease_change(True)
            self._start_lease_watchdog()
        self._reply(writer, {
            "type": "lease", "granted": True, "ttl_s": ttl,
        })

    def _start_lease_watchdog(self) -> None:
        if self._lease_watchdog is not None and not self._lease_watchdog.done():
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        self._lease_watchdog = loop.create_task(self._lease_watchdog_loop())

    async def _lease_watchdog_loop(self) -> None:
        """Orphaned-mic protection path (b): heartbeats stopped while
        the socket wedged open. Sweeps at TTL/2; reads ONLY
        ``time.monotonic()`` + the deadline — never pipeline state
        (Slice-47 watchdog isolation: a watchdog coupled to the system
        it guards deadlocks with it)."""
        try:
            while self._lease_holder is not None:
                await asyncio.sleep(lease_ttl_s() / 2.0)
                if (
                    self._lease_holder is not None
                    and time.monotonic() > self._lease_deadline
                ):
                    self.lease_stats["expiries"] += 1
                    holder = self._lease_holder
                    await self._release_lease(reason="heartbeat_expired")
                    self._reply(holder, {
                        "type": "lease", "granted": False,
                        "reason": "expired",
                    })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[AudioIPC] lease watchdog degraded", exc_info=True)

    def _reply(self, writer: asyncio.StreamWriter, msg: Dict[str, Any]) -> None:
        """Best-effort unicast to one client. NEVER raises."""
        try:
            if writer.is_closing():
                return
            writer.write(
                (json.dumps(msg, separators=(",", ":")) + "\n").encode(),
            )
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------------

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
            "lease": {"held": self.lease_held},
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
            # Upstream lane: newline-JSON lease frames. Anything
            # malformed or off-vocabulary is IGNORED (the export lane
            # stays authority-free; lease is the one narrow verb set).
            # EOF (client gone) unregisters + drop-releases the lease.
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if frame.get("type") == "lease":
                    cmd = str(frame.get("cmd", "")).strip().lower()
                    if cmd in LEASE_CMDS:
                        await self._handle_lease_frame(cmd, writer)
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

    def send_lease(self, cmd: str) -> bool:
        """Pipe one lease frame upstream (``acquire`` / ``release`` /
        ``heartbeat``). Non-blocking; False when detached or the
        command is off-vocabulary. NEVER raises."""
        try:
            cmd = str(cmd or "").strip().lower()
            if cmd not in LEASE_CMDS:
                return False
            w = self._writer
            if not self.connected or w is None or w.is_closing():
                return False
            frame = {"type": "lease", "cmd": cmd}
            w.write((json.dumps(frame, separators=(",", ":")) + "\n").encode())
            return True
        except Exception:  # noqa: BLE001
            return False

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
    "LEASE_CMDS",
    "audio_ipc_enabled",
    "lease_ttl_s",
    "socket_path",
]
