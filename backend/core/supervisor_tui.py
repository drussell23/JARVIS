"""
JARVIS Supervisor TUI — Textual-based terminal UI with 5 tabs.

Architecture (v2 — main-thread Textual):
  Textual MUST own the main thread for terminal control (alternate screen,
  raw mode, signal handlers). The supervisor startup runs as an async
  worker INSIDE Textual's event loop.

  Entry point: run_supervisor_tui(args) called from main() when --ui tui.
  This replaces asyncio.run(async_main(args)) — Textual IS the event loop.

Env vars:
  JARVIS_TUI_POLL_MS       = int  (default: 500)  — snapshot poll interval
  JARVIS_TUI_EVENT_LIMIT   = int  (default: 500)  — max events in ring
  JARVIS_TUI_FAULT_LIMIT   = int  (default: 200)  — max faults in ring
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
from typing import Any, Callable, Deque, Dict, List, Optional, TYPE_CHECKING

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
from textual.worker import Worker, WorkerState

if TYPE_CHECKING:
    import argparse

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
    """File-like object that captures writes to a deque."""

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
    """Logging handler that writes formatted records to a deque."""

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
# Helper — filter log lines by keyword set
# ---------------------------------------------------------------------------
def _filter_logs(lines: tuple, keywords: frozenset, limit: int = 200) -> List[str]:
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
    DEFAULT_CSS = """
    SupervisorTab { padding: 1 2; }
    SupervisorTab .section-label { text-style: bold; margin-bottom: 1; }
    SupervisorTab DataTable { height: auto; max-height: 30; }
    SupervisorTab .gauge-row { height: 3; margin-top: 1; }
    SupervisorTab .gauge-label { width: 14; }
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
                table.add_row(group.get("emoji", ""), f"[bold]{group.get('label', '')}[/bold]", "", "")
                for member in group.get("members", []):
                    style = _STATUS_STYLE.get(member.get("status", ""), "")
                    st = member.get("status", "")
                    st = f"[{style}]{st}[/{style}]" if style else st
                    table.add_row(member.get("emoji", ""), f"  {member.get('name', '')}", st, member.get("code", ""))
        except Exception:
            pass
        try:
            gcp = snap.gcp
            pct = gcp.get("progress", 0)
            self.query_one("#gcp-bar", ProgressBar).update(progress=float(pct))
            self.query_one("#gcp-pct", Label).update(f" {pct:.0f}% {gcp.get('phase_name', '')}")
        except Exception:
            pass
        try:
            mem = snap.memory
            used_pct = mem.get("used_pct", 0)
            self.query_one("#mem-bar", ProgressBar).update(progress=float(used_pct))
            self.query_one("#mem-pct", Label).update(f" {used_pct:.0f}% ({mem.get('used_gb', 0):.1f}/{mem.get('total_gb', 0):.1f} GB)")
        except Exception:
            pass


class PrimeTab(VerticalScroll):
    DEFAULT_CSS = "PrimeTab { padding: 1 2; }"

    def compose(self) -> ComposeResult:
        yield Static("[dim]Waiting for Prime data...[/dim]", id="prime-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#prime-content", Static)
            lines: List[str] = []
            ipc = snap.ipc_status
            if ipc:
                lines.append("[bold underline]JARVIS Prime — Live Status[/bold underline]\n")
                lines.append(f"[bold]State:[/bold] {ipc.get('state', 'unknown')}")
                lines.append(f"[bold]Uptime:[/bold] {ipc.get('uptime_seconds', 0):.0f}s")
                lines.append(f"[bold]PID:[/bold] {ipc.get('pid', 'N/A')}")
                cfg = ipc.get("config", {})
                if cfg:
                    lines.append("\n[bold]Configuration[/bold]")
                    for k in ("kernel_id", "mode", "backend_port"):
                        lines.append(f"  {k}: {cfg.get(k, 'N/A')}")
                trinity = ipc.get("trinity", {})
                if trinity:
                    lines.append("\n[bold]Trinity Integration[/bold]")
                    for k, v in list(trinity.items())[:10]:
                        if isinstance(v, dict):
                            lines.append(f"  {k}: ...")
                        else:
                            lines.append(f"  {k}: {v}")
                inv = ipc.get("invincible_node", {})
                if inv and inv.get("enabled"):
                    lines.append("\n[bold]Invincible Node (GCP)[/bold]")
                    lines.append(f"  Instance: {inv.get('instance_name', 'N/A')}")
                    lines.append(f"  Status: {inv.get('status', 'N/A')}")
                model = snap.model
                if model and model.get("active"):
                    lines.append("\n[bold]Model Loading[/bold]")
                    lines.append(f"  Model: {model.get('model_name', 'N/A')}")
                    lines.append(f"  Progress: {model.get('progress', 0):.0f}%")
            prime_logs = _filter_logs(snap.captured_logs, _PRIME_KEYWORDS, limit=60)
            if prime_logs:
                lines.append(f"\n[bold underline]Prime Log Stream[/bold underline] [dim]({len(prime_logs)} lines)[/dim]\n")
                for ll in prime_logs[-40:]:
                    lines.append(f"[dim]{ll}[/dim]")
            content.update("\n".join(lines) if lines else "[dim]No Prime data yet...[/dim]")
        except Exception:
            pass


class ReactorTab(VerticalScroll):
    DEFAULT_CSS = "ReactorTab { padding: 1 2; }"

    def compose(self) -> ComposeResult:
        yield Static("[dim]Waiting for Reactor data...[/dim]", id="reactor-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#reactor-content", Static)
            lines: List[str] = []
            ipc = snap.ipc_status
            if ipc:
                lines.append("[bold underline]Reactor Core — Live Status[/bold underline]\n")
                two_tier = ipc.get("two_tier", {})
                if two_tier:
                    lines.append("[bold]Two-Tier Security[/bold]")
                    lines.append(f"  Enabled: {two_tier.get('enabled', False)}")
                    lines.append(f"  Runner Wired: {two_tier.get('runner_wired', False)}")
                ecapa = ipc.get("ecapa_policy", {})
                if ecapa and not ecapa.get("error"):
                    lines.append("\n[bold]ECAPA Policy[/bold]")
                    lines.append(f"  Mode: {ecapa.get('mode', 'N/A')}")
                    lines.append(f"  Backend: {ecapa.get('active_backend', 'N/A')}")
                    lines.append(f"  Failures: {ecapa.get('consecutive_failures', 0)}")
                agi = ipc.get("agi_os", {})
                if agi:
                    lines.append("\n[bold]AGI OS[/bold]")
                    lines.append(f"  Enabled: {agi.get('enabled', False)}")
                    lines.append(f"  Status: {agi.get('status', 'N/A')}")
                voice = ipc.get("voice_sidecar", {})
                if voice:
                    lines.append("\n[bold]Voice Sidecar[/bold]")
                    lines.append(f"  Enabled: {voice.get('enabled', False)}")
                    lines.append(f"  Transport: {voice.get('transport', 'N/A')}")
                readiness = ipc.get("readiness", {})
                if readiness:
                    lines.append("\n[bold]Readiness[/bold]")
                    for k, v in list(readiness.items())[:10]:
                        lines.append(f"  {k}: {v}")
            reactor_logs = _filter_logs(snap.captured_logs, _REACTOR_KEYWORDS, limit=60)
            if reactor_logs:
                lines.append(f"\n[bold underline]Reactor Log Stream[/bold underline] [dim]({len(reactor_logs)} lines)[/dim]\n")
                for ll in reactor_logs[-40:]:
                    lines.append(f"[dim]{ll}[/dim]")
            content.update("\n".join(lines) if lines else "[dim]No Reactor data yet...[/dim]")
        except Exception:
            pass


class EventsTab(VerticalScroll):
    DEFAULT_CSS = "EventsTab { padding: 1 2; }"

    def compose(self) -> ComposeResult:
        yield Static("Waiting for events...", id="events-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#events-content", Static)
            lines: List[str] = []
            if snap.events:
                for ev in snap.events[-100:]:
                    ts_str = time.strftime("%H:%M:%S", time.localtime(ev.get("timestamp", 0)))
                    sev = ev.get("severity", "info")
                    style = _SEVERITY_STYLE.get(sev, "")
                    sev_tag = f"[{style}]{sev.upper():8s}[/{style}]" if style else f"{sev.upper():8s}"
                    comp = f"[bold]{ev.get('component', '')}[/bold] " if ev.get("component") else ""
                    lines.append(f"[dim]{ts_str}[/dim] {sev_tag} {comp}{ev.get('message', '')}")
            if snap.captured_logs:
                if lines:
                    lines.append("\n[bold]─── Supervisor Output ───[/bold]")
                for ll in snap.captured_logs[-200:]:
                    lines.append(f"[dim]{ll}[/dim]")
            if lines:
                content.update("\n".join(lines))
                self.scroll_end(animate=False)
            else:
                content.update("[dim]No events yet[/dim]")
        except Exception:
            pass


class FaultsTab(VerticalScroll):
    DEFAULT_CSS = "FaultsTab { padding: 1 2; }"

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
                ts_str = time.strftime("%H:%M:%S", time.localtime(ev.get("timestamp", 0)))
                sev = ev.get("severity", "error")
                style = _SEVERITY_STYLE.get(sev, "bold red")
                comp = f"[bold]{ev.get('component', '')}[/bold] " if ev.get("component") else ""
                lines.append(f"[dim]{ts_str}[/dim] [{style}]{sev.upper():8s}[/{style}] {comp}{ev.get('message', '')}")
            content.update("\n".join(lines))
            self.scroll_end(animate=False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# JarvisTuiApp — main Textual application
# ---------------------------------------------------------------------------
class JarvisTuiApp(App):
    """JARVIS Supervisor TUI with 5 interactive tabs.

    When used as the main entry point (run_supervisor_tui), the supervisor
    startup runs as an async worker inside this app's event loop. Textual
    owns the main thread and has full terminal control.
    """

    TITLE = "JARVIS Supervisor"
    SUB_TITLE = "Enterprise TUI"

    DEFAULT_CSS = """
    Screen { background: $surface; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    Header { dock: top; }
    Footer { dock: bottom; }
    #elapsed-bar { dock: top; height: 1; background: $primary-background; padding: 0 2; }
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

    def __init__(
        self,
        cli_args: Optional["argparse.Namespace"] = None,
        captured_output: Optional[Deque[str]] = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._cli_args = cli_args
        self._captured_output: Deque[str] = captured_output or collections.deque(maxlen=_LOG_LIMIT)
        self._kernel: Any = None
        self._event_buf_lock = threading.Lock()
        self._events: Deque[dict] = collections.deque(maxlen=_EVENT_LIMIT)
        self._faults: Deque[dict] = collections.deque(maxlen=_FAULT_LIMIT)
        self._supervisor_exit_code: int = 1

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("⚡ JARVIS TUI | Starting supervisor... | Press 1-5 for tabs, q to quit", id="elapsed-bar")
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

    async def on_mount(self) -> None:
        """Start the supervisor as an async worker inside Textual's event loop."""
        if self._cli_args is not None:
            self.run_worker(self._run_supervisor(), name="supervisor", exclusive=True)
        self.set_interval(interval=_POLL_MS / 1000.0, callback=self._poll_snapshot)

    async def _run_supervisor(self) -> None:
        """Run the full supervisor startup inside Textual's event loop."""
        try:
            from unified_supervisor import async_main
            self._supervisor_exit_code = await async_main(self._cli_args)
        except SystemExit as e:
            self._supervisor_exit_code = e.code if isinstance(e.code, int) else 1
        except asyncio.CancelledError:
            self._supervisor_exit_code = 130
        except Exception as exc:
            self._captured_output.append(f"[FATAL] Supervisor error: {exc}")
            self._supervisor_exit_code = 1

    def _handle_event(self, event: Any) -> None:
        """Event bus handler — appends to ring buffers."""
        try:
            ev_dict = {
                "type": event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
                "timestamp": getattr(event, "timestamp", time.time()),
                "message": getattr(event, "message", ""),
                "severity": event.severity.value if hasattr(event.severity, "value") else str(getattr(event, "severity", "info")),
                "phase": getattr(event, "phase", ""),
                "component": getattr(event, "component", ""),
            }
            with self._event_buf_lock:
                self._events.append(ev_dict)
                if ev_dict["severity"] in _FAULT_SEVERITIES:
                    self._faults.append(ev_dict)
        except Exception:
            pass

    async def _poll_snapshot(self) -> None:
        """Periodic snapshot builder — reads dashboard + captured output."""
        try:
            from unified_supervisor import get_live_dashboard
            dashboard = get_live_dashboard()
        except Exception:
            dashboard = None

        state: Dict[str, Any] = {}
        if dashboard is not None:
            lock = getattr(dashboard, "_lock", None)
            try:
                if lock:
                    lock.acquire()
                state = dashboard._build_render_state()
            except Exception:
                state = {}
            finally:
                if lock:
                    try:
                        lock.release()
                    except RuntimeError:
                        pass

        ipc_data: dict = {}
        if self._kernel is None:
            try:
                from unified_supervisor import JarvisSystemKernel
                self._kernel = JarvisSystemKernel._instance
            except Exception:
                pass
        if self._kernel and hasattr(self._kernel, "_ipc_status"):
            try:
                ipc_data = await self._kernel._ipc_status()
            except Exception:
                pass

        # Wire event bus subscription (once)
        if self._kernel and not getattr(self, "_event_bus_wired", False):
            eb = getattr(self._kernel, "_event_bus", None)
            if eb:
                try:
                    eb.subscribe(self._handle_event)
                    self._event_bus_wired = True
                except Exception:
                    pass

        captured_snap = tuple(self._captured_output)
        with self._event_buf_lock:
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
        self._snapshot = snapshot

    def watch__snapshot(self, snapshot: Optional[TuiSnapshot]) -> None:
        if snapshot is None:
            return
        try:
            el = self.query_one("#elapsed-bar", Label)
            mins, secs = divmod(int(snapshot.elapsed), 60)
            el.update(
                f"⚡ JARVIS TUI | {mins}m {secs:02d}s | "
                f"Events: {len(snapshot.events)} | Faults: {len(snapshot.faults)} | "
                f"Logs: {len(snapshot.captured_logs)} | Press 1-5, q to quit"
            )
        except Exception:
            pass
        for tab_cls in (SupervisorTab, PrimeTab, ReactorTab, EventsTab, FaultsTab):
            try:
                self.query_one(tab_cls).update_from_snapshot(snapshot)
            except Exception:
                pass
        try:
            self._fault_count = len(snapshot.faults)
        except Exception:
            pass

    def watch__fault_count(self, count: int) -> None:
        try:
            pane = self.query_one("#tabs", TabbedContent).query_one("#tab-faults", TabPane)
            pane.label = f"Faults ({count})" if count > 0 else "Faults"
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
            self.query_one("#tabs", TabbedContent).active = tab_id
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TuiCliRenderer — CliRenderer-compatible (fallback/no-op for non-TUI paths)
# ---------------------------------------------------------------------------
class TuiCliRenderer:
    """Stub renderer returned by _create_cli_renderer('tui').

    When the TUI is the main app (run_supervisor_tui), data flows through
    JarvisTuiApp._poll_snapshot directly — not through this renderer.
    This class exists only to satisfy the CliRenderer interface contract
    so the supervisor's event subscription and attach calls don't crash.
    """

    def __init__(self, verbosity: str = "ops"):
        self._verbosity = verbosity

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def handle_event(self, _event: Any) -> None:
        pass

    def should_display(self, _event: Any) -> bool:
        return False

    def attach_to_supervisor(self, _supervisor: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Entry point — called from main() when --ui tui
# ---------------------------------------------------------------------------
def run_supervisor_tui(args: "argparse.Namespace") -> int:
    """Run the JARVIS supervisor inside the Textual TUI.

    Textual owns the main thread and event loop. The supervisor startup
    runs as an async worker inside the TUI's event loop.

    Returns the supervisor exit code.
    """
    captured_output: Deque[str] = collections.deque(maxlen=_LOG_LIMIT)

    # Redirect stdout so supervisor banners/prints go to capture buffer.
    # Textual uses its own internal console (not sys.stdout) for rendering.
    orig_stdout = sys.stdout
    sys.stdout = _OutputCapture(captured_output, orig_stdout)

    # Replace root logger handlers with buffer handler so log messages
    # go to the TUI instead of cluttering the terminal.
    root_logger = logging.getLogger()
    orig_handlers = root_logger.handlers[:]
    tui_handler = _TuiLogHandler(captured_output)
    for h in orig_handlers:
        root_logger.removeHandler(h)
    root_logger.addHandler(tui_handler)

    exit_code = 1
    try:
        app = JarvisTuiApp(cli_args=args, captured_output=captured_output)
        app.run()
        exit_code = app._supervisor_exit_code
    except Exception as exc:
        sys.stderr.write(f"TUI fatal error: {exc}\n")
        exit_code = 1
    finally:
        # Restore stdout and logging
        sys.stdout = orig_stdout
        if tui_handler in root_logger.handlers:
            root_logger.removeHandler(tui_handler)
        for h in orig_handlers:
            if h not in root_logger.handlers:
                root_logger.addHandler(h)

    return exit_code
