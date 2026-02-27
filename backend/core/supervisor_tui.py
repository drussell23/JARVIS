"""
JARVIS Supervisor TUI — Textual-based terminal UI with 5 tabs.

Architecture (v4 — log transparency + non-blocking snapshot):

  v3 daemon thread architecture (PRESERVED):
    Supervisor runs in a DAEMON THREAD with its own asyncio event loop.
    Textual owns the main thread. Neither can block the other.

  v4 fixes two remaining issues:

  1. LOG TRANSPARENCY (root cause: Textual overrides sys.stdout)
     v3 relied on redirecting sys.stdout to _OutputCapture. But Textual's
     app.run() takes over sys.stdout for its own rendering — our redirect
     gets overridden. All supervisor output via print()/Rich Console goes
     into Textual's void. Only 3 lines captured (direct deque.append() calls).

     v4 fix: TWO independent capture bridges that don't depend on stdout:
       Bridge 1: _TuiLogHandler on root logger (level=INFO, root level=INFO)
                 Captures all Python logging from subsystem loggers.
       Bridge 2: UnifiedLogger monkey-patch — after daemon thread imports
                 unified_supervisor, we intercept UnifiedLogger._log() to
                 ALSO write to our buffer. This captures the 70%+ of output
                 that goes through the custom UnifiedLogger (Rich Console).
     Both bridges write to the same thread-safe deque. Neither depends on
     sys.stdout ownership.

  2. TAB SWITCHING FREEZE (root cause: synchronous copy.deepcopy on event loop)
     v3's _poll_snapshot() ran copy.deepcopy() of state dicts, tuple() of
     3000-element deques, and _build_render_state() — ALL synchronously on
     Textual's event loop. Under 16GB memory pressure, page faults from
     deepcopy blocked input processing for 500ms+.

     v4 fix: ALL heavy work moved to ThreadPoolExecutor via run_in_executor().
     _poll_snapshot() is async — it yields to Textual's event loop while the
     executor thread does deepcopy/tuple/state building. Textual can process
     key events and render while snapshot builds in background.
     Coalescing guard (_snapshot_building) prevents executor queue growth.

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
import logging
import os
import re
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

# Regex to strip ANSI escape codes from captured output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

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
# Log capture bridges — survive Textual's stdout takeover
# ---------------------------------------------------------------------------
class _TuiLogHandler(logging.Handler):
    """Python logging handler that writes formatted records to TUI buffer.

    Installed on the root logger at level=INFO. Survives Textual's stdout
    redirect because it writes directly to a deque, not stdout.

    v4: Includes a _marker attribute so we can detect and re-install it
    if the supervisor's logging setup removes it.
    """

    _TUI_HANDLER_MARKER = "_jarvis_tui_handler"

    def __init__(self, buffer: Deque[str]):
        super().__init__(level=logging.INFO)
        self._buffer = buffer
        self._jarvis_tui_handler = True  # Marker for re-installation detection
        self.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
        except Exception:
            pass


def _install_python_logging_bridge(buffer: Deque[str]) -> _TuiLogHandler:
    """Install _TuiLogHandler on root logger with correct level.

    Sets root logger level to INFO so INFO+ records flow through to handlers.
    Does NOT remove existing handlers (they write to terminal/files which
    Textual suppresses anyway — harmless).
    """
    root = logging.getLogger()
    # Ensure root level allows INFO through (default is WARNING=30)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    handler = _TuiLogHandler(buffer)
    root.addHandler(handler)
    return handler


def _ensure_tui_handler_installed(handler: _TuiLogHandler) -> None:
    """Re-install TUI handler if the supervisor's logging setup removed it."""
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_jarvis_tui_handler", False):
            return  # Still installed
    # Handler was removed — re-install
    root.addHandler(handler)
    # Ensure root level is still INFO
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


def _patch_unified_logger(buffer: Deque[str]) -> bool:
    """Monkey-patch UnifiedLogger._log to also write to TUI buffer.

    Called from daemon thread AFTER unified_supervisor is imported.
    UnifiedLogger writes to _rich_console.print() or print(), both of which
    go to sys.stdout — which Textual owns. This patch captures the log
    content DIRECTLY into our buffer, bypassing stdout entirely.

    Thread-safe: deque.append() is atomic under GIL.

    Returns True if patch was applied.
    """
    try:
        from unified_supervisor import UnifiedLogger
        ul = UnifiedLogger._instance
        if ul is None:
            return False

        # Check if already patched
        if getattr(ul, "_tui_patched", False):
            return True

        # Grab bound method BEFORE patching
        orig_log = ul._log

        def _tui_intercepted_log(level, message, **kwargs):
            """Interceptor: calls original _log, then also writes to TUI buffer."""
            # Call original (writes to Rich Console / print — goes to Textual void)
            orig_log(level, message, **kwargs)
            # Also capture to TUI buffer (bypasses stdout entirely)
            try:
                level_name = (
                    level.value[0]
                    if hasattr(level, "value")
                    else str(level)
                )
                elapsed = ul._elapsed_ms()
                # Strip Rich markup tags for clean display
                clean = _ANSI_RE.sub("", message)
                buffer.append(
                    f"{level_name:8} +{elapsed:>7.0f}ms | {clean}"
                )
            except Exception:
                pass

        # Replace instance method (bound method in instance __dict__)
        # When self.info() calls self._log(level, msg), Python finds
        # _tui_intercepted_log in instance dict — called as plain function
        # with (level, message) args. orig_log already has self bound.
        ul._log = _tui_intercepted_log
        ul._tui_patched = True
        buffer.append("[TUI] UnifiedLogger capture bridge installed")
        return True
    except Exception as exc:
        buffer.append(f"[TUI] UnifiedLogger patch failed: {exc}")
        return False


def _patch_rich_console(buffer: Deque[str]) -> bool:
    """Redirect the module-level _rich_console to also write to TUI buffer.

    Creates a StringIO tee that captures Rich Console output. This catches
    section headers/footers and other direct _rich_console.print() calls
    that don't go through UnifiedLogger._log.

    Returns True if patch was applied.
    """
    try:
        import unified_supervisor as us
        console = getattr(us, "_rich_console", None)
        if console is None:
            return False
        if getattr(console, "_tui_patched", False):
            return True

        class _ConsoleTee:
            """File-like tee: writes to buffer, ignores terminal output."""

            def __init__(self, buf: Deque[str]):
                self._buf = buf

            def write(self, s: str) -> int:
                if s and s.strip():
                    for line in s.splitlines():
                        stripped = line.rstrip()
                        if stripped:
                            # Strip ANSI codes for clean TUI display
                            clean = _ANSI_RE.sub("", stripped)
                            if clean.strip():
                                self._buf.append(clean)
                return len(s) if s else 0

            def flush(self) -> None:
                pass

            def isatty(self) -> bool:
                return True  # Tell Rich to use colors (we strip them anyway)

            @property
            def encoding(self) -> str:
                return "utf-8"

        # Replace the console's file with our tee
        console._file = _ConsoleTee(buffer)
        console._tui_patched = True
        buffer.append("[TUI] Rich Console capture bridge installed")
        return True
    except Exception as exc:
        buffer.append(f"[TUI] Rich Console patch failed: {exc}")
        return False


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
            pass
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
                    "\n".join(snap.captured_logs[-25:])
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
            pass


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
            pass


class FaultsTab(VerticalScroll):
    DEFAULT_CSS = "FaultsTab { padding: 1 2; }"

    def compose(self) -> ComposeResult:
        yield Static("[green]No faults detected[/green]", id="faults-content")

    def update_from_snapshot(self, snap: TuiSnapshot) -> None:
        try:
            content = self.query_one("#faults-content", Static)
            # Also include ERROR/WARNING/CRITICAL from captured logs
            fault_logs: List[str] = []
            for ll in snap.captured_logs:
                lower = ll.lower()
                if any(kw in lower for kw in ("error", "critical", "warning", "fatal", "exception", "traceback")):
                    fault_logs.append(ll)

            if not snap.faults and not fault_logs:
                content.update("[green]No faults detected[/green]")
                return
            lines: List[str] = []
            # Event bus faults
            if snap.faults:
                lines.append("[bold underline]Event Bus Faults[/bold underline]\n")
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
            # Log-extracted faults
            if fault_logs:
                if lines:
                    lines.append("")
                lines.append(
                    f"[bold underline]Log Faults[/bold underline] "
                    f"[dim]({len(fault_logs)} entries)[/dim]\n"
                )
                for ll in fault_logs[-100:]:
                    lines.append(f"[bold red]{ll}[/bold red]")
            content.update("\n".join(lines))
            self.scroll_end(animate=False)
        except Exception:
            pass


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
# JarvisTuiApp — main Textual application (v4: non-blocking + log bridges)
# ---------------------------------------------------------------------------
class JarvisTuiApp(App):
    """JARVIS Supervisor TUI with 5 interactive tabs.

    v4 architecture:
    - Textual owns main thread + event loop (unchanged from v3)
    - Supervisor in daemon thread with own asyncio loop (unchanged from v3)
    - Snapshot building in ThreadPoolExecutor (NEW — prevents tab freeze)
    - Log capture via bridges, not stdout redirect (NEW — survives Textual)
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
        tui_log_handler: Optional[_TuiLogHandler] = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._cli_args = cli_args
        self._captured_output: Deque[str] = (
            captured_output or collections.deque(maxlen=_LOG_LIMIT)
        )
        self._tui_log_handler = tui_log_handler
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
        # v4: Snapshot executor + coalescing guard
        self._snapshot_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tui-snapshot"
        )
        self._snapshot_building = False
        # v4: Track whether log bridges are installed
        self._unified_logger_patched = False
        self._rich_console_patched = False
        self._handler_check_counter = 0

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

        v4: After importing unified_supervisor, installs log capture bridges
        (UnifiedLogger patch + Rich Console patch) so all supervisor output
        flows to TUI buffer regardless of Textual's stdout redirect.
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
                "[TUI] async_main imported — installing log capture bridges..."
            )
            # v4: Install log capture bridges AFTER import (module-level code done)
            self._unified_logger_patched = _patch_unified_logger(
                self._captured_output
            )
            self._rich_console_patched = _patch_rich_console(
                self._captured_output
            )

            self._captured_output.append(
                "[TUI] Starting supervisor startup..."
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
        # Shutdown snapshot executor
        self._snapshot_executor.shutdown(wait=False)
        super().action_quit()

    def _signal_supervisor_shutdown(self) -> None:
        """Signal the supervisor's asyncio event loop to shut down."""
        sup_loop = self._supervisor_loop
        if not sup_loop:
            return
        kernel = self._kernel
        if kernel is None:
            return
        se = getattr(kernel, "_shutdown_event", None)
        if se:
            try:
                sup_loop.call_soon_threadsafe(se.set)
            except RuntimeError:
                pass
        sh = getattr(kernel, "_signal_handler", None)
        if sh:
            sh._shutdown_requested = True
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

    # ── Snapshot building (runs in ThreadPoolExecutor) ────────────────────

    def _build_snapshot_sync(self) -> Optional[TuiSnapshot]:
        """Build a TuiSnapshot synchronously — runs in executor thread.

        ALL heavy work (deepcopy, tuple conversion, dashboard state read,
        kernel discovery, event bus wiring) happens here. This NEVER runs
        on Textual's event loop — Textual stays responsive.
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
        if self._ipc_future is not None and self._ipc_future.done():
            try:
                result = self._ipc_future.result()
                if result:
                    self._cached_ipc = copy.deepcopy(result)
            except Exception:
                pass
            self._ipc_future = None
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
                    pass

        # --- Wire event bus (once) ---
        if self._kernel and not self._event_bus_wired:
            eb = getattr(self._kernel, "_event_bus", None)
            if eb:
                try:
                    eb.subscribe(self._handle_event)
                    self._event_bus_wired = True
                except Exception:
                    pass

        # --- Retry log bridge patches if they failed initially ---
        if not self._unified_logger_patched:
            self._unified_logger_patched = _patch_unified_logger(
                self._captured_output
            )
        if not self._rich_console_patched:
            self._rich_console_patched = _patch_rich_console(
                self._captured_output
            )

        # --- Re-install TUI log handler if supervisor removed it ---
        self._handler_check_counter += 1
        if self._handler_check_counter % 10 == 0 and self._tui_log_handler:
            _ensure_tui_handler_installed(self._tui_log_handler)

        # --- Build snapshot (heavy work — deepcopy, tuple conversion) ---
        captured_snap = tuple(self._captured_output)
        with self._event_buf_lock:
            events_snap = tuple(self._events)
            faults_snap = tuple(self._faults)

        thread_alive = (
            self._supervisor_thread is not None
            and self._supervisor_thread.is_alive()
        )

        return TuiSnapshot(
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

    # ── Snapshot polling (async — yields to Textual while executor works) ─

    async def _poll_snapshot(self) -> None:
        """Poll for new data and update snapshot reactively.

        v4: Heavy work runs in ThreadPoolExecutor via run_in_executor().
        The await yields control to Textual's event loop — key events,
        rendering, tab switching all continue while the snapshot builds
        in a background thread. Coalescing guard prevents executor queue
        growth when builds take longer than poll interval.
        """
        if self._snapshot_building:
            return  # Previous build still running — coalesce
        self._snapshot_building = True
        try:
            loop = asyncio.get_running_loop()
            snapshot = await loop.run_in_executor(
                self._snapshot_executor,
                self._build_snapshot_sync,
            )
            if snapshot is not None:
                self._snapshot = snapshot
        except Exception:
            pass
        finally:
            self._snapshot_building = False

    # ── Snapshot watcher — update active tab only ────────────────────────

    def watch__snapshot(self, snapshot: Optional[TuiSnapshot]) -> None:
        if snapshot is None:
            return
        # Update status bar
        try:
            el = self.query_one("#elapsed-bar", Label)
            mins, secs = divmod(int(snapshot.elapsed), 60)
            alive = "LIVE" if snapshot.supervisor_alive else "EXITED"
            bridge = ""
            if self._unified_logger_patched:
                bridge += " UL"
            if self._rich_console_patched:
                bridge += "+RC"
            el.update(
                f"JARVIS TUI | {alive} | {mins}m {secs:02d}s | "
                f"Events: {len(snapshot.events)} | "
                f"Faults: {len(snapshot.faults)} | "
                f"Logs: {len(snapshot.captured_logs)}{bridge} | 1-5 tabs, q quit"
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

    In v4, data flows through JarvisTuiApp._poll_snapshot → executor thread.
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

    v4: Log capture via bridges (NOT stdout redirect).
    Textual takes over sys.stdout in app.run() — redirecting stdout before
    that is pointless. Instead, we install:
      1. _TuiLogHandler on root logger (captures Python logging)
      2. UnifiedLogger monkey-patch (captures supervisor's custom logging)
      3. Rich Console file redirect (captures Rich formatting output)
    All three write directly to the captured_output deque, bypassing stdout.

    Returns the supervisor exit code.
    """
    captured_output: Deque[str] = collections.deque(maxlen=_LOG_LIMIT)

    # Bridge 1: Python logging handler on root logger
    # Set root level to INFO so INFO+ records reach our handler.
    # Don't remove existing handlers — they're harmless (write to
    # terminal which Textual owns, or to files which are fine).
    tui_handler = _install_python_logging_bridge(captured_output)

    exit_code = 1
    try:
        app = JarvisTuiApp(
            cli_args=args,
            captured_output=captured_output,
            tui_log_handler=tui_handler,
        )
        app.run()
        exit_code = app._supervisor_exit_code
    except Exception as exc:
        # Write to stderr (Textual may or may not own it)
        try:
            sys.stderr.write(f"TUI fatal error: {exc}\n")
        except Exception:
            pass
        exit_code = 1
    finally:
        # Clean up our logging handler
        root = logging.getLogger()
        if tui_handler in root.handlers:
            root.removeHandler(tui_handler)

    return exit_code
