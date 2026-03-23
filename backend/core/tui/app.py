"""JARVIS Live Agent Dashboard -- Textual TUI Application.

Replaces the wall-of-logs experience with a structured, tabbed,
real-time dashboard. Consumes TelemetryEnvelopes only -- never
imports supervisor or processor internals.
"""
from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime
from typing import Optional

import signal

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, TabbedContent, TabPane, RichLog

from backend.core.tui.pipeline_panel import PipelineData
from backend.core.tui.agents_panel import AgentsData
from backend.core.tui.system_panel import SystemData
from backend.core.tui.faults_panel import FaultsData
from backend.core.tui.bus_consumer import TelemetryBusConsumer, StatusBarData

logger = logging.getLogger(__name__)


class StatusBar(Static):
    """Always-visible one-line status summary."""

    def __init__(self, data: StatusBarData, **kwargs):
        super().__init__("", **kwargs)
        self._data = data

    def refresh_display(self) -> None:
        self.update(self._data.to_string())


class _ThreadSafeLinuxDriver:
    """Mixin that skips SIGTSTP/SIGCONT registration when not on the main thread.

    Root cause: Textual's LinuxDriver.__init__ unconditionally calls
    signal.signal(SIGTSTP, ...) which raises ValueError from non-main threads.
    Python only allows signal handlers in the main thread.

    This driver is identical to LinuxDriver except it wraps the signal calls
    in a main-thread guard. When on a daemon thread, Ctrl+Z suspend is simply
    not available — which is correct for JARVIS (it manages its own lifecycle).
    """

    @staticmethod
    def _safe_signal(signum, handler):
        """Register a signal handler only if on the main thread."""
        if threading.current_thread() is threading.main_thread():
            return signal.signal(signum, handler)
        return signal.getsignal(signum)  # return existing handler, no-op


def _patch_driver_class():
    """Create a thread-safe LinuxDriver by patching signal registration."""
    try:
        from textual.drivers.linux_driver import LinuxDriver

        class ThreadSafeLinuxDriver(LinuxDriver):
            def __init__(self, *args, **kwargs):
                # Temporarily replace signal.signal with our safe version
                _orig = signal.signal
                signal.signal = _ThreadSafeLinuxDriver._safe_signal
                try:
                    super().__init__(*args, **kwargs)
                finally:
                    signal.signal = _orig

        return ThreadSafeLinuxDriver
    except ImportError:
        return None


class JarvisDashboard(App):
    """JARVIS Live Agent Dashboard."""

    # Use our thread-safe driver that skips signal registration from daemon threads.
    _thread_safe_driver = _patch_driver_class()
    if _thread_safe_driver is not None:
        driver_class = _thread_safe_driver

    TITLE = "JARVIS Dashboard"
    CSS = """
    Screen {
        background: $surface;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pipeline_data = PipelineData()
        self.agents_data = AgentsData()
        self.system_data = SystemData()
        self.faults_data = FaultsData()
        self.status_data = StatusBarData()
        self.consumer = TelemetryBusConsumer(
            self.pipeline_data, self.agents_data,
            self.system_data, self.faults_data, self.status_data,
        )
        self._pipeline_log: Optional[RichLog] = None
        self._agents_log: Optional[RichLog] = None
        self._system_log: Optional[RichLog] = None
        self._faults_log: Optional[RichLog] = None
        self._status_bar: Optional[StatusBar] = None
        self._last_pipeline_count = 0
        self._last_system_count = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent():
            with TabPane("Pipeline", id="pipeline"):
                yield RichLog(id="pipeline-log", highlight=True, markup=True)
            with TabPane("Agents", id="agents"):
                yield RichLog(id="agents-log", highlight=True, markup=True)
            with TabPane("System", id="system"):
                yield RichLog(id="system-log", highlight=True, markup=True)
            with TabPane("Faults", id="faults"):
                yield RichLog(id="faults-log", highlight=True, markup=True)
        yield StatusBar(self.status_data, id="status-bar")

    def on_mount(self) -> None:
        self._pipeline_log = self.query_one("#pipeline-log", RichLog)
        self._agents_log = self.query_one("#agents-log", RichLog)
        self._system_log = self.query_one("#system-log", RichLog)
        self._faults_log = self.query_one("#faults-log", RichLog)
        self._status_bar = self.query_one("#status-bar", StatusBar)
        self.set_interval(1.0, self._refresh_panels)

    def _refresh_panels(self) -> None:
        self._refresh_pipeline()
        self._refresh_agents()
        self._refresh_system()
        self._refresh_faults()
        if self._status_bar:
            self._status_bar.refresh_display()

    def _refresh_pipeline(self) -> None:
        if not self._pipeline_log:
            return
        count = self.pipeline_data.total_commands
        if count == self._last_pipeline_count:
            return
        self._last_pipeline_count = count
        log = self._pipeline_log
        log.clear()
        for cmd in self.pipeline_data.commands:
            ts = datetime.fromtimestamp(cmd.timestamp).strftime("%H:%M:%S") if cmd.timestamp else "??:??:??"
            if cmd.is_proactive:
                log.write(f"[bold green]{ts}[/] [white]\"{cmd.command}\"[/] trace={cmd.trace_id}")
                log.write(f"  DETECT   proactive=true  conf={cmd.confidence:.2f}  signals={cmd.signals}")
                if cmd.expanded_intents:
                    log.write(f"  EXPAND   {len(cmd.expanded_intents)} intents {cmd.expanded_intents}")
                if cmd.mind_requests:
                    log.write(f"  MIND     {cmd.mind_requests} requests")
                if cmd.delegations:
                    log.write(f"  COORD    {cmd.delegations} delegations")
                log.write(f"  [bold green]DONE[/]     success={cmd.success_rate:.0%}  total={cmd.total_ms:.0f}ms")
            else:
                log.write(f"[dim]{ts}[/] [white]\"{cmd.command}\"[/] -> passthrough ({cmd.total_ms:.0f}ms)")

    def _refresh_agents(self) -> None:
        if not self._agents_log:
            return
        log = self._agents_log
        log.clear()
        log.write(f"[bold]AGENTS ({self.agents_data.initialized}/{self.agents_data.total_agents} initialized)[/]")
        log.write("")
        critical_names = {"coordinator_agent", "predictive_planner"}
        for name, agent in sorted(self.agents_data.agents.items()):
            is_critical = name in critical_names
            color = "green" if agent.state == "idle" else "yellow" if agent.state == "busy" else "red"
            prefix = "[bold]*[/]" if is_critical else " "
            tasks_str = f"  tasks:{agent.tasks_completed}" if agent.tasks_completed else ""
            log.write(f"  {prefix} [{color}]{name:<25}[/] [{color}]{agent.state}[/]{tasks_str}")

    def _refresh_system(self) -> None:
        if not self._system_log:
            return
        count = len(self.system_data.recent_transitions)
        if count == self._last_system_count and count > 0:
            return
        self._last_system_count = count
        log = self._system_log
        log.clear()
        lc = self.system_data.lifecycle_state
        lc_color = "green" if lc == "READY" else "yellow" if lc == "DEGRADED" else "red"
        log.write("[bold]J-PRIME LIFECYCLE[/]")
        log.write(f"  State:    [{lc_color}]{lc}[/]")
        log.write(f"  Restarts: {self.system_data.lifecycle_restarts}")
        log.write("")
        gs = self.system_data.gate_state
        gs_color = "green" if gs == "ACTIVE" else "yellow" if gs == "DEGRADED" else "red" if gs in ("BLOCKED", "TERMINAL") else "dim"
        log.write("[bold]REASONING GATE[/]")
        log.write(f"  State:    [{gs_color}]{gs}[/]")
        log.write(f"  Sequence: {self.system_data.gate_sequence}")
        if self.system_data.gate_deps:
            deps_str = "  ".join(f"{k}={v}" for k, v in self.system_data.gate_deps.items())
            log.write(f"  Deps:     {deps_str}")
        log.write("")
        # --- TELEMETRY BUS (spec: only exception to envelopes-only rule) ---
        try:
            from backend.core.telemetry_contract import get_telemetry_bus
            m = get_telemetry_bus().get_metrics()
            log.write("[bold]TELEMETRY BUS[/]")
            log.write(f"  Emitted:     {m.get('emitted', 0):,}")
            log.write(f"  Delivered:   {m.get('delivered', 0):,}")
            log.write(f"  Dropped:     {m.get('dropped', 0):,}")
            log.write(f"  Deduped:     {m.get('deduped', 0):,}")
            log.write(f"  Dead-letter: {m.get('dead_letter', 0):,}")
            log.write(f"  Queue:       {m.get('queue_size', 0):,}")
        except Exception:
            log.write("[dim]TELEMETRY BUS  (unavailable)[/]")
        log.write("")
        log.write("[bold]RECENT TRANSITIONS[/]")
        for t in list(self.system_data.recent_transitions)[-10:]:
            ts = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
            log.write(f"  {ts}  {t.domain:<10} {t.from_state} -> {t.to_state}  ({t.trigger})")

    def _refresh_faults(self) -> None:
        if not self._faults_log:
            return
        log = self._faults_log
        log.clear()
        log.write(f"[bold]ACTIVE FAULTS ({len(self.faults_data.active_faults)})[/]")
        if not self.faults_data.active_faults:
            log.write("  [dim](none)[/]")
        for f in self.faults_data.active_faults:
            ts = datetime.fromtimestamp(f.timestamp).strftime("%H:%M:%S")
            log.write(f"  [red]{ts}[/]  {f.component}  {f.fault_class}  {f.message}")
        log.write("")
        log.write(f"[bold]RESOLVED ({len(self.faults_data.resolved_faults)})[/]")
        for f in list(self.faults_data.resolved_faults)[-10:]:
            ts = datetime.fromtimestamp(f.timestamp).strftime("%H:%M:%S")
            log.write(f"  [dim]{ts}[/]  {f.component}  {f.fault_class}  {f.resolution}  ({f.duration_ms:.0f}ms)")


def start_dashboard() -> Optional[threading.Thread]:
    """Start the TUI dashboard in a daemon thread.

    Returns the thread if started, None if no terminal.

    Root cause fix (2026-03-22): Textual's LinuxDriver.__init__ calls
    signal.signal(SIGTSTP, ...) which raises ValueError when not on the
    main thread. The fix is to run with headless=True from a daemon thread —
    this skips the signal handler registration while still rendering to the
    terminal via the headless driver. The dashboard still receives telemetry
    and updates panels; it just can't handle Ctrl+Z suspend/resume (which
    is fine — JARVIS manages its own lifecycle).
    """
    if not sys.stdout.isatty():
        logger.info("[TUI] No terminal -- dashboard skipped")
        return None

    try:
        from backend.core.telemetry_contract import get_telemetry_bus

        app = JarvisDashboard()
        bus = get_telemetry_bus()

        async def bus_handler(envelope):
            app.consumer.handle_sync(envelope)

        bus.subscribe("*", bus_handler)

        # JarvisDashboard uses ThreadSafeLinuxDriver which skips signal
        # registration from non-main threads. No headless mode needed.
        def _run_dashboard():
            try:
                app.run()
            except Exception as exc:
                logger.warning("[TUI] Dashboard crashed: %s", exc)

        thread = threading.Thread(
            target=_run_dashboard,
            name="jarvis-tui-dashboard",
            daemon=True,
        )
        thread.start()
        logger.info("[TUI] Dashboard started in daemon thread")
        return thread
    except Exception as exc:
        logger.warning("[TUI] Dashboard failed to start: %s", exc)
        return None
