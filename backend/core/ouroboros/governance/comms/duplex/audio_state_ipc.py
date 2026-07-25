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
#: v2.1 (operator-authorized 2026-07-18): ``acquire_preempt`` revokes
#: the incumbent (FORCE_WAKE); ``ptt_start``/``ptt_end`` bracket an
#: ephemeral push-to-talk hold; ``flush`` halts outbound audio.
LEASE_CMDS = (
    "acquire", "release", "heartbeat",
    "acquire_preempt", "ptt_start", "ptt_end", "flush",
)


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
EVENT_HW_FAULT = "HW_FAULT"
#: Transport-health transitions. These ARE state changes (unlike an amplitude
#: sample), so they ride the GUARANTEED lane and belong in EVENT_KINDS: the
#: cockpit must never miss the edge that tells it the meter went unreliable.
EVENT_TELEMETRY_DEGRADED = "SYS_TELEMETRY_DEGRADED"
EVENT_TELEMETRY_RECOVERED = "SYS_TELEMETRY_RECOVERED"

#: Boot-progress edges. The socket binds before the mic is acquired, so a
#: connected cockpit would otherwise sit on a live-but-silent bridge with no
#: way to tell "still warming" from "armed and quiet". Emitted from the
#: EXISTING bind location — the socket does not move.
EVENT_SYSTEM_WARMING = "SYSTEM_WARMING"
EVENT_SYSTEM_READY = "SYSTEM_READY"

EVENT_KINDS = (
    EVENT_VAD_ACTIVE, EVENT_VAD_INACTIVE, EVENT_TTS_GENERATING,
    EVENT_AUDIO_PLAYING, EVENT_AUDIO_IDLE, EVENT_HW_FAULT,
    EVENT_TELEMETRY_DEGRADED, EVENT_TELEMETRY_RECOVERED,
    EVENT_SYSTEM_WARMING, EVENT_SYSTEM_READY,
)

#: Telemetry frame type — an amplitude SAMPLE, not a state transition.
#: Deliberately NOT in EVENT_KINDS: that tuple is the closed vocabulary the
#: state machine keys off, and every one of those events must be delivered
#: (a dropped VAD_ACTIVE corrupts state). RMS frames are lossy by design, so
#: they ride a separate type and a separate, droppable broadcast path.
MSG_RMS_LEVEL = "rms_level"

#: Write-buffer watermark. Above this many bytes queued on a client's
#: transport, telemetry frames are DROPPED rather than queued. asyncio's
#: ``StreamWriter.write`` never blocks — it buffers without bound — so at
#: 20 FPS a lagging client would grow the daemon's memory indefinitely.
#: The valve trades stale amplitude (worthless) for bounded memory.
def rms_watermark_multiple() -> float:
    """How many socket-buffers' worth of queued bytes is "behind".

    A ratio, not a byte count: the absolute threshold must scale with whatever
    the OS actually gave this socket. 2.0 tolerates a little normal in-flight
    queueing while still shedding long before memory growth matters."""
    try:
        return max(0.25, float(os.environ.get("JARVIS_AUDIO_IPC_RMS_WATERMARK_X", "2.0")))
    except (TypeError, ValueError):
        return 2.0


def socket_send_buffer_bytes(writer: Any) -> Optional[int]:
    """The OS's ACTUAL send-buffer size for this socket (SO_SNDBUF), or None.

    This is the root-cause answer to "how much queueing is too much": ask the
    kernel rather than guess. AF_UNIX buffers vary by platform and by sysctl
    (8KB on this host, 64KB+ elsewhere), so any constant is wrong somewhere."""
    try:
        import socket as _socket
        sock = writer.get_extra_info("socket")
        if sock is None:
            return None
        val = int(sock.getsockopt(_socket.SOL_SOCKET, _socket.SO_SNDBUF))
        return val if val > 0 else None
    except Exception:  # noqa: BLE001
        return None


def rms_drop_watermark_bytes(writer: Any = None) -> int:
    """Per-socket drop threshold, derived from SO_SNDBUF.

    Falls back to a conservative floor ONLY when introspection fails (a
    non-socket transport, or a platform that refuses the getsockopt) — never as
    the primary policy."""
    try:
        floor = max(1024, int(os.environ.get("JARVIS_AUDIO_IPC_RMS_WATERMARK_FLOOR", "8192")))
    except (TypeError, ValueError):
        floor = 8192
    if writer is not None:
        sndbuf = socket_send_buffer_bytes(writer)
        if sndbuf:
            return max(floor, int(sndbuf * rms_watermark_multiple()))
    return floor


def _qos_hysteresis() -> int:
    """Consecutive frames required to flip transport health either way."""
    try:
        return max(1, int(os.environ.get("JARVIS_AUDIO_IPC_QOS_HYSTERESIS", "5")))
    except (TypeError, ValueError):
        return 5


def rms_publish_enabled() -> bool:
    return os.environ.get(
        "JARVIS_AUDIO_IPC_RMS_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


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
        on_flush: Optional[Callable[[], Any]] = None,
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
        self._on_flush = on_flush
        self._lease_holder: Optional[asyncio.StreamWriter] = None
        self._lease_deadline: float = 0.0
        self._lease_watchdog: Optional[asyncio.Task] = None
        self._lease_is_ptt = False
        self.lease_stats: Dict[str, int] = {
            "acquires": 0, "denials": 0, "releases": 0,
            "expiries": 0, "drop_releases": 0, "heartbeats": 0,
            "preempts": 0, "ptt_sessions": 0, "flushes": 0,
            "hw_faults": 0,
        }
        # Telemetry valve accounting — `dropped` climbing means a client is
        # lagging and frames are being shed, which is the design working.
        self.rms_stats: Dict[str, int] = {
            "sent": 0, "dropped": 0, "errors": 0,
            "degraded_edges": 0, "recovered_edges": 0,
        }
        self._qos_degraded = False
        self._qos_shed_run = 0
        self._qos_ok_run = 0

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
        elif kind == EVENT_HW_FAULT:
            # Device vanished — every presentation boolean is a lie now.
            self._state["vad_active"] = False
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

    def publish_rms(self, level: float, plane: str = "user") -> None:
        """Publish one amplitude sample. LOSSY BY CONTRACT — see _broadcast_lossy.

        Called from the audio consumer side at ~20 FPS. Never raises, never
        blocks, and never queues behind a lagging client."""
        if not rms_publish_enabled():
            return
        try:
            lvl = min(1.0, max(0.0, float(level)))
        except (TypeError, ValueError):
            return
        self._broadcast_lossy({
            "type": MSG_RMS_LEVEL,
            "level": round(lvl, 4),
            "plane": str(plane or "user"),
            "ts": time.time(),
        })

    def _note_telemetry_health(self, *, shed: bool) -> None:
        """EDGE-triggered transport-health signalling.

        Silent shedding blinds the cockpit: a still waveform looks identical to
        a silent room. So the client is told — but on the EDGE only.

        Per-frame signalling would be self-defeating: at 20 FPS a congested
        client would generate 20 guaranteed-lane events per second, i.e. more
        traffic than the telemetry being shed, on the lane that must never be
        dropped. Hysteresis (N consecutive sheds to degrade, N consecutive
        sends to recover) also stops a client hovering at the watermark from
        flapping the indicator. NEVER raises."""
        try:
            if shed:
                self._qos_shed_run += 1
                self._qos_ok_run = 0
                if not self._qos_degraded and self._qos_shed_run >= _qos_hysteresis():
                    self._qos_degraded = True
                    self.rms_stats["degraded_edges"] += 1
                    self.publish_event(EVENT_TELEMETRY_DEGRADED)
            else:
                self._qos_ok_run += 1
                self._qos_shed_run = 0
                if self._qos_degraded and self._qos_ok_run >= _qos_hysteresis():
                    self._qos_degraded = False
                    self.rms_stats["recovered_edges"] += 1
                    self.publish_event(EVENT_TELEMETRY_RECOVERED)
        except Exception:  # noqa: BLE001
            pass

    @property
    def telemetry_degraded(self) -> bool:
        return self._qos_degraded

    def _broadcast_lossy(self, msg: Dict[str, Any]) -> None:
        """Broadcast that DROPS rather than queues when a client is behind.

        The difference from :meth:`_broadcast` is deliberate and load-bearing.
        `_broadcast` must deliver: its messages are state transitions and
        transcript chunks, where a drop corrupts the client's model. This path
        carries amplitude samples, where the newest value is the only useful
        one and a stale frame is worse than no frame.

        Backpressure is read from the transport's own write-buffer size, so the
        valve reacts to the ACTUAL socket condition rather than guessing from
        elapsed time. NEVER raises."""
        if not self._clients:
            return
        try:
            data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        except Exception:  # noqa: BLE001
            return
        for w in list(self._clients):
            watermark = rms_drop_watermark_bytes(w)
            try:
                if w.is_closing():
                    self._drop_client(w)
                    continue
                # THE VALVE: consult the real queue depth before writing.
                try:
                    transport = w.transport
                    queued = transport.get_write_buffer_size() if transport else 0
                except Exception:  # noqa: BLE001
                    queued = 0
                if queued >= watermark:
                    self.rms_stats["dropped"] += 1
                    self._note_telemetry_health(shed=True)
                    continue          # drop THIS frame; the client keeps its socket
                w.write(data)
                self.rms_stats["sent"] += 1
                self._note_telemetry_health(shed=False)
            except Exception:  # noqa: BLE001
                # A telemetry write failure must not tear down a client that is
                # otherwise healthy for state events; only a closing socket does.
                self.rms_stats["errors"] += 1

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
        self._lease_is_ptt = False
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
        if cmd == "flush":
            # TTS interruption / ducking — holder-only (a bystander
            # terminal must not silence Karen for the operator who
            # holds the floor).
            if writer is self._lease_holder:
                self.lease_stats["flushes"] += 1
                await self._invoke_flush()
                self.publish_event(EVENT_AUDIO_IDLE)
            return
        if cmd == "ptt_end":
            # Ephemeral hold ends → FULL release (disarm). A non-PTT
            # lease ignores ptt_end (defensive: mismatched brackets
            # never disarm a standing lease).
            if writer is self._lease_holder and self._lease_is_ptt:
                self.lease_stats["releases"] += 1
                await self._release_lease(reason="ptt_end")
                self._reply(writer, {
                    "type": "lease", "granted": False, "reason": "released",
                })
            return
        # acquire / acquire_preempt / ptt_start
        preempting = cmd in ("acquire_preempt", "ptt_start")
        incumbent = self._lease_holder
        if incumbent is not None and incumbent is not writer:
            if not preempting:
                self.lease_stats["denials"] += 1
                self._reply(writer, {
                    "type": "lease", "granted": False, "reason": "held",
                })
                return
            # Graceful revocation over the return channel: the
            # incumbent's TUI morphs to "held by another terminal".
            # The hardware stays ARMED through the transfer — one
            # continuous stream, no disarm/re-arm glitch.
            self.lease_stats["preempts"] += 1
            self._reply(incumbent, {
                "type": "lease", "granted": False, "reason": "preempted",
            })
        first_arm = incumbent is None
        self._lease_holder = writer
        self._lease_deadline = time.monotonic() + ttl
        self._lease_is_ptt = cmd == "ptt_start"
        if self._lease_is_ptt:
            self.lease_stats["ptt_sessions"] += 1
        self.lease_stats["acquires"] += 1
        if first_arm:
            await self._invoke_lease_change(True)
            self._start_lease_watchdog()
        self._reply(writer, {
            "type": "lease", "granted": True, "ttl_s": ttl,
            "ptt": self._lease_is_ptt,
        })

    async def _invoke_flush(self) -> None:
        """Fused invoke of the supervisor's outbound-audio halt seam.
        NEVER raises."""
        cb = self._on_flush
        if cb is None:
            return
        try:
            result = cb()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=lease_ttl_s())
        except asyncio.TimeoutError:
            logger.warning("[AudioIPC] flush callback timed out")
        except Exception:  # noqa: BLE001
            logger.debug("[AudioIPC] flush callback degraded", exc_info=True)

    def publish_hardware_fault(self, detail: str = "") -> None:
        """Hardware Topology Survival: the active audio device vanished
        mid-lease (Bluetooth drop, CoreAudio stream death). Fail-safe
        sequence — revoke the holder over the return channel (reason
        ``hw_fault``), disarm via the lease seam (the bootstrap's
        closure tears down the dead stream), and broadcast the fault
        event so EVERY subscriber renders the truth. Thread-safe
        (callable from the arbiter's fault reporter on any task);
        NEVER raises — the supervisor loop must survive any device
        topology change."""
        try:
            self.lease_stats["hw_faults"] += 1
            holder = self._lease_holder
            if holder is not None:
                self._reply(holder, {
                    "type": "lease", "granted": False, "reason": "hw_fault",
                    "detail": str(detail or "")[:200],
                })
                self._schedule_lease_release(reason="hardware_fault")
            self.publish_event(EVENT_HW_FAULT)
        except Exception:  # noqa: BLE001
            logger.debug("[AudioIPC] hw-fault publish degraded", exc_info=True)

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
    "EVENT_HW_FAULT",
    "EVENT_KINDS",
    "EVENT_TTS_GENERATING",
    "EVENT_VAD_ACTIVE",
    "EVENT_VAD_INACTIVE",
    "LEASE_CMDS",
    "audio_ipc_enabled",
    "lease_ttl_s",
    "socket_path",
]
