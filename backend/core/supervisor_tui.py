"""
JARVIS Supervisor TUI — Textual-based terminal UI with 5 tabs.

Consumes the Phase 1 view-model (_build_render_state()) read-only.
Runs in a daemon thread with its own asyncio event loop.
Crashes never affect the supervisor's event loop or startup.

Architecture:
  - stdout is redirected to a capture buffer so supervisor banners/prints
    don't trample the TUI. Textual uses stderr for terminal rendering.
  - Logging handlers are replaced with a buffer handler so log output
    goes to the TUI's Events tab instead of stderr.
  - SnapshotPump is created in attach_to_supervisor() (not start()) to
    avoid the data race where dashboard_getter was None at pump creation.

Env vars:
  JARVIS_TUI_POLL_MS       = int  (default: 500)  — snapshot poll interval
  JARVIS_TUI_EVENT_LIMIT   = int  (default: 500)  — max events in Events tab ring
  JARVIS_TUI_FAULT_LIMIT   = int  (default: 200)  — max faults in Faults tab ring
  JARVIS_TUI_LOG_LIMIT     = int  (default: 3000) — max captured log lines
"""

from __future__ import annotations

import asyncio
import collections
import copy
import io
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

logger = logging.getLogger(__name__)

_POLL_MS = int(os.environ.get("JARVIS_TUI_POLL_MS", "500"))
_EVENT_LIMIT = int(os.environ.get("JARVIS_TUI_EVENT_LIMIT", "500"))
_FAULT_LIMIT = int(os.environ.get("JARVIS_TUI_FAULT_LIMIT", "200"))
_LOG_LIMIT = int(os.environ.get("JARVIS_TUI_LOG_LIMIT", "3000"))

_STATUS_STYLE: Dict[str, str] = {
    "pending": "dim",
    "initializing": "bright_cyan",
    "starting": "bright_cyan",
    "running": "bold bright_blue",
    "healthy": "bold green",
    "ready": "bold bright_green",
    "degraded": "bold yellow",
    "recovering": "bold bright_yellow",
    "error": "bold red",
    "stopping": "dim yellow",
    "stopped": "dim red",
    "skipped": "dim",
    "unavailable": "dim red",
    "warming_up": "bold bright_yellow",
    "recycling": "bold cyan",
}

_SEVERITY_STYLE: Dict[str, str] = {
    "debug": "dim",
    "info": "bright_blue",
    "warning": "bold yellow",
    "error": "bold red",
    "critical": "bold white on red",
    "success": "bold green",
}

_FAULT_SEVERITIES = frozenset({"error", "warning", "critical"})

_PRIME_KEYWORDS = frozenset({
    "prime", "j-prime", "jprime", "jarvis-prime", "jarvis_prime",
    "llm", "model_load", "model loading", "inference", "prewarm",
    "trinity_integrator", "trinity integrator",
})

_REACTOR_KEYWORDS = frozenset({
    "reactor", "reactor-core", "reactor_core", "training",
    "nightshift", "night_shift", "scout", "mlforge",
    "curriculum", "federated", "checkpoint",
})


# ---------------------------------------------------------------------------
# Output capture — redirect stdout + logging to buffer for TUI display
# ---------------------------------------------------------------------------
class _OutputCapture:
    """File-like object that captures writes to a deque.

    Used to redirect sys.stdout so supervisor banners/prints go to
    the TUI's log buffer instead of trampling the Textual terminal.
    """

    def __init__(self, buffer: Deque[str], original: Any = None):
        self._buffer = buffer
        self._original = original
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        if s and s.strip():
            with self._lock:
                for line in s.splitlines():
                    stripped = line.rstrip()
                    if stripped:
                        self._buffer.append(stripped)
        return len(s) if s else 0

    def flush(self) -> None:
        pass

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", "utf-8")

    def fileno(self) -> int:
        if self._original:
            return self._original.fileno()
        raise io.UnsupportedOperation("fileno")

    def isatty(self) -> bool:
        return False


class _TuiLogHandler(logging.Handler):
    """Logging handler that writes formatted records to a deque for TUI display."""

    def __init__(self, buffer: Deque[str]):
        super().__init__()
        self._buffer = buffer
        self.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._buffer.append(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TuiSnapshot — frozen cross-thread data transfer object
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TuiSnapshot:
    """Immutable snapshot posted from SnapshotPump -> Textual app.

    Frozen dataclass prevents attribute reassignment. Dict fields are
    deep-copied at construction time so no mutable state crosses threads.
    """

    groups: tuple = ()
    gcp: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    logs: tuple = ()
    elapsed: float = 0.0
    schema_version: str = "1.0.0"
    events: tuple = ()
    faults: tuple = ()
    ipc_status: dict = field(default_factory=dict)
    captured_logs: tuple = ()


# ---------------------------------------------------------------------------
# SnapshotPump — daemon thread bridging supervisor -> Textual
# ---------------------------------------------------------------------------
class SnapshotPump(threading.Thread):
    """Daemon thread that polls _build_render_state() and posts snapshots.

    - Calls dashboard._build_render_state() every JARVIS_TUI_POLL_MS ms
    - Collects events via SupervisorEventBus subscription
    - Includes captured stdout/logging output
    - Posts TuiSnapshot to Textual app via call_from_thread
    - Coalescing: skips if previous post hasn't been consumed
    """

    def __init__(
        self,
        dashboard_getter: Callable,
        app: "JarvisTuiApp",
        event_bus: Optional[Any] = None,
        supervisor_loop: Optional[asyncio.AbstractEventLoop] = None,
        ipc_getter: Optional[Callable] = None,
        captured_output: Optional[Deque[str]] = None,
    ):
        super().__init__(name="jarvis-tui-pump", daemon=True)
        self._dashboard_getter = dashboard_getter
        self._app = app
        self._event_bus = event_bus
        self._supervisor_loop = supervisor_loop
        self._ipc_getter = ipc_getter
        self._captured_output = captured_output or collections.deque()
        self._stop_event = threading.Event()
        self._buf_lock = threading.Lock()
        self._events: Deque[dict] = collections.deque(maxlen=_EVENT_LIMIT)
        self._faults: Deque[dict] = collections.deque(maxlen=_FAULT_LIMIT)
        self._consumed = threading.Event()
        self._consumed.set()

    def _handle_event(self, event: Any) -> None:
        """Event bus handler — appends to ring buffers (thread-safe)."""
        try:
            ev_dict = {
                "type": event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
                "timestamp": getattr(event, "timestamp", time.time()),
                "message": getattr(event, "message", ""),
                "severity": event.severity.value if hasattr(event.severity, "value") else str(getattr(event, "severity", "info")),
                "phase": getattr(event, "phase", ""),
                "component": getattr(event, "component", ""),
            }
            with self._buf_lock:
                self._events.append(ev_dict)
                if ev_dict["severity"] in _FAULT_SEVERITIES:
                    self._faults.append(ev_dict)
        except Exception:
            pass

    def run(self) -> None:
        if self._event_bus:
            try:
                self._event_bus.subscribe(self._handle_event)
            except Exception:
                pass

        interval = _POLL_MS / 1000.0
        while not self._stop_event.is_set():
            try:
                self._pump_once()
            except Exception:
                pass
            self._stop_event.wait(interval)

        if self._event_bus:
            try:
                self._event_bus.unsubscribe(self._handle_event)
            except Exception:
                pass

    def _pump_once(self) -> None:
        if not self._consumed.is_set():
            return

        dashboard = self._dashboard_getter()
        if dashboard is None:
            # No dashboard yet — still post captured logs so TUI isn't blank
            captured_snap = tuple(self._captured_output)
            with self._buf_lock:
                events_snap = tuple(self._events)
                faults_snap = tuple(self._faults)

            snapshot = TuiSnapshot(
                captured_logs=captured_snap[-500:] if len(captured_snap) > 500 else captured_snap,
                events=events_snap,
                faults=faults_snap,
            )
            self._consumed.clear()
            try:
                self._app.call_from_thread(self._app.update_snapshot, snapshot, self._consumed)
            except Exception:
                self._consumed.set()
            return

        lock = getattr(dashboard, "_lock", None)
        try:
            if lock:
                lock.acquire()
            state = dashboard._build_render_state()
        except Exception:
            return
        finally:
            if lock:
                try:
                    lock.release()
                except RuntimeError:
                    pass

        ipc_data: dict = {}
        if self._ipc_getter and self._supervisor_loop:
            try:
                if self._supervisor_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._ipc_getter(), self._supervisor_loop,
                    )
                    ipc_data = future.result(timeout=2.0)
            except Exception:
                pass

        captured_snap = tuple(self._captured_output)
        with self._buf_lock:
            events_snap = tuple(self._events)
            faults_snap = tuple(self._faults)

        snapshot = TuiSnapshot(
            groups=tuple(copy.deepcopy(state.get("groups", []))),
            gcp=copy.deepcopy(state.get("gcp", {})),
            memory=copy.deepcopy(state.get("memory", {})),
            model=copy.deepcopy(state.get("model", {})),
            logs=tuple(state.get("logs", [])),
            elapsed=state.get("elapsed", 0.0),
            schema_version=state.get("schema_version", "1.0.0"),
            events=events_snap,
            faults=faults_snap,
            ipc_status=copy.deepcopy(ipc_data),
            captured_logs=captured_snap[-500:] if len(captured_snap) > 500 else captured_snap,
        )

        self._consumed.clear()
        try:
            self._app.call_from_thread(self._app.update_snapshot, snapshot, self._consumed)
        except Exception:
            self._consumed.set()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Helper — filter log lines by keyword set
# ---------------------------------------------------------------------------
def _filter_logs(lines: tuple, keywords: frozenset, limit: int = 200) -> List[str]:
    """Return lines containing any keyword (case-insensitive), most recent first."""
    result: List[str] = []
    for line in reversed(lines):
        lower = line.lower()
        if any(kw in lower for kw in keywords):
            result.append(line)
            if len(result) >= limit:
                break
    result.reverse()
    return result


# ---------------------------------------------------------------------------
# Tab widgets
# ---------------------------------------------------------------------------
class SupervisorTab(Container):
    """Component groups table + GCP progress + memory gauge."""

    DEFAULT_CSS = """
    SupervisorTab {
        padding: 1 2;
    }
    SupervisorTab .section-label {
        text-style: bold;
        margin-bottom: 1;
    }
    SupervisorTab DataTable {
        height: auto;
        max-height: 30;
    }
    SupervisorTab .gauge-row {
        height: 3;
        margin-top: 1;
    }
    SupervisorTab .gauge-label {
        width: 14;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Component Status", classes="section-label")
        table = DataTable(id="comp-table")
        table.add_columns("", "Component", "Status", "Code")
        yield table
        with Horizontal(classes="gauge-row"):
            yield Label("GCP Progress:", classes="gauge-label")
            yield ProgressBar(id="gcp-bar", total=100, show_eta=False)
            yield Label("", id="gcp-pct")
        with Horizontal(classes="gauge-row"):
            yield Label("Memory:", classes="gauge-label")
            yield ProgressBar(id="mem-bar", total=100, show_eta=False)
            yield Label("", id="mem-pct")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            table = self.query_one("#comp-table", DataTable)
            table.clear()
            for group in snap.groups:
                table.add_row(
                    group.get("emoji", ""),
                    f"[bold]{group.get('label', '')}[/bold]",
                    "",
                    "",
                )
                for member in group.get("members", []):
                    style = _STATUS_STYLE.get(member.get("status", ""), "")
                    status_text = member.get("status", "")
                    if style:
                        status_text = f"[{style}]{status_text}[/{style}]"
                    table.add_row(
                        member.get("emoji", ""),
                        f"  {member.get('name', '')}",
                        status_text,
                        member.get("code", ""),
                    )
        except Exception:
            pass

        try:
            gcp = snap.gcp
            pct = gcp.get("progress", 0)
            bar = self.query_one("#gcp-bar", ProgressBar)
            bar.update(progress=float(pct))
            lbl = self.query_one("#gcp-pct", Label)
            phase_name = gcp.get("phase_name", "")
            lbl.update(f" {pct:.0f}% {phase_name}")
        except Exception:
            pass

        try:
            mem = snap.memory
            used_pct = mem.get("used_pct", 0)
            bar = self.query_one("#mem-bar", ProgressBar)
            bar.update(progress=float(used_pct))
            lbl = self.query_one("#mem-pct", Label)
            used_gb = mem.get("used_gb", 0)
            total_gb = mem.get("total_gb", 0)
            lbl.update(f" {used_pct:.0f}% ({used_gb:.1f}/{total_gb:.1f} GB)")
        except Exception:
            pass


class PrimeTab(VerticalScroll):
    """JARVIS Prime real-time status — IPC data + filtered log stream."""

    DEFAULT_CSS = """
    PrimeTab {
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("[dim]Waiting for Prime data...[/dim]", id="prime-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#prime-content", Static)
            lines: List[str] = []
            ipc = snap.ipc_status

            # IPC-sourced structured data
            if ipc:
                lines.append("[bold underline]JARVIS Prime — Live Status[/bold underline]")
                lines.append("")

                lines.append(f"[bold]State:[/bold] {ipc.get('state', 'unknown')}")
                lines.append(f"[bold]Uptime:[/bold] {ipc.get('uptime_seconds', 0):.0f}s")
                lines.append(f"[bold]PID:[/bold] {ipc.get('pid', 'N/A')}")

                cfg = ipc.get("config", {})
                if cfg:
                    lines.append("")
                    lines.append("[bold]Configuration[/bold]")
                    lines.append(f"  Kernel ID: {cfg.get('kernel_id', 'N/A')}")
                    lines.append(f"  Mode: {cfg.get('mode', 'N/A')}")
                    lines.append(f"  Backend Port: {cfg.get('backend_port', 'N/A')}")

                trinity = ipc.get("trinity", {})
                if trinity:
                    lines.append("")
                    lines.append("[bold]Trinity Integration[/bold]")
                    for k, v in trinity.items():
                        if isinstance(v, dict):
                            lines.append(f"  {k}:")
                            for sk, sv in list(v.items())[:8]:
                                lines.append(f"    {sk}: {sv}")
                        else:
                            lines.append(f"  {k}: {v}")

                inv = ipc.get("invincible_node", {})
                if inv and inv.get("enabled"):
                    lines.append("")
                    lines.append("[bold]Invincible Node (GCP)[/bold]")
                    lines.append(f"  Instance: {inv.get('instance_name', 'N/A')}")
                    lines.append(f"  Status: {inv.get('status', 'N/A')}")
                    lines.append(f"  Port: {inv.get('port', 'N/A')}")

                model = snap.model
                if model and model.get("active"):
                    lines.append("")
                    lines.append("[bold]Model Loading[/bold]")
                    lines.append(f"  Model: {model.get('model_name', 'N/A')}")
                    lines.append(f"  Progress: {model.get('progress', 0):.0f}%")
                    lines.append(f"  Phase: {model.get('phase', 'N/A')}")

                modes = ipc.get("startup_modes", {})
                if modes:
                    lines.append("")
                    lines.append("[bold]Startup Modes[/bold]")
                    lines.append(f"  Desired: {modes.get('desired_mode', 'N/A')}")
                    lines.append(f"  Effective: {modes.get('effective_mode', 'N/A')}")
                    heavy = modes.get("can_spawn_heavy", "N/A")
                    lines.append(f"  Can Spawn Heavy: {heavy}")

            # Filtered real-time log stream
            prime_logs = _filter_logs(snap.captured_logs, _PRIME_KEYWORDS, limit=60)
            if prime_logs:
                lines.append("")
                lines.append(f"[bold underline]Prime Log Stream[/bold underline] [dim]({len(prime_logs)} lines)[/dim]")
                lines.append("")
                for log_line in prime_logs[-40:]:
                    lines.append(f"[dim]{log_line}[/dim]")

            if not lines:
                content.update("[dim]No Prime data available yet. Waiting for startup...[/dim]")
            else:
                content.update("\n".join(lines))
        except Exception:
            pass


class ReactorTab(VerticalScroll):
    """Reactor-Core real-time status — IPC data + filtered log stream."""

    DEFAULT_CSS = """
    ReactorTab {
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("[dim]Waiting for Reactor data...[/dim]", id="reactor-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#reactor-content", Static)
            lines: List[str] = []
            ipc = snap.ipc_status

            if ipc:
                lines.append("[bold underline]Reactor Core — Live Status[/bold underline]")
                lines.append("")

                two_tier = ipc.get("two_tier", {})
                if two_tier:
                    lines.append("[bold]Two-Tier Security[/bold]")
                    lines.append(f"  Enabled: {two_tier.get('enabled', False)}")
                    watchdog = two_tier.get("watchdog", {})
                    if watchdog:
                        lines.append(f"  Watchdog: {watchdog}")
                    cross_repo = two_tier.get("cross_repo", {})
                    if cross_repo:
                        lines.append(f"  Cross-repo: {cross_repo}")
                    lines.append(f"  Runner Wired: {two_tier.get('runner_wired', False)}")

                ecapa = ipc.get("ecapa_policy", {})
                if ecapa and not ecapa.get("error"):
                    lines.append("")
                    lines.append("[bold]ECAPA Policy[/bold]")
                    lines.append(f"  Mode: {ecapa.get('mode', 'N/A')}")
                    lines.append(f"  Backend: {ecapa.get('active_backend', 'N/A')}")
                    lines.append(f"  Failures: {ecapa.get('consecutive_failures', 0)}")
                    lines.append(f"  Successes: {ecapa.get('consecutive_successes', 0)}")
                    budget = ecapa.get("retry_budget_remaining", 0)
                    lines.append(f"  Retry Budget: {budget}")

                agi = ipc.get("agi_os", {})
                if agi:
                    lines.append("")
                    lines.append("[bold]AGI OS[/bold]")
                    lines.append(f"  Enabled: {agi.get('enabled', False)}")
                    lines.append(f"  Status: {agi.get('status', 'N/A')}")
                    lines.append(f"  Coordinator: {agi.get('coordinator', False)}")
                    lines.append(f"  Voice Communicator: {agi.get('voice_communicator', False)}")

                voice = ipc.get("voice_sidecar", {})
                if voice:
                    lines.append("")
                    lines.append("[bold]Voice Sidecar[/bold]")
                    lines.append(f"  Enabled: {voice.get('enabled', False)}")
                    lines.append(f"  Transport: {voice.get('transport', 'N/A')}")
                    pid = voice.get("process_pid")
                    if pid:
                        lines.append(f"  PID: {pid}")

                readiness = ipc.get("readiness", {})
                if readiness:
                    lines.append("")
                    lines.append("[bold]Readiness[/bold]")
                    for k, v in list(readiness.items())[:10]:
                        lines.append(f"  {k}: {v}")

                pressure = ipc.get("memory_pressure_signal", {})
                if pressure and not pressure.get("error"):
                    lines.append("")
                    lines.append("[bold]Memory Pressure Signal[/bold]")
                    for k, v in list(pressure.items())[:8]:
                        lines.append(f"  {k}: {v}")

            # Filtered real-time log stream
            reactor_logs = _filter_logs(snap.captured_logs, _REACTOR_KEYWORDS, limit=60)
            if reactor_logs:
                lines.append("")
                lines.append(f"[bold underline]Reactor Log Stream[/bold underline] [dim]({len(reactor_logs)} lines)[/dim]")
                lines.append("")
                for log_line in reactor_logs[-40:]:
                    lines.append(f"[dim]{log_line}[/dim]")

            if not lines:
                content.update("[dim]No Reactor data available yet. Waiting for startup...[/dim]")
            else:
                content.update("\n".join(lines))
        except Exception:
            pass


class EventsTab(VerticalScroll):
    """Unified log view — structured events + captured supervisor output."""

    DEFAULT_CSS = """
    EventsTab {
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Waiting for events...", id="events-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#events-content", Static)
            lines: List[str] = []

            # Structured events from event bus
            if snap.events:
                for ev in snap.events[-100:]:
                    ts = ev.get("timestamp", 0)
                    ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
                    severity = ev.get("severity", "info")
                    style = _SEVERITY_STYLE.get(severity, "")
                    msg = ev.get("message", "")
                    component = ev.get("component", "")

                    prefix = f"[dim]{ts_str}[/dim]"
                    sev_tag = f"[{style}]{severity.upper():8s}[/{style}]" if style else f"{severity.upper():8s}"
                    comp_tag = f"[bold]{component}[/bold]" if component else ""

                    parts = [prefix, sev_tag]
                    if comp_tag:
                        parts.append(comp_tag)
                    parts.append(msg)
                    lines.append(" ".join(parts))

            # Captured supervisor output (stdout + logging)
            if snap.captured_logs:
                if lines:
                    lines.append("")
                    lines.append("[bold]─── Supervisor Output ───[/bold]")
                for log_line in snap.captured_logs[-200:]:
                    lines.append(f"[dim]{log_line}[/dim]")

            if lines:
                content.update("\n".join(lines))
                self.scroll_end(animate=False)
            else:
                content.update("[dim]No events yet[/dim]")
        except Exception:
            pass


class FaultsTab(VerticalScroll):
    """Filtered view of ERROR/WARNING/CRITICAL events only."""

    DEFAULT_CSS = """
    FaultsTab {
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("[green]No faults detected[/green]", id="faults-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#faults-content", Static)
            if not snap.faults:
                content.update("[green]No faults detected[/green]")
                return

            lines: List[str] = []
            for ev in snap.faults:
                ts = ev.get("timestamp", 0)
                ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
                severity = ev.get("severity", "error")
                style = _SEVERITY_STYLE.get(severity, "bold red")
                msg = ev.get("message", "")
                component = ev.get("component", "")

                prefix = f"[dim]{ts_str}[/dim]"
                sev_tag = f"[{style}]{severity.upper():8s}[/{style}]"
                comp_tag = f"[bold]{component}[/bold]" if component else ""

                parts = [prefix, sev_tag]
                if comp_tag:
                    parts.append(comp_tag)
                parts.append(msg)
                lines.append(" ".join(parts))

            content.update("\n".join(lines))
            self.scroll_end(animate=False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# JarvisTuiApp — main Textual application
# ---------------------------------------------------------------------------
class JarvisTuiApp(App):
    """JARVIS Supervisor TUI with 5 interactive tabs."""

    TITLE = "JARVIS Supervisor"
    SUB_TITLE = "Terminal UI"

    DEFAULT_CSS = """
    Screen {
        background: $surface;
    }
    TabbedContent {
        height: 1fr;
    }
    TabPane {
        padding: 0;
    }
    Header {
        dock: top;
    }
    Footer {
        dock: bottom;
    }
    #elapsed-bar {
        dock: top;
        height: 1;
        background: $primary-background;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("1", "tab_1", "Supervisor", show=False),
        Binding("2", "tab_2", "Prime", show=False),
        Binding("3", "tab_3", "Reactor", show=False),
        Binding("4", "tab_4", "Events", show=False),
        Binding("5", "tab_5", "Faults", show=False),
        Binding("r", "refresh", "Refresh"),
    ]

    _snapshot: reactive[Optional[TuiSnapshot]] = reactive(None)
    _fault_count: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("⚡ JARVIS TUI | Elapsed: 0s | Press 1-5 for tabs, q to quit", id="elapsed-bar")
        with TabbedContent(id="tabs"):
            with TabPane("Supervisor", id="tab-supervisor"):
                yield SupervisorTab()
            with TabPane("Prime", id="tab-prime"):
                yield PrimeTab()
            with TabPane("Reactor", id="tab-reactor"):
                yield ReactorTab()
            with TabPane("Events", id="tab-events"):
                yield EventsTab()
            with TabPane("Faults", id="tab-faults"):
                yield FaultsTab()
        yield Footer()

    def update_snapshot(
        self, snapshot: TuiSnapshot, consumed_event: Optional[threading.Event] = None,
    ) -> None:
        """Called from SnapshotPump via call_from_thread."""
        self._snapshot = snapshot
        if consumed_event is not None:
            consumed_event.set()

    def watch__snapshot(self, snapshot: Optional[TuiSnapshot]) -> None:
        if snapshot is None:
            return
        try:
            elapsed_label = self.query_one("#elapsed-bar", Label)
            mins, secs = divmod(int(snapshot.elapsed), 60)
            elapsed_label.update(
                f"⚡ JARVIS TUI | Elapsed: {mins}m {secs:02d}s | "
                f"Schema: {snapshot.schema_version} | "
                f"Events: {len(snapshot.events)} | "
                f"Faults: {len(snapshot.faults)} | "
                f"Logs: {len(snapshot.captured_logs)}"
            )
        except Exception:
            pass

        try:
            self.query_one(SupervisorTab).update_from_snapshot(snapshot)
        except Exception:
            pass
        try:
            self.query_one(PrimeTab).update_from_snapshot(snapshot)
        except Exception:
            pass
        try:
            self.query_one(ReactorTab).update_from_snapshot(snapshot)
        except Exception:
            pass
        try:
            self.query_one(EventsTab).update_from_snapshot(snapshot)
        except Exception:
            pass
        try:
            faults_tab = self.query_one(FaultsTab)
            faults_tab.update_from_snapshot(snapshot)
            self._fault_count = len(snapshot.faults)
        except Exception:
            pass

    def watch__fault_count(self, count: int) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            faults_pane = tabs.query_one("#tab-faults", TabPane)
            faults_pane.label = f"Faults ({count})" if count > 0 else "Faults"
        except Exception:
            pass

    def action_tab_1(self) -> None:
        self._switch_tab("tab-supervisor")

    def action_tab_2(self) -> None:
        self._switch_tab("tab-prime")

    def action_tab_3(self) -> None:
        self._switch_tab("tab-reactor")

    def action_tab_4(self) -> None:
        self._switch_tab("tab-events")

    def action_tab_5(self) -> None:
        self._switch_tab("tab-faults")

    def action_refresh(self) -> None:
        if self._snapshot is not None:
            self.watch__snapshot(self._snapshot)

    def _switch_tab(self, tab_id: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.active = tab_id
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TuiCliRenderer — CliRenderer-compatible for --ui tui
# ---------------------------------------------------------------------------
class TuiCliRenderer:
    """CliRenderer-compatible renderer for the Textual TUI.

    Architecture:
      1. start() redirects stdout and logging to capture buffers, then
         launches the Textual app in a daemon thread. Textual renders
         via stderr (its default), so there's no terminal conflict.
      2. attach_to_supervisor() creates and starts the SnapshotPump
         with a valid dashboard getter (fixing the data race in the
         original implementation where the pump was created before
         the dashboard was available).
      3. stop() restores stdout and logging handlers.
    """

    def __init__(self, verbosity: str = "ops"):
        self._verbosity = verbosity
        self._running = False
        self._app: Optional[JarvisTuiApp] = None
        self._pump: Optional[SnapshotPump] = None
        self._thread: Optional[threading.Thread] = None
        self._supervisor = None
        self._dashboard_getter: Optional[Callable] = None
        self._event_bus = None
        self._supervisor_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ipc_getter: Optional[Callable] = None

        self._captured_output: Deque[str] = collections.deque(maxlen=_LOG_LIMIT)
        self._orig_stdout: Any = None
        self._orig_log_handlers: List[logging.Handler] = []
        self._tui_log_handler: Optional[_TuiLogHandler] = None

    def start(self) -> None:
        """Redirect output, create and launch the Textual TUI app."""
        self._running = True

        # Step 1: Capture stdout so supervisor banners/prints go to our buffer.
        # Textual uses stderr for terminal rendering — no conflict.
        self._orig_stdout = sys.stdout
        sys.stdout = _OutputCapture(self._captured_output, self._orig_stdout)

        # Step 2: Replace root logger handlers with our buffer handler.
        # This prevents log messages from writing to stderr (which would
        # corrupt Textual's terminal rendering).
        root_logger = logging.getLogger()
        self._orig_log_handlers = root_logger.handlers[:]
        self._tui_log_handler = _TuiLogHandler(self._captured_output)
        for h in self._orig_log_handlers:
            root_logger.removeHandler(h)
        root_logger.addHandler(self._tui_log_handler)

        # Step 3: Create and start Textual app in daemon thread
        self._app = JarvisTuiApp()

        def _run_tui() -> None:
            try:
                self._app.run()
            except Exception as exc:
                self._restore_output()
                sys.stderr.write(f"TUI thread exited: {exc}\n")

        self._thread = threading.Thread(
            target=_run_tui,
            name="jarvis-tui-thread",
            daemon=True,
        )
        self._thread.start()

        # Pump is NOT created here — deferred to attach_to_supervisor()
        # so dashboard_getter is available (fixes data race).

    def attach_to_supervisor(self, supervisor: Any) -> None:
        """Wire read-only references and start the SnapshotPump.

        Called from unified_supervisor.py after renderer creation.
        Creates the pump HERE (not in start()) because the dashboard
        getter and event bus are only available after the supervisor
        kernel is initialized.
        """
        self._supervisor = supervisor

        try:
            from unified_supervisor import get_live_dashboard
            self._dashboard_getter = get_live_dashboard
        except ImportError:
            self._dashboard_getter = None

        self._event_bus = getattr(supervisor, "_event_bus", None)

        try:
            self._supervisor_loop = asyncio.get_event_loop()
        except RuntimeError:
            self._supervisor_loop = None

        if hasattr(supervisor, "_ipc_status"):
            self._ipc_getter = supervisor._ipc_status

        # NOW create and start the pump with valid data sources
        if self._app is not None:
            self._pump = SnapshotPump(
                dashboard_getter=self._dashboard_getter or (lambda: None),
                app=self._app,
                event_bus=self._event_bus,
                supervisor_loop=self._supervisor_loop,
                ipc_getter=self._ipc_getter,
                captured_output=self._captured_output,
            )
            self._pump.start()

    def stop(self) -> None:
        """Stop the TUI app and pump, restore output."""
        self._running = False
        if self._pump:
            self._pump.stop()
        if self._app:
            try:
                self._app.call_from_thread(self._app.exit)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)
        self._restore_output()

    def _restore_output(self) -> None:
        """Restore original stdout and logging handlers."""
        if self._orig_stdout is not None:
            sys.stdout = self._orig_stdout
            self._orig_stdout = None
        root_logger = logging.getLogger()
        if self._tui_log_handler and self._tui_log_handler in root_logger.handlers:
            root_logger.removeHandler(self._tui_log_handler)
        for h in self._orig_log_handlers:
            if h not in root_logger.handlers:
                root_logger.addHandler(h)
        self._orig_log_handlers = []

    def handle_event(self, _event: Any) -> None:
        """No-op — TUI gets data via SnapshotPump, not event handler."""

    def should_display(self, _event: Any) -> bool:
        """Returns False — TUI manages its own display."""
        return False
