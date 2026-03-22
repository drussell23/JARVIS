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

import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("JARVIS_ELITE_DASHBOARD", "true").lower() in ("true", "1", "yes")
_REFRESH_HZ = float(os.environ.get("JARVIS_DASHBOARD_REFRESH_HZ", "4"))
_MAX_EVENTS = int(os.environ.get("JARVIS_DASHBOARD_MAX_EVENTS", "50"))
_TICKER_VISIBLE = int(os.environ.get("JARVIS_DASHBOARD_TICKER_LINES", "12"))

# ---------------------------------------------------------------------------
# Shared state (populated by TelemetryBus, rendered by dashboard thread)
# ---------------------------------------------------------------------------


@dataclass
class RepoHealth:
    """Health status for a single repo in the Trinity."""
    name: str
    display_name: str
    status: str = "OFFLINE"
    last_heartbeat: float = 0.0
    port: int = 0
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class BootPhase:
    """Progress of a single boot phase."""
    name: str
    status: str = "pending"  # pending, running, done, failed, skipped
    started_at: float = 0.0
    finished_at: float = 0.0
    detail: str = ""


@dataclass
class TickerEvent:
    """A single event in the live ticker."""
    timestamp: float
    category: str  # lifecycle, fault, recovery, agent, reasoning, proactive
    message: str
    severity: str = "info"  # info, warn, error


@dataclass
class DashboardState:
    """Thread-safe state consumed by the renderer.

    The main event loop writes to the queue; the render thread
    drains the queue and updates these fields.  The render thread
    is the sole writer of the dataclass fields after drain, so
    no lock is needed for reads during rendering.
    """
    repos: Dict[str, RepoHealth] = field(default_factory=lambda: {
        "jarvis": RepoHealth("jarvis", "JARVIS (Body)", status="BOOTING", port=8080),
        "prime": RepoHealth("prime", "J-Prime (Mind)", port=8000),
        "reactor": RepoHealth("reactor", "Reactor (Soul)", port=8090),
    })
    boot_phases: List[BootPhase] = field(default_factory=list)
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

    # Boot tracking
    boot_complete: bool = False
    boot_start: float = field(default_factory=time.monotonic)
    boot_elapsed_s: float = 0.0


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
    table.add_column("Component", style="bold", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Port", justify="center", no_wrap=True)
    table.add_column("Latency", justify="right", no_wrap=True)

    status_styles = {
        "ONLINE": ("bold green", "ONLINE"),
        "READY": ("bold green", "READY"),
        "BOOTING": ("bold yellow", "BOOTING"),
        "DEGRADED": ("bold yellow", "DEGRADED"),
        "PROBING": ("bold yellow", "PROBING"),
        "OFFLINE": ("bold red", "OFFLINE"),
        "DEAD": ("bold red", "DEAD"),
        "ERROR": ("bold red", "ERROR"),
    }

    for repo in state.repos.values():
        style, label = status_styles.get(repo.status, ("dim", repo.status))
        status_text = Text(label, style=style)

        latency = f"{repo.latency_ms:.0f}ms" if repo.latency_ms > 0 else "--"

        table.add_row(
            repo.display_name,
            status_text,
            str(repo.port) if repo.port else "--",
            latency,
        )

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

    category_styles = {
        "lifecycle": "cyan",
        "fault": "bold red",
        "recovery": "bold green",
        "agent": "blue",
        "reasoning": "yellow",
        "proactive": "magenta",
        "governance": "bright_cyan",
        "boot": "white",
    }

    # Show most recent events (newest first)
    recent = list(state.events)[-_TICKER_VISIBLE:]
    for event in reversed(recent):
        ts = datetime.fromtimestamp(event.timestamp)
        time_str = ts.strftime("%H:%M:%S")
        cat_style = category_styles.get(event.category, "dim")
        cat_text = Text(event.category.upper(), style=cat_style)

        msg_style = ""
        if event.severity == "error":
            msg_style = "bold red"
        elif event.severity == "warn":
            msg_style = "yellow"

        table.add_row(time_str, cat_text, Text(event.message, style=msg_style))

    if not recent:
        table.add_row("--", Text("--", style="dim"), Text("Waiting for events...", style="dim"))

    return table


def _render_boot_tree(state: DashboardState) -> Any:
    """Render the Boot Sequence Progress Tree."""
    from rich.tree import Tree

    boot_label = "Boot Sequence"
    if state.boot_complete:
        boot_label += f" [bold green]COMPLETE[/] ({state.boot_elapsed_s:.1f}s)"
    else:
        elapsed = time.monotonic() - state.boot_start
        boot_label += f" [bold yellow]IN PROGRESS[/] ({elapsed:.0f}s)"

    tree = Tree(boot_label, guide_style="bright_black")

    status_icons = {
        "pending": "[dim][ ][/]",
        "running": "[bold yellow][~][/]",
        "done": "[bold green][+][/]",
        "failed": "[bold red][X][/]",
        "skipped": "[dim][-][/]",
    }

    for phase in state.boot_phases:
        icon = status_icons.get(phase.status, "[dim][?][/]")
        label = f"{icon} {phase.name}"
        if phase.status == "running":
            label += " [bold yellow]...[/]"
        elif phase.status == "done" and phase.finished_at > 0 and phase.started_at > 0:
            dur = phase.finished_at - phase.started_at
            label += f" [dim]({dur:.1f}s)[/]"
        elif phase.status == "failed" and phase.detail:
            label += f" [bold red]({phase.detail})[/]"

        tree.add(label)

    if not state.boot_phases:
        tree.add("[dim][ ] Awaiting first phase...[/]")

    return tree


def _render_stats_bar(state: DashboardState) -> Any:
    """Render a compact stats summary."""
    from rich.text import Text

    uptime = time.monotonic() - state.uptime_start
    if uptime < 60:
        uptime_str = f"{uptime:.0f}s"
    elif uptime < 3600:
        uptime_str = f"{uptime / 60:.0f}m"
    else:
        uptime_str = f"{uptime / 3600:.1f}h"

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

    # Memory (if available)
    try:
        import psutil
        mem = psutil.virtual_memory()
        avail_gb = mem.available / (1024 ** 3)
        used_pct = mem.percent
        style = "bold red" if used_pct > 90 else "yellow" if used_pct > 80 else "green"
        table.add_row("RAM", f"[{style}]{avail_gb:.1f}GB free ({used_pct:.0f}%)[/]")
    except Exception:
        pass

    return table


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
    """
    state.total_envelopes += 1
    schema = envelope.event_schema
    payload = envelope.payload
    source = envelope.source
    now = time.time()

    # --- Lifecycle transitions ---
    if schema.startswith("lifecycle.transition"):
        to_state = payload.get("to_state", "")
        from_state = payload.get("from_state", "")

        # Update Prime health
        if source == "jprime_lifecycle_controller":
            repo = state.repos.get("prime")
            if repo:
                repo.status = to_state
                repo.last_heartbeat = time.monotonic()

        state.events.append(TickerEvent(
            timestamp=now,
            category="lifecycle",
            message=f"{source}: {from_state} -> {to_state}",
            severity="warn" if to_state in ("DEGRADED", "DEAD") else "info",
        ))

    elif schema.startswith("lifecycle.health"):
        if source == "jprime_lifecycle_controller":
            repo = state.repos.get("prime")
            if repo:
                repo.last_heartbeat = time.monotonic()
                repo.latency_ms = payload.get("latency_ms", 0.0)

    elif schema.startswith("lifecycle.hardware"):
        repo = state.repos.get("prime")
        if repo:
            repo.latency_ms = payload.get("inference_latency_ms", repo.latency_ms)

    # --- Faults ---
    elif schema.startswith("fault.raised"):
        state.total_faults += 1
        fault_class = payload.get("fault_class", "unknown")
        state.events.append(TickerEvent(
            timestamp=now,
            category="fault",
            message=f"FAULT: {fault_class}",
            severity="error",
        ))

    elif schema.startswith("fault.resolved"):
        state.total_recoveries += 1
        state.events.append(TickerEvent(
            timestamp=now,
            category="recovery",
            message=f"RESOLVED: {payload.get('fault_class', 'unknown')}",
            severity="info",
        ))

    # --- Recovery ---
    elif schema.startswith("recovery.attempt"):
        state.events.append(TickerEvent(
            timestamp=now,
            category="recovery",
            message=f"Recovery attempt: {payload.get('target', 'unknown')}",
            severity="warn",
        ))

    # --- Agents ---
    elif schema.startswith("scheduler.graph_state"):
        state.total_agents = payload.get("total_agents", state.total_agents)
        state.initialized_agents = payload.get("initialized", state.initialized_agents)
        state.events.append(TickerEvent(
            timestamp=now,
            category="agent",
            message=f"Agents: {state.initialized_agents}/{state.total_agents} initialized",
        ))
        # Update JARVIS repo health
        repo = state.repos.get("jarvis")
        if repo and state.initialized_agents > 0:
            repo.status = "ONLINE"
            repo.last_heartbeat = time.monotonic()

    elif schema.startswith("scheduler.unit_state"):
        agent_name = payload.get("agent_name", "")
        agent_state = payload.get("state", "")
        if agent_state in ("running", "initialized"):
            state.events.append(TickerEvent(
                timestamp=now,
                category="agent",
                message=f"{agent_name}: {agent_state}",
            ))

    # --- Reasoning ---
    elif schema.startswith("reasoning.decision"):
        state.events.append(TickerEvent(
            timestamp=now,
            category="reasoning",
            message=f"Decision: {payload.get('command', '')[:60]}",
        ))

    elif schema.startswith("reasoning.activation"):
        state.events.append(TickerEvent(
            timestamp=now,
            category="reasoning",
            message=f"Gate: {payload.get('from_state', '')} -> {payload.get('to_state', '')}",
        ))

    elif schema.startswith("reasoning.proactive_drive"):
        drive_state = payload.get("state", "")
        if drive_state in ("ELIGIBLE", "EXPLORING"):
            state.proactive_explorations += 1
            state.events.append(TickerEvent(
                timestamp=now,
                category="proactive",
                message=f"ProactiveDrive: {drive_state}",
            ))


# ---------------------------------------------------------------------------
# Boot phase tracking (called from main loop, not from TelemetryBus)
# ---------------------------------------------------------------------------

class BootTracker:
    """Thread-safe boot phase tracker.

    The supervisor calls begin_phase/end_phase from the main loop.
    The render thread reads DashboardState.boot_phases for display.
    Uses a thread-safe queue to avoid cross-thread mutation.
    """

    def __init__(self, state: DashboardState, event_queue: queue.Queue) -> None:
        self._state = state
        self._queue = event_queue
        self._phase_map: Dict[str, int] = {}

    def begin_phase(self, name: str) -> None:
        """Mark a boot phase as running."""
        idx = len(self._state.boot_phases)
        phase = BootPhase(name=name, status="running", started_at=time.monotonic())
        self._state.boot_phases.append(phase)
        self._phase_map[name] = idx

    def end_phase(self, name: str, success: bool = True, detail: str = "") -> None:
        """Mark a boot phase as complete or failed."""
        idx = self._phase_map.get(name)
        if idx is not None and idx < len(self._state.boot_phases):
            phase = self._state.boot_phases[idx]
            phase.status = "done" if success else "failed"
            phase.finished_at = time.monotonic()
            phase.detail = detail

    def skip_phase(self, name: str, reason: str = "") -> None:
        """Mark a boot phase as skipped."""
        phase = BootPhase(name=name, status="skipped", detail=reason)
        self._state.boot_phases.append(phase)
        self._phase_map[name] = len(self._state.boot_phases) - 1

    def mark_boot_complete(self) -> None:
        """Mark the entire boot sequence as complete."""
        self._state.boot_complete = True
        self._state.boot_elapsed_s = time.monotonic() - self._state.boot_start


# ---------------------------------------------------------------------------
# EliteDashboard
# ---------------------------------------------------------------------------

class EliteDashboard:
    """Production-grade CLI dashboard for the unified supervisor.

    Renders a three-panel rich.live layout in a daemon thread.
    Consumes TelemetryBus events via a thread-safe queue.

    Usage:
        dashboard = EliteDashboard()
        await dashboard.start()  # subscribes to bus, starts render thread
        dashboard.boot_tracker.begin_phase("Preflight")
        ...
        await dashboard.stop()
    """

    def __init__(self, enabled: bool = _ENABLED) -> None:
        self._enabled = enabled and sys.stdout.isatty()
        self._state = DashboardState()
        self._event_queue: queue.Queue = queue.Queue(maxsize=256)
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self.boot_tracker = BootTracker(self._state, self._event_queue)

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

    def activate_display(self) -> None:
        """Start the render thread — called AFTER boot completes.

        The supervisor calls this when JARVIS is fully online and ready
        for user interaction. Events buffered since ``start()`` are
        drained and rendered immediately.
        """
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return  # Already active

        # Install log handler so WARNING+ appears in ticker
        self._install_log_handler()

        # Start render thread
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._render_loop,
            name="elite_dashboard",
            daemon=True,
        )
        self._thread.start()
        logger.info("[EliteDashboard] Display activated (JARVIS online)")

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
                    dashboard._state.events.append(TickerEvent(
                        timestamp=time.time(),
                        category=category,
                        message=f"[{record.name.split('.')[-1]}] {msg}",
                        severity=severity,
                    ))
                except Exception:
                    pass  # Never crash the logging system

        handler = _DashboardLogHandler()
        handler.setLevel(logging.WARNING)
        logging.getLogger().addHandler(handler)

    async def stop(self) -> None:
        """Stop the render thread."""
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
            self._state.events.append(TickerEvent(
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

    def _render_loop(self) -> None:
        """Daemon thread: drain queue, update state, render via rich.Live.

        Uses ``screen=True`` for full terminal control — prevents the
        dashboard from stacking/duplicating when log output interleaves.
        Log messages are streamed into the Live Event Ticker panel
        instead of competing with the dashboard for cursor position.
        """
        try:
            from rich.live import Live
            from rich.console import Console

            console = Console()
            refresh_interval = 1.0 / _REFRESH_HZ

            with Live(
                _build_layout(self._state),
                console=console,
                refresh_per_second=_REFRESH_HZ,
                screen=True,
            ) as live:
                while not self._stop_flag.is_set():
                    # Drain envelope queue
                    drained = 0
                    while drained < 50:  # max 50 per frame to avoid starvation
                        try:
                            envelope = self._event_queue.get_nowait()
                            _process_envelope(self._state, envelope)
                            drained += 1
                        except queue.Empty:
                            break

                    # Update display
                    try:
                        live.update(_build_layout(self._state))
                    except Exception as render_exc:
                        logger.debug("[EliteDashboard] Render error: %s", render_exc)

                    self._stop_flag.wait(timeout=refresh_interval)

        except ImportError as exc:
            logger.warning("[EliteDashboard] rich not available: %s", exc)
        except Exception as exc:
            logger.error("[EliteDashboard] Render thread crashed: %s", exc)

    # ------------------------------------------------------------------
    # Direct state updates (for supervisor to call)
    # ------------------------------------------------------------------

    def update_repo_status(
        self, repo: str, status: str, latency_ms: float = 0.0,
    ) -> None:
        """Update a repo's health status directly (thread-safe for simple writes)."""
        r = self._state.repos.get(repo)
        if r:
            r.status = status
            r.last_heartbeat = time.monotonic()
            if latency_ms > 0:
                r.latency_ms = latency_ms

    def add_event(self, category: str, message: str, severity: str = "info") -> None:
        """Add an event directly to the ticker."""
        self._state.events.append(TickerEvent(
            timestamp=time.time(),
            category=category,
            message=message,
            severity=severity,
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
