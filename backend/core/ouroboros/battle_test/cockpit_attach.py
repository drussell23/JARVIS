"""Cockpit Attach Bridge — `ov attach` over a state-hydrated UDS.

CLI wiring item #6 (operator-authorized 2026-07-18). The organism (the
battle-test harness process) owns a Unix-domain pub/sub socket; an
``ov attach`` terminal is a DUMB RENDERER of the serialized frames the
daemon emits — zero business logic client-side.

Protocol (newline-delimited JSON, schema ``cockpit_attach.v1``):

  * **Hydration payload** — the FIRST frame on accept. Contains the
    live FSM/status snapshot (phase, cost, idle, active op), the active
    LiveWork/op digest, and real-time provider liquidity — the operator
    NEVER stares at a blank screen waiting for the next FSM tick.
    Providers are injected callables (StatusLineBuilder snapshot, the
    liquidity ledger, GLS active-ops) so this module holds no authority
    and no direct state references.
  * **Downstream frames** — ``{"type":"line","text":...}``: every line
    the harness's ``_repl_print`` chokepoint emits (ALREADY conformed by
    the PresentationRouter — the attach surface inherits the design
    language for free).
  * **Upstream frames** — ``{"type":"input","text":...}``: operator
    stdin from the attached terminal, routed into the harness's
    ``_handle_repl_command`` — the FULL verb set plus the bare-text
    chat bridge, not a chat-only side channel.
  * **Audio orchestration (v2, the Audio-Visual Synapse)** — upstream
    ``{"type":"audio","cmd":"wake"|"sleep"|"barge"}`` arms / disarms /
    interrupts the daemon's karen_duplex voice plane; downstream
    ``{"type":"audio_state","state":...}`` streams the audio FSM
    (``OFFLINE`` / ``UNAVAILABLE`` / ``LISTENING`` / ``HEARING`` /
    ``THINKING`` / ``SPEAKING``) so the attached TUI can morph its
    prompt in real time. The hydration payload carries the CURRENT
    audio state (late joiners never render a stale footer).

Bulletproof contract (mandate 4): the daemon-side publisher is strictly
non-blocking and per-client fail-drop — ``BrokenPipeError`` /
``ConnectionResetError`` / any write fault on a subscriber DROPS that
subscriber and the FSM loop never notices. A SIGKILL'd ``ov attach`` is
a dropped connection, nothing more.

Master ``JARVIS_ATTACH_IPC_ENABLED`` (default on — read/render plus the
same operator input surface the local REPL already exposes; socket is
0600 same-user). NEVER raises anywhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger("Ouroboros.CockpitAttach")

COCKPIT_ATTACH_SCHEMA_VERSION = "cockpit_attach.v2"

#: Closed taxonomy of daemon→TUI audio FSM states (the morphing
#: vocabulary). OFFLINE = voice plane not armed; UNAVAILABLE = wake
#: requested but no duplex handle mounted in this process.
AUDIO_STATES = (
    "OFFLINE", "UNAVAILABLE", "LISTENING", "HEARING", "THINKING",
    "SPEAKING", "HELD",
)

#: Closed taxonomy of TUI→daemon audio commands. v2.1: force_wake
#: preempts an incumbent terminal; ptt/ptt_stop bracket an ephemeral
#: push-to-talk hold; flush halts outbound audio (ducking).
AUDIO_CMDS = (
    "wake", "sleep", "barge", "force_wake", "ptt", "ptt_stop", "flush",
)

_TRUTHY = ("1", "true", "yes", "on")


def attach_enabled() -> bool:
    """Master gate — default ON. NEVER raises."""
    return os.environ.get(
        "JARVIS_ATTACH_IPC_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def attach_socket_path() -> Path:
    return Path(os.environ.get(
        "JARVIS_ATTACH_IPC_SOCKET", ".jarvis/cockpit_attach.sock",
    ))


def _connect_timeout_s() -> float:
    try:
        return max(0.05, float(os.environ.get(
            "JARVIS_ATTACH_CONNECT_TIMEOUT_S", "0.5",
        )))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# Daemon side — lives in the harness process
# ---------------------------------------------------------------------------


class CockpitAttachBridge:
    """The organism's attach server.

    ``status_provider`` / ``ops_provider`` / ``liquidity_provider`` are
    injected zero-authority callables returning JSON-serializable dicts;
    each is consulted fresh at every handshake (pull model — hydration
    is always current, never cached). ``on_input`` receives operator
    text from attached terminals.
    """

    def __init__(
        self,
        *,
        status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        ops_provider: Optional[Callable[[], Any]] = None,
        liquidity_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        on_input: Optional[Callable[[str], None]] = None,
        on_audio: Optional[Callable[[str], None]] = None,
        path: Optional[Path] = None,
    ) -> None:
        self._status = status_provider or (lambda: {})
        self._ops = ops_provider or (lambda: [])
        self._liquidity = liquidity_provider or (lambda: {})
        self._on_input = on_input or (lambda _t: None)
        self._on_audio = on_audio or (lambda _c: None)
        self._path = Path(path) if path is not None else attach_socket_path()
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._audio_state: str = "OFFLINE"
        self.stats: Dict[str, int] = {
            "connects": 0, "dropped": 0, "lines_published": 0,
            "inputs_received": 0, "audio_cmds": 0,
            "audio_states_published": 0,
        }

    # ---- lifecycle ----

    async def start(self) -> bool:
        """Bind (removing any stale socket). False on failure — the
        organism keeps running unattachable. NEVER raises."""
        try:
            if not attach_enabled():
                return False
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
            logger.info("[CockpitAttach] bridge bound at %s", self._path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CockpitAttach] bind failed: %s", exc)
            self._server = None
            return False

    async def stop(self) -> None:
        """Close server + subscribers, unlink socket. NEVER raises."""
        try:
            if self._server is not None:
                self._server.close()
                try:
                    await self._server.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                self._server = None
            for w in list(self._clients):
                self._drop(w)
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                pass
        except Exception:  # noqa: BLE001
            logger.debug("[CockpitAttach] stop degraded", exc_info=True)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---- hydration ----

    def _hydration_payload(self) -> Dict[str, Any]:
        def _safe(fn: Callable[[], Any], fallback: Any) -> Any:
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return fallback

        return {
            "type": "hydration",
            "schema_version": COCKPIT_ATTACH_SCHEMA_VERSION,
            "ts": time.time(),
            "status": _safe(self._status, {}),
            "ops": _safe(self._ops, []),
            "liquidity": _safe(self._liquidity, {}),
            "audio": {"state": self._audio_state},
        }

    # ---- publish (the harness _repl_print mirror) ----

    def publish_line(self, text: str) -> None:
        """Broadcast one (already router-conformed) line to every
        attached terminal. Strictly non-blocking; per-client fail-drop
        (BrokenPipe/ConnectionReset = subscriber gone, organism
        unbothered). Thread-safe. NEVER raises."""
        try:
            if not self._clients:
                return
            msg = {"type": "line", "text": str(text), "ts": time.time()}
            self.stats["lines_published"] += 1
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
        except Exception:  # noqa: BLE001
            logger.debug("[CockpitAttach] publish degraded", exc_info=True)

    def publish_audio_state(self, state: str) -> None:
        """Stream one audio-FSM state to every attached terminal
        (edge-coalesced: republishing the current state is a no-op).
        The state is also retained for hydration so a late joiner's
        footer is never stale. Thread-safe; NEVER raises."""
        try:
            state = str(state or "").strip().upper()
            if state not in AUDIO_STATES or state == self._audio_state:
                return
            self._audio_state = state
            self.stats["audio_states_published"] += 1
            msg = {"type": "audio_state", "state": state, "ts": time.time()}
            loop = self._loop
            if loop is None or loop.is_closed() or not self._clients:
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._broadcast(msg)
            else:
                loop.call_soon_threadsafe(self._broadcast, msg)
        except Exception:  # noqa: BLE001
            logger.debug("[CockpitAttach] audio publish degraded", exc_info=True)

    def _broadcast(self, msg: Dict[str, Any]) -> None:
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        for w in list(self._clients):
            try:
                if w.is_closing():
                    self._drop(w)
                    continue
                w.write(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self._drop(w)
            except Exception:  # noqa: BLE001 — ANY writer fault = drop
                self._drop(w)

    def _drop(self, w: asyncio.StreamWriter) -> None:
        if w in self._clients:
            self.stats["dropped"] += 1
        self._clients.discard(w)
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- per-client session ----

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            hydration = (
                json.dumps(self._hydration_payload(), separators=(",", ":"))
                + "\n"
            ).encode()
            writer.write(hydration)
            await writer.drain()
            self._clients.add(writer)
            self.stats["connects"] += 1
            logger.info(
                "[CockpitAttach] terminal attached (subscribers=%d)",
                len(self._clients),
            )
            # Upstream read-loop: operator input frames. EOF/reset =
            # detach; a malformed frame never kills the session.
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except (ValueError, TypeError):
                    continue
                ftype = frame.get("type")
                if ftype == "input":
                    text = str(frame.get("text", "")).strip()
                    if text:
                        self.stats["inputs_received"] += 1
                        try:
                            self._on_input(text)
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "[CockpitAttach] input sink degraded",
                                exc_info=True,
                            )
                elif ftype == "audio":
                    cmd = str(frame.get("cmd", "")).strip().lower()
                    if cmd in AUDIO_CMDS:
                        self.stats["audio_cmds"] += 1
                        try:
                            self._on_audio(cmd)
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "[CockpitAttach] audio sink degraded",
                                exc_info=True,
                            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                     # SIGKILL'd terminal — just a detach
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._drop(writer)
            logger.info(
                "[CockpitAttach] terminal detached (subscribers=%d)",
                len(self._clients),
            )


# ---------------------------------------------------------------------------
# Client side — the dumb terminal (ov attach)
# ---------------------------------------------------------------------------


class CockpitAttachClient:
    """Render-only subscriber + input pipe for ``ov attach``.

    ``connect()`` shares one bounded deadline across connect + hydration
    read; a missing/refused/dead socket returns False (the CLI renders
    "no organism awake"). NEVER raises."""

    def __init__(
        self,
        *,
        on_hydration: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        on_audio_state: Optional[Callable[[str], None]] = None,
        path: Optional[Path] = None,
    ) -> None:
        self._path = Path(path) if path is not None else attach_socket_path()
        self._on_hydration = on_hydration or (lambda _m: None)
        self._on_line = on_line or (lambda _t: None)
        self._on_audio_state = on_audio_state or (lambda _s: None)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self.connected: bool = False

    async def connect(self) -> bool:
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
            frame = json.loads(line)
            if frame.get("type") == "hydration":
                self._safe_cb(self._on_hydration, frame)
            self._read_task = asyncio.get_running_loop().create_task(
                self._read_loop(),
            )
            self.connected = True
            return True
        except Exception:  # noqa: BLE001
            await self.close()
            return False

    def send_audio(self, cmd: str) -> bool:
        """Pipe one audio-orchestration command upstream (``wake`` /
        ``sleep`` / ``barge``). Non-blocking; False when detached or
        the command is outside the closed taxonomy. NEVER raises."""
        try:
            cmd = str(cmd or "").strip().lower()
            if cmd not in AUDIO_CMDS:
                return False
            w = self._writer
            if not self.connected or w is None or w.is_closing():
                return False
            frame = {"type": "audio", "cmd": cmd}
            w.write((json.dumps(frame, separators=(",", ":")) + "\n").encode())
            return True
        except Exception:  # noqa: BLE001
            return False

    def send_input(self, text: str) -> bool:
        """Pipe one operator line upstream. Non-blocking; False when
        detached. NEVER raises."""
        try:
            w = self._writer
            # ``connected`` is the session truth (the read-loop flips it
            # on EOF/daemon-exit); the writer's own is_closing() lags a
            # dead peer — a detached pipe must refuse input immediately.
            if not self.connected or w is None or w.is_closing():
                return False
            frame = {"type": "input", "text": str(text)}
            w.write((json.dumps(frame, separators=(",", ":")) + "\n").encode())
            return True
        except Exception:  # noqa: BLE001
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
                    frame = json.loads(line)
                except (ValueError, TypeError):
                    continue
                ftype = frame.get("type")
                if ftype == "line":
                    self._safe_cb_text(self._on_line, str(frame.get("text", "")))
                elif ftype == "audio_state":
                    self._safe_cb_text(
                        self._on_audio_state, str(frame.get("state", "")),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.connected = False

    async def close(self) -> None:
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
    def _safe_cb(cb: Callable[[Dict[str, Any]], None], m: Dict[str, Any]) -> None:
        try:
            cb(m)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _safe_cb_text(cb: Callable[[str], None], t: str) -> None:
        try:
            cb(t)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "AUDIO_CMDS",
    "AUDIO_STATES",
    "COCKPIT_ATTACH_SCHEMA_VERSION",
    "CockpitAttachBridge",
    "CockpitAttachClient",
    "attach_enabled",
    "attach_socket_path",
]
