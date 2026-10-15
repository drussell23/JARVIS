"""EliteDashboard -- rich.live CLI dashboard for the unified supervisor.

A production-grade terminal dashboard using rich.live + rich.layout
that runs in-band (no separate terminal required, unlike Textual).

Three panels:
    1. Tri-Repo Health Matrix -- live connection status for JARVIS, Prime, Reactor
    2. Live Event Ticker -- autonomous actions, capability spawns, recoveries
    3. Boot Sequence Progress Tree -- async dependency resolution in real time

Architecture:
    The dashboard runs a rich.Live context in a daemon thread with its own
    asyncio event loop. It consumes TelemetryBus events via a thread-safe
    queue (no shared mutable state with the main loop).

    Main loop ──TelemetryBus.subscribe──> _on_envelope()
                                              │
                                         thread-safe Queue
                                              │
                              ┌───────────────────────────────┐
                              │  _render_thread (daemon)      │
                              │  drains queue -> updates state│
                              │  rich.Live refreshes at 4 Hz  │
                              └───────────────────────────────┘

Design constraints:
    - ZERO imports from unified_supervisor.py
    - Thread-safe: only touches main loop via TelemetryBus subscription
    - Daemon thread: auto-dies on process exit
    - Graceful: catches all rendering exceptions (never crashes supervisor)
    - No terminal takeover: outputs below normal log stream
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from backend.core.session_log import console_of, record

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("JARVIS_ELITE_DASHBOARD", "true").lower() in ("true", "1", "yes")
_REFRESH_HZ = float(os.environ.get("JARVIS_DASHBOARD_REFRESH_HZ", "4"))
_MAX_EVENTS = int(os.environ.get("JARVIS_DASHBOARD_MAX_EVENTS", "50"))
_TICKER_VISIBLE = int(os.environ.get("JARVIS_DASHBOARD_TICKER_LINES", "12"))


_DRAIN_PER_FRAME = int(os.environ.get("JARVIS_DASHBOARD_DRAIN_PER_FRAME", "50"))


class _Scrollback(io.TextIOBase):
    """A terminal stream for the inline layout.

    Whole lines are printed through the live console, which puts them above
    the pinned region and redraws the region below them; a partial line
    waits for its newline. One lock serialises every writing thread, so two
    threads' lines never interleave mid-line, and a carriage-return redraw
    within a line keeps only what it finally shows.
    """

    def __init__(self, console: Any, original: Any) -> None:
        super().__init__()
        self._console = console
        self._original = original
        self._lock = threading.Lock()
        self._partial = ""

    def write(self, data: Any) -> int:
        from rich.text import Text
        text = data if isinstance(data, str) else str(data)
        with self._lock:
            self._partial += text
            *complete, self._partial = self._partial.split("\n")
            for line in complete:
                shown = line.rsplit("\r", 1)[-1]
                self._console.print(Text.from_ansi(shown), soft_wrap=True)
                # The console write went around the session-log tee (the
                # region draws on the bare terminal); the line still belongs
                # in the log, exactly as it would have gone there before.
                record(self._original, shown + "\n")
        return len(text)

    def flush(self) -> None:
        try:
            self._original.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return bool(getattr(self._original, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._original.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._original, "encoding", "utf-8")

    def writable(self) -> bool:
        return True


@contextlib.contextmanager
def _terminal_routed_above(console: Any) -> Iterator[None]:
    """While the inline region lives, every writer to the terminal goes
    through a ``_Scrollback``: sys.stdout / sys.stderr and each logging
    StreamHandler that captured the terminal stream before the region
    existed (the kernel's console handlers do). Everything is put back on
    exit, the cursor included, however the region ends."""
    originals = {id(sys.stdout): sys.stdout, id(sys.stderr): sys.stderr}
    for real in (sys.__stdout__, sys.__stderr__):
        if real is not None:
            originals.setdefault(id(real), real)
    proxies = {key: _Scrollback(console, stream) for key, stream in originals.items()}
    saved_out, saved_err = sys.stdout, sys.stderr
    rerouted: List[Tuple[logging.StreamHandler, Any]] = []
    loggers = [logging.getLogger()] + [
        lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)
    ]
    for lg in loggers:
        for handler in list(lg.handlers):
            if (isinstance(handler, logging.StreamHandler)
                    and not isinstance(handler, logging.FileHandler)
                    and id(handler.stream) in proxies):
                rerouted.append((handler, handler.stream))
    sys.stdout = proxies[id(saved_out)]
    sys.stderr = proxies[id(saved_err)]
    for handler, stream in rerouted:
        handler.setStream(proxies[id(stream)])
    try:
        yield
    finally:
        for handler, stream in rerouted:
            try:
                handler.setStream(stream)
            except Exception:
                pass
        if sys.stdout is proxies[id(saved_out)]:
            sys.stdout = saved_out
        if sys.stderr is proxies[id(saved_err)]:
            sys.stderr = saved_err
        try:
            console.show_cursor(True)
        except Exception:
            pass


def _view_key() -> str:
    """The key that changes the terminal's view (``JARVIS_CONSOLE_VIEW_KEY``;
    empty: none is listened for)."""
    return os.environ.get("JARVIS_CONSOLE_VIEW_KEY", "v").strip()[:1]


class _ViewKeys:
    """Changes the terminal's view on a key while the status region lives.

    Reads single keys from the terminal without echo or Enter: ``msvcrt`` on
    Windows; cbreak mode on POSIX, which keeps Ctrl+C a signal and is put
    back however the listener ends. Only an interactive stdin is read --
    nothing in the kernel reads it after the boot's start -- and every other
    key is ignored.
    """

    def __init__(self, key: str, on_key: Callable[[], None]) -> None:
        self._key = key.lower()
        self._on_key = on_key
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        stdin = sys.stdin
        try:
            interactive = bool(self._key) and stdin is not None and stdin.isatty()
        except (ValueError, OSError):
            interactive = False
        if not interactive:
            return False
        self._thread = threading.Thread(target=self._run, name="console_view_keys", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None

    def _handle(self, char: str) -> None:
        if char.lower() == self._key:
            try:
                self._on_key()
            except Exception:  # noqa: BLE001 -- a key never breaks the terminal
                pass

    def _run(self) -> None:
        poll = 1.0 / _REFRESH_HZ
        if os.name == "nt":
            import msvcrt
            while not self._stop.is_set():
                if msvcrt.kbhit():
                    self._handle(msvcrt.getwch())
                else:
                    self._stop.wait(poll)
            return
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], poll)
                if ready:
                    self._handle(os.read(fd, 1).decode(errors="ignore"))
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _cycle_view() -> None:
    """Move the terminal to the next view and say so in the scrollback."""
    from backend.core.console_view import VIEW_NAMES, cycle_verbosity, narrate
    narrate(f"Terminal view: {VIEW_NAMES[cycle_verbosity()]}", "note")


def _layout() -> str:
    """How the dashboard occupies the terminal.

    ``inline`` (default): a status region pinned below the scrolling output,
    redrawn in place -- the output above stays ordinary scrollback, the way
    Claude Code keeps finished output static and redraws only its live
    region. ``fullscreen``: the three-panel screen, which takes the whole
    terminal (shown once the boot completes).
    """
    value = os.environ.get("JARVIS_DASHBOARD_LAYOUT", "").strip().lower()
    return value if value in ("inline", "fullscreen") else "inline"


def _region_event_lines() -> int:
    try:
        return max(0, int(os.environ.get("JARVIS_STATUS_REGION_EVENTS", "") or 3))
    except ValueError:
        return 3


_STATUS_STYLES = {
    "ONLINE": "bold green",
    "DEGRADED": "bold yellow",
    "STARTING": "bold yellow",
    "TIMEOUT": "bold yellow",
    "OFFLINE": "bold red",
    "ERROR": "bold red",
    "UNCONFIGURED": "dim",
    "MEASURING": "dim",
}

_CATEGORY_STYLES = {
    "lifecycle": "cyan",
    "fault": "bold red",
    "recovery": "bold green",
    "agent": "blue",
    "reasoning": "yellow",
    "proactive": "magenta",
    "governance": "bright_cyan",
    "boot": "white",
}

_SEVERITY_STYLES = {"error": "bold red", "warn": "yellow"}


def _latency_text(repo: "RepoHealth") -> str:
    return f"{repo.latency_ms:.0f}ms" if repo.latency_ms > 0 else "--"


def _duration_text(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _event_text(event: "TickerEvent") -> Any:
    from rich.text import Text
    message = Text(event.message, style=_SEVERITY_STYLES.get(event.severity, ""))
    if event.count > 1:
        message.append(f"  ×{event.count}", style="bold")
    return message

# ---------------------------------------------------------------------------
# Shared state (populated by TelemetryBus, rendered by dashboard thread)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthTarget:
    """One row of the health matrix: what to probe, supplied by the kernel
    (its own backend's URL, the Trinity siblings' from trinity_siblings).
    ``url`` is read at every probe, so a port chosen later in boot is seen."""
    key: str
    display_name: str
    url: Callable[[], str]


@dataclass(frozen=True)
class RepoHealth:
    """A MEASURED health reading: one probe of the component's health URL."""
    name: str
    display_name: str
    status: str
    url: str = ""
    latency_ms: float = 0.0
    detail: str = ""
    measured_at: float = 0.0

    @property
    def port(self) -> Optional[int]:
        try:
            return urlparse(self.url).port
        except ValueError:
            return None


@dataclass
class TickerEvent:
    """A single event in the live ticker."""
    timestamp: float
    category: str  # lifecycle, fault, recovery, agent, reasoning, proactive
    message: str
    severity: str = "info"  # info, warn, error
    key: str = ""           # events with the same key in a row fold into one
    count: int = 1


def _append_event(state: "DashboardState", event: TickerEvent) -> None:
    """Add to the ticker, folding a repeat of the newest event into it.

    A source that reports the same thing over and over (the loop sentinel's
    stall warnings) filled every visible row and pushed everything else out;
    consecutive events with one key now become a single row, newest text,
    with a count.
    """
    events = state.events
    if event.key and events and events[-1].key == event.key:
        last = events[-1]
        event.count = last.count + 1
        events[-1] = event
        return
    events.append(event)


@dataclass
class DashboardState:
    """Thread-safe state consumed by the renderer.

    The main event loop writes to the queue; the render thread
    drains the queue and updates these fields.  The render thread
    is the sole writer of the dataclass fields after drain, so
    no lock is needed for reads during rendering.
    """
    # Written only by the health prober, one measured reading per target.
    repos: Dict[str, RepoHealth] = field(default_factory=dict)
    # The kernel's BootTimeline (backend/core/boot_timeline): the one model
    # of how far the boot is, shared with the loading page.
    timeline: Any = None
    events: Deque[TickerEvent] = field(default_factory=lambda: deque(maxlen=_MAX_EVENTS))

    # Counters
    total_envelopes: int = 0
    total_faults: int = 0
    total_recoveries: int = 0
    total_agents: int = 0
    initialized_agents: int = 0
    governance_ops: int = 0
    proactive_explorations: int = 0
    uptime_start: float = field(default_factory=time.monotonic)
    # The key that changes the terminal's view, while one is listened for.
    view_key: str = ""

    @property
    def boot_complete(self) -> bool:
        return bool(self.timeline is not None and self.timeline.complete)


# ---------------------------------------------------------------------------
# Health probing (the matrix shows measurements, never defaults)
# ---------------------------------------------------------------------------

def _health_interval_s() -> float:
    try:
        return max(0.5, float(os.environ.get("JARVIS_DASHBOARD_HEALTH_INTERVAL_S", "") or 5.0))
    except ValueError:
        return 5.0


def _probe_timeout_s() -> float:
    """One probe's budget: the Trinity siblings' own probe timeout."""
    try:
        from backend.core.ouroboros.governance.trinity_siblings import probe_timeout_s
        return probe_timeout_s()
    except Exception:
        return 2.0


# What a component's own health payload says, mapped onto matrix states.
_REPORTED_STATES = {
    "healthy": "ONLINE", "ok": "ONLINE", "ready": "ONLINE", "online": "ONLINE",
    "running": "ONLINE", "serving": "ONLINE",
    "degraded": "DEGRADED",
    "starting": "STARTING", "booting": "STARTING", "initializing": "STARTING",
    "loading": "STARTING",
}


def probe_health(target: HealthTarget, timeout: Optional[float] = None) -> RepoHealth:
    """One measured reading of *target*: HTTP status, the payload's own
    ``status`` when it reports one, and the round-trip latency."""
    import json
    import socket
    import urllib.error
    import urllib.request

    url = ""
    started = time.monotonic()
    try:
        url = target.url() or ""
        if not url:
            return RepoHealth(target.key, target.display_name, "UNCONFIGURED",
                              detail="no health URL", measured_at=time.time())
        with urllib.request.urlopen(url, timeout=timeout or _probe_timeout_s()) as resp:
            body = resp.read(65536)
        latency = (time.monotonic() - started) * 1000.0
        status, detail = "ONLINE", ""
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("status"), str):
            reported = payload["status"].strip().lower()
            status = _REPORTED_STATES.get(reported, reported.upper() or "ONLINE")
            if status != "ONLINE":
                detail = reported
        return RepoHealth(target.key, target.display_name, status, url, latency, detail, time.time())
    except urllib.error.HTTPError as exc:
        return RepoHealth(target.key, target.display_name, "ERROR", url,
                          (time.monotonic() - started) * 1000.0, f"HTTP {exc.code}", time.time())
    except (socket.timeout, TimeoutError):
        return RepoHealth(target.key, target.display_name, "TIMEOUT", url,
                          detail="no answer in time", measured_at=time.time())
    except Exception as exc:  # refused, unreachable, bad URL: not answering
        reason = getattr(exc, "reason", exc)
        return RepoHealth(target.key, target.display_name, "OFFLINE", url,
                          detail=str(reason)[:60], measured_at=time.time())


# ---------------------------------------------------------------------------
# Rendering functions (pure -- take state, return renderables)
# ---------------------------------------------------------------------------

def _render_health_matrix(state: DashboardState) -> Any:
    """Render the Tri-Repo Health Matrix as a rich Table."""
    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Trinity Health Matrix",
        title_style="bold cyan",
        border_style="bright_black",
        show_lines=True,
        expand=True,
        padding=(0, 1),
    )
    # The measured columns are as wide as their widest reading and never
    # truncate; the name takes what is left and ellipsizes, so a narrow
    # terminal still shows every measurement in full.
    readings = list(state.repos.values())

    def _width(header: str, cells: List[str]) -> int:
        return max([len(header)] + [len(c) for c in cells])

    table.add_column("Component", style="bold", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("Status", justify="center", no_wrap=True,
                     min_width=_width("Status", [r.status for r in readings]))
    table.add_column("Port", justify="center", no_wrap=True,
                     min_width=_width("Port", [str(r.port or "--") for r in readings]))
    table.add_column("Latency", justify="right", no_wrap=True,
                     min_width=_width("Latency", [_latency_text(r) for r in readings]))

    for repo in readings:
        table.add_row(
            repo.display_name,
            Text(repo.status, style=_STATUS_STYLES.get(repo.status, "bold yellow")),
            str(repo.port or "--"),
            _latency_text(repo),
        )

    if not state.repos:
        table.add_row(Text("Measuring...", style="dim"), "", "", "")

    return table


def _render_event_ticker(state: DashboardState) -> Any:
    """Render the Live Event Ticker as a rich Table."""
    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Live Event Ticker",
        title_style="bold magenta",
        border_style="bright_black",
        show_lines=False,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Time", style="dim", min_width=10, no_wrap=True)
    table.add_column("Cat", min_width=10, no_wrap=True)
    table.add_column("Event", ratio=1)

    # Show most recent events (newest first)
    recent = list(state.events)[-_TICKER_VISIBLE:]
    for event in reversed(recent):
        ts = datetime.fromtimestamp(event.timestamp)
        time_str = ts.strftime("%H:%M:%S")
        cat_text = Text(event.category.upper(), style=_CATEGORY_STYLES.get(event.category, "dim"))
        table.add_row(time_str, cat_text, _event_text(event))

    if not recent:
        table.add_row("--", Text("--", style="dim"), Text("Waiting for events...", style="dim"))

    return table


def _render_boot_tree(state: DashboardState) -> Any:
    """Render the Boot Sequence Progress Tree."""
    from rich.tree import Tree

    timeline = state.timeline
    if timeline is None:
        tree = Tree("Boot Sequence [dim](no boot timeline)[/]", guide_style="bright_black")
        tree.add("[dim][ ] Awaiting the kernel's boot timeline...[/]")
        return tree

    elapsed = timeline.elapsed()
    if timeline.complete:
        boot_label = f"Boot Sequence [bold green]COMPLETE[/] ({elapsed:.1f}s)"
    else:
        boot_label = f"Boot Sequence [bold yellow]{timeline.percent()}%[/] · {elapsed:.0f}s"
        remaining = timeline.remaining_seconds()
        boot_label += (" · first measured boot" if remaining is None
                       else f" · about {remaining:.0f}s left by previous boots")
    tree = Tree(boot_label, guide_style="bright_black")

    phases = timeline.phases()
    for name, started, ended in phases:
        if ended is None:
            tree.add(f"[bold yellow][~][/] {name} [bold yellow]running {elapsed - started:.0f}s[/]")
        else:
            tree.add(f"[bold green][+][/] {name} [dim]({ended - started:.1f}s)[/]")
    if not phases:
        tree.add("[dim][ ] Awaiting first phase...[/]")
    return tree


def _render_stats_bar(state: DashboardState) -> Any:
    """Render a compact stats summary."""
    from rich.text import Text

    uptime_str = _duration_text(time.monotonic() - state.uptime_start)
    parts = [
        f"[bold]Uptime:[/] {uptime_str}",
        f"[bold]Events:[/] {state.total_envelopes}",
        f"[bold]Agents:[/] {state.initialized_agents}/{state.total_agents}",
        f"[bold]Faults:[/] {state.total_faults}",
        f"[bold]Recoveries:[/] {state.total_recoveries}",
        f"[bold]Gov Ops:[/] {state.governance_ops}",
        f"[bold]Explorations:[/] {state.proactive_explorations}",
    ]
    return Text.from_markup("  |  ".join(parts))


def _render_system_metrics(state: DashboardState) -> Any:
    """Render system metrics panel with live data."""
    from rich.table import Table

    uptime_s = max(1, time.monotonic() - state.uptime_start)
    table = Table(
        title="System Metrics",
        title_style="bold bright_cyan",
        border_style="bright_black",
        expand=True,
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("Metric", style="bold green")
    table.add_column("Value", justify="right", style="bright_white")

    # Performance
    table.add_row("Envelopes/s", f"{state.total_envelopes / uptime_s:.1f}")
    table.add_row("Total Events", str(state.total_envelopes))
    table.add_row("", "")

    # Agents
    table.add_row("Agents", f"{state.initialized_agents}/{state.total_agents}")
    table.add_row("Gov Ops", str(state.governance_ops))
    table.add_row("Explorations", str(state.proactive_explorations))
    table.add_row("", "")

    # Health
    table.add_row("Faults", f"[bold red]{state.total_faults}[/]" if state.total_faults else "0")
    table.add_row("Recoveries", f"[bold green]{state.total_recoveries}[/]" if state.total_recoveries else "0")
    table.add_row("", "")

    # Configuration
    table.add_row("Gov Mode", os.environ.get("JARVIS_GOVERNANCE_MODE", "sandbox"))
    table.add_row("Proactive", os.environ.get("JARVIS_PROACTIVE_COOLDOWN_S", "3600") + "s")

    ram = _ram_markup()
    if ram:
        table.add_row("RAM", ram)

    return table


def _ram_markup() -> str:
    """Free memory as rich markup, coloured by pressure; "" when unknown."""
    try:
        import psutil
        mem = psutil.virtual_memory()
    except Exception:
        return ""
    style = "bold red" if mem.percent > 90 else "yellow" if mem.percent > 80 else "green"
    return f"[{style}]{mem.available / (1024 ** 3):.1f}GB free ({mem.percent:.0f}%)[/]"


def _build_status_region(state: DashboardState) -> Any:
    """The inline layout's live region, pinned below the scrolling output.

    One line per concern -- boot, health, the newest events, counters --
    each clipped to the terminal width, so the region stays a few rows tall
    however much happens; everything else scrolls by above it untouched.
    """
    from rich.console import Group
    from rich.progress_bar import ProgressBar
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    def clipped(text: Text) -> Text:
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    lines: List[Any] = [Rule("JARVIS", style="bright_black", align="left")]

    timeline = state.timeline
    if timeline is None:
        lines.append(clipped(Text("Waiting for the kernel's boot timeline...", style="dim")))
    elif timeline.complete:
        lines.append(clipped(Text.assemble(
            ("● Online", "bold green"),
            f" · booted in {timeline.elapsed():.1f}s"
            f" · up {_duration_text(time.monotonic() - state.uptime_start)}",
        )))
    else:
        detail = f"{timeline.elapsed():.0f}s"
        remaining = timeline.remaining_seconds()
        detail += (" · first measured boot" if remaining is None
                   else f" · about {remaining:.0f}s left")
        boot = Table.grid(padding=(0, 1), expand=True)
        boot.add_column(no_wrap=True)
        boot.add_column(ratio=1)
        boot.add_column(no_wrap=True, overflow="ellipsis", max_width=None)
        percent = timeline.percent()
        boot.add_row(Text(f"Booting {percent:>2d}%", style="bold yellow"),
                     ProgressBar(total=100, completed=percent), Text(detail, style="dim"))
        lines.append(boot)
        lines.extend(clipped(line) for line in _boot_checklist_lines(timeline))

    health = Text()
    for repo in state.repos.values():
        if health:
            health.append("   ")
        health.append(f"{repo.display_name} ", style="bold")
        health.append(repo.status, style=_STATUS_STYLES.get(repo.status, "bold yellow"))
        if repo.port:
            health.append(f" :{repo.port}", style="dim")
        if repo.latency_ms > 0:
            health.append(f" {_latency_text(repo)}", style="dim")
    lines.append(clipped(health if health else Text("Measuring component health...", style="dim")))

    shown = _region_event_lines()
    for event in (list(state.events)[-shown:] if shown else []):
        row = Text.assemble(
            (datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S") + " ", "dim"),
            (f"{event.category.upper():<10} ", _CATEGORY_STYLES.get(event.category, "dim")),
        )
        row.append_text(_event_text(event))
        lines.append(clipped(row))

    stats = Text.from_markup(
        f"[dim]events {state.total_envelopes} · agents {state.initialized_agents}/"
        f"{state.total_agents} · faults {state.total_faults} · "
        f"recoveries {state.total_recoveries}[/]"
    )
    ram = _ram_markup()
    if ram:
        stats.append_text(Text.from_markup(" [dim]·[/] RAM " + ram))
    if state.view_key:
        from backend.core.console_view import VIEW_NAMES, verbosity
        stats.append_text(Text.from_markup(
            f" [dim]· view: {VIEW_NAMES[verbosity()]} ({state.view_key} to change)[/]"))
    lines.append(clipped(stats))
    return Group(*lines)


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _region_phase_lines() -> int:
    try:
        return max(0, int(os.environ.get("JARVIS_STATUS_REGION_PHASES", "") or _region_event_lines()))
    except ValueError:
        return _region_event_lines()


def _boot_checklist_lines(timeline: Any) -> List[Any]:
    """The live half of the boot checklist: each phase still running, with
    a spinner and how long it has run, then the phases previous boots ran
    next. Finished phases are static lines in the scrollback above
    (RichCliRenderer narrates them)."""
    from rich.text import Text
    from backend.core.console_view import phase_title, seconds

    shown = _region_phase_lines()
    if not shown:
        return []
    elapsed = timeline.elapsed()
    frame = _SPINNER[int(time.monotonic() * _REFRESH_HZ * 2) % len(_SPINNER)]
    running = [(name, started) for name, started, ended in timeline.phases() if ended is None]
    lines: List[Any] = [
        Text.assemble((f"{frame} ", "bold yellow"), (phase_title(name), "bold"),
                      (f"  {seconds(elapsed - started)}", "dim"))
        for name, started in running[-shown:]
    ]
    upcoming = [phase_title(name) for name in timeline.upcoming()]
    if upcoming:
        lines.append(Text("○ next: " + " · ".join(upcoming), style="dim"))
    return lines


def _build_layout(state: DashboardState) -> Any:
    """Build the full dashboard layout."""
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.text import Text

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )

    # Header: title bar
    header_text = Text(
        "  JARVIS UNIFIED SUPERVISOR  --  ELITE DASHBOARD  ",
        style="bold white on dark_blue",
        justify="center",
    )
    layout["header"].update(header_text)

    # Body: three columns (left wider for health + boot)
    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="center", ratio=4),
        Layout(name="right", ratio=3),
    )

    # Left: Health Matrix + Boot Tree
    layout["left"].split_column(
        Layout(name="health", ratio=2),
        Layout(name="boot", ratio=3),
    )
    layout["health"].update(Panel(
        _render_health_matrix(state),
        border_style="cyan",
    ))
    layout["boot"].update(Panel(
        _render_boot_tree(state),
        title="Boot Progress",
        title_align="left",
        border_style="green" if state.boot_complete else "yellow",
    ))

    # Center: Event Ticker
    layout["center"].update(Panel(
        _render_event_ticker(state),
        border_style="magenta",
    ))

    # Right: System Metrics
    layout["right"].update(Panel(
        _render_system_metrics(state),
        border_style="bright_black",
    ))

    # Footer: stats bar
    layout["footer"].update(Panel(
        _render_stats_bar(state),
        style="dim",
    ))

    return layout


# ---------------------------------------------------------------------------
# Event processing (drains the thread-safe queue into DashboardState)
# ---------------------------------------------------------------------------

def _process_envelope(state: DashboardState, envelope: Any) -> None:
    """Process a single TelemetryEnvelope into DashboardState.

    Called exclusively from the render thread after draining the queue.
    Telemetry feeds the ticker and the counters; it never sets a health
    row -- those are measured by the prober, the matrix's only writer.
    """
    state.total_envelopes += 1
    schema = envelope.event_schema
    payload = envelope.payload
    source = envelope.source
    now = time.time()
    key = f"{schema.split('@')[0]}:{source}"

    def ticker(category: str, message: str, severity: str = "info") -> None:
        _append_event(state, TickerEvent(now, category, message, severity, key=key))

    if schema.startswith("lifecycle.transition"):
        to_state = payload.get("to_state", "")
        ticker("lifecycle", f"{source}: {payload.get('from_state', '')} -> {to_state}",
               "warn" if to_state in ("DEGRADED", "UNHEALTHY", "TERMINAL", "DEAD") else "info")

    elif schema.startswith("fault.raised"):
        state.total_faults += 1
        ticker("fault", f"FAULT: {payload.get('fault_class', 'unknown')}", "error")

    elif schema.startswith("fault.resolved"):
        state.total_recoveries += 1
        ticker("recovery", f"RESOLVED: {payload.get('fault_class', 'unknown')}")

    elif schema.startswith("recovery.attempt"):
        ticker("recovery", f"Recovery attempt: {payload.get('target', 'unknown')}", "warn")

    elif schema.startswith("scheduler.graph_state"):
        state.total_agents = payload.get("total_agents", state.total_agents)
        state.initialized_agents = payload.get("initialized", state.initialized_agents)
        ticker("agent", f"Agents: {state.initialized_agents}/{state.total_agents} initialized")

    elif schema.startswith("scheduler.unit_state"):
        agent_state = payload.get("state", "")
        if agent_state in ("running", "initialized"):
            ticker("agent", f"{payload.get('agent_name', '')}: {agent_state}")

    elif schema.startswith("reasoning.decision"):
        ticker("reasoning", f"Decision: {payload.get('command', '')[:60]}")

    elif schema.startswith("reasoning.activation"):
        ticker("reasoning", f"Gate: {payload.get('from_state', '')} -> {payload.get('to_state', '')}")

    elif schema.startswith("reasoning.proactive_drive"):
        drive_state = payload.get("state", "")
        if drive_state in ("ELIGIBLE", "EXPLORING"):
            state.proactive_explorations += 1
            ticker("proactive", f"ProactiveDrive: {drive_state}")


# ---------------------------------------------------------------------------
# EliteDashboard
# ---------------------------------------------------------------------------

class EliteDashboard:
    """Production-grade CLI dashboard for the unified supervisor.

    Renders a three-panel rich.live layout in a daemon thread.
    Consumes TelemetryBus events via a thread-safe queue.

    Usage:
        dashboard = EliteDashboard()
        await dashboard.start()               # subscribes to the TelemetryBus
        dashboard.attach_boot_timeline(timeline)  # the kernel's BootTimeline
        dashboard.watch_health(targets)       # measured health rows
        dashboard.activate_display()
        ...
        await dashboard.stop()
    """

    def __init__(self, enabled: bool = _ENABLED) -> None:
        self._enabled = enabled and sys.stdout.isatty()
        self._state = DashboardState()
        self._event_queue: queue.Queue = queue.Queue(maxsize=256)
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._prober: Optional[threading.Thread] = None
        self._prober_stop = threading.Event()

        # Narrator hook: dashboard receives narrated text for the ticker
        self._narrator_hook_installed = False

    async def start(self) -> None:
        """Subscribe to TelemetryBus and start buffering events.

        The render thread does NOT start yet — it starts when
        ``activate_display()`` is called after boot completes.
        This prevents the dashboard from showing a half-empty UI
        during the boot process while log output is still streaming.
        Events are buffered in the queue so no data is lost.
        """
        if not self._enabled:
            logger.info("[EliteDashboard] Disabled (no TTY or env)")
            return

        # Subscribe to TelemetryBus — start buffering events immediately
        try:
            from backend.core.telemetry_contract import get_telemetry_bus
            bus = get_telemetry_bus()
            bus.subscribe("*", self._on_envelope)
            logger.info("[EliteDashboard] Subscribed to TelemetryBus (buffering, display deferred)")
        except Exception as exc:
            logger.warning("[EliteDashboard] TelemetryBus subscribe failed: %s", exc)

    def attach_boot_timeline(self, timeline: Any) -> None:
        """Render boot progress from the kernel's BootTimeline -- the same
        measurement the loading page shows -- instead of a tracker of the
        dashboard's own that only saw phases begun after it was created."""
        self._state.timeline = timeline

    def watch_health(self, targets: List[HealthTarget]) -> None:
        """Measure each target's health URL every
        ``JARVIS_DASHBOARD_HEALTH_INTERVAL_S`` on a daemon thread; the
        matrix shows those readings and nothing else."""
        if not targets or (self._prober is not None and self._prober.is_alive()):
            return
        self._prober_stop.clear()
        self._prober = threading.Thread(
            target=self._probe_loop, args=(list(targets),),
            name="elite_dashboard_health", daemon=True,
        )
        self._prober.start()

    def _probe_loop(self, targets: List[HealthTarget]) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Every row is visible from the start, and each reading lands as soon
        # as its own probe answers: a slow or dead target never holds back
        # the others.
        for target in targets:
            self._state.repos.setdefault(
                target.key, RepoHealth(target.key, target.display_name, "MEASURING"))
        with ThreadPoolExecutor(max_workers=len(targets),
                                thread_name_prefix="elite_dashboard_probe") as pool:
            while not self._prober_stop.is_set():
                for done in as_completed([pool.submit(probe_health, t) for t in targets]):
                    reading = done.result()
                    self._state.repos[reading.name] = reading
                self._prober_stop.wait(timeout=_health_interval_s())

    @property
    def inline(self) -> bool:
        return _layout() == "inline"

    def activate_display(self) -> None:
        """Start the render thread. Idempotent.

        Inline layout: called when the terminal renderer starts, at the
        beginning of the boot -- the region sits below the output, so it
        can show the boot as it happens. Full-screen layout: called once
        the boot completes, since it takes the whole terminal. Events
        buffered since ``start()`` are drained and rendered immediately.
        """
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return  # Already active

        # Full screen hides the scrollback, so warnings go to its ticker; in
        # the inline layout they are already visible in the scrollback.
        if not self.inline:
            self._install_log_handler()

        # Start render thread
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._render_loop,
            name="elite_dashboard",
            daemon=True,
        )
        self._thread.start()
        logger.info("[EliteDashboard] Display activated (%s layout)", _layout())

    def _install_log_handler(self) -> None:
        """Route WARNING+ log messages into the event ticker."""
        dashboard = self

        class _DashboardLogHandler(logging.Handler):
            """Captures WARNING+ logs and injects them into the ticker."""

            def emit(self, record: logging.LogRecord) -> None:
                try:
                    category = "fault" if record.levelno >= logging.ERROR else "lifecycle"
                    severity = "error" if record.levelno >= logging.ERROR else "warn"
                    msg = record.getMessage()
                    # Truncate long messages for the ticker
                    if len(msg) > 120:
                        msg = msg[:117] + "..."
                    _append_event(dashboard._state, TickerEvent(
                        timestamp=time.time(),
                        category=category,
                        message=f"[{record.name.split('.')[-1]}] {msg}",
                        severity=severity,
                        key=f"log:{record.name}:{record.levelno}",
                    ))
                except Exception:
                    pass  # Never crash the logging system

        handler = _DashboardLogHandler()
        handler.setLevel(logging.WARNING)
        logging.getLogger().addHandler(handler)

    async def stop(self) -> None:
        """Stop the render thread and the health prober."""
        self._prober_stop.set()
        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        logger.info("[EliteDashboard] Stopped")

    def install_narrator_hook(self, narrator: Any) -> None:
        """Wire narrator events into the ticker."""
        if self._narrator_hook_installed:
            return
        self._narrator_hook_installed = True

        def _hook(text: str) -> None:
            _append_event(self._state, TickerEvent(
                timestamp=time.time(),
                category="voice",
                message=f"[Narrator] {text}",
            ))

        narrator.add_hook(_hook)

    # ------------------------------------------------------------------
    # TelemetryBus handler (called from main event loop)
    # ------------------------------------------------------------------

    async def _on_envelope(self, envelope: Any) -> None:
        """Push envelope into thread-safe queue for the render thread."""
        try:
            self._event_queue.put_nowait(envelope)
        except queue.Full:
            pass  # drop silently -- dashboard is best-effort

    # ------------------------------------------------------------------
    # Render thread
    # ------------------------------------------------------------------

    def _drain(self) -> None:
        """Apply queued telemetry to the state (render thread only)."""
        for _ in range(_DRAIN_PER_FRAME):
            try:
                _process_envelope(self._state, self._event_queue.get_nowait())
            except queue.Empty:
                return

    def _render_loop(self) -> None:
        """Daemon thread: drain the telemetry queue and redraw."""
        try:
            from rich.console import Console
            from rich.live import Live
        except ImportError as exc:
            logger.warning("[EliteDashboard] rich not available: %s", exc)
            return
        refresh_interval = 1.0 / _REFRESH_HZ
        try:
            if self.inline:
                # The region is redrawn in place below ordinary scrollback:
                # every terminal writer is routed above it while it lives.
                # The bare terminal: region frames are redrawn several times a
                # second and must not be copied into the session log.
                console = Console(file=console_of(sys.stdout))
                keys = _ViewKeys(_view_key(), _cycle_view)
                with _terminal_routed_above(console), Live(
                    get_renderable=lambda: _build_status_region(self._state),
                    console=console,
                    refresh_per_second=_REFRESH_HZ,
                    redirect_stdout=False,
                    redirect_stderr=False,
                    transient=False,
                ):
                    self._state.view_key = _view_key() if keys.start() else ""
                    try:
                        while not self._stop_flag.is_set():
                            self._drain()
                            self._stop_flag.wait(timeout=refresh_interval)
                    finally:
                        keys.stop()
                        self._state.view_key = ""
            else:
                with Live(
                    get_renderable=lambda: _build_layout(self._state),
                    console=Console(),
                    refresh_per_second=_REFRESH_HZ,
                    screen=True,
                ):
                    while not self._stop_flag.is_set():
                        self._drain()
                        self._stop_flag.wait(timeout=refresh_interval)
        except Exception as exc:
            logger.error("[EliteDashboard] Render thread crashed: %s", exc)

    # ------------------------------------------------------------------
    # Direct state updates (for supervisor to call)
    # ------------------------------------------------------------------

    def add_event(self, category: str, message: str, severity: str = "info",
                  key: str = "") -> None:
        """Add an event directly to the ticker (same ``key`` in a row folds)."""
        _append_event(self._state, TickerEvent(
            timestamp=time.time(),
            category=category,
            message=message,
            severity=severity,
            key=key,
        ))

    def health(self) -> Dict[str, Any]:
        """Return dashboard health snapshot."""
        return {
            "enabled": self._enabled,
            "running": self._thread is not None and self._thread.is_alive(),
            "total_envelopes": self._state.total_envelopes,
            "total_events": len(self._state.events),
            "boot_complete": self._state.boot_complete,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[EliteDashboard] = None


def get_elite_dashboard(**kwargs: Any) -> EliteDashboard:
    """Get or create the singleton EliteDashboard."""
    global _instance
    if _instance is None:
        _instance = EliteDashboard(**kwargs)
    return _instance
