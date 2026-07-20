"""``ov system`` — the System Observability Panel (Phase 12, Slice G).

A deterministic control-plane cockpit for the fully-headless daemon. It is a
PURELY PASSIVE listener (mandate 1 — no ``ps``/``top``/``lsof`` polling): it
attaches to the established Cockpit Attach UDS (``.jarvis/cockpit_attach.sock``,
Phase 0) and renders the live ``TrinityEventBus`` telemetry — DAG hydration
state, DoubleWord failover health, and the Ouroboros Actor loop.

Architecture (mandate 2):
  * **Async Rich TUI** — a ``rich.Live`` surface refreshed off an ``asyncio``
    frame queue; the render never blocks the socket read and vice-versa.
  * **Graceful Socket Detachment** — the ``TelemetryConnectionManager`` owns a
    read loop that treats EOF / ``ConnectionResetError`` / ``IncompleteReadError``
    / any OS fault as an ordinary detach: it NEVER lets the exception tear down
    the TUI. It flips to ``OFFLINE`` (the "DAEMON OFFLINE — ATTEMPTING RECONNECT"
    overlay) and queues an EXPONENTIAL-BACKOFF reconnect task. When the daemon
    returns (``launchd`` restart, manual reboot) it re-attaches automatically.

DRY (mandate 3): reuses the Phase-0 ``cockpit_attach`` socket path + the exact
newline-delimited JSON frame protocol (``hydration`` / ``line`` / ``telemetry``
/ ``audio_state`` / ``thermal``). It invents NO secondary telemetry protocol —
it consumes the frames the organism already broadcasts.

Every public entry point NEVER raises out to the terminal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.OVSystemPanel")

# Reader/writer pair a connector yields (mirrors asyncio.open_unix_connection).
Connection = Tuple[asyncio.StreamReader, asyncio.StreamWriter]
Connector = Callable[[], Awaitable[Connection]]


# ---------------------------------------------------------------------------
# config (env-driven — no hardcoding)
# ---------------------------------------------------------------------------

def _socket_path() -> Path:
    """The Cockpit Attach UDS — reuse the Phase-0 resolver (DRY). Falls back to
    the same default if the battle_test module can't be imported."""
    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import attach_socket_path
        return attach_socket_path()
    except Exception:  # noqa: BLE001
        return Path(os.environ.get(
            "JARVIS_ATTACH_IPC_SOCKET", ".jarvis/cockpit_attach.sock"))


def _base_backoff_s() -> float:
    try:
        return max(0.05, float(os.environ.get("JARVIS_OV_PANEL_BACKOFF_S", "0.5")))
    except (TypeError, ValueError):
        return 0.5


def _cap_backoff_s() -> float:
    try:
        return max(1.0, float(os.environ.get("JARVIS_OV_PANEL_BACKOFF_CAP_S", "30.0")))
    except (TypeError, ValueError):
        return 30.0


def _connect_timeout_s() -> float:
    try:
        return max(0.05, float(os.environ.get("JARVIS_OV_PANEL_CONNECT_TIMEOUT_S", "1.0")))
    except (TypeError, ValueError):
        return 1.0


# ---------------------------------------------------------------------------
# connection state
# ---------------------------------------------------------------------------

class ConnState(str, Enum):
    CONNECTING = "connecting"
    ATTACHED = "attached"
    OFFLINE = "offline"          # detached — the reconnect overlay is showing
    RECONNECTING = "reconnecting"  # backing off before the next attempt
    STOPPED = "stopped"


#: Faults that mean "the peer went away" — an ORDINARY detach, never a crash.
_DETACH_FAULTS: Tuple[type, ...] = (
    EOFError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
    asyncio.IncompleteReadError, OSError,
)


class TelemetryConnectionManager:
    """Owns the resilient UDS attachment. It reconnects forever with
    exponential backoff and NEVER propagates a socket fault to the TUI.

    Fully injectable for tests: ``connector`` yields the (reader, writer)
    pair (default: a real ``open_unix_connection`` to the cockpit socket) and
    ``sleeper`` drives the backoff (default ``asyncio.sleep``)."""

    def __init__(
        self,
        *,
        connector: Optional[Connector] = None,
        on_frame: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_state: Optional[Callable[["ConnState"], None]] = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        base_backoff_s: Optional[float] = None,
        cap_backoff_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connector = connector or self._default_connector
        self._on_frame = on_frame or (lambda _f: None)
        self._on_state = on_state or (lambda _s: None)
        self._sleeper = sleeper
        self._base = base_backoff_s if base_backoff_s is not None else _base_backoff_s()
        self._cap = cap_backoff_s if cap_backoff_s is not None else _cap_backoff_s()
        self._clock = clock

        self.state: ConnState = ConnState.CONNECTING
        self.attempt: int = 0                       # consecutive failed attempts
        self.reconnects: int = 0                    # successful (re)attaches
        self.last_error: str = ""
        self._running: bool = False
        self._writer: Optional[asyncio.StreamWriter] = None
        #: The pending backoff task — the mandate-4 assertion surface.
        self.reconnect_task: Optional[asyncio.Task] = None

    # -- default (live) connector -------------------------------------------

    async def _default_connector(self) -> Connection:
        path = _socket_path()
        return await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(path)),
            timeout=_connect_timeout_s())

    # -- state ---------------------------------------------------------------

    def _set_state(self, state: ConnState) -> None:
        if state != self.state:
            self.state = state
            try:
                self._on_state(state)
            except Exception:  # noqa: BLE001
                pass

    @property
    def is_online(self) -> bool:
        return self.state is ConnState.ATTACHED

    # -- the resilient loop --------------------------------------------------

    async def run(self) -> None:
        """Attach → read → (on any detach) back off → reattach, forever. NEVER
        raises. Cancel the running task (or call :meth:`stop`) to end it."""
        self._running = True
        try:
            while self._running:
                attached = await self._attach_and_read()
                if not self._running:
                    break
                # Detached (or connect failed) → queue an exponential backoff
                # reconnect task and await it (mandate 2 + 4).
                await self._backoff()
        except asyncio.CancelledError:
            raise
        finally:
            self._set_state(ConnState.STOPPED)
            await self._close_writer()

    async def _attach_and_read(self) -> bool:
        """One attach + read-until-detach cycle. Returns False when detached.
        Catches EVERY socket fault — the TUI is never torn down."""
        self._set_state(ConnState.CONNECTING)
        try:
            reader, writer = await self._connector()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — connect refused / no socket
            self.last_error = f"connect: {type(exc).__name__}: {exc}"
            self.attempt += 1
            self._set_state(ConnState.OFFLINE)
            return False

        self._writer = writer
        self._set_state(ConnState.ATTACHED)
        self.attempt = 0
        self.reconnects += 1
        try:
            while self._running:
                line = await reader.readline()
                if not line:                     # clean EOF — peer closed
                    raise EOFError("stream closed by daemon")
                self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except _DETACH_FAULTS as exc:            # EOFError / reset / OS fault
            self.last_error = f"detach: {type(exc).__name__}: {exc}"
            self.attempt += 1
            self._set_state(ConnState.OFFLINE)
            return False
        except BaseException as exc:  # noqa: BLE001 — belt + braces, never crash
            self.last_error = f"unexpected: {type(exc).__name__}: {exc}"
            self.attempt += 1
            self._set_state(ConnState.OFFLINE)
            return False
        finally:
            await self._close_writer()
        return False

    def _dispatch(self, line: bytes) -> None:
        """Parse one newline-JSON frame → the consumer. A malformed frame is
        skipped, never fatal."""
        try:
            frame = json.loads(line)
        except (ValueError, TypeError):
            return
        if isinstance(frame, dict):
            try:
                self._on_frame(frame)
            except Exception:  # noqa: BLE001
                pass

    async def _backoff(self) -> None:
        """Schedule the reconnect as a QUEUED asyncio task, then await it —
        exponential with a cap (mandate 2). The task handle is exposed for the
        bulletproof assertion (mandate 4)."""
        delay = min(self._cap, self._base * (2 ** max(0, self.attempt - 1)))
        self._set_state(ConnState.RECONNECTING)
        self.reconnect_task = asyncio.ensure_future(self._sleeper(delay))
        try:
            await self.reconnect_task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def _close_writer(self) -> None:
        w, self._writer = self._writer, None
        if w is not None:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._running = False
        t = self.reconnect_task
        if t is not None and not t.done():
            t.cancel()


# ---------------------------------------------------------------------------
# the folded cockpit state
# ---------------------------------------------------------------------------

@dataclass
class SystemPanelModel:
    """The live control-plane state, folded from telemetry frames. Authority-
    free — a pure projection of what the daemon broadcasts."""
    system_state: str = "unknown"
    hydration_state: str = "unknown"
    subsystems: Dict[str, str] = field(default_factory=dict)
    selftest: str = "pending"
    failover_provider: str = "—"
    actor_state: str = "unknown"
    actor_restarts: int = 0
    phase: str = "IDLE"
    events: List[str] = field(default_factory=list)
    last_frame_monotonic: float = 0.0

    #: Bounded event feed (newest last).
    _MAX_EVENTS = 200

    def push_event(self, text: str) -> None:
        if not text:
            return
        self.events.append(text)
        if len(self.events) > self._MAX_EVENTS:
            del self.events[: len(self.events) - self._MAX_EVENTS]

    def ingest(self, frame: Dict[str, Any], *, clock: Callable[[], float] = time.monotonic) -> None:
        """Fold one cockpit frame into the model. Tolerates partial frames;
        NEVER raises."""
        try:
            self.last_frame_monotonic = clock()
            ftype = str(frame.get("type", "") or "")
            if ftype == "hydration":
                status = frame.get("status") or {}
                if isinstance(status, dict):
                    self.phase = str(status.get("phase", self.phase) or self.phase)
                # The Phase-0 handshake nests the state snapshot under ``status``.
                self._apply_system(status or frame.get("system") or frame)
            elif ftype == "telemetry":
                self._apply_system(frame)
                narr = str(frame.get("narration_text", "") or "")
                life = str(frame.get("lifecycle", "") or frame.get("kind", "") or "")
                if narr:
                    self.push_event(f"{life + ' — ' if life else ''}{narr}")
            elif ftype == "line":
                self.push_event(str(frame.get("text", "") or ""))
        except Exception:  # noqa: BLE001
            pass

    def _apply_system(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if "system_state" in data:
            self.system_state = str(data.get("system_state") or self.system_state)
        hyd = data.get("hydration")
        if isinstance(hyd, dict):
            self.hydration_state = str(hyd.get("state") or self.hydration_state)
            subs = hyd.get("subsystems")
            if isinstance(subs, dict):
                self.subsystems = {str(k): str(v) for k, v in subs.items()}
        if "selftest" in data:
            self.selftest = str(data.get("selftest") or self.selftest)
        if data.get("provider"):
            self.failover_provider = str(data.get("provider"))
        actor = data.get("actor")
        if isinstance(actor, dict):
            self.actor_state = str(actor.get("state") or self.actor_state)
            try:
                self.actor_restarts = int(actor.get("restarts", self.actor_restarts))
            except (TypeError, ValueError):
                pass


__all__ = [
    "ConnState", "TelemetryConnectionManager", "SystemPanelModel",
    "run_system_panel", "render_panel",
]


# ---------------------------------------------------------------------------
# Rich rendering (import rich lazily — the manager + model stay import-light)
# ---------------------------------------------------------------------------

def render_panel(model: SystemPanelModel, state: ConnState) -> Any:
    """Build the Rich renderable for the current model + connection state.
    When OFFLINE/RECONNECTING it overlays the reconnect banner. NEVER raises."""
    from rich.panel import Panel
    from rich.table import Table
    from rich.console import Group
    from rich.text import Text

    online = state is ConnState.ATTACHED

    header = Table.grid(expand=True)
    header.add_column(justify="left")
    header.add_column(justify="right")
    dot = "🟢" if online else ("🟠" if state is ConnState.RECONNECTING else "🔴")
    header.add_row(
        Text(f"{dot} ov · system observability", style="bold"),
        Text(state.value.upper(), style="green" if online else "yellow"))

    body = Table.grid(expand=True, padding=(0, 2))
    body.add_column(style="dim", justify="right")
    body.add_column()
    body.add_row("system", _pill(model.system_state))
    subs = "  ".join(f"{k}={v}" for k, v in model.subsystems.items()) or "—"
    body.add_row("DAG hydration", f"{model.hydration_state}  [{subs}]")
    body.add_row("DoubleWord failover", f"{model.selftest}  (provider {model.failover_provider})")
    body.add_row("Ouroboros Actor", f"{model.actor_state}  (restarts {model.actor_restarts})")

    feed = Table.grid(expand=True)
    feed.add_column()
    for line in model.events[-12:]:
        feed.add_row(Text("⎿ " + line, style="dim"))
    if not model.events:
        feed.add_row(Text("⎿ (awaiting telemetry…)", style="dim italic"))

    parts: List[Any] = [header, Text(""), body, Text(""), feed]

    if not online:
        banner = Text(
            "  DAEMON OFFLINE — ATTEMPTING RECONNECT  ",
            style="bold white on red" if state is ConnState.OFFLINE
            else "bold black on yellow")
        parts = [banner, Text("")] + parts

    return Panel(Group(*parts), title="JARVIS · Control Plane", border_style=(
        "green" if online else "red"))


def _pill(value: str) -> Any:
    from rich.text import Text
    style = {"ready": "bold green", "degraded": "bold yellow",
             "booting": "cyan", "unknown": "dim"}.get(value, "white")
    return Text(value, style=style)


async def run_system_panel(
    *,
    manager: Optional[TelemetryConnectionManager] = None,
    refresh_hz: float = 8.0,
    console: Any = None,
    stop_after_s: Optional[float] = None,
) -> int:
    """Run the async cockpit: the connection manager streams frames into the
    model while a ``rich.Live`` surface re-renders. NEVER raises; returns an
    exit code."""
    from rich.console import Console
    from rich.live import Live

    model = SystemPanelModel()
    mgr = manager or TelemetryConnectionManager(on_frame=model.ingest)
    if manager is not None:
        # Caller-supplied manager: still route frames into our model.
        prev = mgr._on_frame
        mgr._on_frame = lambda f: (model.ingest(f), prev(f))  # type: ignore
    con = console or Console()

    run_task = asyncio.get_event_loop().create_task(mgr.run(), name="ov-sys-conn")
    started = time.monotonic()
    try:
        with Live(render_panel(model, mgr.state), console=con,
                  refresh_per_second=max(1.0, refresh_hz), screen=False,
                  transient=False) as live:
            while True:
                await asyncio.sleep(1.0 / max(1.0, refresh_hz))
                live.update(render_panel(model, mgr.state))
                if stop_after_s is not None and (time.monotonic() - started) >= stop_after_s:
                    break
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    except Exception:  # noqa: BLE001
        logger.debug("[OVSystemPanel] render degraded", exc_info=True)
    finally:
        mgr.stop()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    return 0
