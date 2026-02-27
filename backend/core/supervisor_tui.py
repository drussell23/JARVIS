"""
JARVIS Supervisor TUI — Textual-based terminal UI with 5 tabs.

Consumes the Phase 1 view-model (_build_render_state()) read-only.
Runs in a daemon thread with its own asyncio event loop.
Crashes never affect the supervisor's event loop or startup.

Env vars:
  JARVIS_TUI_POLL_MS       = int  (default: 500)  — snapshot poll interval
  JARVIS_TUI_EVENT_LIMIT   = int  (default: 500)  — max events in Events tab ring
  JARVIS_TUI_FAULT_LIMIT   = int  (default: 200)  — max faults in Faults tab ring
"""

from __future__ import annotations

import asyncio
import collections
import copy
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional

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

# ---------------------------------------------------------------------------
# Env-configurable constants
# ---------------------------------------------------------------------------
_POLL_MS = int(os.environ.get("JARVIS_TUI_POLL_MS", "500"))
_EVENT_LIMIT = int(os.environ.get("JARVIS_TUI_EVENT_LIMIT", "500"))
_FAULT_LIMIT = int(os.environ.get("JARVIS_TUI_FAULT_LIMIT", "200"))

# ---------------------------------------------------------------------------
# Status → Textual Rich markup style mapping (mirrors STATUS_RICH_STYLE)
# ---------------------------------------------------------------------------
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

# Severity → color for events tab
_SEVERITY_STYLE: Dict[str, str] = {
    "debug": "dim",
    "info": "bright_blue",
    "warning": "bold yellow",
    "error": "bold red",
    "critical": "bold white on red",
    "success": "bold green",
}

_FAULT_SEVERITIES = frozenset({"error", "warning", "critical"})


# ---------------------------------------------------------------------------
# TuiSnapshot — frozen cross-thread data transfer object
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TuiSnapshot:
    """Immutable snapshot posted from SnapshotPump -> Textual app.

    Frozen dataclass prevents attribute reassignment. Dict fields are
    deep-copied at construction time in SnapshotPump._pump_once() so
    no mutable state is shared across threads.
    """

    groups: tuple = ()  # tuple of dicts from _build_render_state()
    gcp: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    logs: tuple = ()
    elapsed: float = 0.0
    schema_version: str = "1.0.0"
    events: tuple = ()  # recent SupervisorEvent dicts
    faults: tuple = ()  # error/warning/critical events only
    ipc_status: dict = field(default_factory=dict)  # supervisor _ipc_status data


# ---------------------------------------------------------------------------
# SnapshotPump — daemon thread bridging supervisor -> Textual
# ---------------------------------------------------------------------------
class SnapshotPump(threading.Thread):
    """Daemon thread that polls _build_render_state() and posts snapshots.

    - Calls dashboard._build_render_state() every JARVIS_TUI_POLL_MS ms
    - Collects events via SupervisorEventBus subscription
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
    ):
        super().__init__(name="jarvis-tui-pump", daemon=True)
        self._dashboard_getter = dashboard_getter
        self._app = app
        self._event_bus = event_bus
        self._supervisor_loop = supervisor_loop
        self._ipc_getter = ipc_getter
        self._stop_event = threading.Event()
        self._buf_lock = threading.Lock()
        self._events: Deque[dict] = collections.deque(maxlen=_EVENT_LIMIT)
        self._faults: Deque[dict] = collections.deque(maxlen=_FAULT_LIMIT)
        # Coalescing: cleared by pump, set by Textual app after consumption
        self._consumed = threading.Event()
        self._consumed.set()  # initially "consumed" — ready for first post

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
            logger.debug("TUI pump: event handler error", exc_info=True)

    def run(self) -> None:
        """Main pump loop."""
        if self._event_bus:
            try:
                self._event_bus.subscribe(self._handle_event)
            except Exception:
                logger.debug("TUI pump: event bus subscribe failed", exc_info=True)

        interval = _POLL_MS / 1000.0

        while not self._stop_event.is_set():
            try:
                self._pump_once()
            except Exception:
                logger.debug("TUI pump: pump cycle error", exc_info=True)
            self._stop_event.wait(interval)

        # Cleanup
        if self._event_bus:
            try:
                self._event_bus.unsubscribe(self._handle_event)
            except Exception:
                pass

    def _pump_once(self) -> None:
        """Build snapshot and post to Textual app."""
        if not self._consumed.is_set():
            return  # coalescing — previous snapshot not yet consumed by Textual

        dashboard = self._dashboard_getter()
        if dashboard is None:
            return

        # Acquire dashboard lock for thread-safe read of shared state
        lock = getattr(dashboard, "_lock", None)
        try:
            if lock:
                lock.acquire()
            state = dashboard._build_render_state()
        except Exception:
            logger.debug("TUI pump: _build_render_state() failed", exc_info=True)
            return
        finally:
            if lock:
                try:
                    lock.release()
                except RuntimeError:
                    pass

        # Collect IPC status via supervisor's event loop (thread-safe)
        ipc_data: dict = {}
        if self._ipc_getter and self._supervisor_loop:
            try:
                if self._supervisor_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._ipc_getter(), self._supervisor_loop,
                    )
                    ipc_data = future.result(timeout=2.0)
            except Exception:
                logger.debug("TUI pump: IPC status fetch failed", exc_info=True)

        # Deep-copy dict fields to eliminate shared mutable state
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
        )

        self._consumed.clear()
        try:
            self._app.call_from_thread(self._app.update_snapshot, snapshot, self._consumed)
        except Exception:
            # If post fails, mark as consumed so next cycle can try again
            self._consumed.set()
            logger.debug("TUI pump: call_from_thread failed", exc_info=True)

    def stop(self) -> None:
        """Signal the pump to stop and join with timeout."""
        self._stop_event.set()
        self.join(timeout=3.0)


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
        """Refresh component table and gauges from snapshot."""
        try:
            table = self.query_one("#comp-table", DataTable)
            table.clear()
            for group in snap.groups:
                # Group header row
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
            logger.debug("TUI: supervisor tab table update failed", exc_info=True)

        # GCP progress
        try:
            gcp = snap.gcp
            pct = gcp.get("progress", 0)
            bar = self.query_one("#gcp-bar", ProgressBar)
            bar.update(progress=float(pct))
            lbl = self.query_one("#gcp-pct", Label)
            phase_name = gcp.get("phase_name", "")
            lbl.update(f" {pct:.0f}% {phase_name}")
        except Exception:
            logger.debug("TUI: GCP gauge update failed", exc_info=True)

        # Memory gauge
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
            logger.debug("TUI: memory gauge update failed", exc_info=True)


class PrimeTab(VerticalScroll):
    """JARVIS Prime status from IPC."""

    DEFAULT_CSS = """
    PrimeTab {
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Loading Prime status...", id="prime-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#prime-content", Static)
            ipc = snap.ipc_status
            if not ipc:
                content.update("[dim]Prime IPC status not available[/dim]")
                return

            lines = []

            # State
            lines.append(f"[bold]State:[/bold] {ipc.get('state', 'unknown')}")
            lines.append(f"[bold]Uptime:[/bold] {ipc.get('uptime_seconds', 0):.0f}s")
            lines.append(f"[bold]PID:[/bold] {ipc.get('pid', 'N/A')}")

            # Config
            cfg = ipc.get("config", {})
            if cfg:
                lines.append("")
                lines.append("[bold underline]Configuration[/bold underline]")
                lines.append(f"  Kernel ID: {cfg.get('kernel_id', 'N/A')}")
                lines.append(f"  Mode: {cfg.get('mode', 'N/A')}")
                lines.append(f"  Backend Port: {cfg.get('backend_port', 'N/A')}")
                lines.append(f"  Dev Mode: {cfg.get('dev_mode', False)}")

            # Trinity
            trinity = ipc.get("trinity", {})
            if trinity:
                lines.append("")
                lines.append("[bold underline]Trinity[/bold underline]")
                for k, v in trinity.items():
                    if isinstance(v, dict):
                        lines.append(f"  {k}:")
                        for sk, sv in v.items():
                            lines.append(f"    {sk}: {sv}")
                    else:
                        lines.append(f"  {k}: {v}")

            # Invincible node
            inv = ipc.get("invincible_node", {})
            if inv:
                lines.append("")
                lines.append("[bold underline]Invincible Node (GCP)[/bold underline]")
                lines.append(f"  Enabled: {inv.get('enabled', False)}")
                lines.append(f"  Instance: {inv.get('instance_name', 'N/A')}")
                lines.append(f"  Status: {inv.get('status', 'N/A')}")

            # Model loading
            model = snap.model
            if model and model.get("active"):
                lines.append("")
                lines.append("[bold underline]Model Loading[/bold underline]")
                lines.append(f"  Model: {model.get('model_name', 'N/A')}")
                lines.append(f"  Progress: {model.get('progress', 0):.0f}%")
                lines.append(f"  Phase: {model.get('phase', 'N/A')}")

            content.update("\n".join(lines) if lines else "[dim]No data[/dim]")
        except Exception:
            logger.debug("TUI: prime tab update failed", exc_info=True)


class ReactorTab(VerticalScroll):
    """Reactor-Core status from IPC."""

    DEFAULT_CSS = """
    ReactorTab {
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Loading Reactor status...", id="reactor-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#reactor-content", Static)
            ipc = snap.ipc_status
            if not ipc:
                content.update("[dim]Reactor IPC status not available[/dim]")
                return

            lines = []

            # Two-tier info
            two_tier = ipc.get("two_tier", {})
            if two_tier:
                lines.append("[bold underline]Two-Tier Security[/bold underline]")
                lines.append(f"  Enabled: {two_tier.get('enabled', False)}")
                watchdog = two_tier.get("watchdog", {})
                if watchdog:
                    lines.append(f"  Watchdog: {watchdog}")
                cross_repo = two_tier.get("cross_repo", {})
                if cross_repo:
                    lines.append(f"  Cross-repo: {cross_repo}")
                lines.append(f"  Runner Wired: {two_tier.get('runner_wired', False)}")

            # AGI OS
            agi = ipc.get("agi_os", {})
            if agi:
                lines.append("")
                lines.append("[bold underline]AGI OS[/bold underline]")
                lines.append(f"  Enabled: {agi.get('enabled', False)}")
                lines.append(f"  Status: {agi.get('status', 'N/A')}")
                lines.append(f"  Coordinator: {agi.get('coordinator', False)}")
                lines.append(f"  Voice Communicator: {agi.get('voice_communicator', False)}")

            # ECAPA
            ecapa = ipc.get("ecapa_policy", {})
            if ecapa:
                lines.append("")
                lines.append("[bold underline]ECAPA Policy[/bold underline]")
                lines.append(f"  Mode: {ecapa.get('mode', 'N/A')}")
                lines.append(f"  Backend: {ecapa.get('active_backend', 'N/A')}")
                lines.append(f"  Consecutive Failures: {ecapa.get('consecutive_failures', 0)}")
                lines.append(f"  Consecutive Successes: {ecapa.get('consecutive_successes', 0)}")

            # Voice sidecar
            voice = ipc.get("voice_sidecar", {})
            if voice:
                lines.append("")
                lines.append("[bold underline]Voice Sidecar[/bold underline]")
                lines.append(f"  Enabled: {voice.get('enabled', False)}")
                lines.append(f"  Transport: {voice.get('transport', 'N/A')}")
                pid = voice.get("process_pid")
                if pid:
                    lines.append(f"  PID: {pid}")

            # Readiness
            readiness = ipc.get("readiness", {})
            if readiness:
                lines.append("")
                lines.append("[bold underline]Readiness[/bold underline]")
                for k, v in readiness.items():
                    lines.append(f"  {k}: {v}")

            content.update("\n".join(lines) if lines else "[dim]No reactor data[/dim]")
        except Exception:
            logger.debug("TUI: reactor tab update failed", exc_info=True)


class EventsTab(VerticalScroll):
    """Scrollable log of recent supervisor events."""

    DEFAULT_CSS = """
    EventsTab {
        padding: 1 2;
    }
    EventsTab .event-line {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Waiting for events...", id="events-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#events-content", Static)
            if not snap.events:
                content.update("[dim]No events yet[/dim]")
                return

            lines = []
            for ev in snap.events:
                ts = ev.get("timestamp", 0)
                ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
                severity = ev.get("severity", "info")
                style = _SEVERITY_STYLE.get(severity, "")
                msg = ev.get("message", "")
                ev_type = ev.get("type", "")
                component = ev.get("component", "")

                prefix = f"[dim]{ts_str}[/dim]"
                sev_tag = f"[{style}]{severity.upper():8s}[/{style}]" if style else f"{severity.upper():8s}"
                comp_tag = f"[bold]{component}[/bold]" if component else ""
                type_tag = f"[dim]{ev_type}[/dim]" if ev_type else ""

                parts = [prefix, sev_tag]
                if comp_tag:
                    parts.append(comp_tag)
                if type_tag:
                    parts.append(type_tag)
                parts.append(msg)
                lines.append(" ".join(parts))

            content.update("\n".join(lines))
            # Auto-scroll to bottom
            self.scroll_end(animate=False)
        except Exception:
            logger.debug("TUI: events tab update failed", exc_info=True)


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

            lines = []
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
            logger.debug("TUI: faults tab update failed", exc_info=True)


# ---------------------------------------------------------------------------
# JarvisTuiApp — main Textual application
# ---------------------------------------------------------------------------
class JarvisTuiApp(App):
    """JARVIS Supervisor TUI with 5 tabs."""

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
        yield Label("Elapsed: 0s", id="elapsed-bar")
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
        """Called from SnapshotPump via call_from_thread. Updates reactive.

        Sets consumed_event when done so pump knows it can post the next snapshot.
        """
        self._snapshot = snapshot
        if consumed_event is not None:
            consumed_event.set()

    def watch__snapshot(self, snapshot: Optional[TuiSnapshot]) -> None:
        """React to new snapshot — update all tabs."""
        if snapshot is None:
            return
        try:
            # Elapsed bar
            elapsed_label = self.query_one("#elapsed-bar", Label)
            mins, secs = divmod(int(snapshot.elapsed), 60)
            elapsed_label.update(
                f"Elapsed: {mins}m {secs:02d}s | "
                f"Schema: {snapshot.schema_version} | "
                f"Events: {len(snapshot.events)} | "
                f"Faults: {len(snapshot.faults)}"
            )
        except Exception:
            logger.debug("TUI: elapsed bar update failed", exc_info=True)

        # Update each tab
        try:
            self.query_one(SupervisorTab).update_from_snapshot(snapshot)
        except Exception:
            logger.debug("TUI: supervisor tab dispatch failed", exc_info=True)
        try:
            self.query_one(PrimeTab).update_from_snapshot(snapshot)
        except Exception:
            logger.debug("TUI: prime tab dispatch failed", exc_info=True)
        try:
            self.query_one(ReactorTab).update_from_snapshot(snapshot)
        except Exception:
            logger.debug("TUI: reactor tab dispatch failed", exc_info=True)
        try:
            self.query_one(EventsTab).update_from_snapshot(snapshot)
        except Exception:
            logger.debug("TUI: events tab dispatch failed", exc_info=True)
        try:
            faults_tab = self.query_one(FaultsTab)
            faults_tab.update_from_snapshot(snapshot)
            self._fault_count = len(snapshot.faults)
        except Exception:
            logger.debug("TUI: faults tab dispatch failed", exc_info=True)

    def watch__fault_count(self, count: int) -> None:
        """Update faults tab badge when count changes."""
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            faults_pane = tabs.query_one("#tab-faults", TabPane)
            if count > 0:
                faults_pane.label = f"Faults ({count})"
            else:
                faults_pane.label = "Faults"
        except Exception:
            logger.debug("TUI: faults badge update failed", exc_info=True)

    # -- Key bindings --

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
        """Force refresh from current snapshot."""
        if self._snapshot is not None:
            self.watch__snapshot(self._snapshot)

    def _switch_tab(self, tab_id: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.active = tab_id
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TuiCliRenderer — CliRenderer subclass for --ui tui
# ---------------------------------------------------------------------------
class TuiCliRenderer:
    """CliRenderer-compatible renderer for the Textual TUI.

    Implements the same interface as CliRenderer (handle_event, start, stop,
    should_display) without inheriting from the ABC to avoid importing the
    full unified_supervisor module at class-definition time.

    Launches JarvisTuiApp in a daemon thread. Data flows through
    SnapshotPump, not through handle_event().
    """

    def __init__(self, verbosity: str = "ops"):
        self._verbosity = verbosity
        self._running = False
        self._app: Optional[JarvisTuiApp] = None
        self._pump: Optional[SnapshotPump] = None
        self._thread: Optional[threading.Thread] = None
        # Set by attach_to_supervisor()
        self._supervisor = None
        self._dashboard_getter: Optional[Callable] = None
        self._event_bus = None
        self._supervisor_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ipc_getter: Optional[Callable] = None

    def attach_to_supervisor(self, supervisor: Any) -> None:
        """Wire read-only references to supervisor data sources.

        Called from unified_supervisor.py after renderer creation.
        Does NOT call update_component() — read-only.
        """
        self._supervisor = supervisor
        # Dashboard is accessed via the global singleton
        try:
            # Import lazily to avoid circular imports
            from unified_supervisor import get_live_dashboard
            self._dashboard_getter = get_live_dashboard
        except ImportError:
            self._dashboard_getter = None

        # Event bus
        self._event_bus = getattr(supervisor, "_event_bus", None)

        # Capture supervisor's event loop for safe cross-thread IPC calls
        try:
            self._supervisor_loop = asyncio.get_event_loop()
        except RuntimeError:
            self._supervisor_loop = None

        # IPC status getter (async — will be called via run_coroutine_threadsafe)
        if hasattr(supervisor, "_ipc_status"):
            self._ipc_getter = supervisor._ipc_status

    def start(self) -> None:
        """Create the TUI app and launch in a daemon thread."""
        self._running = True

        if self._dashboard_getter is None:
            logger.warning("TUI: no dashboard getter — TUI will show empty data")

        self._app = JarvisTuiApp()
        self._pump = SnapshotPump(
            dashboard_getter=self._dashboard_getter or (lambda: None),
            app=self._app,
            event_bus=self._event_bus,
            supervisor_loop=self._supervisor_loop,
            ipc_getter=self._ipc_getter,
        )

        def _run_tui():
            """Run Textual app in its own asyncio event loop."""
            try:
                self._app.run()
            except Exception as exc:
                logger.warning("TUI thread exited: %s", exc)

        self._thread = threading.Thread(
            target=_run_tui,
            name="jarvis-tui-thread",
            daemon=True,
        )
        self._thread.start()
        self._pump.start()

    def stop(self) -> None:
        """Stop the TUI app and pump cleanly."""
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

    def handle_event(self, _event: Any) -> None:
        """No-op — TUI gets data via SnapshotPump, not event handler."""

    def should_display(self, _event: Any) -> bool:
        """Returns False — TUI manages its own display."""
        return False
