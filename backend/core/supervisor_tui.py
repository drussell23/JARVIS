"""
JARVIS Supervisor TUI — Textual-based terminal UI with 5 tabs.

Architecture (v3 — daemon thread decoupling):
  ROOT CAUSE of tab freezing in v2: supervisor ran as a Textual worker INSIDE
  Textual's event loop. Any synchronous section in the supervisor (module
  imports, kernel constructor, native C ops, GIL contention) blocked Textual's
  input processing — tabs couldn't switch, keys weren't processed.

  v3 fix: Supervisor runs in a DAEMON THREAD with its OWN asyncio event loop.
  Textual keeps the main thread. Neither can block the other.

  Data bridge:
    - Dashboard reads: non-blocking trylock on dashboard._lock from Textual's thread
    - IPC status: asyncio.run_coroutine_threadsafe() submits to supervisor's loop,
      result checked non-blocking on next poll cycle (no blocking on Textual's loop)
    - Events: SupervisorEventBus.subscribe() — thread-safe, callbacks fire on
      emitting thread, append to thread-safe deque
    - Logs: _OutputCapture + _TuiLogHandler write to shared deque (thread-safe)

  Shutdown coordination:
    - User presses 'q' → action_quit() →
      loop.call_soon_threadsafe(kernel._shutdown_event.set)
    - Supervisor detects shutdown → async_main() returns → daemon thread exits
    - Textual exits after join(timeout=5)

  Crash isolation: Daemon thread crash → thread dies, Textual stays up showing
  logs and error. Textual crash → daemon=True, thread dies with process.

Env vars:
  JARVIS_TUI_POLL_MS       = int   (default: 500)  — snapshot poll interval
  JARVIS_TUI_IPC_INTERVAL  = float (default: 3.0)  — IPC refresh interval (seconds)
  JARVIS_TUI_EVENT_LIMIT   = int   (default: 500)  — max events in ring
  JARVIS_TUI_FAULT_LIMIT   = int   (default: 200)  — max faults in ring
  JARVIS_TUI_LOG_LIMIT     = int   (default: 3000) — max captured log lines
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import copy
import io
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, TYPE_CHECKING

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

if TYPE_CHECKING:
    import argparse

logger = logging.getLogger(__name__)

_POLL_MS = int(os.environ.get("JARVIS_TUI_POLL_MS", "500"))
_IPC_INTERVAL = float(os.environ.get("JARVIS_TUI_IPC_INTERVAL", "3.0"))
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

# Broad keyword sets — catch real log lines
_PRIME_KEYWORDS = frozenset({
    "prime", "j-prime", "jprime", "jarvis-prime", "jarvis_prime",
    "llm", "model_load", "model loading", "model_loading", "inference",
    "prewarm", "trinity_integrator", "trinity integrator", "trinity",
    "gcp", "invincible", "endpoint", "prime_router", "prime_client",
    "hollowclient", "hollow_client", "apars", "golden_image",
    "model_serving", "unified_model", "gguf", "llama", "metal",
    "quantiz", "mmap", "offload", "routing_decision", "warm",
})

_REACTOR_KEYWORDS = frozenset({
    "reactor", "reactor-core", "reactor_core", "training",
    "nightshift", "night_shift", "scout", "mlforge",
    "curriculum", "federated", "checkpoint", "ecapa",
    "voice_unlock", "voice unlock", "speaker", "biometric",
    "two_tier", "two-tier", "agi_os", "agi os", "sidecar",
    "readiness", "voice_sidecar", "approval",
})


# ---------------------------------------------------------------------------
# Output capture — redirect stdout + logging to TUI buffer
# ---------------------------------------------------------------------------
class _OutputCapture:
    """File-like object that captures writes to a deque (thread-safe)."""

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
    """Logging handler that writes formatted records to a deque (thread-safe)."""

    def __init__(self, buffer: Deque[str]):
        super().__init__()
        self._buffer = buffer
        self.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
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
    supervisor_alive: bool = True


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
    SupervisorTab DataTable { height: auto; max-height: 22; }
    SupervisorTab .gauge-row { height: 3; margin-top: 1; }
    SupervisorTab .gauge-label { width: 14; }
    SupervisorTab #supervisor-logs { height: auto; max-height: 18; margin-top: 1; }
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
        yield Label("Live Output", classes="section-label")
        yield Static("[dim]Waiting for output...[/dim]", id="supervisor-logs")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            table = self.query_one("#comp-table", DataTable)
            new_count = sum(1 + len(g.get("members", [])) for g in snap.groups)
            if table.row_count != new_count or new_count == 0:
                table.clear()
                for group in snap.groups:
                    table.add_row(
                        group.get("emoji", ""),
                        f"[bold]{group.get('label', '')}[/bold]", "", "",
                    )
                    for member in group.get("members", []):
                        style = _STATUS_STYLE.get(member.get("status", ""), "")
                        st = member.get("status", "")
                        st = f"[{style}]{st}[/{style}]" if style else st
                        table.add_row(
                            member.get("emoji", ""),
                            f"  {member.get('name', '')}",
                            st,
                            member.get("code", ""),
                        )
        except Exception:
            logger.debug("TUI: SupervisorTab table update failed", exc_info=True)
        try:
            gcp = snap.gcp
            pct = gcp.get("progress", 0)
            self.query_one("#gcp-bar", ProgressBar).update(progress=float(pct))
            self.query_one("#gcp-pct", Label).update(
                f" {pct:.0f}% {gcp.get('phase_name', '')}"
            )
        except Exception:
            pass
        try:
            mem = snap.memory
            # Dashboard key is "percent" — NOT "used_pct"
            used_pct = mem.get("percent", 0)
            self.query_one("#mem-bar", ProgressBar).update(progress=float(used_pct))
            self.query_one("#mem-pct", Label).update(
                f" {used_pct:.0f}% "
                f"({mem.get('used_gb', 0):.1f}/{mem.get('total_gb', 0):.1f} GB)"
            )
        except Exception:
            pass
        try:
            if snap.captured_logs:
                self.query_one("#supervisor-logs", Static).update(
                    "\n".join(f"[dim]{ll}[/dim]" for ll in snap.captured_logs[-25:])
                )
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
                lines.append(
                    "[bold underline]JARVIS Prime — Live Status[/bold underline]\n"
                )
                lines.append(f"[bold]State:[/bold] {ipc.get('state', 'unknown')}")
                lines.append(
                    f"[bold]Uptime:[/bold] {ipc.get('uptime_seconds', 0):.0f}s"
                )
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
                        lines.append(
                            f"  {k}: ..." if isinstance(v, dict) else f"  {k}: {v}"
                        )
                inv = ipc.get("invincible_node", {})
                if inv and inv.get("enabled"):
                    lines.append("\n[bold]Invincible Node (GCP)[/bold]")
                    lines.append(
                        f"  Instance: {inv.get('instance_name', 'N/A')}"
                    )
                    lines.append(f"  Status: {inv.get('status', 'N/A')}")
                model = snap.model
                if model and model.get("active"):
                    lines.append("\n[bold]Model Loading[/bold]")
                    lines.append(f"  Model: {model.get('model_name', 'N/A')}")
                    lines.append(
                        f"  Progress: {model.get('progress', 0):.0f}%"
                    )
                modes = ipc.get("startup_modes", {})
                if modes:
                    lines.append("\n[bold]Startup Modes[/bold]")
                    for k in (
                        "desired_mode", "effective_mode",
                        "cloud_recovery_candidate",
                    ):
                        lines.append(f"  {k}: {modes.get(k, 'N/A')}")
            # Keyword-filtered Prime logs
            prime_logs = _filter_logs(snap.captured_logs, _PRIME_KEYWORDS, limit=80)
            if prime_logs:
                lines.append(
                    f"\n[bold underline]Prime Log Stream[/bold underline] "
                    f"[dim]({len(prime_logs)} lines)[/dim]\n"
                )
                for ll in prime_logs[-50:]:
                    lines.append(f"[dim]{ll}[/dim]")
            elif snap.captured_logs:
                # Fallback: show recent general output
                lines.append(
                    "\n[bold underline]Recent Output[/bold underline] "
                    "[dim](no Prime-specific logs yet)[/dim]\n"
                )
                for ll in snap.captured_logs[-25:]:
                    lines.append(f"[dim]{ll}[/dim]")
            content.update(
                "\n".join(lines) if lines else "[dim]No Prime data yet...[/dim]"
            )
        except Exception:
            logger.debug("TUI: PrimeTab update failed", exc_info=True)


class ReactorTab(VerticalScroll):
    DEFAULT_CSS = "ReactorTab { padding: 1 2; }"

    def compose(self) -> ComposeResult:
        yield Static(
            "[dim]Waiting for Reactor data...[/dim]", id="reactor-content"
        )

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#reactor-content", Static)
            lines: List[str] = []
            ipc = snap.ipc_status
            if ipc:
                lines.append(
                    "[bold underline]Reactor Core — Live Status"
                    "[/bold underline]\n"
                )
                two_tier = ipc.get("two_tier", {})
                if two_tier:
                    lines.append("[bold]Two-Tier Security[/bold]")
                    lines.append(
                        f"  Enabled: {two_tier.get('enabled', False)}"
                    )
                    lines.append(
                        f"  Runner Wired: {two_tier.get('runner_wired', False)}"
                    )
                ecapa = ipc.get("ecapa_policy", {})
                if ecapa and not ecapa.get("error"):
                    lines.append("\n[bold]ECAPA Policy[/bold]")
                    lines.append(f"  Mode: {ecapa.get('mode', 'N/A')}")
                    lines.append(
                        f"  Backend: {ecapa.get('active_backend', 'N/A')}"
                    )
                    lines.append(
                        f"  Failures: {ecapa.get('consecutive_failures', 0)}"
                    )
                agi = ipc.get("agi_os", {})
                if agi:
                    lines.append("\n[bold]AGI OS[/bold]")
                    lines.append(f"  Enabled: {agi.get('enabled', False)}")
                    lines.append(f"  Status: {agi.get('status', 'N/A')}")
                voice = ipc.get("voice_sidecar", {})
                if voice:
                    lines.append("\n[bold]Voice Sidecar[/bold]")
                    lines.append(
                        f"  Enabled: {voice.get('enabled', False)}"
                    )
                    lines.append(
                        f"  Transport: {voice.get('transport', 'N/A')}"
                    )
                readiness = ipc.get("readiness", {})
                if readiness:
                    lines.append("\n[bold]Readiness[/bold]")
                    for k, v in list(readiness.items())[:10]:
                        lines.append(f"  {k}: {v}")
            reactor_logs = _filter_logs(
                snap.captured_logs, _REACTOR_KEYWORDS, limit=80
            )
            if reactor_logs:
                lines.append(
                    f"\n[bold underline]Reactor Log Stream[/bold underline] "
                    f"[dim]({len(reactor_logs)} lines)[/dim]\n"
                )
                for ll in reactor_logs[-50:]:
                    lines.append(f"[dim]{ll}[/dim]")
            elif snap.captured_logs:
                lines.append(
                    "\n[bold underline]Recent Output[/bold underline] "
                    "[dim](no Reactor-specific logs yet)[/dim]\n"
                )
                for ll in snap.captured_logs[-25:]:
                    lines.append(f"[dim]{ll}[/dim]")
            content.update(
                "\n".join(lines)
                if lines
                else "[dim]No Reactor data yet...[/dim]"
            )
        except Exception:
            logger.debug("TUI: ReactorTab update failed", exc_info=True)


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
                    ts_str = time.strftime(
                        "%H:%M:%S", time.localtime(ev.get("timestamp", 0))
                    )
                    sev = ev.get("severity", "info")
                    style = _SEVERITY_STYLE.get(sev, "")
                    sev_tag = (
                        f"[{style}]{sev.upper():8s}[/{style}]"
                        if style
                        else f"{sev.upper():8s}"
                    )
                    comp = (
                        f"[bold]{ev.get('component', '')}[/bold] "
                        if ev.get("component")
                        else ""
                    )
                    lines.append(
                        f"[dim]{ts_str}[/dim] {sev_tag} "
                        f"{comp}{ev.get('message', '')}"
                    )
            if snap.captured_logs:
                if lines:
                    lines.append("\n[bold]--- Supervisor Output ---[/bold]")
                for ll in snap.captured_logs[-150:]:
                    lines.append(f"[dim]{ll}[/dim]")
            if lines:
                content.update("\n".join(lines))
                self.scroll_end(animate=False)
            else:
                content.update("[dim]No events yet[/dim]")
        except Exception:
            logger.debug("TUI: EventsTab update failed", exc_info=True)


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
                ts_str = time.strftime(
                    "%H:%M:%S", time.localtime(ev.get("timestamp", 0))
                )
                sev = ev.get("severity", "error")
                style = _SEVERITY_STYLE.get(sev, "bold red")
                comp = (
                    f"[bold]{ev.get('component', '')}[/bold] "
                    if ev.get("component")
                    else ""
                )
                lines.append(
                    f"[dim]{ts_str}[/dim] [{style}]{sev.upper():8s}[/{style}] "
                    f"{comp}{ev.get('message', '')}"
                )
            content.update("\n".join(lines))
            self.scroll_end(animate=False)
        except Exception:
            logger.debug("TUI: FaultsTab update failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tab ID → widget class map for selective update
# ---------------------------------------------------------------------------
_TAB_ID_TO_CLS = {
    "tab-supervisor": SupervisorTab,
    "tab-prime": PrimeTab,
    "tab-reactor": ReactorTab,
    "tab-events": EventsTab,
    "tab-faults": FaultsTab,
}


# ---------------------------------------------------------------------------
# JarvisTuiApp — main Textual application (v3: daemon thread architecture)
# ---------------------------------------------------------------------------
class JarvisTuiApp(App):
    """JARVIS Supervisor TUI with 5 interactive tabs.

    v3 architecture: Textual owns the main thread + event loop.
    The supervisor runs in a DAEMON THREAD with its own asyncio event loop.
    Neither can block the other.
    """

    TITLE = "JARVIS Supervisor"
    SUB_TITLE = "Enterprise TUI"

    DEFAULT_CSS = """
    Screen { background: $surface; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    Header { dock: top; }
    Footer { dock: bottom; }
    #elapsed-bar {
        dock: top; height: 1;
        background: $primary-background; padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("1", "tab_1", "Supervisor", priority=True),
        Binding("2", "tab_2", "Prime", priority=True),
        Binding("3", "tab_3", "Reactor", priority=True),
        Binding("4", "tab_4", "Events", priority=True),
        Binding("5", "tab_5", "Faults", priority=True),
        Binding("r", "refresh", "Refresh", priority=True),
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
        self._captured_output: Deque[str] = (
            captured_output or collections.deque(maxlen=_LOG_LIMIT)
        )
        # Supervisor thread state
        self._supervisor_thread: Optional[threading.Thread] = None
        self._supervisor_loop: Optional[asyncio.AbstractEventLoop] = None
        self._supervisor_exit_code: int = 1
        # Kernel reference (set when kernel singleton becomes available)
        self._kernel: Any = None
        # Event bus buffers (thread-safe via deque + lock)
        self._event_buf_lock = threading.Lock()
        self._events: Deque[dict] = collections.deque(maxlen=_EVENT_LIMIT)
        self._faults: Deque[dict] = collections.deque(maxlen=_FAULT_LIMIT)
        self._event_bus_wired = False
        # IPC status cache — refreshed every _IPC_INTERVAL via non-blocking future
        self._cached_ipc: dict = {}
        self._ipc_future: Optional[concurrent.futures.Future] = None
        self._ipc_last_submit: float = 0.0
        # Dashboard cache — reused when lock is contended
        self._last_dashboard_state: Dict[str, Any] = {}

    # ── Compose ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            "JARVIS TUI | Starting supervisor... | Press 1-5 for tabs, q to quit",
            id="elapsed-bar",
        )
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

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        """Start supervisor in daemon thread, start poll timer."""
        self._captured_output.append(
            "[TUI] Textual app mounted — launching supervisor in daemon thread..."
        )
        if self._cli_args is not None:
            self._supervisor_thread = threading.Thread(
                target=self._run_supervisor_thread,
                name="jarvis-supervisor",
                daemon=True,
            )
            self._supervisor_thread.start()
        self.set_interval(interval=_POLL_MS / 1000.0, callback=self._poll_snapshot)

    def _run_supervisor_thread(self) -> None:
        """Supervisor entry point — runs in a daemon thread with its own loop.

        This thread is completely decoupled from Textual's event loop.
        Module-level imports (numpy, aiohttp, etc.) run here and cannot
        block Textual's input processing.
        """
        loop = asyncio.new_event_loop()
        self._supervisor_loop = loop
        asyncio.set_event_loop(loop)
        self._captured_output.append(
            "[TUI] Supervisor thread started — importing async_main..."
        )
        try:
            from unified_supervisor import async_main

            self._captured_output.append(
                "[TUI] async_main imported — starting supervisor startup..."
            )
            exit_code = loop.run_until_complete(async_main(self._cli_args))
            self._supervisor_exit_code = (
                exit_code if isinstance(exit_code, int) else 1
            )
            self._captured_output.append(
                f"[TUI] async_main() completed: exit_code={self._supervisor_exit_code}"
            )
        except SystemExit as e:
            self._supervisor_exit_code = e.code if isinstance(e.code, int) else 1
            self._captured_output.append(
                f"[TUI] SystemExit: code={self._supervisor_exit_code}"
            )
        except Exception as exc:
            self._captured_output.append(
                f"[FATAL] Supervisor error: {type(exc).__name__}: {exc}"
            )
            for line in traceback.format_exc().splitlines():
                self._captured_output.append(f"  {line}")
            self._supervisor_exit_code = 1
        finally:
            try:
                # Replicate asyncio.run() cleanup
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
                if hasattr(loop, "shutdown_default_executor"):
                    loop.run_until_complete(loop.shutdown_default_executor())
            except Exception:
                pass
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                self._supervisor_loop = None
                self._captured_output.append("[TUI] Supervisor thread exiting")

    # ── Quit with graceful supervisor shutdown ───────────────────────────

    def action_quit(self) -> None:
        """Quit TUI — signal supervisor to shut down, then exit."""
        self._captured_output.append("[TUI] Quit requested — shutting down supervisor...")
        self._signal_supervisor_shutdown()
        # Join supervisor thread with timeout (don't block indefinitely)
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            self._supervisor_thread.join(timeout=5.0)
        super().action_quit()

    def _signal_supervisor_shutdown(self) -> None:
        """Signal the supervisor's asyncio event loop to shut down.

        Uses call_soon_threadsafe to set the shutdown event from Textual's
        thread onto the supervisor's event loop — the correct cross-thread
        asyncio primitive.
        """
        sup_loop = self._supervisor_loop
        if not sup_loop:
            return
        kernel = self._kernel
        if kernel is None:
            return
        # Set kernel shutdown event
        se = getattr(kernel, "_shutdown_event", None)
        if se:
            try:
                sup_loop.call_soon_threadsafe(se.set)
            except RuntimeError:
                pass  # Loop already closed
        # Set signal handler shutdown
        sh = getattr(kernel, "_signal_handler", None)
        if sh:
            sh._shutdown_requested = True  # bool assignment — GIL-safe
            se2 = getattr(sh, "_shutdown_event", None)
            if se2:
                try:
                    sup_loop.call_soon_threadsafe(se2.set)
                except RuntimeError:
                    pass

    # ── Event bus handler ────────────────────────────────────────────────

    def _handle_event(self, event: Any) -> None:
        """Event bus callback — called from supervisor thread, appends to deque."""
        try:
            ev_dict = {
                "type": (
                    event.event_type.value
                    if hasattr(event.event_type, "value")
                    else str(event.event_type)
                ),
                "timestamp": getattr(event, "timestamp", time.time()),
                "message": getattr(event, "message", ""),
                "severity": (
                    event.severity.value
                    if hasattr(event.severity, "value")
                    else str(getattr(event, "severity", "info"))
                ),
                "phase": getattr(event, "phase", ""),
                "component": getattr(event, "component", ""),
            }
            with self._event_buf_lock:
                self._events.append(ev_dict)
                if ev_dict["severity"] in _FAULT_SEVERITIES:
                    self._faults.append(ev_dict)
        except Exception:
            pass

    # ── Snapshot polling (runs on Textual's event loop) ──────────────────

    async def _poll_snapshot(self) -> None:
        """Build a TuiSnapshot from dashboard + IPC + events + logs.

        Thread-safety guarantees:
        1. Dashboard lock: non-blocking trylock — if contended, reuse cache
        2. IPC status: fire-and-forget future on supervisor's loop, check
           result non-blocking on next cycle. NEVER blocks Textual's loop.
        3. Event buffers: deque + threading.Lock — safe for cross-thread
        4. Captured logs: deque — thread-safe for append + tuple()
        """
        # --- Dashboard state (non-blocking trylock) ---
        state: Dict[str, Any] = self._last_dashboard_state
        try:
            from unified_supervisor import get_live_dashboard

            dashboard = get_live_dashboard()
        except Exception:
            dashboard = None

        if dashboard is not None:
            lock = getattr(dashboard, "_lock", None)
            acquired = False
            try:
                if lock:
                    acquired = lock.acquire(blocking=False)
                    if acquired:
                        state = dashboard._build_render_state()
                        self._last_dashboard_state = state
                    # else: reuse cached state — don't block
                else:
                    state = dashboard._build_render_state()
                    self._last_dashboard_state = state
            except Exception:
                pass
            finally:
                if acquired and lock:
                    try:
                        lock.release()
                    except RuntimeError:
                        pass

        # --- Discover kernel (lazy, once) ---
        if self._kernel is None:
            try:
                from unified_supervisor import JarvisSystemKernel

                self._kernel = JarvisSystemKernel._instance
            except Exception:
                pass

        # --- IPC status (non-blocking fire-and-forget) ---
        now = time.monotonic()
        # Check if previous IPC future completed
        if self._ipc_future is not None and self._ipc_future.done():
            try:
                result = self._ipc_future.result()
                if result:
                    self._cached_ipc = copy.deepcopy(result)
            except Exception:
                pass  # Keep stale cache
            self._ipc_future = None
        # Submit new IPC call if interval elapsed
        if (
            self._ipc_future is None
            and now - self._ipc_last_submit >= _IPC_INTERVAL
        ):
            sup_loop = self._supervisor_loop
            if sup_loop and self._kernel and hasattr(self._kernel, "_ipc_status"):
                try:
                    self._ipc_future = asyncio.run_coroutine_threadsafe(
                        self._kernel._ipc_status(), sup_loop
                    )
                    self._ipc_last_submit = now
                except RuntimeError:
                    pass  # Loop closed

        # --- Wire event bus (once) ---
        if self._kernel and not self._event_bus_wired:
            eb = getattr(self._kernel, "_event_bus", None)
            if eb:
                try:
                    eb.subscribe(self._handle_event)
                    self._event_bus_wired = True
                except Exception:
                    pass

        # --- Build snapshot ---
        captured_snap = tuple(self._captured_output)
        with self._event_buf_lock:
            events_snap = tuple(self._events)
            faults_snap = tuple(self._faults)

        thread_alive = (
            self._supervisor_thread is not None
            and self._supervisor_thread.is_alive()
        )

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
            ipc_status=copy.deepcopy(self._cached_ipc),
            captured_logs=(
                captured_snap[-500:] if len(captured_snap) > 500 else captured_snap
            ),
            supervisor_alive=thread_alive,
        )
        self._snapshot = snapshot

    # ── Snapshot watcher — update active tab only ────────────────────────

    def watch__snapshot(self, snapshot: Optional[TuiSnapshot]) -> None:
        if snapshot is None:
            return
        # Update status bar
        try:
            el = self.query_one("#elapsed-bar", Label)
            mins, secs = divmod(int(snapshot.elapsed), 60)
            alive = "LIVE" if snapshot.supervisor_alive else "EXITED"
            el.update(
                f"JARVIS TUI | {alive} | {mins}m {secs:02d}s | "
                f"Events: {len(snapshot.events)} | "
                f"Faults: {len(snapshot.faults)} | "
                f"Logs: {len(snapshot.captured_logs)} | 1-5 tabs, q quit"
            )
        except Exception:
            pass
        # Only update the ACTIVE tab — saves CPU, prevents lag
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            active_id = tabs.active
            tab_cls = _TAB_ID_TO_CLS.get(active_id)
            if tab_cls:
                self.query_one(tab_cls).update_from_snapshot(snapshot)
        except Exception:
            # Fallback: update all
            for tab_cls in _TAB_ID_TO_CLS.values():
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
            pane = self.query_one("#tabs", TabbedContent).query_one(
                "#tab-faults", TabPane
            )
            pane.label = f"Faults ({count})" if count > 0 else "Faults"
        except Exception:
            pass

    # ── Tab switching ────────────────────────────────────────────────────

    _TAB_IDS = (
        "tab-supervisor",
        "tab-prime",
        "tab-reactor",
        "tab-events",
        "tab-faults",
    )
    _KEY_TO_TAB = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 4}

    def on_key(self, event: Any) -> None:
        """Direct key handler — instant tab switching."""
        idx = self._KEY_TO_TAB.get(event.key)
        if idx is not None:
            self._switch_tab(self._TAB_IDS[idx])
            event.prevent_default()
            event.stop()

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
        """Force refresh active tab from current snapshot."""
        if self._snapshot is not None:
            try:
                tabs = self.query_one("#tabs", TabbedContent)
                tab_cls = _TAB_ID_TO_CLS.get(tabs.active)
                if tab_cls:
                    self.query_one(tab_cls).update_from_snapshot(self._snapshot)
            except Exception:
                pass

    def _switch_tab(self, tab_id: str) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = tab_id
            # Immediately render the target tab with current snapshot
            if self._snapshot is not None:
                tab_cls = _TAB_ID_TO_CLS.get(tab_id)
                if tab_cls:
                    self.query_one(tab_cls).update_from_snapshot(self._snapshot)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TuiCliRenderer — CliRenderer-compatible no-op stub
# ---------------------------------------------------------------------------
class TuiCliRenderer:
    """Stub renderer returned by _create_cli_renderer('tui').

    In v3, data flows through JarvisTuiApp._poll_snapshot on Textual's loop.
    This stub satisfies the CliRenderer interface so the supervisor's event
    subscription and attach calls don't crash.
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

    Textual owns the main thread and its event loop.
    The supervisor runs in a daemon thread with its own asyncio event loop.
    Neither can block the other.

    Returns the supervisor exit code.
    """
    captured_output: Deque[str] = collections.deque(maxlen=_LOG_LIMIT)

    # Redirect stdout — supervisor prints go to capture buffer.
    # Textual uses its own internal console for rendering.
    orig_stdout = sys.stdout
    sys.stdout = _OutputCapture(captured_output, orig_stdout)

    # Replace root logger handlers with buffer handler
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
