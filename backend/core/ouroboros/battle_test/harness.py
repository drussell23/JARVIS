"""BattleTestHarness -- orchestrates Ouroboros boot, event-driven session loop, and shutdown.

The harness is the centerpiece of the battle test runner.  It boots the full
Ouroboros brain in headless mode, waits for one of three stop signals
(shutdown, budget, idle), then tears everything down and generates a summary
report.

All imports of real Ouroboros components are performed lazily *inside* method
bodies so that this module is importable even when the full stack has missing
dependencies.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Memory-type decoration (used by /memory Rich renderers)
# ---------------------------------------------------------------------------
def _memory_type_emoji(type_name: str) -> str:
    return {
        "user": "👤",
        "feedback": "🗣",
        "project": "📋",
        "reference": "🔗",
        "forbidden_path": "🚫",
        "style": "🎨",
    }.get(type_name.lower(), "•")


def _memory_border_for_type(type_name: str) -> str:
    return {
        "user": "cyan",
        "feedback": "yellow",
        "project": "blue",
        "reference": "magenta",
        "forbidden_path": "red",
        "style": "green",
    }.get(type_name.lower(), "white")


# ---------------------------------------------------------------------------
# HarnessConfig
# ---------------------------------------------------------------------------


@dataclass
class HarnessConfig:
    """Configuration for the BattleTestHarness.

    Parameters
    ----------
    repo_path:
        Path to the repository root.
    cost_cap_usd:
        Maximum API spend for the session.
    idle_timeout_s:
        Seconds of inactivity before the session is stopped.
    max_wall_seconds_s:
        Hard wall-clock ceiling on total session duration. ``None`` or
        ``<=0`` disables the cap (legacy behavior — only ``idle_timeout_s``
        and ``cost_cap_usd`` bound the session). When set and exceeded,
        the session terminates with ``stop_reason="wall_clock_cap"`` via
        the same graceful-shutdown path used by ``idle_timeout``. Added
        per Ticket A (2026-04-23) to prevent provider retry storms from
        defeating both idle-gap and budget watchdogs. Graduation soaks
        MUST set this to a bounded value (e.g. 2400 = 40 min).
    headless:
        Ticket C (2026-04-23). Tri-state flag controlling whether the
        harness starts the interactive ``SerpentREPL`` input task.
        ``True`` → skip REPL (agent-conducted / CI / daemon runs).
        ``False`` → force interactive REPL even when stdin isn't a TTY
        (rare; escape hatch). ``None`` (default) → auto-detect via
        ``not sys.stdin.isatty()`` which is correct for every real
        headless launch pattern (background shell, CI runner, pipeline).
        When headless, the REPL's ``PromptSession.prompt_async()`` loop
        is never started, so graduation soaks no longer need the
        opaque ``tail -f /dev/null | ...`` stdin guard to prevent a
        ``EOFError → break`` exit in ~16 log lines. Other TUI surfaces
        (SerpentFlow renderer, CommProtocol transports, status line)
        remain active.
    branch_prefix:
        Prefix for the accumulation branch name.
    session_dir:
        Directory for session artifacts.  Auto-generated when ``None``.
    notebook_output_dir:
        Directory for the generated notebook.  Defaults to ``"notebooks"``.
    """

    repo_path: Path = field(default_factory=lambda: Path("."))
    cost_cap_usd: float = 0.50
    idle_timeout_s: float = 600.0
    max_wall_seconds_s: Optional[float] = None
    headless: Optional[bool] = None
    branch_prefix: str = "ouroboros/battle-test"
    session_dir: Optional[Path] = None
    notebook_output_dir: Optional[Path] = None

    @classmethod
    def from_env(cls) -> HarnessConfig:
        """Build a HarnessConfig from environment variables.

        Reads:
        - ``OUROBOROS_BATTLE_COST_CAP``
        - ``OUROBOROS_BATTLE_IDLE_TIMEOUT``
        - ``OUROBOROS_BATTLE_MAX_WALL_SECONDS`` (``0`` or unset = disabled)
        - ``OUROBOROS_BATTLE_HEADLESS`` (``1``/``true`` → True,
          ``0``/``false`` → False, unset → None = auto-detect)
        - ``OUROBOROS_BATTLE_BRANCH_PREFIX``
        - ``JARVIS_REPO_PATH``
        """
        _wall = float(os.environ.get("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "0"))
        _headless_raw = os.environ.get("OUROBOROS_BATTLE_HEADLESS", "").strip().lower()
        _headless: Optional[bool]
        if _headless_raw in ("1", "true", "yes", "on"):
            _headless = True
        elif _headless_raw in ("0", "false", "no", "off"):
            _headless = False
        else:
            _headless = None
        return cls(
            repo_path=Path(os.environ.get("JARVIS_REPO_PATH", ".")),
            cost_cap_usd=float(os.environ.get("OUROBOROS_BATTLE_COST_CAP", "0.50")),
            idle_timeout_s=float(os.environ.get("OUROBOROS_BATTLE_IDLE_TIMEOUT", "600.0")),
            max_wall_seconds_s=_wall if _wall > 0 else None,
            headless=_headless,
            branch_prefix=os.environ.get("OUROBOROS_BATTLE_BRANCH_PREFIX", "ouroboros/battle-test"),
        )

    def resolve_headless(self) -> bool:
        """Resolve the tri-state ``headless`` field into a concrete bool.

        ``True`` / ``False`` are returned unchanged. ``None`` triggers
        auto-detect via ``not sys.stdin.isatty()`` — correct for every
        real headless launch pattern (Claude Code Bash-tool background,
        CI runner, daemon pipeline). Guarded against rare
        ``OSError`` / ``ValueError`` from ``isatty()`` on closed or
        invalid stdin (treat as headless, same pattern as
        ``_headless_auto_approve_reason`` in serpent_flow.py).
        """
        if self.headless is not None:
            return self.headless
        try:
            return not sys.stdin.isatty()
        except (ValueError, OSError):
            return True


# ---------------------------------------------------------------------------
# BattleTestHarness
# ---------------------------------------------------------------------------


class BattleTestHarness:
    """Orchestrates the full Ouroboros boot, event-driven session loop, and shutdown.

    Parameters
    ----------
    config:
        A :class:`HarnessConfig` instance.
    """

    def __init__(self, config: HarnessConfig) -> None:
        self._config = config

        # Session identity
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        self._session_id = f"bt-{ts}"

        # Session directories
        self._session_dir = config.session_dir or Path(f".ouroboros/sessions/{self._session_id}")
        self._notebook_output_dir = config.notebook_output_dir or Path("notebooks")

        # Publish session dir for downstream non-streaming callers
        # (e.g. CompactionCallerStrategy writes compaction_shadow.jsonl here).
        os.environ.setdefault("JARVIS_OUROBOROS_SESSION_DIR", str(self._session_dir))

        # Battle-test utilities
        self._cost_tracker = CostTracker(
            budget_usd=config.cost_cap_usd,
            persist_path=self._session_dir / "cost_tracker.json",
        )
        self._idle_watchdog = IdleWatchdog(timeout_s=config.idle_timeout_s)
        self._session_recorder = SessionRecorder(session_id=self._session_id)

        # Stop signals — created lazily in run() to avoid event-loop mismatch on Python 3.9
        self._shutdown_event: Optional[asyncio.Event] = None
        self._stop_reason: str = "unknown"
        self._started_at: float = 0.0

        # Partial-shutdown insurance: flipped True by _generate_report's
        # save_summary call. Read by the atexit fallback — if False at
        # interpreter shutdown, the fallback writes a minimal best-effort
        # summary.json so the session dir is never left with only
        # debug.log. Covers the gap where SIGTERM + asyncio finally can't
        # complete (exception in shutdown components, parent kills hard
        # before async cleanup, interpreter teardown during async work).
        self._summary_written: bool = False
        self._install_atexit_fallback()

        # Harness Epic Slice 1 — bounded shutdown watchdog.
        # Daemon thread, idle until ``arm()``-ed by signal handler / wall
        # cap. On arm, sleeps the deadline; if not disarmed, calls
        # ``os._exit(75)``. Closes the 14-incident Py_FinalizeEx zombie
        # class + S5/S6 SIGTERM-partial-summary regression + S6
        # WallClockWatchdog asyncio-task starvation.
        from backend.core.ouroboros.battle_test.shutdown_watchdog import (
            BoundedShutdownWatchdog as _BoundedShutdownWatchdog,
        )
        self._shutdown_watchdog = _BoundedShutdownWatchdog()

        # TerminationHookRegistry Slice 3 — install per-process
        # active-harness singleton so termination hooks dispatched
        # from contexts where the harness instance is not in scope
        # (signal-handler callbacks, wall-clock watchdog tasks)
        # can resolve us via get_active_harness(). Then run the
        # auto-discovery loop once so the default adapters' hook
        # (partial_summary_writer) is installed in the registry.
        # NEVER raises — both are documented defensive surfaces.
        try:
            from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
                set_active_harness as _set_active_harness,
            )
            _set_active_harness(self)
            from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
                discover_and_register_default as _disc_term,
                get_default_registry as _term_default_registry,
            )
            _disc_term()
            # Slice 4 — wire the registry's dispatch-listener
            # channel to the IDE SSE broker so consumers can
            # subscribe to ``termination_hook_dispatched`` events.
            # Best-effort: a missing broker (cold IDE-stream
            # subsystem) degrades to a noop listener — never
            # blocks termination-hook firing. The listener is
            # registered ONCE at boot; the registry's listener
            # list is shared across dispatches.
            try:
                from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                    EVENT_TYPE_TERMINATION_HOOK_DISPATCHED,
                    get_default_broker,
                )

                def _publish_termination_event(payload: dict) -> None:
                    try:
                        if payload.get("event_type") != (
                            "termination_hook_dispatched"
                        ):
                            return
                        broker = get_default_broker()
                        if broker is None:
                            return
                        dr = payload.get("dispatch_result", {})
                        broker.publish(
                            event_type=(
                                EVENT_TYPE_TERMINATION_HOOK_DISPATCHED
                            ),
                            op_id=str(dr.get("cause", "") or ""),
                            payload=dr,
                        )
                    except Exception:  # noqa: BLE001 — defensive
                        # Listener exception is already swallowed
                        # by the registry's _fire_dispatch, but
                        # belt-and-suspender it here too.
                        pass

                _term_default_registry().on_transition(
                    _publish_termination_event,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[Harness] termination-SSE bridge "
                    "degraded: %s", exc,
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[Harness] termination-hook wire-up degraded: %s",
                exc,
            )

        # Component references (populated during boot)
        self._oracle: Any = None
        self._governance_stack: Any = None
        self._governed_loop_service: Any = None
        self._predictive_engine: Any = None
        self._branch_manager: Any = None
        self._branch_name: Optional[str] = None
        self._intake_service: Any = None
        self._intake_paused: bool = False
        self._graduation_orchestrator: Any = None
        self._plan_before_execute: bool = (
            os.environ.get("JARVIS_SHOW_PLAN_BEFORE_EXECUTE", "").strip().lower()
            in _TRUTHY
        )

    # ------------------------------------------------------------------
    # Partial-shutdown insurance (atexit fallback)
    # ------------------------------------------------------------------

    def _install_atexit_fallback(self) -> None:
        """Register the atexit fallback summary writer.

        The fallback ensures that every battle-test session dir ends up
        with a ``summary.json`` — even when the clean async path in
        ``_generate_report`` never completed (SIGTERM arrives mid-cleanup,
        exception in ``_shutdown_components``, parent sends SIGKILL
        shortly after SIGTERM, interpreter teardown during async work).

        atexit cannot intercept ``SIGKILL`` (signal 9, uncatchable) or a
        hard ``os._exit``; those remain unrecoverable by design. For every
        other exit path — normal exit, uncaught exception, SIGTERM-driven
        shutdown — atexit fires exactly once after the interpreter stops
        running user code but before the process goes away.

        The fallback is a no-op when ``_summary_written`` is True (the
        clean path already wrote a full summary).
        """
        atexit.register(self._atexit_fallback_write)

    def _atexit_fallback_write(self, session_outcome: Optional[str] = None) -> None:
        """Best-effort synchronous summary.json writer for partial shutdown.

        Stamps ``stop_reason="partial_shutdown:atexit_fallback"`` (or
        preserves the existing ``_stop_reason`` if it has a signal-driven
        value like ``shutdown_signal``, with a ``+atexit`` suffix so
        downstream can tell the clean path didn't finish). Uses the
        SessionRecorder's already-accumulated state (stats, ops_digest,
        operations list) so partial sessions still yield v1.1a-compatible
        summaries — LastSessionSummary on the next boot can parse them
        the same way it parses clean summaries.

        Ticket B (2026-04-23): when called from the signal-driven path
        (SIGHUP / SIGTERM / SIGINT) the caller passes
        ``session_outcome="incomplete_kill"`` which gets stamped on the
        partial summary so audit tooling can distinguish clean vs
        interrupted sessions without parsing free-form ``stop_reason``
        strings. Also captures ``last_activity_ts`` best-effort from
        the idle watchdog's monotonic clock.

        Never raises: any exception is logged and swallowed. Running
        during interpreter teardown means we can't trust the event loop
        or many imports; this writer is pure-sync and defensive.
        """
        if self._summary_written:
            return  # Clean path won — no fallback needed.
        try:
            session_dir = self._session_dir
            session_dir.mkdir(parents=True, exist_ok=True)

            # Preserve any meaningful stop_reason already stamped; suffix
            # it with "+atexit" so the caller can distinguish the partial
            # case. When nothing was stamped ("unknown" default), use an
            # explicit partial-shutdown tag.
            raw = self._stop_reason or "unknown"
            if raw in ("unknown", ""):
                reason = "partial_shutdown:atexit_fallback"
            else:
                reason = f"{raw}+atexit_fallback"

            # Duration: best-effort from _started_at if run() got that far.
            duration_s = (
                time.time() - self._started_at if self._started_at else 0.0
            )

            # Cost snapshot — CostTracker exposes `total_spent` / `breakdown`
            # as live accumulators; read defensively.
            try:
                cost_total = float(self._cost_tracker.total_spent)
                cost_breakdown = dict(self._cost_tracker.breakdown)
            except Exception:  # noqa: BLE001
                cost_total = 0.0
                cost_breakdown = {}

            # Branch stats: if the branch manager made it up, snapshot;
            # otherwise emit zeros so downstream parsing stays stable.
            branch_stats: dict = {
                "commits": 0, "files_changed": 0,
                "insertions": 0, "deletions": 0,
            }
            try:
                if self._branch_manager is not None:
                    branch_stats = self._branch_manager.get_diff_stats()
            except Exception:  # noqa: BLE001
                pass

            # Ticket B (v1.1b): stamp session_outcome when caller provided
            # it (signal-driven path sends "incomplete_kill"). Best-effort
            # last-activity timestamp from the idle watchdog's monotonic
            # clock converted to wall-clock via _started_at.
            _last_activity_ts: Optional[float] = None
            try:
                _wd = self._idle_watchdog
                if self._started_at and _wd is not None:
                    _last_poke = getattr(_wd, "_last_poke", None)
                    if _last_poke is not None:
                        # Watchdog uses time.monotonic(); convert to wall
                        # clock by anchoring at _started_at.
                        _last_activity_ts = self._started_at + (
                            _last_poke - getattr(_wd, "_start_monotonic", _last_poke)
                        )
            except Exception:  # noqa: BLE001
                _last_activity_ts = None

            self._session_recorder.save_summary(
                output_dir=session_dir,
                stop_reason=reason,
                duration_s=duration_s,
                cost_total=cost_total,
                cost_breakdown=cost_breakdown,
                branch_stats=branch_stats,
                convergence_state="INSUFFICIENT_DATA",
                convergence_slope=0.0,
                convergence_r2=0.0,
                session_outcome=session_outcome,
                last_activity_ts=_last_activity_ts,
            )
            self._summary_written = True
            # Can't trust logger during interpreter teardown — fallback
            # to stderr for visibility if the log handlers are gone.
            try:
                logger.warning(
                    "[Harness] atexit fallback wrote partial summary.json "
                    "(stop_reason=%s session=%s)",
                    reason, self._session_id,
                )
            except Exception:  # noqa: BLE001
                import sys as _sys
                _sys.stderr.write(
                    f"[Harness] atexit fallback wrote partial summary.json "
                    f"(stop_reason={reason} session={self._session_id})\n"
                )
        except Exception as exc:  # noqa: BLE001
            try:
                logger.error(
                    "[Harness] atexit fallback failed: %r", exc,
                )
            except Exception:  # noqa: BLE001
                import sys as _sys
                _sys.stderr.write(
                    f"[Harness] atexit fallback failed: {exc!r}\n"
                )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """Unique session identifier in ``bt-YYYY-MM-DD-HHMMSS`` format."""
        return self._session_id

    @property
    def stop_reason(self) -> str:
        """Terminal reason this session stopped.

        Common values: ``shutdown_signal``, ``budget_exhausted``, ``idle_timeout``,
        ``stale_ops_detected``, ``boot_failure: ...``, ``restart_pending: ...``.
        The wrapper script reads this after `run()` returns to decide whether
        to re-exec on the hot-reload restart sentinel.
        """
        return self._stop_reason

    # ------------------------------------------------------------------
    # Main lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main lifecycle method: boot, wait for stop signal, shutdown, report."""
        self._started_at = time.time()
        # Create events inside the running loop (Python 3.9 compat)
        self._shutdown_event = asyncio.Event()
        self._cost_tracker.budget_event = asyncio.Event()
        self._idle_watchdog.idle_event = asyncio.Event()
        # Ticket A Guard 2: wall-clock ceiling. Event fires when total session
        # wall time exceeds config.max_wall_seconds_s. Prevents provider retry
        # storms from hijacking both --idle-timeout (reset by retry activity)
        # and budget caps (retries may not be billable). Disabled when
        # max_wall_seconds_s is None or <= 0.
        self._wall_clock_event: asyncio.Event = asyncio.Event()

        # Publish the session id to the strategic_direction module global so
        # that the Orchestrator's CLASSIFY-phase GoalActivityLedger append can
        # stamp rows with the correct session. Cleared in _generate_report.
        try:
            from backend.core.ouroboros.governance.strategic_direction import (
                set_active_session_id,
            )
            set_active_session_id(self._session_id)
        except Exception:  # noqa: BLE001
            logger.debug("set_active_session_id(boot) failed", exc_info=True)

        # LastSessionSummary v0.1: same pattern — stamp the active session
        # id so the self-skip logic knows NOT to read our own still-empty
        # summary.json (which only gets written at session end).
        try:
            from backend.core.ouroboros.governance.last_session_summary import (
                set_active_session_id as _lss_set_active,
            )
            _lss_set_active(self._session_id)
        except Exception:  # noqa: BLE001
            logger.debug("lss set_active_session_id(boot) failed", exc_info=True)

        # LastSessionSummary v1.1a: register SessionRecorder as the process-
        # wide OpsDigestObserver so orchestrator / AutoCommitter call sites
        # reach the harness without importing the recorder directly. This
        # is the only seam between governance code (hook consumer) and
        # harness code (digest implementer) — keeps dependency direction
        # clean (governance → observer protocol only).
        try:
            from backend.core.ouroboros.governance.ops_digest_observer import (
                register_ops_digest_observer,
            )
            register_ops_digest_observer(self._session_recorder)
        except Exception:  # noqa: BLE001
            logger.debug("register_ops_digest_observer(boot) failed", exc_info=True)

        try:
            # Boot sequence
            await self.boot_oracle()
            await self.boot_governance_stack()
            await self.boot_governed_loop_service()
            await self.boot_jarvis_tiers()
            self._branch_name = await self.create_branch()
            await self.boot_intake()
            await self.boot_graduation()

            # Wire SerpentApprovalProvider — wraps the inner CLIApprovalProvider
            # with diff preview + interactive [Y/n] Iron Gate prompt when
            # SerpentFlow is the active transport.
            try:
                if hasattr(self, "_serpent_flow") and self._serpent_flow is not None:
                    _gls_ref = self._governed_loop_service
                    _inner_ap = getattr(_gls_ref, "_approval_provider", None)
                    if _inner_ap is not None:
                        from backend.core.ouroboros.battle_test.serpent_flow import SerpentApprovalProvider
                        _serpent_ap = SerpentApprovalProvider(flow=self._serpent_flow, inner=_inner_ap)
                        _gls_ref._approval_provider = _serpent_ap
                        # Also update the orchestrator's reference
                        _orch = getattr(_gls_ref, "_orchestrator", None)
                        if _orch is not None:
                            _orch._approval_provider = _serpent_ap
                        logger.info("SerpentApprovalProvider wired (Iron Gate prompt active)")
            except Exception as _ap_exc:
                logger.debug("SerpentApprovalProvider wiring failed: %s", _ap_exc)

            logger.info(
                "Ouroboros is alive — session %s | budget=$%.2f | idle=%ds",
                self._session_id,
                self._config.cost_cap_usd,
                int(self._config.idle_timeout_s),
            )
            # ── Compact boot banner via SerpentFlow.boot_banner() ──
            # Detects active subsystems and renders a single Rich Panel
            # instead of 30+ loose print lines.
            _gls = self._governed_loop_service
            _has_consciousness = (
                _gls is not None
                and getattr(_gls, "_consciousness_bridge", None) is not None
            )
            _has_strategic = (
                _gls is not None
                and getattr(_gls, "_strategic_direction", None) is not None
                and getattr(_gls._strategic_direction, "is_loaded", False)
            )
            _has_tool_loop = (
                _gls is not None
                and getattr(_gls, "_config", None) is not None
                and getattr(_gls._config, "tool_use_enabled", False)
            )
            _has_l2 = bool(os.environ.get("JARVIS_L2_ENABLED", "").lower() == "true")
            _has_bg_pool = (
                _gls is not None
                and getattr(_gls, "_bg_pool", None) is not None
            )

            _n_principles = 0
            if _has_strategic:
                _n_principles = len(_gls._strategic_direction.principles)

            _pool_info = (
                f"parallel ({getattr(_gls._bg_pool, '_pool_size', 2)} workers)"
                if _has_bg_pool else "sequential"
            )
            _venom_info = "bash + web + tests + L2" if _has_l2 else "tools active"

            # Build 6-layer status: (icon, name, is_on, detail)
            _layers = [
                ("🧭", "Strategic Direction", _has_strategic, f"{_n_principles} Manifesto principles"),
                ("🧠", "Consciousness", _has_consciousness, "Memory + Prophecy + Health"),
                ("📡", "Event Spine", True, "FileWatch → TrinityBus → sensors"),
                ("⚙️ ", "Ouroboros Pipeline", True, _pool_info),
                ("🐍", "Venom Agentic Loop", _has_tool_loop, _venom_info),
                ("📝", "Thought Log", True, "ouroboros_thoughts.jsonl"),
            ]

            _log_path = getattr(self, "_log_file_path", "")

            if hasattr(self, "_serpent_flow") and self._serpent_flow is not None:
                self._serpent_flow.boot_banner(
                    layers=_layers,
                    n_sensors=0,  # Updated below after intake boot
                    log_path=_log_path,
                )
            else:
                # Fallback: basic Rich console output
                from rich.console import Console as _C
                _c = _C(emoji=True, highlight=False)
                _c.print("[bold cyan]🐍 OUROBOROS + VENOM[/bold cyan]", highlight=False)
                _c.print(f"  Session: {self._session_id}", highlight=False)
                _c.print(f"  Branch:  {self._branch_name or 'N/A'}", highlight=False)
                _c.print(f"  Budget:  ${self._config.cost_cap_usd:.2f}", highlight=False)
                _c.print()

            # Subscribe to operation completion events for session recording
            try:
                _emitter = getattr(self._governed_loop_service, "_event_emitter", None)
                if _emitter is not None:
                    from backend.core.ouroboros.governance.autonomy.autonomy_types import (
                        EventType,
                    )

                    async def _on_op_completed(event: Any) -> None:
                        """Record completed operations with cost data for notebook."""
                        try:
                            p = event.payload
                            status = "completed" if p.get("success") else "failed"
                            if p.get("rollback"):
                                status = "rolled_back"
                            self._session_recorder.record_operation(
                                op_id=p.get("op_id", ""),
                                status=status,
                                sensor=p.get("outcome_source", "unknown"),
                                technique=p.get("provider", "unknown"),
                                composite_score=0.0,
                                elapsed_s=p.get("duration_s", 0.0),
                                provider=p.get("provider", ""),
                                cost_usd=p.get("cost_usd", 0.0),
                                input_tokens=p.get("input_tokens", 0),
                                output_tokens=p.get("output_tokens", 0),
                                cached_tokens=p.get("cached_tokens", 0),
                                tool_calls=p.get("tool_calls", 0),
                                files_changed=len(p.get("affected_files", [])),
                            )
                            # Show cost in TUI / dashboard
                            if hasattr(self, "_serpent_flow") and self._serpent_flow:
                                self._serpent_flow.update_cost(
                                    total=self._cost_tracker.total_spent,
                                    remaining=self._cost_tracker.remaining,
                                    breakdown=self._cost_tracker.breakdown,
                                )
                            elif hasattr(self, "_tui_console") and self._tui_console:
                                self._tui_console.show_cost_update(
                                    total=self._cost_tracker.total_spent,
                                    remaining=self._cost_tracker.remaining,
                                    breakdown=self._cost_tracker.breakdown,
                                )
                            logger.debug(
                                "SessionRecorder: recorded op=%s status=%s provider=%s cost=$%.4f",
                                p.get("op_id", "")[:16], status,
                                p.get("provider", ""), p.get("cost_usd", 0.0),
                            )
                        except Exception:
                            pass  # Recording is non-critical

                    _emitter.subscribe(
                        EventType.OP_COMPLETED, _on_op_completed,
                    )
                    _emitter.subscribe(
                        EventType.OP_ROLLED_BACK, _on_op_completed,
                    )
                    logger.info("SessionRecorder subscribed to operation events")
            except Exception as exc:
                logger.debug("SessionRecorder event subscription failed: %s", exc)

            # Start dashboard / TUI controls
            if hasattr(self, "_serpent_flow") and self._serpent_flow is not None:
                # Update flow with detected sensor count
                _intake = self._intake_service
                if _intake is not None:
                    n_sensors = len(getattr(_intake, "_sensors", []))
                    self._serpent_flow.update_sensors(n_sensors)
                await self._serpent_flow.start()

                # Boot non-blocking REPL (prompt_toolkit) — runs alongside
                # background telemetry without blocking the event loop.
                #
                # Ticket C (2026-04-23): skip the REPL entirely in
                # headless mode. Starting PromptSession.prompt_async()
                # against a non-TTY stdin hits `EOFError → break` on the
                # first iteration and ends the session in ~16 log lines.
                # The tail -f /dev/null stdin guard documented in the
                # matrix runbook was the prior workaround; this native
                # skip retires it. Other surfaces (SerpentFlow renderer,
                # CommProtocol transports, status line) stay active.
                if self._config.resolve_headless():
                    logger.info(
                        "[Harness] Headless mode: REPL input disabled "
                        "(headless=%s, stdin.isatty=%s)",
                        self._config.headless,
                        getattr(sys.stdin, "isatty", lambda: None)()
                        if hasattr(sys.stdin, "isatty") else None,
                    )
                else:
                    try:
                        from backend.core.ouroboros.battle_test.serpent_flow import SerpentREPL
                        self._serpent_repl = SerpentREPL(
                            flow=self._serpent_flow,
                            on_command=self._handle_repl_command,
                        )
                        await self._serpent_repl.start()
                    except Exception as _repl_exc:
                        logger.debug("SerpentREPL not available: %s", _repl_exc)
            elif hasattr(self, "_tui_console") and self._tui_console is not None:
                self._tui_console.show_controls_bar()
            if hasattr(self, "_keyboard_handler") and self._keyboard_handler is not None:
                await self._keyboard_handler.start()

            # Start idle watchdog
            await self._idle_watchdog.start()

            # Start GLS activity monitor — pokes watchdog when operations are in-flight
            self._activity_monitor_task = asyncio.ensure_future(self._monitor_gls_activity())

            # Start provider cost monitor — feeds real API spend into CostTracker
            self._cost_monitor_task = asyncio.ensure_future(self._monitor_provider_costs())

            # Ticket A Guard 2: wall-clock watchdog. Opaque hard ceiling on
            # total session duration — fires independently of any activity
            # signal so retry storms cannot hijack termination. Only spawned
            # when max_wall_seconds_s is a positive float.
            self._wall_clock_monitor_task: Optional[asyncio.Future] = None
            self._wall_clock_hard_deadline_thread: Optional[
                threading.Thread
            ] = None
            self._wall_clock_hard_deadline_stop: Optional[
                threading.Event
            ] = None
            _wall_cap = self._config.max_wall_seconds_s
            if _wall_cap is not None and _wall_cap > 0:
                self._wall_clock_monitor_task = asyncio.ensure_future(
                    self._monitor_wall_clock(_wall_cap)
                )
                # Defect #1 Slice B (2026-05-03) — thread-based safety
                # net immune to asyncio starvation. The asyncio task
                # above is the primary path (handles normal termination
                # cleanly). The thread is the backstop: if the loop is
                # wedged for longer than the grace window, the thread
                # fires the event via call_soon_threadsafe.
                self._start_wall_clock_hard_deadline_thread(_wall_cap)
                logger.info(
                    "[WallClockWatchdog] armed: max_wall_seconds=%.0fs — session will "
                    "terminate with stop_reason=wall_clock_cap if not already stopped.",
                    _wall_cap,
                )

            # Start hot-reload restart-pending monitor — graceful respawn on
            # quarantined self-modifications. See _monitor_restart_pending.
            self._restart_monitor_task = asyncio.ensure_future(
                self._monitor_restart_pending()
            )

            # Register signal handlers
            try:
                loop = asyncio.get_running_loop()
                self.register_signal_handlers(loop)
            except Exception:  # noqa: BLE001
                pass

            # Wait for first stop signal
            shutdown_waiter = asyncio.ensure_future(self._shutdown_event.wait())
            budget_waiter = asyncio.ensure_future(self._cost_tracker.budget_event.wait())
            idle_waiter = asyncio.ensure_future(self._idle_watchdog.idle_event.wait())
            # Ticket A Guard 2: wall-clock waiter joins the 4-way race. When
            # max_wall_seconds_s is None/disabled the event is never set, so
            # the waiter blocks forever and has no effect on the legacy
            # 3-way race — backwards-compatible.
            wall_clock_waiter = asyncio.ensure_future(self._wall_clock_event.wait())

            done, pending = await asyncio.wait(
                [shutdown_waiter, budget_waiter, idle_waiter, wall_clock_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel the pending waiters
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # Determine stop reason — but preserve a restart_pending stamp
            # if the restart monitor already set it (otherwise it would be
            # overwritten with the generic "shutdown_signal").
            if self._stop_reason.startswith("restart_pending:"):
                pass  # already set by _monitor_restart_pending
            elif wall_clock_waiter in done:
                self._stop_reason = "wall_clock_cap"
                logger.warning(
                    "Session %s stopping: wall_clock_cap — max_wall_seconds=%.0fs "
                    "exceeded. This is a harness-class clean stop (Ticket A Guard 2).",
                    self._session_id,
                    self._config.max_wall_seconds_s or 0.0,
                )
            elif shutdown_waiter in done:
                self._stop_reason = "shutdown_signal"
            elif budget_waiter in done:
                self._stop_reason = "budget_exhausted"
            elif idle_waiter in done:
                diag = self._idle_watchdog.diagnostics
                if diag and diag.reason == "all_ops_stale":
                    self._stop_reason = "stale_ops_detected"
                    logger.warning(
                        "Session stopping: all in-flight ops were stale. "
                        "Stale ops: %s",
                        ", ".join(
                            f"{s.op_id}({s.phase}, {s.elapsed_s:.0f}s)"
                            for s in diag.stale_ops
                        ) if diag.stale_ops else "none",
                    )
                else:
                    self._stop_reason = "idle_timeout"
            else:
                self._stop_reason = "unknown"

            logger.info("Session %s stopping: %s", self._session_id, self._stop_reason)

        except Exception as exc:
            logger.error("Session %s boot failed: %s", self._session_id, exc)
            self._stop_reason = f"boot_failure: {exc}"
        finally:
            await self._shutdown_components()
            await self._generate_report()
            # Harness Epic Slice 1 — disarm the bounded-shutdown watchdog
            # since clean shutdown completed within deadline. Without
            # this disarm, a race between graceful shutdown completion
            # and the watchdog's deadline could cause spurious os._exit.
            # Idempotent — no-op if not armed.
            try:
                _wdg = getattr(self, "_shutdown_watchdog", None)
                if _wdg is not None:
                    _wdg.disarm()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Boot methods (all overridable for test mocking)
    # ------------------------------------------------------------------

    async def boot_oracle(self) -> None:
        """Import and initialize TheOracle."""
        try:
            from backend.core.ouroboros.oracle import TheOracle

            self._oracle = TheOracle()
            await self._oracle.initialize()
            logger.info("Oracle booted")
        except Exception as exc:
            logger.warning("Oracle failed to boot: %s", exc)

    async def boot_governance_stack(self) -> None:
        """Create GovernanceConfig and call create_governance_stack()."""
        try:
            import argparse
            from backend.core.ouroboros.governance.integration import (
                GovernanceConfig,
                create_governance_stack,
            )

            args = argparse.Namespace(skip_governance=False, governance_mode="governed")
            gov_config = GovernanceConfig.from_env_and_args(args)
            self._governance_stack = await create_governance_stack(
                gov_config,
                oracle=self._oracle,
            )

            # Start governance stack and promote to GOVERNED mode so
            # can_write() allows file changes through the GATE phase.
            # Without this, the stack stays in SANDBOX (_started=False)
            # and all operations silently CANCEL at GATE.
            await self._governance_stack.start()
            await self._governance_stack.controller.mark_gates_passed()
            await self._governance_stack.controller.enable_governed_autonomy()
            logger.info(
                "GovernanceStack started → %s (writes_allowed=%s)",
                self._governance_stack.controller.mode.value,
                self._governance_stack.controller.writes_allowed,
            )

            # Prewarm mutation-gate catalogs so the first critical-path
            # APPLY doesn't pay enumeration cost. Skip silently when the
            # gate is disabled — no-op for operators who haven't opted in.
            try:
                from backend.core.ouroboros.governance import mutation_gate as _mg
                if _mg.gate_enabled() and _mg.prewarm_enabled():
                    _summary = _mg.prewarm_allowlist(
                        project_root=Path("."),
                    )
                    logger.info(
                        "[MutationGate] prewarm_at_boot mode=%s summary=%s",
                        _mg.gate_mode(), _summary,
                    )
            except Exception:
                logger.debug(
                    "[MutationGate] boot-time prewarm skipped", exc_info=True,
                )

            # Inject SerpentFlow — flowing organism CLI (preferred)
            # Falls back to scrolling OuroborosTUI, then basic diff transport.
            self._serpent_flow = None
            try:
                from backend.core.ouroboros.battle_test.serpent_flow import (
                    SerpentFlow,
                    SerpentTransport,
                )
                self._serpent_flow = SerpentFlow(
                    session_id=self._session_id,
                    branch_name=self._branch_name or "",
                    cost_cap_usd=self._config.cost_cap_usd,
                    idle_timeout_s=self._config.idle_timeout_s,
                    repo_path=self._config.repo_path,
                )
                self._serpent_flow.set_plan_review_mode(self._plan_before_execute)
                _serpent_transport = SerpentTransport(flow=self._serpent_flow)
                if hasattr(self._governance_stack, "comm") and self._governance_stack.comm is not None:
                    self._governance_stack.comm._transports.append(_serpent_transport)
                    logger.info("SerpentFlow wired (flowing organism CLI)")
                # Suppress raw stdout streaming — SerpentFlow handles it via CommProtocol
                try:
                    from backend.core.ouroboros.governance.serpent_animation import suppress as _suppress_serpent
                    _suppress_serpent()
                except Exception:
                    pass
                # Redirect logging from terminal → log file so SerpentFlow
                # output isn't drowned by DEBUG noise.  The log file lives
                # next to the session artifacts for post-mortem analysis.
                try:
                    _log_dir = self._config.repo_path / ".ouroboros" / "sessions" / self._session_id
                    _log_dir.mkdir(parents=True, exist_ok=True)
                    _log_file = _log_dir / "debug.log"
                    _file_handler = logging.FileHandler(str(_log_file), encoding="utf-8")
                    _file_handler.setLevel(logging.DEBUG)
                    _file_handler.setFormatter(logging.Formatter(
                        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S",
                    ))
                    _root = logging.getLogger()
                    _root.addHandler(_file_handler)
                    # Remove all StreamHandlers (stdout/stderr) from root logger
                    for _h in list(_root.handlers):
                        if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
                            _root.removeHandler(_h)
                    logger.info("Logging redirected to %s", _log_file)
                    self._log_file_path = str(_log_file)
                except Exception as _log_exc:
                    logger.debug("Log redirect failed: %s", _log_exc)
                self._keyboard_handler = None
                # Also keep a reference for cost updates
                self._tui_console = None
            except Exception as exc:
                logger.debug("SerpentFlow not available, trying OuroborosTUI: %s", exc)
                self._serpent_flow = None
                # Fallback to scrolling OuroborosTUI
                try:
                    from backend.core.ouroboros.battle_test.ouroboros_tui import (
                        OuroborosConsole,
                        OuroborosTUITransport,
                        KeyboardHandler,
                    )
                    self._tui_console = OuroborosConsole(repo_path=self._config.repo_path)
                    _tui_transport = OuroborosTUITransport(tui=self._tui_console)
                    if hasattr(self._governance_stack, "comm") and self._governance_stack.comm is not None:
                        self._governance_stack.comm._transports.append(_tui_transport)
                        logger.info("OuroborosTUI wired (Rich scrolling fallback)")
                    self._keyboard_handler = KeyboardHandler(
                        tui=self._tui_console,
                        shutdown_event=self._shutdown_event,
                    )
                except Exception as exc2:
                    logger.debug("OuroborosTUI not available, using basic: %s", exc2)
                    try:
                        from backend.core.ouroboros.battle_test.diff_display import BattleDiffTransport
                        if hasattr(self._governance_stack, "comm") and self._governance_stack.comm is not None:
                            diff_transport = BattleDiffTransport(repo_path=self._config.repo_path)
                            self._governance_stack.comm._transports.append(diff_transport)
                    except Exception:
                        pass

            logger.info("Governance stack booted")
        except Exception as exc:
            logger.warning("Governance stack failed to boot: %s", exc)

    async def boot_governed_loop_service(self) -> None:
        """Create GovernedLoopConfig, GovernedLoopService, and start()."""
        try:
            from backend.core.ouroboros.governance.governed_loop_service import (
                GovernedLoopConfig,
                GovernedLoopService,
            )

            gls_config = GovernedLoopConfig.from_env(
                project_root=self._config.repo_path,
            )
            # Widen canary slices for battle test — allow all files.
            # Production default is ("tests/", "docs/") which blocks
            # autonomous writes to backend/ and root-level files.
            import dataclasses as _dc
            gls_config = _dc.replace(gls_config, initial_canary_slices=("",))
            self._governed_loop_service = GovernedLoopService(
                stack=self._governance_stack,
                config=gls_config,
            )
            # Inject pre-booted Oracle to prevent double ChromaDB initialization
            # (concurrent PersistentClient instances cause SQLite segfault)
            if self._oracle is not None:
                self._governed_loop_service._oracle = self._oracle

            try:
                from backend.core.ouroboros.governance.rate_limiter import RateLimitService
                self._rate_limiter = RateLimitService()
                logger.info("RateLimitService booted")
            except Exception as exc:
                self._rate_limiter = None
                logger.warning("RateLimitService failed: %s", exc)

            await self._governed_loop_service.start()
            logger.info("GovernedLoopService booted")

            # ── Glanceable operator status line (Priority 2B) ─────────
            # Aggregates cost / idle / phase / op / route data from the
            # live subsystems and feeds SerpentFlow's bottom_toolbar.
            # Registration is best-effort — any failure leaves the
            # legacy verbose toolbar intact.
            try:
                from backend.core.ouroboros.battle_test.status_line import (
                    StatusLineBuilder,
                    register_status_line_builder,
                )
                _status_builder = StatusLineBuilder(
                    cost_tracker=self._cost_tracker,
                    idle_watchdog=self._idle_watchdog,
                    governed_loop_service=self._governed_loop_service,
                )
                register_status_line_builder(_status_builder)
                logger.info("[Harness] StatusLineBuilder registered")
            except Exception as exc:
                logger.debug(
                    "[Harness] StatusLineBuilder registration failed: %s",
                    exc, exc_info=True,
                )

            # ── Boot-time orphan scan (Priority 2F resume) ────────────
            # Non-blocking: logs one INFO line if orphans exist so the
            # operator knows to run /resume list. Never prompts.
            try:
                self._notify_orphaned_ops_at_boot()
            except Exception:
                logger.debug(
                    "[Harness] orphan-scan notification failed",
                    exc_info=True,
                )

            # ── Plugin registry discovery + load (Priority 3 Feature B) ──
            # Gated by JARVIS_PLUGINS_ENABLED (default OFF). Discovers
            # plugins under .ouroboros/plugins/ + $JARVIS_PLUGINS_PATH,
            # validates manifests, loads + wires into the appropriate
            # subsystem (sensor / gate / repl). Every error is isolated
            # — one bad plugin never blocks others.
            self._plugin_registry = None
            try:
                from backend.core.ouroboros.plugins import (
                    PluginRegistry,
                    register_default_plugins,
                    plugins_enabled,
                )
                if plugins_enabled():
                    # Resolve the intake router for sensor plugins.
                    _router = None
                    for _attr in (
                        "_intake_router", "intake_router",
                        "_router", "router",
                    ):
                        _cand = getattr(
                            self._governed_loop_service, _attr, None,
                        )
                        if _cand is not None:
                            _router = _cand
                            break
                    self._plugin_registry = PluginRegistry(
                        repo_root=self._config.repo_path,
                    )
                    await self._plugin_registry.discover_and_load(
                        intake_router=_router,
                    )
                    register_default_plugins(self._plugin_registry)
                else:
                    logger.debug(
                        "[Harness] plugins disabled — set "
                        "JARVIS_PLUGINS_ENABLED=1 to enable",
                    )
            except Exception:
                logger.warning(
                    "[Harness] plugin discovery/load failed — continuing",
                    exc_info=True,
                )

            # Boot each subsystem independently — failure of one should not
            # prevent others from starting (Manifesto §2: progressive awakening).

            # --- Trinity Consciousness (memory + prediction + health) ---
            try:
                from backend.core.ouroboros.governance.consciousness_bridge import (
                    ConsciousnessBridge,
                )
                from backend.core.ouroboros.consciousness.consciousness_service import (
                    TrinityConsciousness,
                )
                from backend.core.ouroboros.consciousness.health_cortex import HealthCortex
                from backend.core.ouroboros.consciousness.memory_engine import MemoryEngine
                from backend.core.ouroboros.consciousness.prophecy_engine import ProphecyEngine

                _consciousness_dir = self._config.repo_path / ".jarvis" / "ouroboros" / "consciousness"
                _consciousness_dir.mkdir(parents=True, exist_ok=True)

                # MemoryEngine needs a ledger and persistence dir
                _ledger = getattr(self._governed_loop_service, "_ledger", None)
                _memory = MemoryEngine(
                    ledger=_ledger,
                    persistence_dir=_consciousness_dir,
                    repo_path=str(self._config.repo_path),
                )
                # HealthCortex needs subsystems dict and comm
                _comm = None
                if self._governance_stack is not None:
                    _comm = getattr(self._governance_stack, "comm", None)
                _cortex = HealthCortex(
                    subsystems={},  # No subsystem probes in battle test — health is best-effort
                    comm=_comm,
                    trend_path=_consciousness_dir / "health_trend.json",
                )
                _prophecy = ProphecyEngine(memory_engine=_memory)

                from backend.core.ouroboros.consciousness.types import ConsciousnessConfig
                _c_config = ConsciousnessConfig.from_env()

                # DreamEngine — uses DW (primary) + Claude (fallback) + J-Prime (legacy)
                # Falls back to no-op stub if DreamEngine import fails.
                _dream_instance: Any = None
                try:
                    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
                    from backend.core.ouroboros.consciousness.dream_metrics import DreamMetricsTracker

                    _dw_ref = getattr(self._governed_loop_service, "_doubleword_ref", None)
                    # Claude provider for Dream Engine Tier 2 fallback
                    _claude_ref = None
                    _gen = getattr(self._governed_loop_service, "_generator", None)
                    if _gen is not None:
                        _fb = getattr(_gen, "_fallback", None)
                        if _fb is not None and getattr(_fb, "provider_name", "") == "claude-api":
                            _claude_ref = _fb
                    _dream_metrics = DreamMetricsTracker()

                    # Lightweight stubs — battle test always considers user "idle"
                    # and never yields to resource pressure.
                    class _ActivityStub:
                        def last_activity_s(self) -> float:
                            return 9999.0  # Always idle
                    class _GovernorStub:
                        async def should_yield(self) -> bool:
                            return False  # Never yield

                    _dream_instance = DreamEngine(
                        health_cortex=_cortex,
                        memory_engine=_memory,
                        activity_monitor=_ActivityStub(),
                        resource_governor=_GovernorStub(),
                        metrics_tracker=_dream_metrics,
                        config=_c_config,
                        jprime_url=os.environ.get("JPRIME_URL", ""),
                        persistence_dir=_consciousness_dir / "dreams",
                        comm=_comm,
                        dw_provider=_dw_ref,
                        claude_provider=_claude_ref,
                    )
                    logger.info(
                        "DreamEngine booted (dw=%s, claude=%s, jprime=%s)",
                        "active" if _dw_ref else "none",
                        "active" if _claude_ref else "none",
                        "active" if os.environ.get("JPRIME_URL") else "none",
                    )
                except Exception as _dream_exc:
                    logger.debug("DreamEngine boot failed, using stub: %s", _dream_exc)

                if _dream_instance is None:
                    class _NoOpDream:
                        async def start(self) -> None: pass
                        async def stop(self) -> None: pass
                        def get_blueprints(self, top_n: int = 5) -> list: return []
                        def get_blueprint(self, bid: str): return None
                        def discard_stale(self) -> int: return 0
                    _dream_instance = _NoOpDream()

                _consciousness = TrinityConsciousness(
                    health_cortex=_cortex,
                    memory_engine=_memory,
                    dream_engine=_dream_instance,
                    prophecy_engine=_prophecy,
                    config=_c_config,
                )
                await asyncio.wait_for(
                    asyncio.shield(_consciousness.start()), timeout=10.0,
                )
                _cb = ConsciousnessBridge(consciousness=_consciousness)
                self._governed_loop_service._consciousness_bridge = _cb
                logger.info(
                    "Trinity Consciousness booted (memory=%s, prophecy=%s)",
                    "active", "active",
                )
            except Exception as exc:
                logger.warning("Trinity Consciousness failed to boot: %s", exc)

            # --- Heap stabilization gate ---
            # Oracle just initialized ChromaDB PersistentClient #1 (C extension).
            # Force GC before GoalMemoryBridge creates PersistentClient #2 to
            # prevent concurrent C-heap allocation that triggers libmalloc
            # corruption on macOS ARM64 (Python 3.9).
            import gc as _gc
            _gc.collect()

            # --- Goal Memory Bridge (ChromaDB cross-session learning) ---
            try:
                from backend.core.ouroboros.governance.goal_memory_bridge import (
                    GoalMemoryBridge,
                )
                from backend.intelligence.long_term_memory import (
                    get_long_term_memory,
                )
                _ltm = await get_long_term_memory()
                _gmb = GoalMemoryBridge(memory_manager=_ltm)
                self._governed_loop_service._goal_memory_bridge = _gmb
                logger.info("Goal Memory Bridge booted (ChromaDB)")
            except Exception as exc:
                logger.warning("Goal Memory Bridge failed: %s", exc)

            # --- Strategic Direction (Manifesto → every prompt) ---
            try:
                from backend.core.ouroboros.governance.strategic_direction import (
                    StrategicDirectionService,
                )
                _sds = StrategicDirectionService(
                    project_root=self._config.repo_path,
                )
                await _sds.load()
                self._governed_loop_service._strategic_direction = _sds
                logger.info(
                    "Strategic Direction loaded (%d principles, %d char digest)",
                    len(_sds.principles), len(_sds.digest),
                )
            except Exception as exc:
                logger.warning("Strategic Direction failed: %s", exc)

        except Exception as exc:
            logger.warning("GovernedLoopService failed to boot: %s", exc)

    async def boot_jarvis_tiers(self) -> None:
        """Import and start PredictiveRegressionEngine (Tier 3).

        Tiers 1, 2, 5, 6, 7 activate lazily via orchestrator imports;
        no explicit boot is needed for them.
        """
        try:
            from backend.core.ouroboros.governance.predictive_engine import (
                PredictiveRegressionEngine,
            )

            self._predictive_engine = PredictiveRegressionEngine(
                project_root=self._config.repo_path,
            )
            await self._predictive_engine.start()
            logger.info("PredictiveRegressionEngine (Tier 3) booted")
        except Exception as exc:
            logger.warning("PredictiveRegressionEngine failed to boot: %s", exc)

    async def create_branch(self) -> str:
        """Create the accumulation branch using BranchManager."""
        try:
            from backend.core.ouroboros.battle_test.branch_manager import BranchManager

            self._branch_manager = BranchManager(
                repo_path=self._config.repo_path,
                branch_prefix=self._config.branch_prefix,
            )
            branch_name = self._branch_manager.create_branch()
            logger.info("Accumulation branch created: %s", branch_name)
            return branch_name
        except Exception as exc:
            logger.warning("Branch creation failed: %s", exc)
            return f"{self._config.branch_prefix}/failed"

    async def boot_intake(self) -> None:
        """Create IntakeLayerConfig, IntakeLayerService, and start()."""
        try:
            from backend.core.ouroboros.governance.intake.intake_layer_service import (
                IntakeLayerConfig,
                IntakeLayerService,
            )

            intake_config = IntakeLayerConfig(project_root=self._config.repo_path)
            self._intake_service = IntakeLayerService(
                gls=self._governed_loop_service,
                config=intake_config,
                say_fn=None,
            )
            await self._intake_service.start()
            logger.info("IntakeLayerService booted")
        except Exception as exc:
            logger.warning("IntakeLayerService failed to boot: %s", exc)

    async def boot_graduation(self) -> None:
        """Create GraduationOrchestrator."""
        try:
            from backend.core.ouroboros.governance.graduation_orchestrator import (
                GraduationOrchestrator,
            )

            self._graduation_orchestrator = GraduationOrchestrator()
            logger.info("GraduationOrchestrator booted")
        except Exception as exc:
            logger.warning("GraduationOrchestrator failed to boot: %s", exc)

    # ------------------------------------------------------------------
    # REPL command handler
    # ------------------------------------------------------------------

    async def _handle_repl_command(self, command: str) -> None:
        """Process a user command from the SerpentREPL.

        Supports: stop, shutdown, cost, pause, resume, status, ops,
        /risk, /budget, /goal, /plan.
        """
        cmd = command.strip().lower()
        if cmd in ("stop", "shutdown"):
            self._shutdown_event.set()
        elif cmd == "cost":
            self._repl_cmd_cost()
        elif cmd == "pause":
            self._repl_cmd_pause()
        elif cmd == "resume":
            self._repl_cmd_resume()
        elif cmd == "status":
            self._repl_cmd_status()
        elif cmd == "ops":
            self._repl_cmd_ops()
        elif cmd == "plan" or cmd.startswith("/plan") or cmd.startswith("plan "):
            self._repl_cmd_plan(command.strip())
        elif cmd.startswith("/goal") or cmd.startswith("goal "):
            self._repl_cmd_goal(command.strip())
        elif cmd.startswith("/budget") or cmd.startswith("budget "):
            self._repl_cmd_budget(command.strip())
        elif cmd.startswith("/risk") or cmd.startswith("risk "):
            # /risk is handled entirely in SerpentREPL (env var only)
            pass
        elif (
            cmd.startswith("/memory")
            or cmd.startswith("memory ")
            or cmd == "memory"
        ):
            self._repl_cmd_memory(command.strip())
        elif cmd.startswith("/remember") or cmd.startswith("remember "):
            self._repl_cmd_remember(command.strip())
        elif cmd.startswith("/forget") or cmd.startswith("forget "):
            self._repl_cmd_forget(command.strip())
        elif cmd.startswith("/undo") or cmd.startswith("undo ") or cmd == "undo":
            # /undo N (revert last N O+V commits); /undo preview [N];
            # /undo --hard N. Async because executor awaits comm emits.
            await self._repl_cmd_undo(command.strip())
        elif cmd.startswith("/resume") or (
            cmd.startswith("resume ") and not cmd == "resume"
        ):
            # /resume, /resume list, /resume all, /resume <op-prefix>.
            # Note: bare "resume" (no slash, no args) is handled above
            # by the legacy intake-resume handler — that pauses /
            # unpauses the sensor fleet. Anything with a slash or args
            # routes to the new orphan-replay flow.
            await self._repl_cmd_resume_op(command.strip())
        elif cmd.startswith("/tdd") or cmd.startswith("tdd ") or cmd == "tdd":
            # /tdd <op-id> — mark an active/pending op as TDD-shaped.
            # The orchestrator picks up the flag at CONTEXT_EXPANSION
            # and injects the TDD prompt directive.
            self._repl_cmd_tdd(command.strip())
        elif cmd == "/plugins" or cmd.startswith("/plugins "):
            # /plugins — list loaded + failed plugins (Rich table).
            self._repl_cmd_plugins(command.strip())
        elif cmd == "/infer" or cmd.startswith("/infer "):
            # /infer — Rich table of inferred directions;
            # /infer accept <id> / reject <id> / stats
            self._repl_cmd_infer(command.strip())
        elif await self._try_dispatch_plugin_command(command.strip()):
            # Plugin-registered slash commands: /greet, /<plugin_cmd>, etc.
            # Returns True iff a plugin handled the command.
            pass
        else:
            logger.debug("Unknown REPL command: %s", cmd)

    # -- REPL sub-commands ------------------------------------------------

    def _repl_print(self, msg: str) -> None:
        """Print to SerpentFlow console if available, else log."""
        sf = getattr(self, "_serpent_flow", None)
        if sf is not None:
            sf.console.print(msg, highlight=False)
        else:
            logger.info(msg)

    def _repl_cmd_cost(self) -> None:
        breakdown = self._cost_tracker.breakdown
        parts = "  ".join(f"{k}: ${v:.4f}" for k, v in breakdown.items())
        self._repl_print(
            f"[dim]💰 ${self._cost_tracker.total_spent:.4f} / "
            f"${self._config.cost_cap_usd:.2f}  ({parts})[/dim]",
        )

    def _repl_cmd_pause(self) -> None:
        if self._intake_paused:
            self._repl_print("[yellow]Intake already paused.[/yellow]")
            return
        self._intake_paused = True
        svc = self._intake_service
        if svc is not None:
            svc._state = type(svc._state)["DEGRADED"] if hasattr(svc._state, "name") else svc._state
        self._repl_print("[yellow]⏸  Intake paused — no new signals will be accepted.[/yellow]")

    def _repl_cmd_resume(self) -> None:
        if not self._intake_paused:
            self._repl_print("[yellow]Intake is not paused.[/yellow]")
            return
        self._intake_paused = False
        svc = self._intake_service
        if svc is not None:
            try:
                svc._state = type(svc._state)["ACTIVE"]
            except (KeyError, TypeError):
                pass
        self._repl_print("[green]▶  Intake resumed — signals flowing.[/green]")

    def _repl_cmd_status(self) -> None:
        gls = self._governed_loop_service
        active = len(getattr(gls, "_active_ops", set())) if gls else 0
        completed_map = getattr(gls, "_completed_ops", {}) if gls else {}
        completed = len(completed_map)
        failed = sum(
            1 for r in completed_map.values()
            if getattr(r, "terminal_class", "") not in ("PRIMARY_SUCCESS", "FALLBACK_SUCCESS", "NOOP")
        )
        cost = self._cost_tracker.total_spent
        cap = self._config.cost_cap_usd
        paused_tag = "  [yellow](intake paused)[/yellow]" if self._intake_paused else ""
        plan_tag = (
            "  [cyan](plan review on)[/cyan]"
            if self._plan_before_execute else ""
        )
        self._repl_print(
            f"[bold]Status:[/bold]  active={active}  completed={completed}  "
            f"failed={failed}  cost=${cost:.4f}/${cap:.2f}{paused_tag}{plan_tag}"
        )

    def _repl_cmd_ops(self) -> None:
        gls = self._governed_loop_service
        active_ids: set = getattr(gls, "_active_ops", set()) if gls else set()
        fsm_ctxs: dict = getattr(gls, "_fsm_contexts", {}) if gls else {}
        completed_map: dict = getattr(gls, "_completed_ops", {}) if gls else {}

        if not active_ids and not completed_map:
            self._repl_print("[dim]No operations recorded yet.[/dim]")
            return

        lines: list = []
        # Active operations
        for op_id in sorted(active_ids):
            short = op_id[:12]
            fsm = fsm_ctxs.get(op_id)
            state = getattr(fsm, "state", None)
            state_str = state.name if state is not None else "RUNNING"
            lines.append(f"  [bold green]▸[/bold green] {short}  [cyan]{state_str}[/cyan]")

        # Recent completed (last 10)
        recent = sorted(
            completed_map.values(),
            key=lambda r: getattr(r, "op_id", ""),
            reverse=True,
        )[:10]
        for r in recent:
            short = getattr(r, "op_id", "?")[:12]
            phase = getattr(r, "terminal_phase", None)
            phase_str = phase.name if phase is not None else "?"
            tc = getattr(r, "terminal_class", "")
            if tc in ("PRIMARY_SUCCESS", "FALLBACK_SUCCESS"):
                tag = "[green]OK[/green]"
            elif tc == "NOOP":
                tag = "[dim]NOOP[/dim]"
            else:
                tag = f"[red]{tc}[/red]"
            lines.append(f"  [dim]•[/dim] {short}  {phase_str}  {tag}")

        header = f"[bold]Operations[/bold]  (active={len(active_ids)}, completed={len(completed_map)})"
        self._repl_print(header)
        for ln in lines:
            self._repl_print(ln)

    def _repl_cmd_budget(self, line: str) -> None:
        """Adjust the session budget mid-run."""
        parts = line.replace("/budget", "budget", 1).split(None, 1)
        if len(parts) < 2:
            self._repl_print(
                f"[dim]💰 ${self._cost_tracker.total_spent:.4f} / "
                f"${self._config.cost_cap_usd:.2f}[/dim]"
            )
            return
        try:
            amount = float(parts[1].strip().lstrip("$"))
        except ValueError:
            self._repl_print("[red]Invalid amount. Usage: /budget 1.00[/red]")
            return
        if amount <= 0:
            self._repl_print("[red]Budget must be positive[/red]")
            return
        self._config.cost_cap_usd = amount
        self._cost_tracker._budget_usd = amount
        self._repl_print(f"[green]Budget updated to ${amount:.2f}[/green]")

    def _set_plan_review_mode(self, enabled: bool) -> None:
        """Toggle session-scoped plan review before execution."""
        self._plan_before_execute = enabled
        os.environ["JARVIS_SHOW_PLAN_BEFORE_EXECUTE"] = "1" if enabled else "0"
        sf = getattr(self, "_serpent_flow", None)
        if sf is not None and hasattr(sf, "set_plan_review_mode"):
            sf.set_plan_review_mode(enabled)

    def _repl_cmd_plan(self, line: str) -> None:
        """Show or toggle plan-review + dry-run modes for the current session.

        Grammar
        -------
        /plan                        → show status of every gate (Rich panel)
        /plan status                 → same as /plan
        /plan on | off               → toggle plan-review (gates GENERATE)
        /plan dry-run [on|off]       → toggle dry-run (blocks APPLY session-wide)
        /plan dry-run                → toggle dry-run (on if off, off if on)

        Plan review vs dry run — distinct gates:

          ``plan-review`` gates at the PLAN→GENERATE boundary. With it on,
          the orchestrator shows the plan and waits for human approval
          before any code generation. Disabling it does not affect later
          gates.

          ``dry-run`` is a harder gate that fires just before APPLY. The
          op runs the full pipeline (CLASSIFY, PLAN, GENERATE, VALIDATE,
          security review, risk classification, guardian, floor), then
          short-circuits to CANCELLED(dry_run_session) before any disk
          write / git mutation / push. Operators get full observability
          into "what the model wanted to do" with zero side effects.
        """
        normalized = line[1:] if line.startswith("/") else line
        parts = normalized.split(None, 2)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        arg = parts[2].strip().lower() if len(parts) > 2 else ""

        # No subcommand → show status panel.
        if not sub or sub == "status":
            self._render_plan_status_panel()
            return

        # Plan-review toggle (legacy semantics).
        if sub in {"on", "enable", "enabled", "true", "1"}:
            self._set_plan_review_mode(True)
            self._repl_print(
                "[green]🗺 Plan review enabled — the next operation will show a plan "
                "and wait for approval before GENERATE.[/green]"
            )
            return
        if sub in {"off", "disable", "disabled", "false", "0"}:
            self._set_plan_review_mode(False)
            os.environ["JARVIS_DRY_RUN"] = "0"
            self._repl_print(
                "[yellow]🗺 Plan review + dry-run both disabled — operations can "
                "execute normally.[/yellow]"
            )
            return

        # Dry-run toggle. ``/plan dry-run`` (no arg) flips; explicit on|off wins.
        if sub in {"dry-run", "dry_run", "dryrun", "dry"}:
            cur_on = os.environ.get("JARVIS_DRY_RUN", "").strip().lower() in _TRUTHY
            if arg in {"on", "enable", "enabled", "true", "1"}:
                new_on = True
            elif arg in {"off", "disable", "disabled", "false", "0"}:
                new_on = False
            else:
                new_on = not cur_on
            os.environ["JARVIS_DRY_RUN"] = "1" if new_on else "0"
            if new_on:
                self._repl_print(
                    "[bold yellow]🧪 Dry-run ENABLED[/bold yellow] — ops will run "
                    "through every gate but stop before APPLY. No disk writes, "
                    "no commits, no pushes. Flip off with [bold]/plan dry-run off[/bold]."
                )
            else:
                self._repl_print(
                    "[green]🧪 Dry-run disabled — ops can modify files again.[/green]"
                )
            return

        self._repl_print(
            "[red]Usage:[/red] /plan | /plan status | /plan on | off | "
            "dry-run [on|off]"
        )

    def _render_plan_status_panel(self) -> None:
        """Rich panel summarising every gate that affects execution in
        the current session. Called by ``/plan`` / ``/plan status``.
        Falls back to plain text when Rich is unavailable.
        """
        _truthy = _TRUTHY
        plan_review = bool(self._plan_before_execute) or (
            os.environ.get("JARVIS_SHOW_PLAN_BEFORE_EXECUTE", "").strip().lower() in _truthy
        )
        dry_run = os.environ.get("JARVIS_DRY_RUN", "").strip().lower() in _truthy
        paranoia = os.environ.get("JARVIS_PARANOIA_MODE", "").strip().lower() in _truthy
        min_tier = os.environ.get("JARVIS_MIN_RISK_TIER", "").strip().lower() or "(unset)"
        risk_ceiling = os.environ.get("JARVIS_RISK_CEILING", "").strip() or "(unset)"
        quiet_raw = os.environ.get("JARVIS_AUTO_APPLY_QUIET_HOURS", "").strip() or "(unset)"
        quiet_tz = os.environ.get("JARVIS_AUTO_APPLY_QUIET_HOURS_TZ", "").strip() or "UTC"

        try:
            from rich.panel import Panel
            from rich.table import Table
            from rich.text import Text
            from rich.console import Group
        except Exception:
            # Plain fallback.
            self._repl_print(
                f"plan_review={plan_review}  dry_run={dry_run}  "
                f"paranoia={paranoia}  min_tier={min_tier}  "
                f"risk_ceiling={risk_ceiling}  quiet={quiet_raw} tz={quiet_tz}"
            )
            return

        tbl = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        tbl.add_column("Gate")
        tbl.add_column("State")
        tbl.add_column("Effect when active", no_wrap=False)

        def _row(name: str, on: bool, effect: str, value: str = "") -> None:
            state = (
                Text("ON", style="bold yellow") if on else Text("off", style="dim")
            )
            if value and on:
                state = Text.assemble(state, " ", Text(f"({value})", style="dim"))
            tbl.add_row(name, state, effect)

        _row(
            "plan-review",
            plan_review,
            "PLAN→GENERATE paused; human approves plan first",
        )
        _row(
            "dry-run",
            dry_run,
            "pipeline runs; APPLY short-circuits (no side effects)",
        )
        _row(
            "paranoia",
            paranoia,
            "forces NOTIFY_APPLY floor; no SAFE_AUTO landings",
        )
        _row(
            "min-risk-tier",
            min_tier not in {"(unset)", ""},
            "explicit tier floor; strictest-wins composition",
            value=min_tier if min_tier != "(unset)" else "",
        )
        _row(
            "risk-ceiling",
            risk_ceiling not in {"(unset)", ""},
            "/risk command floor (legacy)",
            value=risk_ceiling if risk_ceiling != "(unset)" else "",
        )
        _row(
            "quiet-hours",
            quiet_raw != "(unset)",
            f"window-based paranoia (tz={quiet_tz})",
            value=quiet_raw if quiet_raw != "(unset)" else "",
        )

        summary = Text()
        if dry_run:
            summary.append(
                "⚠ DRY-RUN ACTIVE — no ops will modify files this session.",
                style="bold yellow",
            )
        elif paranoia or plan_review or min_tier not in {"(unset)", ""}:
            summary.append(
                "✓ At least one paranoia gate is on. Auto-apply is gated.",
                style="green",
            )
        else:
            summary.append(
                "No paranoia gates active — ops auto-apply per risk engine.",
                style="dim",
            )

        panel = Panel(
            Group(tbl, Text(""), summary),
            title="[bold]Plan & Execution Gates[/bold]",
            border_style="cyan",
            padding=(1, 2),
        )
        try:
            sf = getattr(self, "_serpent_flow", None)
            console = getattr(sf, "console", None) if sf is not None else None
            if console is not None:
                console.print(panel, highlight=False)
            else:
                self._repl_print(str(panel))
        except Exception:
            self._repl_print(str(panel))

    def _repl_cmd_goal(self, line: str) -> None:
        """Manage active goals at runtime.

        Usage:
          /goal                                 — list active goals
          /goal all                             — list every goal (any status)
          /goal tree                            — show hierarchy (v3 parent chains)
          /goal show <id>                       — one goal + parent + children
          /goal add <desc>                      — add a goal (auto slug + keywords)
          /goal add --parent <id> <desc>        — add as child of <id>
          /goal remove <id>                     — remove by ID
          /goal pause <id>                      — skip scoring + injection
          /goal resume <id>                     — resume a paused goal
          /goal complete <id>                   — mark as completed
          /goal purge                           — drop all completed goals
          /goal activity [--goal <id>] [--limit N]  — read activity ledger
          /goal drift                           — show strategic-drift summary
          /goal explain <op-id>                 — per-goal reasons for an op
        """
        parts = line.replace("/goal", "goal", 1).split(None, 2)
        subcmd = parts[1].strip().lower() if len(parts) > 1 else "list"

        # Lazy import GoalTracker — the harness keeps booting on older
        # checkouts where the new API isn't present.
        try:
            from backend.core.ouroboros.governance.strategic_direction import (
                ActiveGoal, GoalStatus, GoalTracker,
            )
        except ImportError:
            self._repl_print("[red]GoalTracker not available[/red]")
            return

        tracker = GoalTracker(self._config.repo_path)

        def _render_one(g: "ActiveGoal") -> str:
            kw = ", ".join(g.keywords[:5])
            weight = ""
            if g.priority_weight >= 2.0:
                weight = " [bold red]HIGH[/bold red]"
            elif g.priority_weight <= 0.5:
                weight = " [dim]low[/dim]"
            tags = ""
            if g.tags:
                tags = f" [magenta]#{' #'.join(g.tags[:3])}[/magenta]"
            status_color = {
                GoalStatus.ACTIVE: "green",
                GoalStatus.PAUSED: "yellow",
                GoalStatus.COMPLETED: "blue",
            }.get(g.status, "white")
            status_tag = f" [{status_color}]{g.status.value}[/{status_color}]"
            return (
                f"  [cyan]{g.goal_id}[/cyan]{status_tag}  {g.description}"
                f"{tags}  [dim]({kw}){weight}[/dim]"
            )

        if subcmd == "list":
            goals = tracker.active_goals
            if not goals:
                self._repl_print("[dim]No active goals. Use: /goal add <description>[/dim]")
                return
            self._repl_print("[bold]Active Goals[/bold]")
            for g in goals:
                self._repl_print(_render_one(g))

        elif subcmd == "all":
            goals = tracker.all_goals
            if not goals:
                self._repl_print("[dim]No goals stored. Use: /goal add <description>[/dim]")
                return
            self._repl_print("[bold]All Goals[/bold]")
            for g in goals:
                self._repl_print(_render_one(g))

        elif subcmd == "tree":
            tree = tracker.hierarchy_tree(include_inactive=True)
            if not tree:
                self._repl_print(
                    "[dim]No goals stored. Use: /goal add <description>[/dim]"
                )
                return
            self._repl_print(
                f"[bold]Goal Hierarchy[/bold]  "
                f"[dim]({len(tree)} goal(s), "
                f"{len(tracker.roots)} root(s))[/dim]"
            )
            for g, depth in tree:
                indent = "  " * depth
                branch = "" if depth == 0 else "└─ "
                status_color = {
                    GoalStatus.ACTIVE: "green",
                    GoalStatus.PAUSED: "yellow",
                    GoalStatus.COMPLETED: "blue",
                }.get(g.status, "white")
                weight_tag = ""
                if g.priority_weight >= 2.0:
                    weight_tag = " [bold red]HIGH[/bold red]"
                elif g.priority_weight <= 0.5:
                    weight_tag = " [dim]low[/dim]"
                self._repl_print(
                    f"  {indent}{branch}[cyan]{g.goal_id}[/cyan] "
                    f"[{status_color}]{g.status.value}[/{status_color}]"
                    f"{weight_tag}  [dim]{g.description[:60]}[/dim]"
                )

        elif subcmd == "show" and len(parts) > 2:
            goal_id = parts[2].strip()
            g = tracker.get(goal_id)
            if g is None:
                self._repl_print(f"[red]No goal matching '{goal_id}'[/red]")
                return
            self._repl_print(f"[bold cyan]{g.goal_id}[/bold cyan]  [dim]({g.status.value})[/dim]")
            self._repl_print(f"  description: {g.description}")
            self._repl_print(f"  keywords:    {', '.join(g.keywords) or '(none)'}")
            if g.path_patterns:
                self._repl_print(f"  paths:       {', '.join(g.path_patterns)}")
            if g.tags:
                self._repl_print(f"  tags:        {', '.join(g.tags)}")
            self._repl_print(f"  priority:    {g.priority_weight}")
            # v3 hierarchy: render ancestor chain + direct children
            if g.parent_id is not None:
                chain = tracker.ancestors_of(goal_id)
                chain_str = " ← ".join(a.goal_id for a in chain)
                self._repl_print(f"  ancestors:   {chain_str or '(none)'}")
            children = tracker.children_of(goal_id)
            if children:
                kids = ", ".join(sorted(c.goal_id for c in children))
                self._repl_print(f"  children:    {kids}")

        elif subcmd == "add" and len(parts) > 2:
            rest = parts[2].strip()
            parent_id: Optional[str] = None
            # Support: /goal add --parent <id> <desc>
            if rest.startswith("--parent"):
                tokens = rest.split(None, 2)
                if len(tokens) < 3:
                    self._repl_print(
                        "[red]Usage: /goal add --parent <parent-id> "
                        "<description>[/red]"
                    )
                    return
                parent_id = tokens[1].strip()
                rest = tokens[2].strip()
                if tracker.get(parent_id) is None:
                    self._repl_print(
                        f"[red]Parent '{parent_id}' not found — "
                        f"run /goal tree to see available ids[/red]"
                    )
                    return
            desc = rest.strip("'\"")
            # Delegate slug + keyword extraction to GoalTracker so the
            # stopword list lives in one place (and is env-overridable).
            slug = GoalTracker.slugify(desc)
            keywords = GoalTracker.extract_keywords(desc)
            goal = ActiveGoal(
                goal_id=slug,
                description=desc,
                keywords=keywords,
                parent_id=parent_id,
            )
            # Preview cycle check before mutating — defensive, since the
            # tracker heals this anyway but we surface the reason.
            if parent_id and tracker.has_cycle(slug, parent_id):
                self._repl_print(
                    f"[yellow]Note: parent '{parent_id}' would cycle — "
                    f"installing '{slug}' as root[/yellow]"
                )
            tracker.add_goal(goal)
            stored = tracker.get(slug)
            parent_note = ""
            if stored and stored.parent_id:
                parent_note = f"  [dim]parent: {stored.parent_id}[/dim]"
            self._repl_print(
                f"[green]Goal added:[/green] [cyan]{slug}[/cyan]  "
                f"[dim]keywords: {', '.join(keywords) or '(none)'}[/dim]"
                f"{parent_note}"
            )

        elif subcmd in ("remove", "rm") and len(parts) > 2:
            goal_id = parts[2].strip()
            if tracker.remove_goal(goal_id):
                self._repl_print(f"[green]Removed goal: {goal_id}[/green]")
            else:
                self._repl_print(f"[red]No goal matching '{goal_id}'[/red]")

        elif subcmd == "pause" and len(parts) > 2:
            goal_id = parts[2].strip()
            if tracker.pause(goal_id):
                self._repl_print(f"[yellow]Paused goal: {goal_id}[/yellow]")
            else:
                self._repl_print(f"[red]No goal matching '{goal_id}'[/red]")

        elif subcmd == "resume" and len(parts) > 2:
            goal_id = parts[2].strip()
            if tracker.resume(goal_id):
                self._repl_print(f"[green]Resumed goal: {goal_id}[/green]")
            else:
                self._repl_print(f"[red]No goal matching '{goal_id}'[/red]")

        elif subcmd == "complete" and len(parts) > 2:
            goal_id = parts[2].strip()
            if tracker.complete(goal_id):
                self._repl_print(f"[blue]Completed goal: {goal_id}[/blue]")
            else:
                self._repl_print(f"[red]No goal matching '{goal_id}'[/red]")

        elif subcmd == "purge":
            removed = tracker.purge_completed()
            if removed:
                self._repl_print(f"[blue]Purged {removed} completed goals[/blue]")
            else:
                self._repl_print("[dim]No completed goals to purge[/dim]")

        elif subcmd == "activity":
            self._repl_goal_activity(parts[2] if len(parts) > 2 else "")

        elif subcmd == "drift":
            self._repl_goal_drift()

        elif subcmd == "explain" and len(parts) > 2:
            self._repl_goal_explain(parts[2].strip())

        elif subcmd == "explain":
            self._repl_print("[red]Usage: /goal explain <op-id>[/red]")

        else:
            self._repl_print(
                "[dim]Usage: /goal | /goal all | /goal tree | "
                "/goal show <id> | /goal add [--parent <id>] <desc> | "
                "/goal remove <id> | /goal pause|resume|complete <id> | "
                "/goal purge | /goal activity [--goal <id>] [--limit N] | "
                "/goal drift | /goal explain <op-id>[/dim]"
            )

    # -- /goal activity|drift|explain helpers (Increment 3) -------------

    def _goal_activity_ledger(self):
        """Return a GoalActivityLedger bound to this repo, or None on import err."""
        try:
            from backend.core.ouroboros.governance.strategic_direction import (
                GoalActivityLedger,
            )
        except ImportError:
            self._repl_print("[red]GoalActivityLedger not available[/red]")
            return None
        return GoalActivityLedger(self._config.repo_path)

    def _repl_goal_activity(self, rest: str) -> None:
        """Render recent ledger rows for the current session.

        Supports ``--goal <id>`` and ``--limit N`` flags. Defaults to
        the current harness session; ``--session <sid>`` overrides.
        """
        ledger = self._goal_activity_ledger()
        if ledger is None:
            return

        tokens = rest.split() if rest else []
        goal_filter: Optional[str] = None
        session_filter: Optional[str] = self._session_id
        limit: Optional[int] = 20
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--goal" and i + 1 < len(tokens):
                goal_filter = tokens[i + 1]
                i += 2
            elif tok == "--session" and i + 1 < len(tokens):
                session_filter = tokens[i + 1]
                i += 2
            elif tok == "--limit" and i + 1 < len(tokens):
                try:
                    limit = max(1, int(tokens[i + 1]))
                except ValueError:
                    self._repl_print(f"[red]Invalid --limit '{tokens[i + 1]}'[/red]")
                    return
                i += 2
            else:
                self._repl_print(f"[red]Unknown token '{tok}'[/red]")
                return

        try:
            rows = ledger.read(
                session_id=session_filter,
                goal_id=goal_filter,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            self._repl_print(f"[red]Ledger read failed: {exc}[/red]")
            return

        if not rows:
            self._repl_print(
                f"[dim]No activity rows for session={session_filter or '*'}"
                f"{' goal=' + goal_filter if goal_filter else ''}[/dim]"
            )
            return

        self._repl_print(
            f"[bold]Goal Activity[/bold]  [dim](session={session_filter or '*'}, "
            f"{len(rows)} row(s))[/dim]"
        )
        for row in rows:
            op_id = str(row.get("op_id", "?"))[:12]
            if row.get("zero_match"):
                self._repl_print(
                    f"  [dim]{op_id}  ·  (no matches)[/dim]"
                )
                continue
            gid = str(row.get("goal_id", "?"))
            kind = str(row.get("kind", "direct"))
            score = row.get("score", 0.0)
            try:
                score_f = float(score) if score is not None else 0.0  # type: ignore[arg-type]
            except (TypeError, ValueError):
                score_f = 0.0
            reasons = row.get("reasons") or []
            if not isinstance(reasons, list):
                reasons = []
            reason_str = ", ".join(str(r) for r in reasons[:4])
            color = {"direct": "green", "ancestor": "cyan", "sibling": "yellow"}.get(kind, "white")
            self._repl_print(
                f"  {op_id}  [{color}]{kind:<8}[/{color}]  "
                f"[cyan]{gid}[/cyan]  [bold]{score_f:.2f}[/bold]  "
                f"[dim]{reason_str}[/dim]"
            )

    def _repl_goal_drift(self) -> None:
        """Render the live drift summary for the current session."""
        ledger = self._goal_activity_ledger()
        if ledger is None:
            return
        try:
            summary = ledger.compute_drift(session_id=self._session_id)
        except Exception as exc:  # noqa: BLE001
            self._repl_print(f"[red]Drift compute failed: {exc}[/red]")
            return

        status = summary.status
        total = summary.total_ops
        drifted = summary.drifted_ops
        ratio = summary.ratio
        ratio_s = f"{ratio:.2%}" if ratio is not None else "n/a"
        min_ops = int(os.environ.get("JARVIS_GOAL_DRIFT_MIN_OPS", "5"))
        warn_ratio = float(os.environ.get("JARVIS_GOAL_DRIFT_WARN_RATIO", "0.30"))

        if status == "drift_warning":
            self._repl_print(
                f"[bold red]⚠ Strategic drift: WARN[/bold red]  "
                f"{drifted}/{total} ops missed all goals ({ratio_s})  "
                f"[dim]threshold: {warn_ratio:.0%}[/dim]"
            )
        elif status == "insufficient_data":
            self._repl_print(
                f"[dim]Strategic drift: insufficient data "
                f"({total}/{min_ops} ops minimum)[/dim]"
            )
        else:
            self._repl_print(
                f"[green]Strategic drift: ok[/green]  "
                f"({drifted}/{total} missed, {ratio_s})  "
                f"[dim]threshold: {warn_ratio:.0%}[/dim]"
            )

    def _repl_goal_explain(self, op_id: str) -> None:
        """Dump per-goal alignment reasons for a single op."""
        ledger = self._goal_activity_ledger()
        if ledger is None:
            return
        if not op_id:
            self._repl_print("[red]Usage: /goal explain <op-id>[/red]")
            return
        try:
            rows = ledger.read(session_id=self._session_id, op_id=op_id)
        except Exception as exc:  # noqa: BLE001
            self._repl_print(f"[red]Ledger read failed: {exc}[/red]")
            return
        if not rows:
            self._repl_print(
                f"[dim]No ledger rows for op '{op_id}' in session "
                f"{self._session_id}[/dim]"
            )
            return
        self._repl_print(
            f"[bold]Explain op[/bold] [cyan]{op_id}[/cyan]  "
            f"[dim]({len(rows)} row(s))[/dim]"
        )
        for row in rows:
            if row.get("zero_match"):
                self._repl_print("  [dim](zero-match marker — no goals scored)[/dim]")
                continue
            gid = str(row.get("goal_id", "?"))
            kind = str(row.get("kind", "direct"))
            score = row.get("score", 0.0)
            try:
                score_f = float(score) if score is not None else 0.0  # type: ignore[arg-type]
            except (TypeError, ValueError):
                score_f = 0.0
            src = str(row.get("source_goal_id") or "")
            src_note = f"  [dim](from {src})[/dim]" if src else ""
            reasons = row.get("reasons") or []
            if not isinstance(reasons, list):
                reasons = []
            reason_str = ", ".join(str(r) for r in reasons)
            color = {"direct": "green", "ancestor": "cyan", "sibling": "yellow"}.get(kind, "white")
            self._repl_print(
                f"  [cyan]{gid}[/cyan]  [{color}]{kind}[/{color}]  "
                f"[bold]{score_f:.2f}[/bold]{src_note}"
            )
            if reason_str:
                self._repl_print(f"      [dim]reasons: {reason_str}[/dim]")

    # -- UserPreferenceMemory sub-commands -------------------------------

    def _user_pref_store(self):
        """Return the process-wide UserPreferenceStore singleton.

        Lazy import so the harness keeps booting on older checkouts
        where the module is absent. Returns ``None`` on import failure.
        """
        try:
            from backend.core.ouroboros.governance.user_preference_memory import (
                get_default_store,
            )
        except ImportError:
            self._repl_print("[red]UserPreferenceStore not available[/red]")
            return None
        try:
            return get_default_store(self._config.repo_path)
        except Exception as exc:  # noqa: BLE001
            self._repl_print(f"[red]Store init failed: {exc}[/red]")
            return None

    def _repl_cmd_memory(self, line: str) -> None:
        """Manage UserPreferenceStore memories at runtime.

        Usage:
          /memory                           — list all memories
          /memory list [type]               — list (optionally filter by type)
          /memory add <type> <name> | <desc>  — add a memory
          /memory rm <id>                   — remove a memory by id
          /memory forbid <path>             — shortcut: add FORBIDDEN_PATH memory
          /memory show <id>                 — print a single memory's content

        The ``type`` argument accepts any of user/feedback/project/reference/
        forbidden_path/style. The ``name`` is slugified into the memory id.
        """
        from backend.core.ouroboros.governance.user_preference_memory import (
            MemoryType,
        )

        store = self._user_pref_store()
        if store is None:
            return

        parts = line.replace("/memory", "memory", 1).split(None, 2)
        subcmd = parts[1].strip().lower() if len(parts) > 1 else "list"
        rest = parts[2].strip() if len(parts) > 2 else ""

        if subcmd == "list":
            mem_filter: Optional[MemoryType] = None
            if rest:
                try:
                    mem_filter = MemoryType.from_str(rest)
                except Exception:
                    mem_filter = None
            mems = (
                store.find_by_type(mem_filter) if mem_filter is not None
                else store.list_all()
            )
            if not mems:
                self._repl_print("[dim]No memories recorded.[/dim]")
                return
            header = (
                f"[bold]User Preference Memories[/bold]  ({len(mems)})"
                + (f"  [dim]type={mem_filter.value}[/dim]" if mem_filter else "")
            )
            self._repl_print(header)
            for m in mems:
                type_tag = f"[cyan]{m.type.value}[/cyan]"
                tag_str = f" [dim]{', '.join(m.tags)}[/dim]" if m.tags else ""
                path_str = (
                    f" [yellow]paths={', '.join(m.paths)}[/yellow]" if m.paths else ""
                )
                self._repl_print(
                    f"  {type_tag}  [bold]{m.name}[/bold] "
                    f"[dim]({m.id})[/dim]  {m.description}{tag_str}{path_str}"
                )
            return

        if subcmd == "add":
            # Format: /memory add <type> <name> | <description>
            if "|" not in rest:
                self._repl_print(
                    "[red]Usage: /memory add <type> <name> | <description>[/red]"
                )
                return
            head, _, description = rest.partition("|")
            head_parts = head.strip().split(None, 1)
            if len(head_parts) < 2:
                self._repl_print(
                    "[red]Usage: /memory add <type> <name> | <description>[/red]"
                )
                return
            type_raw, name = head_parts[0], head_parts[1].strip()
            description = description.strip()
            if not name or not description:
                self._repl_print(
                    "[red]Name and description must both be non-empty[/red]"
                )
                return
            try:
                mem_type = MemoryType.from_str(type_raw)
                mem = store.add(mem_type, name, description, source="repl")
                self._repl_print(
                    f"[green]Memory added:[/green] [cyan]{mem.type.value}[/cyan]  "
                    f"[bold]{mem.name}[/bold] [dim]({mem.id})[/dim]"
                )
            except ValueError as exc:
                self._repl_print(f"[red]{exc}[/red]")
            return

        if subcmd in ("rm", "remove", "delete"):
            if not rest:
                self._repl_print("[red]Usage: /memory rm <id>[/red]")
                return
            if store.delete(rest):
                self._repl_print(f"[green]Removed memory: {rest}[/green]")
            else:
                self._repl_print(f"[red]No memory matching '{rest}'[/red]")
            return

        if subcmd == "forbid":
            if not rest:
                self._repl_print(
                    "[red]Usage: /memory forbid <path-substring>[/red]"
                )
                return
            try:
                mem = store.add(
                    MemoryType.FORBIDDEN_PATH,
                    f"forbid_{rest}",
                    f"Hard-blocked by user: {rest}",
                    content="Added via /memory forbid — blocks Venom write tools.",
                    how_to_apply=f"Never edit, write, or delete under {rest}.",
                    paths=(rest,),
                    source="repl",
                )
                self._repl_print(
                    f"[green]Forbidden path added:[/green] [yellow]{rest}[/yellow] "
                    f"[dim]({mem.id})[/dim]"
                )
            except ValueError as exc:
                self._repl_print(f"[red]{exc}[/red]")
            return

        if subcmd == "show":
            if not rest:
                self._repl_print("[red]Usage: /memory show <id>[/red]")
                return
            mem = store.get(rest)
            if mem is None:
                self._repl_print(f"[red]No memory matching '{rest}'[/red]")
                return
            self._render_memory_detail_panel(mem)
            return

        # ---- New subcommands (super-beef) ------------------------------------
        if subcmd == "stats":
            self._render_memory_stats_panel(store, MemoryType)
            return

        if subcmd == "search":
            if not rest:
                self._repl_print("[red]Usage: /memory search <query>[/red]")
                return
            self._render_memory_search(store, rest)
            return

        if subcmd == "recent":
            try:
                n = int(rest) if rest else 10
            except ValueError:
                n = 10
            self._render_memory_recent(store, max(1, min(100, n)))
            return

        self._repl_print(
            "[dim]Usage: /memory | /memory list [type] | /memory add <type> "
            "<name> | <desc> | /memory rm <id> | /memory forbid <path> | "
            "/memory show <id> | /memory stats | /memory search <q> | "
            "/memory recent [N][/dim]"
        )

    # ------------------------------------------------------------------
    # /memory Rich renderers (super-beef)
    # ------------------------------------------------------------------

    def _repl_cmd_infer(self, line: str) -> None:
        """/infer — inspect and manage inferred goal hypotheses.

        Grammar:
          /infer               → Rich table of current hypotheses
          /infer refresh       → rebuild now (ignores cache)
          /infer accept <id>   → promote to declared goal (via GoalTracker)
          /infer reject <id>   → record FEEDBACK memory; future builds filter
          /infer stats         → counts per source + cache age

        The engine is OFF by default (JARVIS_GOAL_INFERENCE_ENABLED=0).
        """
        try:
            from backend.core.ouroboros.governance.goal_inference import (
                GoalInferenceEngine,
                accept_inferred_goal,
                get_default_engine,
                inference_enabled,
                reject_inferred_goal,
                register_default_engine,
            )
        except Exception:
            self._repl_print("[red]Goal inference module unavailable.[/red]")
            return

        if not inference_enabled():
            self._repl_print(
                "[yellow]Goal inference disabled.[/yellow]  "
                "[dim]Set JARVIS_GOAL_INFERENCE_ENABLED=1 and retry.[/dim]"
            )
            return

        engine = get_default_engine(self._config.repo_path)
        if engine is None:
            engine = GoalInferenceEngine(repo_root=self._config.repo_path)
            register_default_engine(engine)

        parts = line.split(None, 2)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        arg = parts[2].strip() if len(parts) > 2 else ""

        if sub == "refresh":
            result = engine.build(force=True)
            self._repl_print(
                f"[green]✓ rebuilt[/green]  hypotheses={len(result.inferred)}  "
                f"samples={result.total_samples}  build_ms={result.build_ms}"
            )
            sub = ""   # fall through to render

        if not sub or sub == "list":
            result = engine.build()
            self._render_infer_panel(result)
            return

        if sub == "stats":
            result = engine.get_current() or engine.build()
            self._render_infer_stats(result)
            return

        if sub in ("accept", "reject"):
            if not arg:
                self._repl_print(
                    f"[red]Usage: /infer {sub} <id>[/red]"
                )
                return
            result = engine.get_current() or engine.build()
            target = next(
                (g for g in result.inferred if g.inferred_id == arg),
                None,
            )
            if target is None:
                self._repl_print(
                    f"[red]No inferred goal with id {arg!r}[/red]"
                )
                return
            if sub == "accept":
                ok, msg = accept_inferred_goal(
                    repo_root=self._config.repo_path, inferred=target,
                )
            else:
                ok, msg = reject_inferred_goal(
                    repo_root=self._config.repo_path, inferred=target,
                )
            engine.invalidate()   # next build picks up the change
            color = "green" if ok else "red"
            self._repl_print(f"[{color}]{msg}[/{color}]")
            return

        self._repl_print(
            "[dim]Usage: /infer | /infer refresh | /infer accept <id> | "
            "/infer reject <id> | /infer stats[/dim]"
        )

    def _render_infer_panel(self, result: Any) -> None:
        """Rich table of current inferred goals."""
        try:
            from rich.panel import Panel
            from rich.table import Table
            from rich.text import Text
            from rich.console import Group
        except Exception:
            if not result.inferred:
                self._repl_print("[dim]No inferred goals.[/dim]")
                return
            for g in result.inferred:
                self._repl_print(
                    f"  {g.inferred_id}  conf={g.confidence:.2f}  "
                    f"{g.theme}"
                )
            return

        if not result.inferred:
            self._repl_print(
                "[dim]No inferred goals yet — insufficient signal.[/dim]"
            )
            return

        tbl = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        tbl.add_column("id", no_wrap=True)
        tbl.add_column("conf", justify="right")
        tbl.add_column("theme")
        tbl.add_column("sources")
        tbl.add_column("files")
        for g in result.inferred:
            conf_color = (
                "bold green" if g.confidence >= 0.75
                else "yellow" if g.confidence >= 0.5
                else "dim"
            )
            tbl.add_row(
                g.inferred_id,
                Text(f"{g.confidence:.2f}", style=conf_color),
                g.theme[:60],
                ", ".join(g.supporting_sources),
                ", ".join(g.supporting_files[:2]),
            )

        summary = Text()
        summary.append(
            "inferred direction — hypotheses, NOT declared goals. "
            "accept / reject per id.",
            style="dim italic",
        )
        age = time.time() - result.built_at if result.built_at else 0
        summary.append(
            f"\nbuilt {int(age)}s ago · samples={result.total_samples} · "
            f"build_ms={result.build_ms}",
            style="dim",
        )

        panel = Panel(
            Group(tbl, Text(""), summary),
            title="[bold]Inferred Direction[/bold]",
            border_style="cyan",
            padding=(1, 2),
        )
        sf = getattr(self, "_serpent_flow", None)
        console = getattr(sf, "console", None) if sf is not None else None
        if console is not None:
            console.print(panel, highlight=False)
        else:
            self._repl_print(str(panel))

    def _render_infer_stats(self, result: Any) -> None:
        if result is None:
            self._repl_print("[dim]No build yet.[/dim]")
            return
        self._repl_print(
            f"[bold]Goal inference stats[/bold]\n"
            f"  build_ms:   {result.build_ms}\n"
            f"  samples:    {result.total_samples}\n"
            f"  hypotheses: {len(result.inferred)}\n"
            f"  reason:     {result.build_reason}"
        )
        if result.sources_contributing:
            self._repl_print("  sources:")
            for src, n in sorted(
                result.sources_contributing.items(),
                key=lambda kv: kv[1], reverse=True,
            ):
                self._repl_print(f"    {src:<20} {n}")

    def _repl_cmd_plugins(self, line: str) -> None:
        """/plugins — list loaded / failed / disabled plugins.

        Usage
        -----
        /plugins           → Rich table of all plugins + states
        /plugins failed    → only show failed plugins with error text
        """
        reg = getattr(self, "_plugin_registry", None)
        if reg is None:
            from backend.core.ouroboros.plugins import plugins_enabled
            if not plugins_enabled():
                self._repl_print(
                    "[yellow]Plugins disabled.[/yellow] "
                    "[dim]Set JARVIS_PLUGINS_ENABLED=1 and restart.[/dim]"
                )
            else:
                self._repl_print(
                    "[dim]No plugin registry (enabled but not yet booted).[/dim]"
                )
            return

        parts = line.split(None, 1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        outcomes = list(reg.outcomes)
        if sub == "failed":
            outcomes = [o for o in outcomes if o.state == "failed"]

        try:
            from rich.panel import Panel
            from rich.table import Table
            from rich.text import Text
        except Exception:
            # Plain fallback.
            if not outcomes:
                self._repl_print("[dim]No plugins loaded.[/dim]")
                return
            for o in outcomes:
                self._repl_print(
                    f"  {o.state:>10}  {o.name:<30}  {o.error or ''}"
                )
            return

        if not outcomes:
            self._repl_print("[dim]No plugins discovered.[/dim]")
            return

        tbl = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        tbl.add_column("state", justify="center")
        tbl.add_column("type")
        tbl.add_column("name")
        tbl.add_column("version", justify="right")
        tbl.add_column("details", no_wrap=False)

        state_color = {
            "loaded": "green",
            "failed": "red",
            "disabled_by_type": "dim",
            "skipped_master_off": "dim",
            "pending": "yellow",
        }
        for o in outcomes:
            m = o.manifest
            color = state_color.get(o.state, "white")
            details = ""
            if o.state == "failed" and o.error:
                details = o.error
            elif o.state == "loaded" and m is not None:
                details = (m.description or "")[:80]
            tbl.add_row(
                Text(o.state, style=color),
                (m.type if m else "?"),
                (m.name if m else o.name),
                (m.version if m else ""),
                details,
            )
        summary = Text()
        loaded = sum(1 for o in outcomes if o.state == "loaded")
        failed = sum(1 for o in outcomes if o.state == "failed")
        summary.append(f"loaded={loaded}  failed={failed}", style="bold")
        if failed:
            summary.append(
                "  /plugins failed for error details", style="dim",
            )
        panel = Panel(
            tbl,
            title="[bold]Plugin Registry[/bold]",
            subtitle=str(summary),
            border_style="cyan",
            padding=(1, 2),
        )
        sf = getattr(self, "_serpent_flow", None)
        console = getattr(sf, "console", None) if sf is not None else None
        if console is not None:
            console.print(panel, highlight=False)
        else:
            self._repl_print(str(panel))

    async def _try_dispatch_plugin_command(self, line: str) -> bool:
        """Return True if a plugin-registered REPL command matched and
        executed. False means the dispatch should fall through to the
        "Unknown REPL command" DEBUG log.

        The dispatch is tolerant — exceptions in plugin ``run()``
        surface as an error message to the operator, never crash the
        REPL loop.
        """
        reg = getattr(self, "_plugin_registry", None)
        if reg is None:
            return False
        stripped = line.strip()
        if not stripped.startswith("/"):
            return False
        head, _, args = stripped[1:].partition(" ")
        plugin = reg.repl_command(head)
        if plugin is None:
            return False
        try:
            output = await plugin.run(args.strip())
        except Exception as exc:
            self._repl_print(
                f"[red]/{head}: plugin raised {type(exc).__name__}: {exc}[/red]"
            )
            return True
        if output:
            self._repl_print(output)
        return True

    def _repl_cmd_tdd(self, line: str) -> None:
        """/tdd <op-id> — mark an in-flight / pending op as TDD-shaped.

        Stamps ``evidence["tdd_mode"] = True`` on the op's FSM context
        (and on any queued IntentEnvelope if the op hasn't yet reached
        CLASSIFY). The orchestrator picks up the flag at
        CONTEXT_EXPANSION and injects the TDD prompt directive.

        Honest scope: V1 is a **prompt contract**, not a red-green proof
        obligation. VALIDATE still runs the tests against the final
        bundle and L2 Repair engages when they fail — but we don't yet
        execute tests BEFORE impl to confirm they fail first. That's a
        separate orchestrator sub-phase project (V1.1).
        """
        parts = line.replace("/tdd", "tdd", 1).split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            self._repl_print(
                "[dim]Usage: /tdd <op-id>[/dim]\n"
                "[dim]Marks an op's evidence with tdd_mode=True. "
                "V1 injects a prompt directive; V1.1 will add red-green "
                "proof as a separate phase.[/dim]"
            )
            return
        target = parts[1].strip()
        gls = self._governed_loop_service
        if gls is None:
            self._repl_print(
                "[red]/tdd: governed_loop_service not booted yet[/red]"
            )
            return

        # Try to locate the op in the active FSM context set first.
        matched = False
        try:
            fsm_contexts = getattr(gls, "_fsm_contexts", {}) or {}
            for op_id, fsm_ctx in fsm_contexts.items():
                if op_id == target or op_id.startswith(target):
                    try:
                        import dataclasses as _dc
                        from backend.core.ouroboros.governance.tdd_directive import (
                            stamp_tdd_evidence,
                        )
                        _old_evidence = getattr(fsm_ctx, "evidence", None) or {}
                        new_evidence = stamp_tdd_evidence(_old_evidence, on=True)
                        try:
                            fsm_contexts[op_id] = _dc.replace(
                                fsm_ctx, evidence=new_evidence,
                            )
                        except Exception:
                            # Frozen dataclass without ``evidence`` field:
                            # fallback is no-op — operators can still set
                            # the env for a fresh op or mark via sensor.
                            pass
                        matched = True
                        self._repl_print(
                            f"[green]✓ /tdd[/green]  op=[bold]{op_id}[/bold] "
                            "marked TDD-shaped (prompt directive active at "
                            "next CONTEXT_EXPANSION)"
                        )
                    except Exception as exc:
                        self._repl_print(
                            f"[red]/tdd failed for {op_id}:[/red] {exc}"
                        )
                    break
        except Exception:
            pass
        if not matched:
            self._repl_print(
                f"[yellow]/tdd:[/yellow] no active op matching [bold]{target}[/bold]. "
                "Set intake-time via evidence[tdd_mode]=True, or retry "
                "after the op reaches CLASSIFY."
            )

    def _render_memory_detail_panel(self, mem: Any) -> None:
        """Rich panel for a single memory entry."""
        try:
            from rich.panel import Panel
            from rich.text import Text
            from rich.console import Group
        except Exception:
            # Plain fallback — same content as the legacy show output.
            self._repl_print(
                f"[bold]{mem.type.value}:{mem.name}[/bold]  [dim]({mem.id})[/dim]"
            )
            self._repl_print(f"  [dim]description:[/dim] {mem.description}")
            if mem.why:
                self._repl_print(f"  [dim]why:[/dim] {mem.why}")
            if mem.how_to_apply:
                self._repl_print(f"  [dim]how:[/dim] {mem.how_to_apply}")
            if mem.tags:
                self._repl_print(f"  [dim]tags:[/dim] {', '.join(mem.tags)}")
            if mem.paths:
                self._repl_print(f"  [dim]paths:[/dim] {', '.join(mem.paths)}")
            return

        lines: List[Any] = []
        header = Text()
        header.append(_memory_type_emoji(mem.type.value), style="bold")
        header.append(" ")
        header.append(mem.type.value, style="bold cyan")
        header.append("  ")
        header.append(mem.name, style="bold")
        header.append("  ")
        header.append(f"({mem.id})", style="dim")
        lines.append(header)
        lines.append(Text(mem.description))
        if mem.why:
            lines.append(Text.assemble(("Why:  ", "bold dim"), Text(mem.why)))
        if mem.how_to_apply:
            lines.append(Text.assemble(("How:  ", "bold dim"), Text(mem.how_to_apply)))
        if mem.tags:
            lines.append(Text.assemble(
                ("Tags: ", "bold dim"),
                Text(", ".join(mem.tags), style="yellow"),
            ))
        if mem.paths:
            lines.append(Text.assemble(
                ("Paths: ", "bold dim"),
                Text(", ".join(mem.paths), style="magenta"),
            ))
        if mem.content:
            lines.append(Text(""))
            lines.append(Text(mem.content[:800], style="dim"))
            if len(mem.content) > 800:
                lines.append(Text(
                    f"… ({len(mem.content) - 800} chars truncated)",
                    style="dim italic",
                ))

        panel = Panel(
            Group(*lines),
            title=f"[bold]Memory[/bold]  [dim]{mem.source or 'unknown-source'}[/dim]",
            border_style=_memory_border_for_type(mem.type.value),
            padding=(1, 2),
        )
        sf = getattr(self, "_serpent_flow", None)
        console = getattr(sf, "console", None) if sf is not None else None
        if console is not None:
            console.print(panel, highlight=False)
        else:
            self._repl_print(str(panel))

    def _render_memory_stats_panel(self, store: Any, MemoryType: Any) -> None:
        """Count + most-recent per type, as a Rich table."""
        try:
            from rich.panel import Panel
            from rich.table import Table
            from rich.text import Text
        except Exception:
            mems = store.list_all()
            counts = {}
            for m in mems:
                counts[m.type.value] = counts.get(m.type.value, 0) + 1
            for t, c in sorted(counts.items()):
                self._repl_print(f"  {t}: {c}")
            self._repl_print(f"  total: {len(mems)}")
            return

        tbl = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        tbl.add_column("Type")
        tbl.add_column("Count", justify="right")
        tbl.add_column("Most recent", no_wrap=False)

        all_mems = list(store.list_all())
        total = len(all_mems)
        types_seen = {}
        for t in MemoryType:
            mems_t = [m for m in all_mems if m.type is t]
            most_recent = (
                max(mems_t, key=lambda m: m.updated_at or m.created_at or "")
                if mems_t else None
            )
            types_seen[t] = (len(mems_t), most_recent)

        for t, (count, most_recent) in types_seen.items():
            count_style = "dim" if count == 0 else "bold"
            recent_txt = (
                Text(
                    f"{most_recent.name} ({most_recent.id})",
                    style="dim",
                )
                if most_recent else Text("—", style="dim")
            )
            tbl.add_row(
                Text(
                    f"{_memory_type_emoji(t.value)} {t.value}",
                    style="cyan",
                ),
                Text(str(count), style=count_style),
                recent_txt,
            )

        summary = Text()
        summary.append(f"Total memories: ", style="dim")
        summary.append(str(total), style="bold")
        summary.append(
            f"  |  store path: {store._root}",
            style="dim",
        )

        from rich.console import Group
        panel = Panel(
            Group(tbl, Text(""), summary),
            title="[bold]Memory Stats[/bold]",
            border_style="cyan",
            padding=(1, 2),
        )
        sf = getattr(self, "_serpent_flow", None)
        console = getattr(sf, "console", None) if sf is not None else None
        if console is not None:
            console.print(panel, highlight=False)
        else:
            self._repl_print(str(panel))

    def _render_memory_search(self, store: Any, query: str) -> None:
        """Full-text search across name + description + content."""
        q = query.strip().lower()
        if not q:
            return
        hits = []
        for m in store.list_all():
            haystack = " ".join([
                m.name or "", m.description or "",
                m.content or "", m.why or "", m.how_to_apply or "",
                " ".join(m.tags or ()), " ".join(m.paths or ()),
            ]).lower()
            if q in haystack:
                hits.append(m)
        if not hits:
            self._repl_print(
                f"[dim]No memories match '{query}'[/dim]"
            )
            return
        self._repl_print(
            f"[bold]Search[/bold]  [dim]query='{query}'  hits={len(hits)}[/dim]"
        )
        for m in hits[:25]:
            type_tag = f"[cyan]{m.type.value}[/cyan]"
            self._repl_print(
                f"  {type_tag}  [bold]{m.name}[/bold] "
                f"[dim]({m.id})[/dim]  {m.description[:80]}"
            )
        if len(hits) > 25:
            self._repl_print(
                f"  [dim]… {len(hits) - 25} more — refine the query[/dim]"
            )

    def _render_memory_recent(self, store: Any, n: int) -> None:
        """Show the N most-recently-updated memories."""
        all_mems = sorted(
            store.list_all(),
            key=lambda m: m.updated_at or m.created_at or "",
            reverse=True,
        )[:n]
        if not all_mems:
            self._repl_print("[dim]No memories recorded.[/dim]")
            return
        self._repl_print(
            f"[bold]Recent memories[/bold]  [dim](top {len(all_mems)})[/dim]"
        )
        for m in all_mems:
            ts = m.updated_at or m.created_at or ""
            ts_short = ts[:19] if ts else ""
            type_tag = f"[cyan]{m.type.value}[/cyan]"
            self._repl_print(
                f"  [dim]{ts_short}[/dim]  {type_tag}  "
                f"[bold]{m.name}[/bold] [dim]({m.id})[/dim]  "
                f"{m.description[:70]}"
            )

    def _repl_cmd_remember(self, line: str) -> None:
        """Shortcut for /memory add user: stores a free-form USER memory.

        Usage:
          /remember <text>

        The slug of the first 40 chars becomes the memory id so repeat
        ``/remember`` calls with matching prefixes upsert rather than
        pile up. The whole text becomes the description.
        """
        from backend.core.ouroboros.governance.user_preference_memory import (
            MemoryType,
        )

        store = self._user_pref_store()
        if store is None:
            return

        parts = line.replace("/remember", "remember", 1).split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            self._repl_print("[red]Usage: /remember <text>[/red]")
            return
        text = parts[1].strip()
        # Derive a short memory name from the first ~40 chars.
        short = text[:60].strip()
        try:
            mem = store.add(
                MemoryType.USER,
                short,
                text,
                source="repl:remember",
            )
            self._repl_print(
                f"[green]Remembered:[/green] [cyan]user[/cyan]  "
                f"[bold]{mem.name}[/bold] [dim]({mem.id})[/dim]"
            )
        except ValueError as exc:
            self._repl_print(f"[red]{exc}[/red]")

    def _repl_cmd_forget(self, line: str) -> None:
        """Shortcut for /memory rm: removes a memory by id.

        Usage:
          /forget <id>
        """
        store = self._user_pref_store()
        if store is None:
            return

        parts = line.replace("/forget", "forget", 1).split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            self._repl_print("[red]Usage: /forget <id>[/red]")
            return
        mem_id = parts[1].strip()
        if store.delete(mem_id):
            self._repl_print(f"[green]Forgotten: {mem_id}[/green]")
        else:
            self._repl_print(f"[red]No memory matching '{mem_id}'[/red]")

    async def _repl_cmd_undo(self, line: str) -> None:
        """/undo N — revert the last N O+V auto-commits.

        Grammar
        -------
        /undo                  → undo last 1 auto-commit
        /undo N                → undo last N auto-commits (N ≤ MAX_BATCH)
        /undo preview [N]      → dry-run: plan + safety check, no git mutation
        /undo --hard N         → reset --hard (history rewrite, unpushed only)

        Safety gate (all must pass before execute):
          • JARVIS_UNDO_ENABLED truthy
          • Working tree clean
          • No in-flight ops (gls._active_ops empty)
          • Every target commit bears the O+V trailer
          • N ≤ JARVIS_UNDO_MAX_BATCH (default 10)
          • --hard is refused on pushed branches
        """
        try:
            from backend.core.ouroboros.battle_test.undo_command import (
                UndoExecutor,
                UndoPlanner,
                parse_undo_args,
                render_plan,
            )
        except Exception:
            self._repl_print("[red]Undo module unavailable.[/red]")
            return

        n, mode, parse_err = parse_undo_args(line)
        if parse_err:
            self._repl_print(
                f"[red]/undo: {parse_err}[/red]\n"
                f"[dim]usage: /undo [N] | /undo preview [N] | /undo --hard N[/dim]"
            )
            return

        repo_root = self._config.repo_path
        planner = UndoPlanner(
            repo_root=repo_root,
            governed_loop_service=self._governed_loop_service,
        )
        plan = planner.plan(n, mode=mode)

        # Render + print the plan so the operator sees the verdict
        # before any mutation (and always for preview mode).
        self._repl_print(render_plan(plan))

        if mode == "preview":
            return

        if not plan.is_safe:
            # Errors already printed inside render_plan output.
            self._repl_print(
                "[bold red]/undo aborted — resolve errors above.[/bold red]"
            )
            return

        # Execute the mutation.
        comm = None
        try:
            comm = getattr(self._governance_stack, "comm", None)
        except Exception:
            comm = None

        executor = UndoExecutor(repo_root=repo_root, comm=comm)
        result = await executor.execute(plan)

        if not result.executed:
            self._repl_print(
                f"[bold red]/undo failed:[/bold red] {result.error or 'unknown error'}"
            )
            return

        # Session stats counter (surfaced in end-of-session summary).
        try:
            self._undone_count = getattr(self, "_undone_count", 0) + result.n_reverted
        except Exception:
            pass

        sha_tail = f" → {result.committed_sha[:10]}" if result.committed_sha else ""
        self._repl_print(
            f"[bold green]✓ /undo {mode}[/bold green]  "
            f"reverted=[bold]{result.n_reverted}[/bold]  "
            f"files=[bold]{len(result.files_affected)}[/bold]"
            f"{sha_tail}"
        )

    async def _repl_cmd_resume_op(self, line: str) -> None:
        """/resume — re-enqueue an orphaned in-flight op from the ledger.

        Distinct from the legacy sync :meth:`_repl_cmd_resume` which
        unpauses the intake sensor fleet. This handler owns the
        orphan-replay flow (ledger scan + intake re-enqueue).

        Grammar
        -------
        /resume                → resume the most recent orphan
        /resume list           → Rich table of all orphans (read-only)
        /resume <op-prefix>    → resume a specific op by id/short-id prefix
        /resume all            → batch re-enqueue every qualifying orphan

        V1 scope: re-enqueues the intent (goal + target_files) as a
        fresh IntentEnvelope. The in-flight candidate, validation, and
        L2 iteration state are NOT preserved (require a separate
        orchestrator refactor). Workspace restoration is also deferred
        until checkpoint persistence lands — operators see an explicit
        honesty note in every render.
        """
        try:
            from backend.core.ouroboros.battle_test.resume_command import (
                ResumeExecutor,
                ResumeScanner,
                parse_resume_args,
                render_plan,
            )
        except Exception:
            self._repl_print("[red]Resume module unavailable.[/red]")
            return

        mode, op_prefix, err = parse_resume_args(line)
        if err:
            self._repl_print(
                f"[red]/resume: {err}[/red]\n"
                f"[dim]usage: /resume | /resume list | /resume all | /resume <op-prefix>[/dim]"
            )
            return

        ledger_root = (
            self._config.repo_path
            / ".ouroboros" / "state" / "ouroboros" / "ledger"
        )
        scanner = ResumeScanner(
            ledger_root=ledger_root,
            governed_loop_service=self._governed_loop_service,
        )
        plan = scanner.plan(mode=mode, op_id_prefix=op_prefix)
        self._repl_print(render_plan(plan))

        # List mode is read-only by contract.
        if mode == "list":
            return
        # Global errors block execution.
        if plan.has_global_errors:
            self._repl_print(
                "[bold red]/resume aborted — resolve errors above.[/bold red]"
            )
            return
        if plan.resumable_count == 0:
            self._repl_print(
                "[yellow]No resumable orphans.[/yellow]"
            )
            return

        # Resolve the intake router via the GLS (the executor talks
        # directly to the router to avoid re-entering the sensor chain).
        router = None
        try:
            gls = self._governed_loop_service
            # Multiple attribute paths used in the codebase at different
            # versions; try each.
            for attr in (
                "_intake_router", "intake_router", "_router", "router",
            ):
                cand = getattr(gls, attr, None)
                if cand is not None:
                    router = cand
                    break
        except Exception:
            router = None
        if router is None:
            self._repl_print(
                "[bold red]/resume failed:[/bold red] intake router unavailable"
            )
            return

        comm = None
        try:
            comm = getattr(self._governance_stack, "comm", None)
        except Exception:
            comm = None

        executor = ResumeExecutor(
            repo_name=getattr(self._config, "primary_repo", "") or "jarvis",
            intake_router=router,
            comm=comm,
        )
        result = await executor.execute(plan)

        if not result.executed and result.error:
            self._repl_print(
                f"[bold red]/resume failed:[/bold red] {result.error}"
            )
            return

        new_ids = ", ".join(
            x[:12] for x in result.resumed_op_ids[:4]
        ) + (" …" if len(result.resumed_op_ids) > 4 else "")
        self._repl_print(
            f"[bold green]✓ /resume[/bold green]  "
            f"resumed=[bold]{len(result.resumed_op_ids)}[/bold]  "
            f"skipped=[bold]{len(result.skipped_reasons)}[/bold]"
            + (f"  new_ids=[dim]{new_ids}[/dim]" if new_ids else "")
        )
        for parent, reason in result.skipped_reasons[:5]:
            parent_short = parent.split("-", 1)[1][:10] if "-" in parent else parent[:10]
            self._repl_print(
                f"  [dim]skipped {parent_short}: {reason}[/dim]"
            )

    def _notify_orphaned_ops_at_boot(self) -> None:
        """Emit a non-blocking INFO line when orphaned ops are available.

        Called once after GLS is booted but before the REPL opens.
        Intentionally does not prompt — operators drive /resume when
        ready. A noisy interactive prompt would block the boot sequence
        for headless / CI runs.
        """
        try:
            from backend.core.ouroboros.battle_test.resume_command import (
                ResumeScanner,
                resume_enabled,
            )
        except Exception:
            return
        if not resume_enabled():
            return
        ledger_root = (
            self._config.repo_path
            / ".ouroboros" / "state" / "ouroboros" / "ledger"
        )
        try:
            scanner = ResumeScanner(
                ledger_root=ledger_root,
                governed_loop_service=self._governed_loop_service,
            )
            orphans = scanner.scan_orphans()
        except Exception:
            logger.debug("[Resume] boot scan failed", exc_info=True)
            return
        if not orphans:
            return
        # Only surface orphans within the age window; otherwise operators
        # get pestered by ancient history.
        try:
            from backend.core.ouroboros.battle_test.resume_command import (
                max_age_s,
            )
            cutoff = max_age_s()
        except Exception:
            cutoff = 86400
        fresh = [o for o in orphans if o.age_s <= cutoff]
        if not fresh:
            return
        logger.info(
            "[Resume] %d orphaned op(s) available — /resume list to review "
            "(oldest=%ds last_phase=%s)",
            len(fresh),
            int(max(o.age_s for o in fresh)),
            fresh[0].last_state,
        )

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def register_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register SIGHUP/SIGINT/SIGTERM handlers to trigger clean shutdown.

        Insurance pattern: each signal handler does a **synchronous**
        partial-summary write BEFORE setting the shutdown event. This
        ensures the session dir ends up with a summary.json even when
        the subsequent async cleanup can't complete (parent escalates
        SIGTERM to SIGKILL, asyncio gets stuck in a C extension, or
        an exception in ``_shutdown_components`` aborts the finally
        block mid-execution).

        **Ticket B (2026-04-23): SIGHUP added.** Previously only SIGINT
        and SIGTERM were handled; when the agent-conducted soak harness
        is launched as a background pipeline (``tail -f /dev/null |
        python3 scripts/ouroboros_battle_test.py ...``) and the parent
        bash is killed via Claude Code's ``TaskStop``, Python's default
        SIGHUP action is to terminate without running ``atexit`` — the
        signature left behind was a session dir with only ``debug.log``
        and no ``summary.json`` (#7 GENERATE S2, ``bt-2026-04-23-070317``).
        SIGHUP now routes through the same sync-partial-write path as
        SIGTERM/SIGINT and stamps ``session_outcome="incomplete_kill"``
        plus a signal-specific ``stop_reason`` so audit tooling can
        distinguish parent-death from operator-interrupt.

        **Ticket B: SIGPIPE explicitly ignored.** Harness runs under the
        ``tail -f /dev/null | ...`` idiom (see Ticket C). If the parent
        bash dies, the stdin pipe closes and subsequent writes to stdout
        (log handlers during shutdown) could raise ``BrokenPipeError``
        via SIGPIPE. Setting ``SIG_IGN`` prevents the interpreter from
        crashing on the first pipe-broken write during the signal-driven
        cleanup path.

        The sync write is idempotent: the ``_summary_written`` flag
        prevents the atexit fallback from double-writing if the async
        path DOES complete afterward. If the async clean path runs
        successfully, it overwrites the partial summary with the full
        one (same flag check, opposite direction).

        Catches ``NotImplementedError`` on Windows where
        ``loop.add_signal_handler`` is not supported.
        """
        # SIGPIPE: ignore process-wide so broken-pipe writes during
        # shutdown don't crash before atexit runs. Safe — the harness
        # doesn't rely on SIGPIPE for anything else. Must happen OUTSIDE
        # loop.add_signal_handler (SIG_IGN is a plain signal.signal call).
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        except (AttributeError, ValueError, OSError):
            # Windows lacks SIGPIPE; ValueError/OSError can surface when
            # called off the main thread. All non-fatal — fall through.
            pass
        try:
            import functools as _functools
            loop.add_signal_handler(
                signal.SIGINT,
                _functools.partial(self._handle_shutdown_signal, "sigint"),
            )
            loop.add_signal_handler(
                signal.SIGTERM,
                _functools.partial(self._handle_shutdown_signal, "sigterm"),
            )
            # Ticket B: SIGHUP. macOS/Linux only — Windows lacks SIGHUP
            # and the getattr probe below keeps this import-safe.
            _sighup = getattr(signal, "SIGHUP", None)
            if _sighup is not None:
                loop.add_signal_handler(
                    _sighup,
                    _functools.partial(self._handle_shutdown_signal, "sighup"),
                )
        except NotImplementedError:
            logger.warning("Signal handlers not supported on this platform")

    def _handle_shutdown_signal(self, signal_name: Optional[str] = None) -> None:
        """Bound callback fired by asyncio on SIGHUP/SIGINT/SIGTERM.

        Performs the sync partial-summary write BEFORE setting the
        shutdown event so the async cleanup cannot be cut off without
        at least a v1.1a-parseable summary.json landing on disk. If
        the async clean path subsequently completes, it overwrites
        the partial with the full summary (same ``_summary_written``
        flag gate).

        Ticket B (2026-04-23): ``signal_name`` is bound via
        ``functools.partial`` at registration time (one of ``"sighup"``,
        ``"sigterm"``, ``"sigint"``). Stamps a signal-specific
        ``_stop_reason`` before the sync write so the partial summary
        distinguishes parent-death (SIGHUP) from operator-interrupt
        (SIGINT) from container-kill (SIGTERM), and passes
        ``session_outcome="incomplete_kill"`` through to the writer.

        When called with ``signal_name=None`` (legacy test-harness path
        before Ticket B), behavior is preserved: no ``_stop_reason``
        stamping, atexit fallback writes ``"partial_shutdown:atexit_fallback"``
        and no ``session_outcome`` field. This preserves the pre-Ticket-B
        contract that the ``register_signal_handlers`` production path
        always supplies the signal name.
        """
        # Harness Epic Slice 1 — arm the bounded-shutdown watchdog FIRST
        # (before any of the existing async / partial-summary work). If
        # the rest of this handler (or the downstream asyncio shutdown
        # path) wedges, the watchdog's daemon thread will fire
        # os._exit(75) after the deadline. Master flag default true;
        # ``=false`` reverts to pre-Slice-1 (asyncio-only shutdown).
        try:
            from backend.core.ouroboros.battle_test.shutdown_watchdog import (
                default_deadline_s as _bsw_deadline_s,
            )
            _wdg = getattr(self, "_shutdown_watchdog", None)
            if _wdg is not None and signal_name is not None:
                _wdg.arm(reason=signal_name, deadline_s=_bsw_deadline_s())
        except Exception:  # noqa: BLE001 — never let watchdog arm crash signal handler
            pass

        # Stamp the signal-specific reason before the write runs so the
        # partial summary carries an actionable classifier instead of the
        # generic "shutdown_signal" catch-all. Keep the existing value if
        # something earlier on the path already set a more informative
        # one (e.g. wall_clock_cap raced ahead of the signal).
        if signal_name is not None and self._stop_reason in ("unknown", "", None):
            self._stop_reason = signal_name
        # TerminationHookRegistry Slice 3 — migrate from direct
        # _atexit_fallback_write call to registry dispatch.
        # Byte-equivalent: the registered partial_summary_writer
        # hook (priority 10, runs first in PRE_SHUTDOWN_EVENT_SET)
        # invokes the SAME _atexit_fallback_write with the SAME
        # session_outcome="incomplete_kill" kwarg the direct call
        # used. Pinned by the byte-equivalency test in
        # test_termination_hook_slice3_wiring.py. The registry
        # wrap adds: (a) wall-cap path symmetry — the bug fix;
        # (b) future-hook composability — operator-defined hooks
        # can register at PRE_SHUTDOWN_EVENT_SET to fire on
        # every termination path.
        try:
            from backend.core.ouroboros.battle_test.termination_hook import (  # noqa: E501
                TerminationCause as _TermCause,
                TerminationPhase as _TermPhase,
            )
            from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
                get_default_registry as _term_registry,
            )
            # Map signal name → cause. The two-way mapping is
            # tight: the only signal_names the existing handler
            # supports are sigterm/sigint/sighup; None is the
            # legacy test-harness path (no session_outcome stamp,
            # writer uses default — preserved by the adapter's
            # NORMAL_EXIT branch which calls writer() with no
            # kwargs).
            if signal_name == "sigterm":
                _cause = _TermCause.SIGTERM
            elif signal_name == "sigint":
                _cause = _TermCause.SIGINT
            elif signal_name == "sighup":
                _cause = _TermCause.SIGHUP
            elif signal_name is None:
                # Legacy test-harness path — preserve the no-kwarg
                # writer call by using NORMAL_EXIT (the adapter's
                # cause→outcome mapping returns None for this,
                # which calls writer() with no session_outcome —
                # the pre-Ticket-B contract).
                _cause = _TermCause.NORMAL_EXIT
            else:
                _cause = _TermCause.UNKNOWN
            _term_registry().dispatch(
                phase=_TermPhase.PRE_SHUTDOWN_EVENT_SET,
                cause=_cause,
                session_dir=str(self._session_dir),
                started_at=self._started_at,
                stop_reason=self._stop_reason or (
                    signal_name or ""
                ),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "signal-driven termination-hook dispatch failed",
                exc_info=True,
            )
        # W3(7) Slice 4 — Class F cancel emission for in-flight ops.
        # ADDITIVE to the existing partial-summary write above (operator
        # resolution-4: no harness dependency for correctness; the
        # partial-summary path keeps working regardless of this hook).
        # Master flag off OR signal sub-flag off (both default false) →
        # emit_signal_cancel returns 0 — silent no-op, byte-for-byte
        # pre-W3(7). Never raises into the signal handler — interrupt-safe.
        if signal_name is not None:
            try:
                from backend.core.ouroboros.governance.cancel_token import (
                    emit_signal_cancel as _emit_signal_cancel,
                )
                _gls = getattr(self, "_governed_loop_service", None)
                _registry = getattr(_gls, "_cancel_token_registry", None) if _gls else None
                if _registry is not None:
                    _emit_signal_cancel(
                        signal_name=signal_name,
                        registry=_registry,
                        session_dir=getattr(self, "_session_dir", None),
                        phase_at_trigger="unknown",
                        reason=f"signal {signal_name} received during session",
                    )
            except Exception:  # noqa: BLE001 — interrupt-safe
                logger.debug(
                    "signal-driven Class F emission skipped",
                    exc_info=True,
                )
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    # ------------------------------------------------------------------
    # GLS Activity Monitor (staleness-aware)
    # ------------------------------------------------------------------

    # Per-op staleness threshold: if an operation hasn't transitioned FSM
    # state in this many seconds, it's considered stale. The watchdog is
    # NOT poked for stale ops — if ALL ops are stale the idle timer fires.
    _OP_STALE_THRESHOLD_S: float = float(
        os.environ.get("OUROBOROS_OP_STALE_THRESHOLD_S", "600")
    )

    # After this many seconds of zero phase transitions we forcibly cancel
    # the stale op via GLS (if it exposes a cancel API). 0 = never cancel.
    _OP_FORCE_CANCEL_S: float = float(
        os.environ.get("OUROBOROS_OP_FORCE_CANCEL_S", "900")
    )

    async def _monitor_gls_activity(self) -> None:
        """Background task: staleness-aware watchdog feeder.

        Every 5 seconds, inspects each in-flight operation's FSM context to
        determine whether it has made progress (phase transition) recently.

        - If at least one op is progressing → poke the idle watchdog.
        - If ALL ops are stale (no transitions beyond threshold) → stop
          poking.  The idle watchdog will eventually fire and stop the session.
        - If an op exceeds the force-cancel threshold → attempt cancellation
          and log forensics so the session can move on.

        This prevents a single hung operation from keeping the session alive
        indefinitely while producing no useful work.
        """
        # Track per-op first-seen-stale time for force-cancel escalation
        _stale_since: dict[str, float] = {}

        try:
            while True:
                await asyncio.sleep(5.0)
                gls = self._governed_loop_service
                if gls is None:
                    continue

                try:
                    active_ops: set = getattr(gls, "_active_ops", set())
                    if not active_ops:
                        continue  # No ops — let the normal idle timer handle it

                    fsm_contexts: dict = getattr(gls, "_fsm_contexts", {})
                    now = datetime.now(tz=timezone.utc)
                    now_mono = time.monotonic()

                    progressing_count = 0
                    stale_count = 0
                    stale_details: list = []

                    for dedupe_key in list(active_ops):
                        # Find the matching FSM context (op_id may differ from dedupe_key)
                        fsm_ctx = None
                        for op_id, ctx in fsm_contexts.items():
                            if op_id == dedupe_key or dedupe_key in op_id:
                                fsm_ctx = ctx
                                break

                        if fsm_ctx is None:
                            # No FSM context yet — op is still in early setup, count as progressing
                            progressing_count += 1
                            _stale_since.pop(dedupe_key, None)
                            continue

                        last_transition = getattr(fsm_ctx, "last_transition_at_utc", None)
                        # Phase-Aware Heartbeats (Move 2 v4): a long GENERATE
                        # that's actively streaming tokens updates
                        # ``last_activity_at_utc`` between phase transitions.
                        # Take the max so a producing stream is observably
                        # fresh and not mis-classified stale.
                        last_activity = getattr(fsm_ctx, "last_activity_at_utc", None)
                        if last_transition is None and last_activity is None:
                            progressing_count += 1
                            _stale_since.pop(dedupe_key, None)
                            continue
                        if last_transition is None:
                            freshness_ts = last_activity
                        elif last_activity is None:
                            freshness_ts = last_transition
                        else:
                            freshness_ts = max(last_transition, last_activity)

                        elapsed_s = (now - freshness_ts).total_seconds()

                        if elapsed_s < self._OP_STALE_THRESHOLD_S:
                            # Op is progressing normally
                            progressing_count += 1
                            _stale_since.pop(dedupe_key, None)
                        else:
                            # Op is stale
                            stale_count += 1
                            phase_name = getattr(
                                getattr(fsm_ctx, "state", None), "name", "UNKNOWN"
                            )

                            if dedupe_key not in _stale_since:
                                _stale_since[dedupe_key] = now_mono
                                logger.warning(
                                    "[ActivityMonitor] Op %s is STALE: "
                                    "phase=%s, no transition for %.0fs (threshold=%.0fs)",
                                    dedupe_key[:16], phase_name,
                                    elapsed_s, self._OP_STALE_THRESHOLD_S,
                                )

                            stale_details.append({
                                "op_id": dedupe_key[:16],
                                "phase": phase_name,
                                "elapsed_s": round(elapsed_s, 1),
                                "last_transition_utc": str(last_transition),
                            })

                            # Force-cancel escalation
                            stale_duration = now_mono - _stale_since[dedupe_key]
                            if (
                                self._OP_FORCE_CANCEL_S > 0
                                and stale_duration >= self._OP_FORCE_CANCEL_S
                            ):
                                logger.error(
                                    "[ActivityMonitor] FORCE-CANCELLING stale op %s "
                                    "(stuck in %s for %.0fs, force-cancel threshold=%.0fs)",
                                    dedupe_key[:16], phase_name,
                                    elapsed_s, self._OP_FORCE_CANCEL_S,
                                )
                                await self._force_cancel_op(gls, dedupe_key)
                                _stale_since.pop(dedupe_key, None)

                    # Clean up tracking for ops that are no longer active
                    for key in list(_stale_since):
                        if key not in active_ops:
                            _stale_since.pop(key, None)

                    # Decision: poke or starve the watchdog
                    if progressing_count > 0:
                        self._idle_watchdog.poke()
                        if stale_count > 0:
                            logger.info(
                                "[ActivityMonitor] %d progressing, %d stale — poked watchdog",
                                progressing_count, stale_count,
                            )
                        else:
                            logger.debug(
                                "[ActivityMonitor] %d ops progressing, poked watchdog",
                                progressing_count,
                            )
                    else:
                        # ALL ops are stale — do NOT poke. If this persists,
                        # the idle watchdog fires and stops the session.
                        logger.warning(
                            "[ActivityMonitor] ALL %d ops are stale — NOT poking watchdog "
                            "(idle timer will fire in ≤%.0fs)",
                            stale_count, self._config.idle_timeout_s,
                        )
                        # If stale for long enough, fire immediately with diagnostics
                        from backend.core.ouroboros.battle_test.idle_watchdog import StaleOpInfo
                        all_stale_long_enough = all(
                            (now_mono - _stale_since.get(k, now_mono)) >= self._OP_STALE_THRESHOLD_S
                            for k in active_ops
                        )
                        if all_stale_long_enough and stale_details:
                            stale_infos = [
                                StaleOpInfo(
                                    op_id=d["op_id"],
                                    phase=d["phase"],
                                    elapsed_s=d["elapsed_s"],
                                    last_transition_utc=d["last_transition_utc"],
                                )
                                for d in stale_details
                            ]
                            self._idle_watchdog.fire_stale(stale_infos)

                except Exception as exc:
                    logger.debug("[ActivityMonitor] Error in staleness check: %s", exc)

        except asyncio.CancelledError:
            pass

    async def _force_cancel_op(self, gls: Any, dedupe_key: str) -> None:
        """Best-effort cancellation of a stuck operation.

        Removes the op from _active_ops and _fsm_contexts so the system
        can move on. The orchestrator's own timeout should eventually clean
        up the actual task, but this unblocks the harness immediately.
        """
        try:
            active_ops: set = getattr(gls, "_active_ops", set())
            active_ops.discard(dedupe_key)

            fsm_contexts: dict = getattr(gls, "_fsm_contexts", {})
            # Find and remove matching FSM context
            to_remove = [
                op_id for op_id in fsm_contexts
                if op_id == dedupe_key or dedupe_key in op_id
            ]
            for op_id in to_remove:
                fsm_contexts.pop(op_id, None)

            logger.info(
                "[ActivityMonitor] Force-cancelled op %s (removed from active_ops + fsm_contexts)",
                dedupe_key[:16],
            )
        except Exception as exc:
            logger.warning("[ActivityMonitor] Force-cancel failed for %s: %s", dedupe_key[:16], exc)

    # ------------------------------------------------------------------
    # Provider Cost Monitor
    # ------------------------------------------------------------------

    async def _monitor_provider_costs(self) -> None:
        """Background task: polls provider stats and feeds costs into CostTracker.

        Poll cadence is env-driven. Default 1.0s (was 5.0s — Task #95 hard-cap
        fix). The old 5s gap allowed a full Claude call to land between polls
        and bill *after* the budget was already crossed, causing ~$0.036
        overshoot on a $0.50 cap. Env: ``JARVIS_COST_POLL_INTERVAL_S``.
        """
        try:
            interval = float(os.environ.get("JARVIS_COST_POLL_INTERVAL_S", "1.0"))
            if interval <= 0.0:
                interval = 1.0
        except (TypeError, ValueError):
            interval = 1.0
        _last_dw_cost: float = 0.0
        _last_claude_cost: float = 0.0
        try:
            while True:
                await asyncio.sleep(interval)
                gls = self._governed_loop_service
                if gls is None:
                    continue

                # DoubleWord: read cumulative cost from get_stats()
                dw = getattr(gls, "doubleword_provider", None)
                if dw is not None:
                    try:
                        stats = dw.get_stats()
                        total = stats.get("total_cost_usd", 0.0)
                        delta = total - _last_dw_cost
                        if delta > 0:
                            self._cost_tracker.record("doubleword", delta)
                            _last_dw_cost = total
                    except Exception:
                        pass

                # Claude: read cumulative daily spend
                gen = getattr(gls, "_generator", None)
                if gen is not None:
                    fallback = getattr(gen, "_fallback", None)
                    if fallback is not None and getattr(fallback, "provider_name", "") == "claude-api":
                        try:
                            total = getattr(fallback, "_daily_spend", 0.0)
                            delta = total - _last_claude_cost
                            if delta > 0:
                                self._cost_tracker.record("claude", delta)
                                _last_claude_cost = total
                        except Exception:
                            pass

                    # Also check primary (Claude could be primary if DW was demoted)
                    primary = getattr(gen, "_primary", None)
                    if (
                        primary is not None
                        and primary is not fallback
                        and getattr(primary, "provider_name", "") == "claude-api"
                    ):
                        try:
                            total = getattr(primary, "_daily_spend", 0.0)
                            delta = total - _last_claude_cost
                            if delta > 0:
                                self._cost_tracker.record("claude", delta)
                                _last_claude_cost = total
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Hot-reload Restart Monitor
    # ------------------------------------------------------------------

    def _start_wall_clock_hard_deadline_thread(
        self, cap_s: float,
    ) -> None:
        """Defect #1 Slice B (2026-05-03) — thread-based safety net
        immune to asyncio starvation.

        The asyncio :meth:`_monitor_wall_clock` is the primary fire
        path. This thread runs in parallel and fires the SAME
        ``self._wall_clock_event`` (via ``loop.call_soon_threadsafe``)
        if the asyncio path is wedged for longer than ``cap_s + grace``.

        Grace defaults to 2 * check_interval_s so under normal
        conditions the asyncio path always wins (no double-firing).
        Env-tunable via ``JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S``;
        floor 5s, ceiling 600s.

        Daemonic thread — process exit kills it cleanly. Stop event
        signals graceful exit when the asyncio path fired first.
        NEVER raises into the host loop.
        """
        try:
            raw_grace = os.environ.get(
                "JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S", "",
            ).strip()
            grace_s = max(
                5.0, min(600.0, float(raw_grace) if raw_grace else 30.0),
            )
        except (TypeError, ValueError):
            grace_s = 30.0
        # Anchor on monotonic clock — same discipline as the asyncio
        # path; immune to NTP adjustments mid-soak.
        anchor_monotonic = time.monotonic()
        deadline_monotonic = anchor_monotonic + cap_s + grace_s
        loop = asyncio.get_event_loop()
        stop_event = threading.Event()
        self._wall_clock_hard_deadline_stop = stop_event

        def _watch() -> None:
            try:
                while not stop_event.is_set():
                    now = time.monotonic()
                    remaining = deadline_monotonic - now
                    if remaining <= 0:
                        # Deadline reached. Fire the asyncio event from
                        # this thread via call_soon_threadsafe so the
                        # async race in run() picks it up. If the loop
                        # is wedged AND call_soon_threadsafe doesn't
                        # process, the BoundedShutdownWatchdog (which
                        # the asyncio path arms when it eventually
                        # fires) is the next layer; failing that,
                        # the OS-level signal handler is the last layer.
                        try:
                            loop.call_soon_threadsafe(
                                self._wall_clock_event.set,
                            )
                        except Exception:  # noqa: BLE001 -- defensive
                            pass
                        # Also stamp stop_reason if not already set
                        # (mirror the asyncio path's discipline).
                        if self._stop_reason in ("unknown", "", None):
                            self._stop_reason = "wall_clock_cap"
                        try:
                            import logging as _logging
                            _logging.getLogger(
                                "backend.core.ouroboros.battle_test.harness"
                            ).warning(
                                "[WallClockWatchdog] HARD DEADLINE "
                                "thread fired: monotonic elapsed "
                                "%.0fs >= cap %.0fs + grace %.0fs "
                                "(asyncio path was wedged or already "
                                "fired).",
                                time.monotonic() - anchor_monotonic,
                                cap_s, grace_s,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        return
                    # Sleep at most 5s OR remaining-to-deadline.
                    # Bounds wake-up so stop_event.set() from the
                    # asyncio path takes effect within 5s rather than
                    # waiting for the full remaining duration.
                    stop_event.wait(timeout=min(5.0, remaining))
            except Exception:  # noqa: BLE001 -- contract: never crash
                pass

        thread = threading.Thread(
            target=_watch,
            daemon=True,
            name="WallClockHardDeadlineThread",
        )
        self._wall_clock_hard_deadline_thread = thread
        thread.start()

    async def _monitor_wall_clock(self, cap_s: float) -> None:
        """Ticket A Guard 2 — hard wall-clock watchdog.

        Opaque ceiling on total session duration. Unlike ``_monitor_gls_activity``
        which gates on phase-transition progress (and can be hijacked by
        provider retry storms that reset per-op last_transition_at_utc), this
        watchdog fires strictly on wall-clock elapsed time and is immune to
        any activity signal. Graduation soaks MUST arm this via
        ``--max-wall-seconds`` to guarantee deterministic termination.

        Fires ``self._wall_clock_event`` once the configured cap is exceeded,
        which joins the FIRST_COMPLETED race in ``run()`` alongside shutdown /
        budget / idle waiters and routes to ``stop_reason="wall_clock_cap"``.

        Defect #1 fix (2026-05-03): the original implementation issued a
        single ``asyncio.sleep(cap_s)`` for the entire cap duration. When
        the event loop was starved by long-running coroutines (e.g. 200+s
        background ops doing blocking I/O), the sleep callback waited its
        turn — observed soak v5 firing 22 minutes after the cap was hit.
        Defense-in-depth fix:

          1. Periodic check loop using monotonic clock — wakes every
             ``JARVIS_WALL_CLOCK_CHECK_INTERVAL_S`` seconds (default 5s,
             floor 1s), checks elapsed against cap_s. Caps fire delay at
             one tick interval under normal conditions.
          2. Thread-based hard deadline (Slice B) — runs in parallel,
             immune to asyncio starvation entirely; fires at
             ``cap_s + JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S`` if the
             asyncio path is wedged.

        Per the harness-class footnote in the graduation matrix,
        ``wall_clock_cap`` is treated equivalent to ``idle_timeout`` for
        clean-bar purposes — both are orderly graceful-shutdown paths.
        """
        # Capture monotonic anchor at task entry — immune to NTP
        # adjustments during the soak (wall-clock time.time() can jump
        # backwards or forwards; monotonic cannot).
        anchor_monotonic = time.monotonic()
        # Env-tunable check interval (no hardcoding; floor 1s to avoid
        # busy-loop; ceiling 60s to cap fire delay under sane configs).
        try:
            raw = os.environ.get(
                "JARVIS_WALL_CLOCK_CHECK_INTERVAL_S", "",
            ).strip()
            check_interval_s = max(
                1.0, min(60.0, float(raw) if raw else 5.0),
            )
        except (TypeError, ValueError):
            check_interval_s = 5.0
        while True:
            try:
                await asyncio.sleep(check_interval_s)
            except asyncio.CancelledError:
                return
            elapsed = time.monotonic() - anchor_monotonic
            if elapsed >= cap_s:
                break
            # Loop continues; next sleep will wake at +check_interval_s.
        # If another waiter already fired and the shutdown path is in progress,
        # this is a no-op — set() on an already-set event is idempotent, and
        # the FIRST_COMPLETED race has already picked its winner.
        logger.warning(
            "[WallClockWatchdog] fired: wall time %.0fs exceeded max_wall_seconds=%.0fs — "
            "triggering graceful shutdown with stop_reason=wall_clock_cap.",
            time.time() - self._started_at,
            cap_s,
        )
        # Stamp stop_reason FIRST so the termination-hook adapter
        # below sees "wall_clock_cap" instead of falling back to
        # the cause-derived value. Mirrors the signal handler's
        # discipline at lines 3286-3287 (only stamp if not already
        # classified — preserves any earlier-path classification).
        if self._stop_reason in ("unknown", "", None):
            self._stop_reason = "wall_clock_cap"
        # TerminationHookRegistry Slice 3 — THE bug fix.
        # Synchronously dispatch the PRE_SHUTDOWN_EVENT_SET phase
        # BEFORE arming the BoundedShutdownWatchdog + setting the
        # wall_clock_event. The registered partial_summary_writer
        # hook lands a v1.1a-parseable summary.json on disk so
        # the bounded watchdog's eventual os._exit(75) doesn't
        # leave the session dir summary-less (the
        # bt-2026-05-02-203805 reproduction). The registry's
        # 10s phase budget keeps this within the
        # BoundedShutdownWatchdog's 30s grace; per-hook timeouts
        # bound any single hook. Strict-sync (threading-only —
        # survives a wedged asyncio loop). NEVER raises.
        try:
            from backend.core.ouroboros.battle_test.termination_hook import (  # noqa: E501
                TerminationCause as _TermCause,
                TerminationPhase as _TermPhase,
            )
            from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
                get_default_registry as _term_registry,
            )
            _term_registry().dispatch(
                phase=_TermPhase.PRE_SHUTDOWN_EVENT_SET,
                cause=_TermCause.WALL_CLOCK_CAP,
                session_dir=str(self._session_dir),
                started_at=self._started_at,
                stop_reason=self._stop_reason or "wall_clock_cap",
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[WallClockWatchdog] termination-hook dispatch "
                "degraded: %s", exc,
            )
        # Harness Epic Slice 1 — arm the bounded-shutdown watchdog in
        # PARALLEL to the asyncio event. If the asyncio loop is wedged
        # (the S6 hypothesis — wall watchdog never fired at 2400s),
        # this thread-side arm guarantees os._exit fires after the
        # deadline. Best-effort, never raises.
        try:
            from backend.core.ouroboros.battle_test.shutdown_watchdog import (
                default_deadline_s as _bsw_deadline_s,
            )
            _wdg = getattr(self, "_shutdown_watchdog", None)
            if _wdg is not None:
                _wdg.arm(reason="wall_clock_cap", deadline_s=_bsw_deadline_s())
        except Exception:  # noqa: BLE001
            pass
        self._wall_clock_event.set()
        # Defect #1 Slice B (2026-05-03) — signal the thread-based
        # safety net to exit cleanly. Without this, the daemon thread
        # would keep ticking until process exit (harmless but noisy).
        try:
            stop_evt = getattr(
                self, "_wall_clock_hard_deadline_stop", None,
            )
            if stop_evt is not None:
                stop_evt.set()
        except Exception:  # noqa: BLE001
            pass

    async def _monitor_restart_pending(self) -> None:
        """Background task: poll the orchestrator's hot-reloader for a
        restart-pending flag, and trigger graceful shutdown when set.

        The flag is raised by ModuleHotReloader when O+V self-modifies a
        quarantined or unsafe-to-reload module — the running process can't
        safely swap that code in-place, so we shut down cleanly and let
        the wrapper script re-exec with the same argv via exit code 75.

        Polls every 3 seconds — frequent enough that the next op doesn't
        start before we shut down, infrequent enough that the cost is
        invisible (~0.5ms × 0.33 Hz). Disable via
        ``JARVIS_HOT_RELOAD_RESTART_MONITOR=false``.
        """
        if os.environ.get("JARVIS_HOT_RELOAD_RESTART_MONITOR", "true").lower() == "false":
            return
        try:
            while True:
                await asyncio.sleep(3.0)
                gls = self._governed_loop_service
                if gls is None:
                    continue
                orch = getattr(gls, "_orchestrator", None)
                if orch is None:
                    continue
                reloader = getattr(orch, "_hot_reloader", None)
                if reloader is None:
                    continue
                reason = getattr(reloader, "restart_pending", None)
                if reason:
                    self._stop_reason = f"restart_pending: {reason}"
                    logger.warning(
                        "[RestartMonitor] Hot-reload requires restart: %s; "
                        "triggering graceful shutdown for re-exec",
                        reason,
                    )
                    if self._shutdown_event is not None:
                        self._shutdown_event.set()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("[RestartMonitor] crashed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown_components(self) -> None:
        """Stop all components in reverse boot order.

        Each stop call is wrapped in try/except so that one failure does
        not prevent the remaining components from being cleaned up.
        """
        logger.info("Shutting down session %s ...", self._session_id)

        # 0. Activity monitor
        try:
            if hasattr(self, "_activity_monitor_task") and self._activity_monitor_task:
                self._activity_monitor_task.cancel()
                try:
                    await self._activity_monitor_task
                except asyncio.CancelledError:
                    pass
        except Exception:
            pass

        # 0b. REPL + keyboard handler + SerpentFlow
        try:
            if hasattr(self, "_serpent_repl") and self._serpent_repl is not None:
                await self._serpent_repl.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "_keyboard_handler") and self._keyboard_handler is not None:
                await self._keyboard_handler.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "_serpent_flow") and self._serpent_flow is not None:
                await self._serpent_flow.stop()
        except Exception:
            pass

        # 0c. Cost monitor
        try:
            if hasattr(self, "_cost_monitor_task") and self._cost_monitor_task:
                self._cost_monitor_task.cancel()
                try:
                    await self._cost_monitor_task
                except asyncio.CancelledError:
                    pass
        except Exception:
            pass

        # 0d. Restart-pending monitor
        try:
            if hasattr(self, "_restart_monitor_task") and self._restart_monitor_task:
                self._restart_monitor_task.cancel()
                try:
                    await self._restart_monitor_task
                except asyncio.CancelledError:
                    pass
        except Exception:
            pass

        # 0e. Wall-clock watchdog (Ticket A Guard 2) — may be None when
        # max_wall_seconds_s is disabled.
        try:
            if hasattr(self, "_wall_clock_monitor_task") and self._wall_clock_monitor_task:
                self._wall_clock_monitor_task.cancel()
                try:
                    await self._wall_clock_monitor_task
                except asyncio.CancelledError:
                    pass
        except Exception:
            pass

        # 1. Idle watchdog
        try:
            self._idle_watchdog.stop()
        except Exception as exc:
            logger.warning("IdleWatchdog stop failed: %s", exc)

        # 2. Intake
        if self._intake_service is not None:
            try:
                await self._intake_service.stop()
            except Exception as exc:
                logger.warning("IntakeLayerService stop failed: %s", exc)

        # 3. Predictive engine
        if self._predictive_engine is not None:
            try:
                self._predictive_engine.stop()
            except Exception as exc:
                logger.warning("PredictiveRegressionEngine stop failed: %s", exc)

        # 4. Governed loop service
        if self._governed_loop_service is not None:
            try:
                await self._governed_loop_service.stop()
            except Exception as exc:
                logger.warning("GovernedLoopService stop failed: %s", exc)

        # 5. Governance stack
        if self._governance_stack is not None:
            try:
                await self._governance_stack.stop()
            except Exception as exc:
                logger.warning("GovernanceStack stop failed: %s", exc)

        # 6. Oracle
        if self._oracle is not None:
            try:
                await self._oracle.shutdown()
            except Exception as exc:
                logger.warning("Oracle shutdown failed: %s", exc)
            self._oracle = None

        # Save rate limiter state
        if hasattr(self, '_rate_limiter') and self._rate_limiter is not None:
            try:
                self._rate_limiter.save()
            except Exception:
                pass

        # Save cost tracker state
        try:
            self._cost_tracker.save()
        except Exception as exc:
            logger.warning("CostTracker save failed: %s", exc)

        logger.info("Shutdown complete for session %s", self._session_id)

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    async def _generate_report(self) -> None:
        """Generate the session summary report and optional notebook."""
        duration_s = time.time() - self._started_at if self._started_at else 0.0

        # --- Convergence data ---
        convergence_state = "INSUFFICIENT_DATA"
        convergence_slope = 0.0
        convergence_r2 = 0.0

        try:
            from backend.core.ouroboros.governance.composite_score import ScoreHistory
            from backend.core.ouroboros.governance.convergence_tracker import ConvergenceTracker

            score_history = ScoreHistory(persistence_dir=self._session_dir)
            tracker = ConvergenceTracker()
            scores = score_history.get_composite_values()
            if scores:
                report = tracker.analyze(scores)
                convergence_state = report.state.value if hasattr(report.state, "value") else str(report.state)
                convergence_slope = report.slope
                convergence_r2 = report.r_squared_log
        except Exception as exc:
            logger.warning("Convergence analysis failed: %s", exc)

        # --- Branch stats ---
        branch_stats: dict = {
            "commits": 0,
            "files_changed": 0,
            "insertions": 0,
            "deletions": 0,
        }
        try:
            if self._branch_manager is not None:
                branch_stats = self._branch_manager.get_diff_stats()
        except Exception as exc:
            logger.warning("Branch stats retrieval failed: %s", exc)

        # --- Compute strategic drift (Increment 3) ---
        strategic_drift: Optional[dict] = None
        try:
            from backend.core.ouroboros.governance.strategic_direction import (
                GoalActivityLedger,
            )
            ledger = GoalActivityLedger(self._config.repo_path)
            drift_summary = ledger.compute_drift(session_id=self._session_id)
            strategic_drift = drift_summary.to_dict()
            logger.info(
                "[Harness] strategic_drift status=%s total=%d drifted=%d ratio=%s",
                drift_summary.status,
                drift_summary.total_ops,
                drift_summary.drifted_ops,
                f"{drift_summary.ratio:.3f}" if drift_summary.ratio is not None else "n/a",
            )
        except Exception as exc:
            logger.debug("[Harness] strategic_drift computation failed: %s", exc, exc_info=True)

        # --- Save session summary JSON ---
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            summary_path = self._session_recorder.save_summary(
                output_dir=self._session_dir,
                stop_reason=self._stop_reason,
                duration_s=duration_s,
                cost_total=self._cost_tracker.total_spent,
                cost_breakdown=self._cost_tracker.breakdown,
                branch_stats=branch_stats,
                convergence_state=convergence_state,
                convergence_slope=convergence_slope,
                convergence_r2=convergence_r2,
                strategic_drift=strategic_drift,
                # Ticket B (v1.1b): clean path stamps session_outcome="complete".
                # Signal-driven partial path stamps "incomplete_kill" via
                # _atexit_fallback_write(session_outcome=...). Together these
                # two values form the enum audit tooling consumes.
                session_outcome="complete",
                last_activity_ts=time.time(),
            )
            # Flag prevents the atexit fallback from double-writing a
            # partial summary on top of the clean one.
            self._summary_written = True
            logger.info("Summary written to %s", summary_path)
        except Exception as exc:
            logger.warning("Failed to save session summary: %s", exc)

        # --- Phase 4 P4 Slice 5 follow-up: MetricsSessionObserver ---
        # Wires the metrics observer at the harness session-end site
        # (deferred from Slice 5 graduation 2026-04-26). Reads the
        # ops list captured by the SessionRecorder + the cost-tracker
        # totals + branch_stats commits, asks the observer to compute
        # a MetricsSnapshot, and lets the observer:
        #   1. append the snapshot to the JSONL ledger (Slice 2),
        #   2. merge it into the per-session summary.json under a
        #      top-level "metrics" key (Slice 4),
        #   3. publish the EVENT_TYPE_METRICS_UPDATED SSE event for
        #      live IDE consumers (Slice 4).
        # All three steps are best-effort inside the observer; this
        # wiring is also try/except wrapped so any failure NEVER
        # blocks the rest of _generate_report (replay viewer + terminal
        # summary still run).
        # Master flag JARVIS_METRICS_SUITE_ENABLED is checked inside
        # the observer; when off, record_session_end short-circuits
        # with notes=("master_off",) and this wiring no-ops.
        try:
            from backend.core.ouroboros.governance.metrics_observability import (
                get_default_observer,
            )
            _metrics_observer = get_default_observer()
            _ops_for_metrics = getattr(
                self._session_recorder, "_operations", [],
            ) or []
            _metrics_observation = _metrics_observer.record_session_end(
                session_id=self._session_id,
                session_dir=self._session_dir,
                ops=_ops_for_metrics,
                sessions_history=(),
                posture_dwells=(),
                total_cost_usd=self._cost_tracker.total_spent,
                commits=branch_stats.get("commits", 0),
            )
            if _metrics_observation.snapshot is not None:
                logger.info(
                    "[Harness] MetricsObserver: snapshot recorded "
                    "(ledger=%s summary=%s sse=%s notes=%s)",
                    _metrics_observation.ledger_appended,
                    _metrics_observation.summary_merged,
                    _metrics_observation.sse_published,
                    _metrics_observation.notes or "()",
                )
            elif "master_off" in _metrics_observation.notes:
                logger.debug(
                    "[Harness] MetricsObserver: master_off — skipped",
                )
            else:
                logger.debug(
                    "[Harness] MetricsObserver: no snapshot "
                    "(notes=%s)",
                    _metrics_observation.notes,
                )
        except ImportError:
            logger.debug(
                "[Harness] MetricsObserver module unavailable — skipping",
            )
        except Exception as exc:
            logger.warning(
                "[Harness] MetricsObserver session-end wiring failed: %s",
                exc,
                exc_info=True,
            )

        # --- Session replay viewer (Priority 3 §8 observability) ---
        # Consolidate debug.log + summary.json + cost_tracker.json +
        # per-op ledger into one standalone replay.html. Written after
        # summary.json so the replay sees the final summary contents.
        # Env-gated + error-isolated so shutdown never depends on it.
        try:
            from backend.core.ouroboros.battle_test.session_replay import (
                SessionReplayBuilder,
                replay_enabled,
            )
            if replay_enabled():
                SessionReplayBuilder(self._session_dir).build()
        except Exception:
            logger.debug(
                "[Harness] session replay generation skipped",
                exc_info=True,
            )

        # --- Terminal summary ---
        try:
            terminal_summary = self._session_recorder.format_terminal_summary(
                stop_reason=self._stop_reason,
                duration_s=duration_s,
                cost_total=self._cost_tracker.total_spent,
                cost_breakdown=self._cost_tracker.breakdown,
                branch_name=self._branch_name or "N/A",
                branch_stats=branch_stats,
                convergence_state=convergence_state,
                convergence_slope=convergence_slope,
                convergence_r2=convergence_r2,
            )
            # Use Rich console for proper formatting / prompt_toolkit compat
            _c = self._serpent_flow.console if hasattr(self, "_serpent_flow") and self._serpent_flow else None
            if _c is not None:
                _c.print(terminal_summary, highlight=False)
            else:
                print(terminal_summary)
        except Exception as exc:
            logger.warning("Failed to format terminal summary: %s", exc)

        # --- Strategic drift line (Increment 3) ---
        if strategic_drift is not None:
            try:
                _status = strategic_drift.get("status", "unknown")
                _total = strategic_drift.get("total_ops", 0)
                _drifted = strategic_drift.get("drifted_ops", 0)
                _ratio = strategic_drift.get("ratio")
                _ratio_s = f"{_ratio:.2%}" if isinstance(_ratio, (int, float)) else "n/a"
                if _status == "drift_warning":
                    _line = (
                        f"[bold red]⚠ Strategic drift:[/bold red] "
                        f"{_drifted}/{_total} ops missed every active goal "
                        f"({_ratio_s}) — goals may be stale or prompts under-aligned."
                    )
                elif _status == "insufficient_data":
                    _min = int(os.environ.get("JARVIS_GOAL_DRIFT_MIN_OPS", "5"))
                    _line = (
                        f"[dim]Strategic drift: insufficient data "
                        f"({_total}/{_min} ops minimum)[/dim]"
                    )
                else:
                    _line = (
                        f"[green]Strategic drift: ok[/green] "
                        f"({_drifted}/{_total} missed, {_ratio_s})"
                    )
                _c = self._serpent_flow.console if hasattr(self, "_serpent_flow") and self._serpent_flow else None
                if _c is not None:
                    _c.print(_line, highlight=False)
                else:
                    print(_line)
            except Exception as exc:
                logger.debug("[Harness] drift line render failed: %s", exc)

        # --- Notebook ---
        try:
            from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator

            summary_json_path = self._session_dir / "summary.json"
            if summary_json_path.exists():
                nb_gen = NotebookGenerator(summary_path=summary_json_path)
                nb_path = nb_gen.generate(output_dir=self._notebook_output_dir)
                logger.info("Notebook generated at %s", nb_path)
        except Exception as exc:
            logger.warning("Notebook generation failed: %s", exc)

        # --- Clear session id (Increment 3) ---
        # Release the strategic_direction module global so that a subsequent
        # harness run (same process) starts with a clean slate. Post-report
        # intentionally — any stray ledger append during teardown still lands
        # in the correct session.
        try:
            from backend.core.ouroboros.governance.strategic_direction import (
                set_active_session_id,
            )
            set_active_session_id(None)
        except Exception:
            logger.debug("set_active_session_id(clear) failed", exc_info=True)

        # LastSessionSummary: symmetric teardown.
        try:
            from backend.core.ouroboros.governance.last_session_summary import (
                set_active_session_id as _lss_set_active,
            )
            _lss_set_active(None)
        except Exception:
            logger.debug("lss set_active_session_id(clear) failed", exc_info=True)

        # OpsDigestObserver: symmetric teardown — restore the default
        # no-op so a subsequent in-process harness run doesn't leak
        # digest events into a stale recorder.
        try:
            from backend.core.ouroboros.governance.ops_digest_observer import (
                reset_ops_digest_observer,
            )
            reset_ops_digest_observer()
        except Exception:
            logger.debug("reset_ops_digest_observer(clear) failed", exc_info=True)


# ---------------------------------------------------------------------------
# Defect #1 fix (2026-05-03) — WallClockWatchdog AST pin
# ---------------------------------------------------------------------------
#
# Substrate pin enforcing the periodic-loop + thread-safety-net pattern
# that replaced the original single-asyncio.sleep(cap_s) implementation.
# Without this pin, a future edit could silently regress to the
# starvation-vulnerable single-sleep pattern that fired 22 minutes
# late in soak v5 (bt-2026-05-03-060330).


def register_shipped_invariants() -> list:
    """WallClockWatchdog substrate invariants. Pins:

      * ``_monitor_wall_clock`` async method present.
      * ``_start_wall_clock_hard_deadline_thread`` method present.
      * ``_monitor_wall_clock`` body uses a periodic check loop
        (must contain ``while True`` AND must NOT contain a top-level
        ``asyncio.sleep(cap_s)`` call as the only sleep).
      * ``JARVIS_WALL_CLOCK_CHECK_INTERVAL_S`` env var referenced
        somewhere in the module (the periodic-check tick env knob).
      * ``JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S`` env var referenced
        (the thread-safety-net grace env knob).
      * ``WallClockHardDeadlineThread`` thread name referenced
        (proves the thread is actually spawned).
      * No exec/eval/compile.
    """
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "_monitor_wall_clock",
        "_start_wall_clock_hard_deadline_thread",
    )
    REQUIRED_LITERALS = (
        "JARVIS_WALL_CLOCK_CHECK_INTERVAL_S",
        "JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S",
        "WallClockHardDeadlineThread",
    )

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        wall_clock_node: Optional[_ast.AsyncFunctionDef] = None
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
                if node.name == "_monitor_wall_clock":
                    wall_clock_node = node
            elif isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"harness MUST NOT call {node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for lit in REQUIRED_LITERALS:
            if lit not in source:
                violations.append(
                    f"missing string literal {lit!r}"
                )
        # Periodic-loop check: _monitor_wall_clock body MUST contain
        # ``while True`` to enforce the periodic-check pattern; the
        # original starvation-vulnerable design had only the
        # ``await asyncio.sleep(cap_s)`` single-call.
        if wall_clock_node is not None:
            saw_while_true = False
            for sub in _ast.walk(wall_clock_node):
                if isinstance(sub, _ast.While):
                    if (
                        isinstance(sub.test, _ast.Constant)
                        and sub.test.value is True
                    ):
                        saw_while_true = True
                        break
            if not saw_while_true:
                violations.append(
                    "_monitor_wall_clock MUST contain a 'while True' "
                    "periodic-check loop (the Defect #1 fix); single-"
                    "sleep regressions caused 22-min fire delay in "
                    "soak v5"
                )
        return tuple(violations)

    target = "backend/core/ouroboros/battle_test/harness.py"
    return [
        ShippedCodeInvariant(
            invariant_name="wall_clock_watchdog_substrate",
            target_file=target,
            description=(
                "WallClockWatchdog: periodic-check asyncio loop + "
                "thread-based hard-deadline safety net + env-knob "
                "references; protects against single-sleep "
                "starvation regression (Defect #1, 2026-05-03)."
            ),
            validate=_validate,
        ),
    ]
