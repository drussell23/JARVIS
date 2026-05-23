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
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _boot_mark(name: str) -> None:
    """Defensive boot-timing mark. NEVER raises into boot path.

    Lazy imports the timer to keep this module importable when the
    timing module is unavailable. Marks are zero-duration timestamps
    that let us compute deltas between adjacent marks.
    """
    try:
        from backend.core.ouroboros.battle_test.boot_timing import (
            get_default_timer,
        )
        get_default_timer().mark(name)
    except Exception:  # noqa: BLE001 — defensive
        pass


class _BootPhase:
    """Context manager wrapping ``BootTimer.phase()`` with full
    defensive isolation. NEVER raises."""

    __slots__ = ("_name", "_timer")

    def __init__(self, name: str) -> None:
        self._name = name
        self._timer = None

    def __enter__(self) -> "_BootPhase":
        try:
            from backend.core.ouroboros.battle_test.boot_timing import (
                get_default_timer,
            )
            self._timer = get_default_timer()
            self._timer.begin(self._name)
        except Exception:  # noqa: BLE001
            self._timer = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            if self._timer is not None:
                self._timer.end(self._name)
        except Exception:  # noqa: BLE001
            pass
        return False  # don't swallow exceptions

    async def __aenter__(self) -> "_BootPhase":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        return self.__exit__(exc_type, exc_val, exc_tb)


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
    # Phase 9 Slice 2 — synthetic workload injection count.
    # Default 0 = zero behavior change for non-cadence runs (production
    # default). Only the cadence wrapper (and its cron entry) sets this
    # to N >= 1. Hard-capped at module level via
    # ``phase_9_synthetic_workload.seed_intents_max()`` (default 16,
    # clamped [1, 64]) so misconfiguration cannot spam ops.
    # Injection runs ONLY when headless is True (resolved); production
    # interactive sessions never inject synthetic load — the operator's
    # real workload IS the workload.
    seed_intents: int = 0

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
        # PRD §session-budget-preflight: register the CostTracker as
        # the authoritative session-budget provider for governance.
        # Adapter pattern — governance never imports battle_test; this
        # harness side calls the registration. The duck-typed protocol
        # (.remaining) means no type-import dependency either.
        # NEVER raises (helper is defensive).
        try:
            from backend.core.ouroboros.governance.session_budget_authority import (  # noqa: E501
                set_session_budget_provider,
            )
            set_session_budget_provider(self._cost_tracker)
        except Exception as _sba_reg_exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[Harness] session_budget_authority registration "
                "degraded: %s", _sba_reg_exc,
            )
        self._idle_watchdog = IdleWatchdog(timeout_s=config.idle_timeout_s)
        self._session_recorder = SessionRecorder(session_id=self._session_id)
        # Slice 12Q — register the active session's recorder so the
        # orchestrator's _record_ledger terminal hook can route
        # terminal operations into summary.json.operations[].
        # Cleared at shutdown by reset_active_recorder. NEVER raises.
        try:
            from backend.core.ouroboros.battle_test.session_recorder import (
                set_active_recorder as _slice12q_set_recorder,
            )
            _slice12q_set_recorder(self._session_recorder)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[Harness] Slice 12Q set_active_recorder raised — "
                "orchestrator terminal hook will no-op",
                exc_info=True,
            )
        # Session-liveness probes — zero-arg callables returning True
        # while background closed-loop work (e.g. autoscore
        # parallel_evaluate) is in flight. The ActivityMonitor pokes
        # the idle watchdog while any probe is hot, so fire-and-forget
        # work cannot be idle-reaped (v16 bt-2026-05-16-085224 fix).
        self._session_liveness_probes: List[Callable[[], bool]] = []

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
        # Task #94 (2026-05-14) — suspension diagnostic state.  Set by
        # the WallClockWatchdog when monotonic/wall ratio falls below
        # JARVIS_HARNESS_SUSPENSION_WARN_RATIO (default 0.5).  Read by
        # ``_generate_report`` + ``_atexit_fallback_write`` so the
        # additive ``suspension_likely`` + ``suspension_ratio`` fields
        # land in summary.json without changing stop_reason (per
        # operator binding 2026-05-14: avoid breaking summary.json
        # consumers — additive over modifying).
        self._suspension_likely: bool = False
        self._suspension_ratio: Optional[float] = None
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
        # Tracked handle for the deferred Oracle initialization task —
        # awaited (with timeout) during shutdown so a half-initialized
        # ChromaDB / cache deserialize doesn't leak across SIGTERM.
        # Single source of truth for the init coroutine; cancellation
        # safe (task is wrapped to swallow CancelledError on shutdown).
        self._oracle_init_task: Optional[asyncio.Task] = None
        self._governance_stack: Any = None
        self._governed_loop_service: Any = None
        self._predictive_engine: Any = None
        self._branch_manager: Any = None
        self._branch_name: Optional[str] = None
        self._intake_service: Any = None
        self._intake_paused: bool = False
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
                # Task #94 (2026-05-14) — carry the suspension
                # diagnostic into partial summaries too.  If the
                # WallClockWatchdog already detected suspension and
                # set these fields before atexit fires, the partial
                # summary still surfaces them to PRD/audit trails.
                suspension_likely=getattr(self, "_suspension_likely", False),
                suspension_ratio=getattr(self, "_suspension_ratio", None),
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

        # D1 silent boot — route INFO/DEBUG to session_dir/debug.log,
        # leave terminal clean for the banner. Must be the FIRST thing
        # in run() so all subsequent lazy imports' init logs land in
        # the file, not on the operator's screen. Master flag
        # JARVIS_SILENT_BOOT_ENABLED default true; hot-revert via
        # =false restores legacy behavior. NEVER raises (boot is not
        # blocked by logging glue).
        try:
            from backend.core.ouroboros.governance.silent_boot import (
                configure_silent_boot,
            )
            _silent_boot_handler = configure_silent_boot(
                session_dir=(
                    self._config.repo_path / ".ouroboros"
                    / "sessions" / self._session_id
                ),
            )
            if _silent_boot_handler is not None:
                # Retain reference so the harness can close it
                # explicitly on shutdown (Slice 7 follow-up #4 path).
                self._log_file_path = _silent_boot_handler.baseFilename
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[harness] silent_boot setup failed; falling back "
                "to legacy redirect path", exc_info=True,
            )

        # Asyncio loop-level exception handler — global safety net for
        # the entire session lifetime (2026-05-03 audit). Replaces both
        # asyncio's default handler (which formats with no message,
        # producing the "Unhandled exception in event loop" stderr noise
        # that bypasses patch_stdout) AND prompt_toolkit's handler
        # (which prints + blocks on "Press ENTER to continue...").
        #
        # Architectural intent: 182+ ensure_future/create_task spawn
        # sites exist across the repo. Per-callsite add_done_callback
        # consumers (Defect #4 pattern) remain best-practice but
        # cannot scale to every callsite without ongoing audit churn.
        # The loop handler is the structural safety net — every leaked
        # exception lands in debug.log with full classification, no
        # terminal pollution, no operator-visible chatter. Per-callsite
        # callbacks remain valuable as DEBUG-classified swallowers
        # (silent on expected patterns); this handler catches the rest.
        #
        # Reuses _EXPECTED_BACKGROUND_EXC_PATTERNS from candidate_generator
        # for classification — single source of truth.
        try:
            from backend.core.ouroboros.governance.candidate_generator import (
                _EXPECTED_BACKGROUND_EXC_PATTERNS,
            )
        except Exception:
            _EXPECTED_BACKGROUND_EXC_PATTERNS = ()
        _async_leak_logger = logging.getLogger("asyncio.leak")
        _running_loop = asyncio.get_event_loop()

        def _harness_loop_exception_handler(loop_, ctx_):
            msg = ctx_.get("message", "Unhandled exception in event loop")
            exc = ctx_.get("exception")
            extras = " | ".join(
                f"{k}={ctx_[k]!r}"
                for k in sorted(ctx_)
                if k not in ("message", "exception")
            )
            full = f"[asyncio leak] {msg}" + (f" | {extras}" if extras else "")
            if exc is None:
                _async_leak_logger.warning(full)
                return
            if isinstance(exc, asyncio.CancelledError):
                _async_leak_logger.debug(full, exc_info=exc)
                return
            err_str = str(exc)
            if any(p in err_str for p in _EXPECTED_BACKGROUND_EXC_PATTERNS):
                _async_leak_logger.debug(full, exc_info=exc)
                return
            _async_leak_logger.warning(full, exc_info=exc)

        try:
            _running_loop.set_exception_handler(_harness_loop_exception_handler)
        except Exception:
            logger.debug("Failed to install harness asyncio exception handler", exc_info=True)

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

        # ProcessMemoryWatchdog (2026-05-18) — sibling of the wall-clock
        # watchdog, fired when this process TREE's RSS crosses an
        # (adaptive, env-overridable) cap. The MemoryPressureGate is
        # blind here by construction: it probes *system-wide free %*
        # (host stayed 71% free while one process tree hit 52GB) and
        # can only clamp NEW L3 fan-out, never a running in-process
        # leaker (Oracle cold-reindex accretion). This event joins the
        # same FIRST_COMPLETED race and routes stop_reason=
        # "process_memory_cap" — a graceful, summary-producing stop
        # BEFORE the OS OOM-kills the tree (which leaves no artifacts).
        self._process_memory_event: asyncio.Event = asyncio.Event()

        # Slice 12D (Graceful Shutdown on Global Breaker Trip) —
        # joins the FIRST_COMPLETED race. Fires when the global
        # provider circuit breaker transitions CLOSED → OPEN_TERMINAL
        # (five structural trips within window — see Slice 7c +
        # circuit_breaker._GlobalBreaker). When this event wins the
        # race the session terminates with
        # ``stop_reason="session_exhausted"`` — the harness drains
        # cleanly, flushes telemetry, writes summary.json with
        # ``session_outcome=complete`` BEFORE the wall-cap timer
        # would fire. Replaces the pre-Slice-12D behaviour where
        # post-global-trip sessions sat idle until Layer-3 SIGKILL.
        self._session_exhausted_event: asyncio.Event = asyncio.Event()
        self._session_exhausted_payload: Optional[Any] = None
        # Register the in-process callback against the global
        # breaker singleton. Lazy import keeps the harness module
        # free of a hard dependency on circuit_breaker (mirrors
        # the publish_invariant_drift_detected lazy-import shape).
        # The callback marshals via call_soon_threadsafe because
        # ``_GlobalBreaker.report_structural_trip`` may fire from
        # a Slice 7e GENERATE worker — possibly off the main
        # asyncio thread under stress — and ``asyncio.Event.set``
        # is documented as not thread-safe.
        try:
            from backend.core.ouroboros.governance.circuit_breaker import (  # noqa: E501
                get_global_breaker as _slice12d_get_global_breaker,
            )

            def _slice12d_on_global_trip(_payload: Any) -> None:
                """Bridge from the global breaker's on_trip
                callback (synchronous, possibly off-loop) into
                the harness's shutdown event (asyncio, must be
                set from the loop thread)."""
                self._session_exhausted_payload = _payload
                try:
                    _running_loop.call_soon_threadsafe(
                        self._session_exhausted_event.set,
                    )
                except Exception:  # noqa: BLE001 — defensive
                    # Loop may already be closing; nothing we
                    # can do besides record the payload.
                    logger.debug(
                        "[Slice12D] session_exhausted callback "
                        "could not marshal to loop (likely "
                        "shutdown race)",
                        exc_info=True,
                    )

            _slice12d_get_global_breaker().on_trip(
                _slice12d_on_global_trip,
            )
            logger.info(
                "[Slice12D] session_exhausted shutdown waiter "
                "registered against global circuit breaker",
            )
        except Exception:  # noqa: BLE001 — defensive
            # Defensive: if circuit_breaker is somehow unavailable
            # the harness keeps the 5-way race (legacy behaviour).
            logger.debug(
                "[Slice12D] global breaker on_trip registration "
                "failed (legacy 5-way race remains)",
                exc_info=True,
            )
        self._process_memory_hard_deadline_stop: Optional[
            threading.Event
        ] = None
        self._process_memory_monitor_task: Optional[asyncio.Future] = None

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

        _boot_mark("harness_run_pre_boot_done")
        try:
            # Boot sequence — each phase wrapped for boot-timing visibility
            # GitIndexGuard (Phase C Slice 2) MUST be the first phase:
            # a missing .git/index (the background-Cursor-Agent unlink
            # failure mode) corrupts every git-touching subsystem
            # downstream. §2 Progressive Awakening — mirrors
            # WorktreeManager.reap_orphans boot recovery. Master-
            # gated inside the guard (default-OFF → DISABLED no-op);
            # NEVER raises into boot.
            with _BootPhase("boot_git_index_guard"):
                await self._boot_git_index_guard()
            with _BootPhase("boot_oracle"):
                await self.boot_oracle()
            with _BootPhase("boot_governance_stack"):
                await self.boot_governance_stack()
            with _BootPhase("boot_governed_loop_service"):
                await self.boot_governed_loop_service()
            # Provider-readiness gate (§33.1, default-FALSE). Fail-fast
            # *before* any op-emitting subsystem boots when Claude/DW
            # are unhealthy — closes the v18 27-min thrash failure mode
            # (8 EXHAUSTIONs before idle-timeout). Composes canonical
            # ClaudeProvider.health_probe + claude_circuit_breaker +
            # optional DW probe. NEVER raises.
            with _BootPhase("boot_provider_readiness_gate"):
                if await self._gate_provider_readiness_or_refuse():
                    # Gate refused — finally-block runs shutdown +
                    # report; stop_reason already stamped.
                    return
            # P1 Slice 2 — Ledger Sovereignty workspace. Under
            # master flag, creates an isolated worktree at
            # ouroboros/auto/<session> via the canonical
            # WorktreeManager. The AutoCommitter resolves its
            # cwd from JARVIS_AUTO_COMMIT_WORKSPACE at commit
            # time and refuses (typed) if the path isn't an
            # owned work-area. Master-FALSE path is byte-
            # identical; this phase no-ops cleanly.
            with _BootPhase("boot_ledger_sovereignty_workspace"):
                await self._boot_ledger_sovereignty_workspace()
            with _BootPhase("boot_jarvis_tiers"):
                await self.boot_jarvis_tiers()
            with _BootPhase("create_branch"):
                self._branch_name = await self.create_branch()
            with _BootPhase("boot_intake"):
                await self.boot_intake()
            # Phase 9 Slice 2 — synthetic workload injection.
            # Composes the canonical UnifiedIntakeRouter pipeline via
            # IntakeLayerService.ingest_envelope. Headless-only +
            # config-gated + hard-capped + transparency-tagged. Per
            # operator binding 2026-05-05: single pipeline, honest
            # source token, defaults safe, no dilution of P9.2
            # graduation contract.
            await self._inject_phase_9_synthetic_workload()
            _boot_mark("harness_boot_sequence_done")

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
                # Task #21 — Dynamic Timeout Coherence seam. Publish
                # the absolute monotonic wall deadline so the
                # governance layer (swe_bench_pro.evaluator) can
                # structurally clamp its inner eval timeout BELOW the
                # outer bounded-shutdown WITHOUT importing battle_test
                # (env-var seam; mirrors the strategic_direction
                # session-id module-global precedent). Composes the
                # already-computed _wall_cap; additive; only set when a
                # wall cap is armed — absent env ⇒ evaluator no-ops
                # (byte-identical legacy for non-battle-test callers).
                os.environ["OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC"] = (
                    repr(time.monotonic() + float(_wall_cap))
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

            # ProcessMemoryWatchdog — adaptive RSS ceiling on the soak
            # process TREE. Protective by default (the 52GB OOM had NO
            # guard); disable with JARVIS_PROCESS_MEMORY_WATCHDOG_ENABLED
            # =false. Cap is env-absolute (JARVIS_PROCESS_MEMORY_CAP_MB)
            # or adaptively derived as a fraction of total system RAM —
            # never a hardcoded byte count, so it travels across hosts.
            _pm_warn_mb, _pm_cap_mb, _pm_interval_s = (
                self._resolve_process_memory_thresholds()
            )
            if _pm_cap_mb is not None and _pm_cap_mb > 0:
                self._process_memory_monitor_task = asyncio.ensure_future(
                    self._monitor_process_memory(
                        _pm_warn_mb, _pm_cap_mb, _pm_interval_s,
                    )
                )
                # Thread backstop: Oracle cold-indexing is the documented
                # dominant event-loop suffocator, so asyncio starvation
                # is a KNOWN risk here — the dual-path discipline from
                # WallClockWatchdog applies. The daemon thread re-probes
                # RSS independently and fires the same event even if the
                # loop is fully wedged by a leaking in-process op.
                self._start_process_memory_hard_deadline_thread(
                    _pm_warn_mb, _pm_cap_mb, _pm_interval_s,
                )
                logger.info(
                    "[ProcessMemoryWatchdog] armed: warn=%.0fMB cap=%.0fMB "
                    "interval=%.0fs — graceful stop_reason="
                    "process_memory_cap before OS OOM-kill.",
                    _pm_warn_mb, _pm_cap_mb, _pm_interval_s,
                )

            # Start hot-reload restart-pending monitor — graceful respawn on
            # quarantined self-modifications. See _monitor_restart_pending.
            self._restart_monitor_task = asyncio.ensure_future(
                self._monitor_restart_pending()
            )

            # Defect #2 fix (2026-05-03) — Production Oracle observer
            # boot wire-up. The substrate's ``run_periodic`` was never
            # scheduled by any caller, so the observer's history ring
            # buffer stayed empty across all soaks (verified in
            # bt-2026-05-03-060330: production_oracle_observer_tick=0).
            # Without this boot wire-up, the GET /observability/
            # production-oracle endpoint returns empty AND the
            # auto_action_router's oracle veto rule (Rule 1.5) reads
            # current()=None and falls through to existing rules. The
            # whole Tier 2 #6 substrate is empirically dead until this
            # task starts. Master flag JARVIS_PRODUCTION_ORACLE_ENABLED
            # gates the entire boot path; cancelled on shutdown like
            # the other monitor tasks.
            self._production_oracle_monitor_task: Optional[
                asyncio.Future
            ] = None
            try:
                from backend.core.ouroboros.governance.production_oracle_observer import (  # noqa: E501
                    get_default_observer as _po_get_observer,
                    production_oracle_enabled as _po_enabled,
                )
                if _po_enabled():
                    # HarnessConfig exposes the repo root as `repo_path`
                    # (not `project_root`); every other caller in this
                    # file uses `self._config.repo_path`. The observer's
                    # kwarg is named `project_root` — keep that, pull
                    # from the actual source attribute.
                    _observer = _po_get_observer(
                        project_root=self._config.repo_path,
                    )

                    def _posture_provider() -> str:
                        """Adaptive cadence input: read current posture
                        from the posture observer's persistent store.
                        Returns ``"EXPLORE"`` (most conservative
                        cadence) on any failure -- defensive default
                        keeps the observer ticking under degraded
                        posture-store conditions."""
                        try:
                            from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
                                get_default_store as _ps_get_store,
                            )
                            store = _ps_get_store()
                            reading = store.load_current()
                            if reading is None:
                                return "EXPLORE"
                            return str(reading.posture.value)
                        except Exception:  # noqa: BLE001
                            return "EXPLORE"

                    self._production_oracle_monitor_task = (
                        asyncio.ensure_future(
                            _observer.run_periodic(
                                posture_provider=_posture_provider,
                            )
                        )
                    )
                    logger.info(
                        "[ProductionOracleObserver] boot wire-up "
                        "complete — adapters=%d (Defect #2 fix "
                        "2026-05-03)",
                        _observer.adapter_count,
                    )
            except Exception as _po_exc:  # noqa: BLE001 -- defensive
                logger.debug(
                    "[ProductionOracleObserver] boot wire-up degraded: "
                    "%s", _po_exc, exc_info=True,
                )

            # ──────────────────────────────────────────────────────────
            # Slice 5 — EvaluatorTraceObserver ignition.
            # ──────────────────────────────────────────────────────────
            # Wires the structural-probe observer (Slices 1-4 — PR #48711)
            # into the harness boot path. Composes existing primitives
            # only:
            #   * StreamEventBroker (Gap #6 Slice 2) — observer publishes
            #     ``evaluator_trace_frame`` events via the canonical
            #     ``get_default_broker().publish`` surface (NOT a parallel
            #     bus).
            #   * PostureObserver (Wave 1 #1) — posture-aware cadence via
            #     the same store-load pattern the ProductionOracleObserver
            #     above uses (fail-soft default ``"EXPLORE"``).
            #
            # Master flag ``JARVIS_EVALUATOR_TRACE_ENABLED`` default-FALSE
            # per §33.1 (byte-equivalent boot when the flag is off:
            # ``evaluator_trace_enabled()`` short-circuits + nothing
            # else runs). Started here AFTER the broker singleton is
            # known to be lazily-available + AFTER the production-oracle
            # observer is wired (preserving boot ordering invariants).
            #
            # Fail-soft contract: every call is wrapped — a degraded
            # observer must NEVER block the main loop's boot. Failures
            # downgrade to DEBUG, the attribute stays ``None``, and
            # ``_shutdown_components`` step 0d3 gracefully skips when
            # the attribute is missing.
            self._evaluator_trace_observer = None
            try:
                from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
                    EvaluatorTraceObserver,
                    evaluator_trace_enabled,
                )
                if evaluator_trace_enabled():
                    # Canonical broker singleton (Gap #6 Slice 2).
                    # NEVER raises; lazily-constructed on first call.
                    _eto_broker = None
                    try:
                        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                            get_default_broker as _eto_get_broker,
                        )
                        _eto_broker = _eto_get_broker()
                    except Exception as _eto_b_exc:  # noqa: BLE001
                        logger.debug(
                            "[EvaluatorTraceObserver] broker lookup "
                            "degraded: %s — observer will run without "
                            "SSE publish (JSONL persistence still active)",
                            _eto_b_exc,
                        )

                    def _eto_posture_provider() -> "Optional[str]":
                        """Fail-soft posture reader. Mirrors the
                        ProductionOracleObserver pattern above. Returns
                        ``None`` (= base interval) on any failure —
                        defensive default keeps the observer ticking
                        under degraded posture-store conditions."""
                        try:
                            from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
                                get_default_store as _ps_get_store,
                            )
                            store = _ps_get_store()
                            reading = store.load_current()
                            if reading is None:
                                return None
                            return str(reading.posture.value)
                        except Exception:  # noqa: BLE001
                            return None

                    self._evaluator_trace_observer = EvaluatorTraceObserver(
                        session_id=self._session_id,
                        broker_publish=(
                            _eto_broker.publish if _eto_broker is not None
                            else None
                        ),
                        posture_provider=_eto_posture_provider,
                    )
                    # start() is sync; returns True on spawn, False on
                    # master-flag-off OR no running loop. Logs INFO line
                    # internally on success.
                    _eto_started = self._evaluator_trace_observer.start()
                    if not _eto_started:
                        # Couldn't spawn (most likely no running loop);
                        # clear the attribute so shutdown step 0d3
                        # skips it.
                        self._evaluator_trace_observer = None
                else:
                    logger.debug(
                        "[EvaluatorTraceObserver] master flag "
                        "JARVIS_EVALUATOR_TRACE_ENABLED is FALSE — "
                        "observer not started (default behavior)"
                    )
            except Exception as _eto_exc:  # noqa: BLE001
                logger.debug(
                    "[EvaluatorTraceObserver] boot wire-up degraded: "
                    "%s — observer not started; investigation will "
                    "need a separate diagnostic path",
                    _eto_exc, exc_info=True,
                )
                self._evaluator_trace_observer = None

            # ──────────────────────────────────────────────────────────
            # Slice 11A — ControlPlaneWatchdog ignition.
            # ──────────────────────────────────────────────────────────
            # Independent watchdog task that detects event-loop lag
            # (the empirical bt-2026-05-22-011927 pathology: main thread
            # blocked in builtin_compile → gc_collect_main for many
            # seconds at a time, asyncio loop unable to tick).
            #
            # The watchdog sleeps for ``interval_s`` and measures the
            # actual wall-clock vs requested sleep delta. When the lag
            # exceeds the threshold (env-knobbed, default 500ms), logs
            # ``[ControlPlaneStarvation]`` at WARNING with the exact
            # observed lag — operators see the wedge in real time.
            #
            # Master flag JARVIS_CONTROL_PLANE_WATCHDOG_ENABLED default
            # TRUE per Phase 11A (pure telemetry, no behavior change).
            # Explicit "false" opts out.
            #
            # Fail-soft contract per Slice 5 precedent.
            self._control_plane_watchdog = None
            try:
                from backend.core.ouroboros.governance.control_plane_watchdog import (  # noqa: E501
                    get_default_watchdog as _cpw_get_default,
                    watchdog_enabled as _cpw_enabled,
                )
                if _cpw_enabled():
                    _cpw = _cpw_get_default()
                    _cpw_started = _cpw.start()
                    if _cpw_started:
                        self._control_plane_watchdog = _cpw
                else:
                    logger.debug(
                        "[ControlPlaneWatchdog] master flag FALSE — "
                        "watchdog not started"
                    )
            except Exception as _cpw_exc:  # noqa: BLE001
                logger.debug(
                    "[ControlPlaneWatchdog] boot wire-up degraded: "
                    "%s — watchdog not started",
                    _cpw_exc, exc_info=True,
                )
                self._control_plane_watchdog = None

            # Slice 12G-2 — LoopDeadman wire-up.
            #
            # The Slice 11A ControlPlaneWatchdog above surfaces bursty
            # asyncio starvation (single events of 100ms+ lag). Empirical
            # evidence from bt-2026-05-22-195721 proved a stronger class
            # of wedge: when the loop is TOTALLY dead in sync work, even
            # the watchdog's own asyncio.sleep cannot fire — it goes
            # silent and operators see zero symptoms until the wall-cap
            # Layer-3 SIGKILL fires 82 minutes later.
            #
            # LoopDeadman runs in a daemon OS thread independent of the
            # asyncio loop. The asyncio loop pings ``heartbeat()`` every
            # ~5s; the deadman thread polls the timestamp. If the loop
            # hasn't ticked for ``deadman_timeout_s`` (default 300s),
            # the deadman fires faulthandler stack dump + os._exit(75).
            # That trades a 82-min silent kill for a 5-min loud
            # structured exit with forensic trace.
            #
            # Fail-soft contract — boot failure of LoopDeadman MUST NOT
            # block the harness; the legacy wall-cap Layer-3 still
            # provides a coarser backstop.
            self._loop_deadman = None
            try:
                from backend.core.ouroboros.governance.loop_deadman import (  # noqa: E501
                    get_default_deadman as _deadman_get_default,
                    deadman_enabled as _deadman_enabled,
                )
                # ── Slice 12T Part 1 — wire the tombstone dir ──
                # Set JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR to the
                # active session dir so the deadman's wedge fire
                # path writes
                # ``<session_dir>/loop_deadman_tombstone.txt``
                # alongside debug.log. setdefault preserves any
                # explicit operator override.
                try:
                    os.environ.setdefault(
                        "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR",
                        str(self._session_dir),
                    )
                    # ── Slice 12V Phase 1 — ShutdownWatchdog
                    # tombstone dir wiring (mirrors Slice 12T) ──
                    os.environ.setdefault(
                        "JARVIS_SHUTDOWN_TOMBSTONE_DIR",
                        str(self._session_dir),
                    )
                except Exception:  # noqa: BLE001
                    pass
                if _deadman_enabled():
                    _deadman = _deadman_get_default()
                    _deadman_started = _deadman.start()
                    if _deadman_started:
                        self._loop_deadman = _deadman
                else:
                    logger.debug(
                        "[LoopDeadman] master flag FALSE — "
                        "deadman not started"
                    )
            except Exception as _deadman_exc:  # noqa: BLE001
                logger.debug(
                    "[LoopDeadman] boot wire-up degraded: %s — "
                    "deadman not started",
                    _deadman_exc, exc_info=True,
                )
                self._loop_deadman = None

            # ── Slice 12V Phase 2 — Sidecar Profiler wire-up ──
            #
            # The ControlPlaneStarvation snapshot path runs on the
            # asyncio loop itself; when MainThread is wedged, that
            # path is suspended and the snapshot only fires post-
            # recovery (capturing the watchdog observing itself).
            # The Sidecar Profiler runs in a dedicated daemon
            # thread, polling sys._current_frames() out-of-band so
            # it captures the IN-PROGRESS MainThread frame WHILE
            # the wedge is active. Same fail-soft contract as
            # LoopDeadman: boot failure NEVER blocks the harness.
            self._sidecar_profiler = None
            try:
                from backend.core.ouroboros.governance.sidecar_profiler import (  # noqa: E501
                    get_default_sidecar as _sidecar_get_default,
                    sidecar_enabled as _sidecar_enabled,
                )
                if _sidecar_enabled():
                    _sidecar = _sidecar_get_default()
                    _sidecar_started = _sidecar.start()
                    if _sidecar_started:
                        self._sidecar_profiler = _sidecar
                else:
                    logger.debug(
                        "[SidecarProfiler] master flag FALSE — "
                        "profiler not started"
                    )
            except Exception as _sidecar_exc:  # noqa: BLE001
                logger.debug(
                    "[SidecarProfiler] boot wire-up degraded: %s "
                    "— profiler not started",
                    _sidecar_exc, exc_info=True,
                )
                self._sidecar_profiler = None

            # Slice 12G-3 — Continuous WAL / atomic summary.json
            # checkpointing.
            #
            # The clean shutdown path writes summary.json once at the
            # end via session_recorder.save_summary. When the loop
            # wedges and Layer-3 SIGKILL fires (or LoopDeadman
            # os._exit(75) trips), that final write never lands.
            # Empirical evidence: bt-2026-05-22-195721 lost its
            # verdict artifact after an 82-min wedge.
            #
            # Per operator binding ("no panic-saves, build robust
            # state persistence"), the WAL writes continuously
            # during normal operation — every ~15s a snapshot of
            # the current session state is atomically written
            # (temp + os.replace) to summary.json. When SIGKILL
            # drops, the latest checkpoint is already at rest.
            #
            # Fail-soft contract — WAL boot failure MUST NOT block
            # the harness; clean shutdown's save_summary remains
            # the canonical final write.
            self._session_wal = None
            self._session_wal_task = None
            try:
                from backend.core.ouroboros.governance.session_wal import (  # noqa: E501
                    install_default_wal as _wal_install,
                    wal_enabled as _wal_enabled,
                )
                if _wal_enabled():
                    self._session_wal = _wal_install(self._session_dir)
                    # Start the periodic checkpoint task. Cadence
                    # is operator-tunable but defaults to 15s — a
                    # balance between freshness (last checkpoint
                    # is at most 15s stale on hard kill) and I/O
                    # cost (<1KB/write, 4 writes/min).
                    self._session_wal_task = asyncio.create_task(
                        self._slice12g3_periodic_checkpoint_loop(),
                        name="session_wal_periodic_checkpoint",
                    )
                    logger.info(
                        "[SessionWAL] armed: summary.json checkpoint "
                        "every %.1fs (atomic temp+rename; survives "
                        "Layer-3 SIGKILL / LoopDeadman os._exit)",
                        float(os.environ.get(
                            "JARVIS_SESSION_WAL_PERIODIC_S", "15.0",
                        )),
                    )
                else:
                    logger.debug(
                        "[SessionWAL] master flag FALSE — "
                        "continuous WAL not started"
                    )
            except Exception as _wal_exc:  # noqa: BLE001
                logger.debug(
                    "[SessionWAL] boot wire-up degraded: %s — "
                    "WAL not started",
                    _wal_exc, exc_info=True,
                )
                self._session_wal = None
                self._session_wal_task = None

            # Register signal handlers
            try:
                loop = asyncio.get_running_loop()
                self.register_signal_handlers(loop)
            except Exception:  # noqa: BLE001
                pass

            _boot_mark("harness_main_loop_entered")
            # Wait for first stop signal
            shutdown_waiter = asyncio.ensure_future(self._shutdown_event.wait())
            budget_waiter = asyncio.ensure_future(self._cost_tracker.budget_event.wait())
            idle_waiter = asyncio.ensure_future(self._idle_watchdog.idle_event.wait())
            # Ticket A Guard 2: wall-clock waiter joins the 4-way race. When
            # max_wall_seconds_s is None/disabled the event is never set, so
            # the waiter blocks forever and has no effect on the legacy
            # 3-way race — backwards-compatible.
            wall_clock_waiter = asyncio.ensure_future(self._wall_clock_event.wait())
            # ProcessMemoryWatchdog joins the race. When the watchdog is
            # disabled the event is never set, so the waiter blocks
            # forever and has zero effect — backwards-compatible, same
            # contract as wall_clock_waiter.
            process_memory_waiter = asyncio.ensure_future(
                self._process_memory_event.wait()
            )
            # Slice 12D — global circuit breaker shutdown waiter
            # joins the race. Fires when the global breaker
            # transitions CLOSED → OPEN_TERMINAL (5 structural
            # trips within window). Same shape as the other
            # waiters: when the global breaker never trips the
            # event is never set, so this waiter blocks forever
            # with zero effect — backwards-compatible.
            session_exhausted_waiter = asyncio.ensure_future(
                self._session_exhausted_event.wait()
            )

            done, pending = await asyncio.wait(
                [
                    shutdown_waiter, budget_waiter, idle_waiter,
                    wall_clock_waiter, process_memory_waiter,
                    session_exhausted_waiter,
                ],
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
            elif process_memory_waiter in done:
                self._stop_reason = "process_memory_cap"
                logger.warning(
                    "Session %s stopping: process_memory_cap — process "
                    "tree RSS exceeded the configured cap. Graceful "
                    "summary-producing stop ahead of an OS OOM-kill "
                    "(ProcessMemoryWatchdog).",
                    self._session_id,
                )
            elif session_exhausted_waiter in done:
                # Slice 12D — global circuit breaker fired. Graceful
                # summary-producing stop BEFORE the wall-cap timer
                # would fire. The cascade is structural: per-op trips
                # (Slice 7c terminal_structural) → 5 within window →
                # _GlobalBreaker → on_trip callback → asyncio.Event →
                # this waiter wins the race.
                self._stop_reason = "session_exhausted"
                payload = self._session_exhausted_payload
                trip_count = getattr(payload, "trip_count", "?")
                window_s = getattr(payload, "window_s", 0.0) or 0.0
                threshold = getattr(payload, "threshold", "?")
                logger.warning(
                    "Session %s stopping: session_exhausted — global "
                    "circuit breaker tripped (trips=%s threshold=%s "
                    "window=%.0fs). Slice 12D graceful drain ahead of "
                    "wall-cap.",
                    self._session_id,
                    trip_count, threshold, float(window_s),
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
        """Import + construct TheOracle, defer ``initialize()`` to a
        background task by default.

        **Why deferred** — ``TheOracle.initialize()`` does graph cache
        load → optional full index → ChromaDB semantic index init in
        strictly-sequential order (a libmalloc-safety constraint on
        macOS ARM64). On real installs this takes 8-9s and was
        previously blocking the entire harness boot path. Per
        Manifesto §2 (Progressive Awakening), a background service's
        full warm-up MUST NOT block the operator-facing REPL.

        **What we preserve** — the libmalloc-safe ordering inside
        ``initialize()`` is unchanged; only the *await site* moves
        from the boot path to a tracked background task. The Oracle
        object exists immediately; consumers gate on its composed
        :class:`OracleReadiness` primitive (``oracle.wait_until_ready``)
        instead of polling ``_running`` or assuming a warm graph.

        **Env-flag opt-out** — ``JARVIS_ORACLE_BLOCK_BOOT=true``
        reverts to the legacy synchronous-await path. Use for
        deterministic CI / perf-baseline harness runs where boot
        ordering must be reproducible and a warm Oracle is
        prerequisite to op #1.
        """
        try:
            from backend.core.ouroboros.oracle import TheOracle

            # Construct synchronously — the constructor only touches
            # filesystem (cache dir mkdir) and Python objects; cheap.
            # The Oracle reference is now valid for downstream wiring
            # (GovernanceStack, GLS) even before init completes.
            self._oracle = TheOracle()

            block_on_boot = (
                os.environ.get("JARVIS_ORACLE_BLOCK_BOOT", "")
                .strip().lower() in _TRUTHY
            )

            if block_on_boot:
                # Legacy synchronous-await path. Single env flag, no
                # secret modes — operators who need deterministic boot
                # ordering opt in explicitly.
                logger.info(
                    "Oracle init: synchronous (JARVIS_ORACLE_BLOCK_BOOT=true)",
                )
                await self._oracle.initialize()
                logger.info("Oracle booted (synchronous)")
                return

            # Deferred path — spawn initialize() as a tracked task and
            # return immediately so the harness boot continues. The
            # Oracle object is wired into GLS / governance stack at
            # the normal sites; consumers awaiting graph or semantic
            # readiness unblock the moment each phase finishes.
            logger.info(
                "Oracle init: deferred to background task "
                "(JARVIS_ORACLE_BLOCK_BOOT=false; default)",
            )

            async def _deferred_init() -> None:
                try:
                    await self._oracle.initialize()
                    logger.info("Oracle booted (deferred)")
                except asyncio.CancelledError:
                    # Shutdown cancellation — propagate cleanly.
                    raise
                except Exception as inner_exc:  # noqa: BLE001
                    # Failure already recorded via OracleReadiness
                    # (initialize()'s except branch); waiters will
                    # surface OracleInitFailed instead of hanging.
                    logger.warning(
                        "Oracle deferred init failed: %s", inner_exc,
                    )

            self._oracle_init_task = asyncio.ensure_future(_deferred_init())
            # Defensive: install a done_callback that swallows
            # CancelledError so the asyncio loop-level handler
            # doesn't classify shutdown cancellation as a leak.
            def _swallow(task: asyncio.Task) -> None:
                try:
                    if task.cancelled():
                        return
                    task.exception()  # consume so it isn't logged as unhandled
                except Exception:  # noqa: BLE001
                    pass
            self._oracle_init_task.add_done_callback(_swallow)
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

                # CC1 — operator-selectable per-op rendering. Default
                # CLAUDE: terse one-line-per-op idiom matching Claude
                # Code's tool-call visual model. Hot-revert via
                # JARVIS_RENDER_MODE=SERPENT restores the legacy
                # multi-line per-op blocks.
                _render_mode_label = "serpent"
                _chosen_transport: Any = None
                try:
                    from backend.core.ouroboros.governance.claude_style_transport import (  # noqa: E501
                        ClaudeStyleTransport,
                        RenderMode,
                        resolve_render_mode,
                    )
                    if resolve_render_mode() is RenderMode.CLAUDE:
                        _chosen_transport = ClaudeStyleTransport(
                            console=self._serpent_flow.console,
                        )
                        _render_mode_label = "claude"
                except Exception as _crm_exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[harness] claude_style_transport selection "
                        "failed: %s — falling back to SerpentTransport",
                        _crm_exc,
                    )
                if _chosen_transport is None:
                    _chosen_transport = SerpentTransport(flow=self._serpent_flow)
                    _render_mode_label = "serpent"

                if hasattr(self._governance_stack, "comm") and self._governance_stack.comm is not None:
                    self._governance_stack.comm._transports.append(
                        _chosen_transport,
                    )
                    logger.info(
                        "Per-op transport wired (mode=%s)",
                        _render_mode_label,
                    )
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
                    _root = logging.getLogger()
                    # Idempotency gate: configure_silent_boot() runs first
                    # in run() and installs a *marked* session FileHandler
                    # on the root logger.  Adding a second FileHandler to
                    # the SAME debug.log here makes every record emit
                    # twice (the observed 2x duplication).  This legacy
                    # redirect is now a genuine fallback: it runs ONLY
                    # when silent_boot did not install its handler (e.g.
                    # it raised), detected via silent_boot's own marker —
                    # the single source of truth, not a hardcoded string.
                    from backend.core.ouroboros.governance.silent_boot import (
                        _HANDLER_MARKER as _SB_MARKER,
                    )
                    _sb_installed = any(
                        getattr(_h, _SB_MARKER, False)
                        and isinstance(_h, logging.FileHandler)
                        for _h in _root.handlers
                    )
                    if _sb_installed:
                        logger.debug(
                            "[harness] silent_boot session log already "
                            "active; skipping legacy FileHandler to "
                            "prevent 2x log emission",
                        )
                    else:
                        _log_dir = self._config.repo_path / ".ouroboros" / "sessions" / self._session_id
                        _log_dir.mkdir(parents=True, exist_ok=True)
                        _log_file = _log_dir / "debug.log"
                        _file_handler = logging.FileHandler(str(_log_file), encoding="utf-8")
                        _file_handler.setLevel(logging.DEBUG)
                        _file_handler.setFormatter(logging.Formatter(
                            "%(asctime)s [%(name)s] %(levelname)s %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S",
                        ))
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

            # RenderConductor Slice 2 — wire whichever renderers are alive
            # as backends. Master flag (JARVIS_RENDER_CONDUCTOR_ENABLED)
            # graduated default-true at Slice 7. Producer-side flags
            # (REASONING_STREAM / INPUT_CONTROLLER / THREAD_OBSERVER /
            # CONTEXTUAL_HELP) stay default-false so each surface opts
            # in independently; substrate is alive at default but no
            # producer emits events until those flags flip. NEVER
            # raises; boot is not blocked by rendering glue.
            try:
                from backend.core.ouroboros.governance.render_backends import (
                    wire_render_conductor,
                )
                from backend.core.ouroboros.battle_test.stream_renderer import (
                    get_stream_renderer,
                )

                def _posture_provider() -> Optional[str]:
                    try:
                        from backend.core.ouroboros.governance.posture_store import (
                            PostureStore,
                        )
                        store = PostureStore(
                            base_dir=self._config.repo_path / ".jarvis",
                        )
                        reading = store.load_current()
                        if reading is None:
                            return None
                        return reading.posture.value
                    except Exception:  # noqa: BLE001 — defensive
                        return None

                wire_render_conductor(
                    stream_renderer=get_stream_renderer(),
                    serpent_flow=self._serpent_flow,
                    ouroboros_console=getattr(self, "_tui_console", None),
                    posture_provider=_posture_provider,
                )
            except Exception as _wire_exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[harness] render conductor wire failed: %s",
                    _wire_exc,
                )

            # RenderConductor Slice 7 — graduation wiring. Constructs
            # the producer-side observers (InputController / ThreadObserver
            # / ContextualHelpResolver) and registers them as process
            # singletons. Each is gated by its own master flag (default
            # false) so default behavior is unchanged. Operators opt-in
            # per surface. SerpentFlow's existing _handle_cancel(op_id,
            # immediate=True) is registered into KeyActionRegistry under
            # CANCEL_CURRENT_OP — the Esc-mid-token wire (Gap #5).
            # NEVER raises; boot is not blocked by render glue.
            try:
                from backend.core.ouroboros.governance import key_input as _ki
                from backend.core.ouroboros.governance import (
                    render_thread as _rt,
                )
                from backend.core.ouroboros.governance import (
                    render_help as _rh,
                )

                # InputController — raw-stdin reader. Construction is
                # cheap; .start() short-circuits per its own gates
                # (master flag / TTY / REPL active).
                _input_ctrl = _ki.InputController()
                _ki.register_input_controller(_input_ctrl)

                # Wire SerpentFlow._handle_cancel into the action
                # registry. The handler is async; KeyActionRegistry
                # schedules it via asyncio.ensure_future when a loop
                # is running, else closes the coroutine cleanly.
                _flow_for_cancel = self._serpent_flow
                if _flow_for_cancel is not None and hasattr(
                    _flow_for_cancel, "_handle_cancel",
                ):
                    def _cancel_current_op_via_flow(_event: Any) -> Any:
                        try:
                            gls = getattr(_flow_for_cancel, "_gls", None)
                            op_id = ""
                            if gls is not None and hasattr(
                                gls, "current_generating_op_id",
                            ):
                                try:
                                    op_id = (
                                        gls.current_generating_op_id() or ""
                                    )
                                except Exception:  # noqa: BLE001
                                    op_id = ""
                            if not op_id:
                                return None
                            return _flow_for_cancel._handle_cancel(
                                op_id, immediate=True,
                            )
                        except Exception:  # noqa: BLE001 — defensive
                            return None
                    _input_ctrl.registry.register(
                        _ki.KeyAction.CANCEL_CURRENT_OP,
                        _cancel_current_op_via_flow,
                    )

                # ThreadObserver — sync bridge → conductor pump.
                # .start() is no-op when its master flag is off; bridge
                # remains alive for its CONTEXT_EXPANSION consumer.
                _thread_obs = _rt.ThreadObserver()
                _rt.register_thread_observer(_thread_obs)
                _thread_obs.start()

                # ContextualHelpResolver — read-only ranking surface.
                _help_resolver = _rh.ContextualHelpResolver()
                _rh.register_help_resolver(_help_resolver)
                _rh.register_help_action_handlers(
                    _help_resolver,
                    posture_provider=_posture_provider,
                )

                # InputController.start() — actually begin reading
                # stdin if all gates pass. Async — schedule + forget.
                try:
                    _ic_start = _input_ctrl.start()
                    if asyncio.iscoroutine(_ic_start):
                        try:
                            _loop = asyncio.get_running_loop()
                            _loop.create_task(_ic_start)
                        except RuntimeError:
                            try:
                                _ic_start.close()
                            except Exception:  # noqa: BLE001
                                pass
                except Exception:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[harness] InputController start scheduling failed",
                        exc_info=True,
                    )

                logger.info(
                    "[harness] Slice 7 graduation wired: input_controller=%s "
                    "thread_observer=%s help_resolver=%s",
                    "registered" if _ki.get_input_controller() else "off",
                    "active" if _thread_obs.active else "inactive",
                    "registered" if _rh.get_help_resolver() else "off",
                )
            except Exception as _grad_exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[harness] Slice 7 graduation wire failed: %s",
                    _grad_exc,
                )

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

    async def _gate_provider_readiness_or_refuse(self) -> bool:
        """Check provider readiness; refuse soak start when unhealthy.

        Composes the canonical provider_readiness_gate substrate. The
        gate is master-FALSE by default (§33.1) — when disabled returns
        ``False`` (proceed) without probing. When enabled, runs the
        configured probe cascade and:

          * READY / DISABLED → returns ``False`` (proceed)
          * any other verdict → writes a structured report to the
            session dir, stamps ``_stop_reason``, returns ``True``
            (refuse soak start)

        NEVER raises. A failure of the gate itself logs + returns
        ``False`` (fail-open: don't block the soak on gate breakage).
        """
        try:
            from backend.core.ouroboros.battle_test.provider_readiness_gate import (
                ReadinessVerdict,
                check_provider_readiness,
                master_enabled,
                write_readiness_report,
            )
        except Exception as imp_err:  # noqa: BLE001
            logger.debug(
                "provider_readiness_gate import failed: %s",
                imp_err,
            )
            return False

        if not master_enabled():
            return False

        try:
            report = await check_provider_readiness()
        except Exception as gate_err:  # noqa: BLE001 — gate is NEVER-raise
            logger.warning(
                "provider_readiness_gate crashed (fail-open): %s",
                gate_err,
            )
            return False

        # Persist report to session dir regardless of verdict — operator
        # audit always benefits from the readiness ledger.
        try:
            write_readiness_report(
                report, session_dir=self._session_dir,
            )
        except Exception as write_err:  # noqa: BLE001
            logger.debug(
                "provider_readiness_gate write failed: %s",
                write_err,
            )

        if report.soak_should_proceed:
            logger.info(
                "Provider readiness: %s (proceeding)",
                report.verdict.value,
            )
            return False

        # Refusal path — stamp stop_reason, log loud, return True so
        # caller short-circuits the boot sequence.
        self._stop_reason = (
            f"provider_readiness_refused:{report.verdict.value}"
        )
        logger.error(
            "Provider readiness REFUSED soak start: verdict=%s "
            "diagnostic=%s — report at %s/provider_readiness.json",
            report.verdict.value,
            report.diagnostic,
            self._session_dir,
        )
        # Best-effort visible surface (operator monitoring TUI).
        try:
            if (
                hasattr(self, "_serpent_flow")
                and self._serpent_flow is not None
            ):
                from rich.console import Console as _RC
                _RC().print(
                    f"[bold red]✘ Provider readiness refused soak: "
                    f"{report.verdict.value}[/bold red]",
                    highlight=False,
                )
        except Exception:  # noqa: BLE001
            pass
        # Mark verdict on session recorder if available — surfaces in
        # summary.json so audits show why the soak ended at boot.
        try:
            if (
                getattr(self, "_session_recorder", None) is not None
                and hasattr(
                    self._session_recorder, "record_event"
                )
            ):
                self._session_recorder.record_event(
                    "provider_readiness_refused",
                    {
                        "verdict": report.verdict.value,
                        "diagnostic": report.diagnostic,
                    },
                )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _boot_git_index_guard(self) -> None:
        """GitIndexGuard boot recovery (Phase C Slice 2).

        Detects a **missing** ``.git/index`` (the background-
        Cursor-Agent unlink failure mode that produces the false
        "7856 staged deletions" in Source Control) and advisorily
        rebuilds it from HEAD via ``git read-tree HEAD`` — working
        tree untouched, a present index is never modified.

        Pure composition, zero new logic here:
          * ``git_index_guard.detect_and_rebuild`` does the work
            (master-gated inside: ``JARVIS_GIT_INDEX_GUARD_ENABLED``
            default-OFF → ``DISABLED`` no-op, byte-identical boot).
          * The ``on_anomaly`` seam is wired to
            ``ide_observability_stream.publish_git_index_anomaly``
            so a rebuilt/failed index surfaces as a
            ``git_index_anomaly`` SSE frame. The guard imports NO
            governance module; the harness owns this wiring.

        NEVER raises into the boot path (mirrors ``_boot_mark`` /
        ``reap_orphans`` discipline) — a guard failure must not
        abort the organism boot.
        """
        try:
            from backend.core.ouroboros.governance import (
                git_index_guard as _gig,
            )

            def _on_anomaly(anomaly: Any) -> None:
                try:
                    from backend.core.ouroboros.governance import (
                        ide_observability_stream as _stream,
                    )
                    _stream.publish_git_index_anomaly(
                        anomaly.to_dict()
                    )
                except Exception:  # noqa: BLE001 — fail-silent seam
                    logger.debug(
                        "publish_git_index_anomaly failed",
                        exc_info=True,
                    )

            outcome = _gig.detect_and_rebuild(
                Path(self._config.repo_path),
                on_anomaly=_on_anomaly,
            )
            if outcome.outcome is (
                _gig.GitIndexGuardOutcome.MISSING_REBUILT
            ):
                logger.warning(
                    "[GitIndexGuard] boot: .git/index was missing "
                    "— rebuilt from HEAD (working tree untouched)"
                )
            elif outcome.outcome is (
                _gig.GitIndexGuardOutcome.MISSING_REBUILD_FAILED
            ):
                logger.error(
                    "[GitIndexGuard] boot: .git/index missing AND "
                    "rebuild failed: %s", outcome.detail,
                )
        except Exception:  # noqa: BLE001 — never abort boot
            logger.debug(
                "_boot_git_index_guard degraded", exc_info=True,
            )

    async def _boot_ledger_sovereignty_workspace(self) -> None:
        """Create the auto-commit worktree under the Ledger
        Sovereignty master flag (P1 Slice 2).

        When master is **off** (default per §33.1): pure no-op.
        ``JARVIS_AUTO_COMMIT_WORKSPACE`` is left unset, so
        ``AutoCommitter._effective_repo_root`` falls through to
        ``self._repo_root`` and the loop's commit behavior is
        byte-identical to pre-substrate.

        When master is **on**: derives a session-scoped branch
        name ``ouroboros/auto/<session>`` (collision-free per
        session — the harness session_id is a timestamp prefix),
        invokes the canonical ``WorktreeManager.create``, sets
        ``JARVIS_OUROBOROS_SESSION_ID`` + ``JARVIS_AUTO_COMMIT_
        WORKSPACE`` env vars, and lets ``WorktreeManager._stamp_
        ownership_marker`` write the sovereignty marker. Every
        subsequent ``AutoCommitter.commit`` resolves to that
        worktree and refuses (typed) if the marker is missing.

        NEVER raises — fail-open at this layer. If the worktree
        can't be created (disk full, branch collision, etc.) the
        downstream sovereignty assertion fires at commit time
        with the same typed result. The boot does NOT short-
        circuit on workspace failure; that decision belongs to
        the operator (they may want the loop to run in dry-mode
        without committing).
        """
        try:
            from backend.core.ouroboros.governance.ledger_sovereignty import (  # noqa: E501
                master_enabled,
            )
        except Exception:  # noqa: BLE001
            return

        if not master_enabled():
            return  # §33.1 master-FALSE byte-identical path

        # Session id MUST be exported BEFORE WorktreeManager.create
        # so the marker payload carries the correct value.
        os.environ.setdefault(
            "JARVIS_OUROBOROS_SESSION_ID",
            str(self._session_id),
        )

        # Compose canonical WorktreeManager. Worktree base lives
        # under the repo root's ``.worktrees/`` by default — same
        # as L3 subagent units, sweep-able by the same orphan
        # reaper (``WorktreeManager.reap_orphans``).
        try:
            from backend.core.ouroboros.governance.worktree_manager import (  # noqa: E501
                WorktreeManager,
            )
            mgr = WorktreeManager(
                repo_root=self._config.repo_path,
            )
            branch_name = f"ouroboros/auto/{self._session_id}"
            wt_path = await mgr.create(branch_name)
        except Exception as wt_err:  # noqa: BLE001 — fail-open
            logger.warning(
                "[ledger_sovereignty] auto-commit worktree "
                "create failed: %r — soak will proceed but "
                "AutoCommitter will refuse commits "
                "(sovereignty assertion fires at commit time)",
                wt_err,
            )
            return

        os.environ["JARVIS_AUTO_COMMIT_WORKSPACE"] = str(wt_path)
        # Stash on self for telemetry / cleanup decisions.
        self._auto_commit_workspace = wt_path
        logger.info(
            "[ledger_sovereignty] auto-commit worktree ready: "
            "%s (branch=%s, session=%s)",
            wt_path, branch_name, self._session_id,
        )

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

        # ── v3.6 Phase 1.5.C v2 — L2 exercise corpus boot hook ────────
        # Default-FALSE per §33.1 — when JARVIS_L2_EXERCISE_CORPUS_ENABLED
        # is unset, this short-circuits at the master-flag check
        # inside maybe_inject_exercise_at_boot without any fixture
        # I/O / worktree allocation.  When operator opts in (Phase 9
        # graduation soak or harness-exercise gap closure), the hook
        # lifts N problems from JARVIS_L2_EXERCISE_CORPUS_PATH into
        # isolated worktrees + emits them via the canonical
        # IntakeLayerService.ingest_envelope path — exactly the
        # surface Phase 9 cadence synthetic workload uses, so the
        # pipeline runs naturally and VALIDATE fails → L2 fires →
        # tree mode engages → repair_tree.jsonl receives a row.
        #
        # Positioning: this hook MUST fire AFTER the IntakeLayerService
        # boot block above so ``self._intake_service`` is non-None.
        # The v1 (Phase 1.5.C) positioning was BEFORE the subsystem
        # boot block which made the router walker resolve to None —
        # caught by the harness-exercise soak (bt-2026-05-12-202511,
        # 2026-05-12 13:25:16: "no intake router resolved — skipping").
        # never blocks boot.  Composes the canonical
        # IntakeLayerService.ingest_envelope surface — no parallel
        # router / no parallel worktree manager.
        try:
            from backend.core.ouroboros.governance.l2_exercise_seed import (  # noqa: E501
                maybe_inject_exercise_at_boot,
            )
            from backend.core.ouroboros.governance.worktree_manager import (  # noqa: E501
                WorktreeManager,
            )
            if self._intake_service is not None:
                _exercise_wm = WorktreeManager(
                    self._config.repo_path,
                )
                _exercise_verdict = await maybe_inject_exercise_at_boot(
                    self._intake_service,
                    worktree_manager=_exercise_wm,
                    repo_root=str(self._config.repo_path),
                )
                logger.info(
                    "[Harness] L2 exercise boot hook: verdict=%s",
                    _exercise_verdict.value,
                )
            else:
                logger.debug(
                    "[Harness] L2 exercise boot hook: "
                    "_intake_service is None — skipping",
                )
        except Exception:  # noqa: BLE001 — boot must NEVER fail
            logger.debug(
                "[Harness] L2 exercise boot hook raised — continuing",
                exc_info=True,
            )

        # ── v3.7 Phase 2 — SWE-Bench-Pro harness boot hook ─────────────
        # Default-FALSE per §33.1 — when JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED
        # is unset, this short-circuits at the master-flag check inside
        # maybe_inject_swe_bench_at_boot without any fixture I/O /
        # worktree allocation. When operator opts in (first live
        # benchmark soak), the hook lifts cached ProblemSpec records
        # through the full SWE-Bench-Pro vertical:
        #
        #     Phase A load_problem        — pulls from cache/HF
        #     Phase B.1 prepare_problem   — clones repo, applies test_patch
        #     Phase B.2.1 build_envelope  — composes IntentEnvelope
        #     canonical ingest_envelope   — same surface L2 exercise +
        #                                   Phase 9 synthetic use
        #
        # Orthogonal to JARVIS_SWE_BENCH_PRO_ENABLED (operators can
        # have loader enabled without auto-injecting every boot).
        # Positioning: AFTER the IntakeLayerService boot block above
        # so self._intake_service is non-None — mirrors the v2 L2
        # exercise hook positioning fix (df4b70a4a8).
        try:
            from backend.core.ouroboros.governance.swe_bench_pro.harness_inject import (  # noqa: E501
                SWEBenchProInjectionVerdict,
                autoscore_work_in_flight,
                maybe_inject_swe_bench_at_boot,
            )
            if self._intake_service is not None:
                # ── P1 provider-readiness pre-flight (pre-spend) ──
                # Composes the canonical Claude circuit-breaker state
                # (+ optional bounded health_probe). §33.1
                # default-FALSE → byte-identical (no probe, no gate)
                # when off. On REFUSE_* we SKIP the SWE inject
                # entirely so $0 is spent into a known 5xx/529
                # outage window (the v18 wasted-spend hole). Runs
                # STRICTLY before maybe_inject_swe_bench_at_boot.
                # NEVER raises (assess is total; this wrapper is
                # belt-and-suspenders for boot-must-never-fail).
                _preflight_refused = False
                try:
                    from backend.core.ouroboros.battle_test.provider_preflight import (  # noqa: E501
                        PreflightVerdict,
                        assess_provider_readiness,
                    )
                    _pf = await assess_provider_readiness()
                    logger.info(
                        "[Harness] provider pre-flight: verdict=%s",
                        _pf.value,
                    )
                    if _pf.is_refusal:
                        logger.error(
                            "[Harness] provider pre-flight REFUSED "
                            "(%s) — skipping SWE-Bench-Pro inject; "
                            "$0 spent into a known-bad provider "
                            "window.",
                            _pf.value,
                        )
                        _preflight_refused = True
                except Exception:  # noqa: BLE001 — gate never fails boot
                    logger.debug(
                        "[Harness] provider pre-flight raised — "
                        "proceeding (Option A; CB is the safety net)",
                        exc_info=True,
                    )

                # ── SWE-Bench Path↔Advisor pre-flight (cb2825326a) ──
                # Strict advisor-envelope fail-closed: asserts the
                # SWE-Bench worktree base + repo cache resolve UNDER
                # the operation_advisor anchor (project_root). A
                # TMPDIR base that escaped the anchor is exactly what
                # contaminated the shared tree in bt-2026-05-17-002318.
                # Folded into the SAME canonical _preflight_refused
                # gate (no parallel guard). §33.1 default-FALSE →
                # byte-identical no-op when off. NEVER raises.
                try:
                    from backend.core.ouroboros.battle_test.swe_path_preflight import (  # noqa: E501
                        SwePathVerdict,
                        assess_swe_path_readiness,
                    )

                    _gls = self._governed_loop_service
                    _cfg = (
                        getattr(_gls, "_config", None)
                        if _gls is not None
                        else None
                    )
                    _proj_root = (
                        getattr(_cfg, "project_root", None) or Path.cwd()
                    )
                    _swe_path_verdict = await assess_swe_path_readiness(
                        str(self._session_dir),
                        project_root=Path(_proj_root),
                    )
                    logger.info(
                        "[Harness] swe-path preflight: verdict=%s",
                        _swe_path_verdict.value,
                    )
                    if _swe_path_verdict == (
                        SwePathVerdict.REFUSE_OUTSIDE_ANCHOR
                    ):
                        logger.error(
                            "[Harness] swe-path preflight REFUSED — "
                            "worktree base outside advisor anchor; "
                            "skipping SWE-Bench-Pro inject (drop "
                            "TMPDIR overrides).",
                        )
                        _preflight_refused = True
                except Exception:  # noqa: BLE001 — gate never fails boot
                    logger.debug(
                        "[Harness] swe-path preflight raised — "
                        "proceeding (runtime fail-closed guard is "
                        "authoritative)",
                        exc_info=True,
                    )

                if _preflight_refused:
                    _swebp_verdict = (
                        SWEBenchProInjectionVerdict.SKIPPED_DISABLED
                    )
                else:
                    _swebp_verdict = await maybe_inject_swe_bench_at_boot(
                        self._intake_service,
                    )
                logger.info(
                    "[Harness] SWE-Bench-Pro boot hook: verdict=%s",
                    _swebp_verdict.value,
                )
                # Closed-loop autoscore runs as a fire-and-forget
                # background task invisible to the GLS-op staleness
                # check. Register it as a session-liveness probe so
                # the ActivityMonitor keeps the session alive while a
                # discriminator eval is still solving/scoring
                # (v16 bt-2026-05-16-085224 idle-reap fix).
                if _swebp_verdict == (
                    SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE
                ):
                    self.register_session_liveness_probe(
                        autoscore_work_in_flight
                    )
                    logger.info(
                        "[Harness] registered autoscore session-"
                        "liveness probe (idle watchdog will not reap "
                        "while closed-loop eval is in flight)"
                    )
            else:
                logger.debug(
                    "[Harness] SWE-Bench-Pro boot hook: "
                    "_intake_service is None — skipping",
                )
        except Exception:  # noqa: BLE001 — boot must NEVER fail
            logger.debug(
                "[Harness] SWE-Bench-Pro boot hook raised — continuing",
                exc_info=True,
            )

    async def _inject_phase_9_synthetic_workload(self) -> None:
        """Phase 9 cadence synthetic workload injection.

        Operator binding 2026-05-05 — closes the headless-cadence
        zero-ops blocker WITHOUT diluting the P9.2 graduation
        contract:

          * **Single pipeline**: envelopes built via the canonical
            ``make_envelope`` factory + routed via the canonical
            ``UnifiedIntakeRouter`` (delegated through
            ``IntakeLayerService.ingest_envelope``). No second
            router. No direct decision-trace writes.
          * **Honest source token**: ``source="cadence_synthetic"``
            + ``evidence.category="cadence_synthetic"`` +
            ``evidence.sensor="Phase9SyntheticSeeder"``. Operators
            filter cadence load from real load by token.
          * **Defaults / safety**: skipped entirely when
            ``config.seed_intents <= 0`` (production default) OR
            when not headless (interactive sessions never inject).
            Hard cap via ``phase_9_synthetic_workload.seed_intents_max()``
            (env-clamped [1, 64]).
          * **Proof not vibes**: routes through Iron Gate / risk
            tier / SemanticGuardian like any other envelope. The
            P9.2 contract still demands ``ops_count >= 1`` from
            real FSM execution; this method just ensures the FSM
            actually has something to execute.
          * **NEVER raises** — defensive at every layer.
        """
        n = int(getattr(self._config, "seed_intents", 0) or 0)
        if n <= 0:
            return
        if not self._config.resolve_headless():
            logger.debug(
                "[Phase9Seeder] non-headless mode — skipping "
                "synthetic workload injection (operator's real "
                "workload IS the workload)",
            )
            return
        if self._intake_service is None:
            logger.warning(
                "[Phase9Seeder] intake service not booted — "
                "cannot inject synthetic workload",
            )
            return
        try:
            from backend.core.ouroboros.governance.graduation.phase_9_synthetic_workload import (  # noqa: E501
                build_synthetic_envelopes,
            )
        except ImportError:
            logger.debug(
                "[Phase9Seeder] factory unavailable — skipping",
                exc_info=True,
            )
            return
        repo_str = str(self._config.repo_path)
        envelopes = build_synthetic_envelopes(
            n=n,
            repo=repo_str,
            project_root=self._config.repo_path,
        )
        if not envelopes:
            logger.warning(
                "[Phase9Seeder] factory returned 0 envelopes "
                "for n=%d — check JARVIS_PHASE9_SEED_INTENTS_MAX",
                n,
            )
            return
        accepted = 0
        rejected = 0
        for envelope in envelopes:
            try:
                ok = await self._intake_service.ingest_envelope(
                    envelope,
                )
                if ok:
                    accepted += 1
                else:
                    rejected += 1
            except Exception:  # noqa: BLE001 -- defensive
                # Single bad envelope cannot poison the whole
                # injection path. The intake_service.ingest_envelope
                # already swallows exceptions; this is belt-and-
                # braces for robustness.
                rejected += 1
                logger.debug(
                    "[Phase9Seeder] ingest_envelope raised — "
                    "continuing with remaining envelopes",
                    exc_info=True,
                )
                continue
        logger.info(
            "[Phase9Seeder] injected n=%d/%d synthetic envelopes "
            "(accepted=%d rejected=%d source=cadence_synthetic)",
            accepted, len(envelopes), accepted, rejected,
        )

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

                # A-fix-v2: UNCONDITIONAL grader-liveness heartbeat,
                # evaluated every tick BEFORE the active_ops gate and
                # as the SINGLE probe-consult site. Background autoscore
                # / parallel_evaluate holds NO op in _active_ops while
                # it awaits operation_terminal, so the v16/v17 idle
                # path is "active_ops empty + grader still running":
                # the old in-tree probe (A-v1) was bypassed by the
                # `if not active_ops: continue` early-out below. If the
                # grader is running we poke here regardless of GLS op
                # population. We do NOT `continue` when ops exist — the
                # normal progressing/stale classification (incl.
                # stale-op force-cancel) must still run; we only skip
                # it when there are no ops to classify.
                _probe_hot = self._any_session_liveness_probe_hot()
                if _probe_hot:
                    self._idle_watchdog.poke()

                try:
                    active_ops: set = getattr(gls, "_active_ops", set())
                    if not active_ops:
                        # No GLS ops to classify. If the grader probe
                        # is hot we already poked → session stays
                        # alive while closed-loop work runs (the fix).
                        # Otherwise the normal idle timer handles it.
                        if _probe_hot:
                            logger.debug(
                                "[ActivityMonitor] no active GLS ops "
                                "but session-liveness probe hot — "
                                "poked watchdog, session kept alive"
                            )
                        continue  # No ops — normal idle timer otherwise

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
        # Layer 7 fix (v2.92, 2026-05-10) — dual-clock authority.
        # Both monotonic AND wall-clock anchored; the deadline check
        # below fires on whichever ticks fastest (preserves NTP-
        # rollback safety AND catches host sleep/suspend gaps that
        # pause monotonic on macOS). See _monitor_wall_clock for
        # the full rationale (soak bt-2026-05-10-093428 ran 11h
        # instead of 40min because monotonic paused during laptop
        # sleep).
        anchor_monotonic = time.monotonic()
        anchor_wall = time.time()
        deadline_monotonic = anchor_monotonic + cap_s + grace_s
        deadline_wall = anchor_wall + cap_s + grace_s
        loop = asyncio.get_event_loop()
        stop_event = threading.Event()
        self._wall_clock_hard_deadline_stop = stop_event

        # ── Phase D: resource-zero panic channel ───────────────────────
        # Captured NOW (arm time, pre-wedge): a raw dup of fd 2. The
        # kill path writes to THIS fd via os.write and never the logging
        # module — a poisoned logging lock cannot wedge it. Falls back
        # to fd 2 directly if dup is unavailable.
        try:
            self._wd_panic_fd = os.dup(2)
        except OSError:
            self._wd_panic_fd = 2
        # Resource-zero hard-kill deadline = Layer-2 deadline + margin,
        # so the graceful asyncio path still gets its full window first;
        # only a WEDGE past this extra margin trips the unblockable
        # SIGKILL. Env-tunable (no hardcoding); floor 5s, ceiling 300s.
        try:
            _raw_hk = os.environ.get(
                "JARVIS_WALL_CLOCK_HARD_KILL_MARGIN_S", "",
            ).strip()
            hard_kill_margin_s = max(
                5.0, min(300.0, float(_raw_hk) if _raw_hk else 30.0),
            )
        except (TypeError, ValueError):
            hard_kill_margin_s = 30.0
        hard_kill_monotonic = deadline_monotonic + hard_kill_margin_s
        hard_kill_wall = deadline_wall + hard_kill_margin_s

        # ── Phase D: Layers 3 & 4 collapsed ─────────────────────────────
        # The old Layer 3 (SIGTERM-to-self → harness signal handler →
        # partial summary) and Layer 4 (logging-gated diagnostic +
        # _atexit_fallback_write → os._exit(75)) are DELETED, not
        # tuned. The postmortem proved both poisonable: bt-2026-05-17-
        # 024509 ignored the external SIGTERM (signal delivery wedged
        # behind the same starved interpreter), and the Layer-4
        # diagnostic acquired the poisoned logging lock before it
        # could reach os._exit. A watchdog that shares the signal
        # path AND the logging lock with the system it guards is not
        # a watchdog. Resource-zero collapse replaces both. The
        # JARVIS_WALL_CLOCK_ESCALATION_SIGTERM_S / _EXIT_S env knobs
        # are intentionally retired — they tuned a path that no
        # longer exists; keeping them would imply a tunable that does
        # nothing (operator-misleading dead config).

        def _watch() -> None:
            # ── Phase D: resource-zero thread startup ───────────────
            # The startup announce is itself on the critical path: if
            # the logging lock is ALREADY poisoned at arm time, a
            # _wd_log.info() here would block on lock acquisition
            # BEFORE the resource-zero tripwire is ever established —
            # a try/except cannot rescue a deadlocked lock acquire
            # (it blocks, it does not raise). So the announce uses the
            # SAME raw os.write to the pre-dup'd panic fd as the kill
            # path. The entire thread — not merely the kill closure —
            # is now severed from the logging module.
            try:
                os.write(
                    self._wd_panic_fd,
                    (
                        "\n[WallClockWatchdog] resource-zero hard-"
                        "deadline thread alive: cap=%.0fs grace=%.0fs "
                        "hard_kill_margin=%.0fs (logging/loop/SIGTERM "
                        "severed).\n" % (
                            cap_s, grace_s, hard_kill_margin_s,
                        )
                    ).encode("ascii", "replace"),
                )
            except Exception:  # noqa: BLE001 -- announce is best-effort
                pass
            try:
                # ── Phase D: resource-zero kill (ZERO shared resources)
                # No logging module, no asyncio loop, no SIGTERM — only
                # raw os syscalls. A poisoned logging lock / wedged loop
                # / wedged signal handler CANNOT block this. The fd was
                # dup'd at arm time, pre-wedge. NEVER returns.
                def _resource_zero_kill() -> None:
                    try:
                        os.write(
                            self._wd_panic_fd,
                            b"\n[WallClockWatchdog] RESOURCE-ZERO HARD "
                            b"KILL: wall-clock deadline exceeded; "
                            b"logging/loop/SIGTERM bypassed; SIGKILL "
                            b"now (stop_reason=wall_clock_cap).\n",
                        )
                    except Exception:
                        pass
                    try:
                        # `signal` is a module-level import, already
                        # resident in sys.modules before this thread
                        # ever ran — reference it directly. An inner
                        # `import signal` here would touch the import
                        # lock on the LAST line of defense; resource-
                        # zero means not even that.
                        os.kill(os.getpid(), signal.SIGKILL)
                    except Exception:
                        pass
                    os._exit(137)  # 128+SIGKILL — absolute backstop

                # ── Layer 2: graceful asyncio fire at cap + grace ──
                # Dual-clock authority (Layer 7): remaining = MIN of
                # monotonic/wall remaining; whichever expires first
                # wins (macOS sleep pauses monotonic; wall keeps
                # ticking → deadline_wall fires correctly).
                while not stop_event.is_set():
                    # Resource-zero tripwire FIRST, on raw clocks —
                    # before any poisonable call below. If a wedge ever
                    # lets the thread keep looping past the hard-kill
                    # deadline, it dies here, unblockably.
                    if (
                        time.monotonic() >= hard_kill_monotonic
                        or time.time() >= hard_kill_wall
                    ):
                        _resource_zero_kill()  # never returns
                    now_monotonic = time.monotonic()
                    now_wall = time.time()
                    remaining_monotonic = deadline_monotonic - now_monotonic
                    remaining_wall = deadline_wall - now_wall
                    remaining = min(remaining_monotonic, remaining_wall)
                    if remaining <= 0:
                        # Best-effort graceful nudge: enqueue-only, NOT
                        # waited — gives the clean summary.json path its
                        # chance without ever blocking this thread.
                        try:
                            loop.call_soon_threadsafe(
                                self._wall_clock_event.set,
                            )
                        except Exception:  # noqa: BLE001 -- defensive
                            pass
                        if self._stop_reason in ("unknown", "", None):
                            self._stop_reason = "wall_clock_cap"
                        # Diagnostic via RAW os.write — NEVER the
                        # logging module (the exact poison that wedged
                        # bt-2026-05-17-024509).
                        try:
                            os.write(
                                self._wd_panic_fd,
                                b"\n[WallClockWatchdog] Layer 2 fired "
                                b"(cap+grace): graceful asyncio nudge "
                                b"sent; resource-zero SIGKILL armed if "
                                b"clean shutdown does not win the "
                                b"bounded grace.\n",
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    stop_event.wait(timeout=min(5.0, max(0.1, remaining)))
                # ── Phase D collapse: bounded grace → resource-zero
                # kill. Replaces the old logging-gated Layer 3 (SIGTERM
                # self — PROVEN wedgeable: bt-2026-05-17-024509 ignored
                # external SIGTERM) and Layer 4 (_wd_log +
                # _atexit_fallback_write before os._exit — both
                # poisonable by the wedged logging lock). Reached via
                # Layer 2 fire OR clean stop_event.set().
                if stop_event.is_set():
                    return
                # Bounded window for the clean asyncio summary.json path
                # to win. Event.wait() returns True iff stop_event got
                # set (clean shutdown won) — only then skip the kill.
                if stop_event.wait(timeout=hard_kill_margin_s):
                    return
                # Clean shutdown did NOT win within the margin → loop is
                # wedged. Resource-zero SIGKILL: no logging, no loop, no
                # SIGTERM, no _atexit_fallback_write — the one thing a
                # poisoned process cannot defeat. (debug.log is the
                # canonical session record per CLAUDE.md; a partial
                # summary.json is not worth a poisonable blocker on the
                # last line of defense.)
                _resource_zero_kill()  # never returns
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
        # Capture BOTH monotonic AND wall-clock anchors at task entry.
        # Layer 7 fix (v2.92, 2026-05-10) — monotonic-only enforcement
        # silently failed under host sleep/suspend on macOS, where
        # `time.monotonic()` (`mach_absolute_time()`) pauses while the
        # CPU is halted. Observed in soak bt-2026-05-10-093428: laptop
        # slept ~10.5h, monotonic advanced 1892s, cap (2400s) never
        # fired, soak ran 11h instead of 40min. The watchdog's stated
        # contract is "wall-clock cap" — monotonic violates that
        # contract under sleep.
        #
        # Dual-clock authority: compose both clocks and take the max
        # as effective elapsed. This preserves NTP-rollback safety
        # (wall jumping backward falls back to monotonic via max())
        # AND catches sleep/suspend gaps (wall keeps advancing
        # against frozen monotonic). Forward NTP jumps fire the cap
        # earlier than intended — acceptable for soak semantics where
        # the operator wants "kill after N seconds of real time".
        anchor_monotonic = time.monotonic()
        anchor_wall = time.time()
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
        # Skew threshold — Layer 7 (v2.92). When wall_elapsed exceeds
        # monotonic_elapsed by this much, log a warning (host sleep
        # OR forward NTP jump). Debounced: only re-warn after another
        # threshold-worth of additional skew.
        try:
            raw_skew = os.environ.get(
                "JARVIS_WALL_CLOCK_SKEW_WARN_THRESHOLD_S", "",
            ).strip()
            skew_threshold_s = max(
                5.0, min(3600.0, float(raw_skew) if raw_skew else 60.0),
            )
        except (TypeError, ValueError):
            skew_threshold_s = 60.0
        # Diagnostic — confirm the task actually started running.
        # Without this, a silently-cancelled task is invisible
        # (the previous Defect #1 regression hid behind exactly
        # this gap — the 'armed' log fires before ensure_future
        # even returns; the task itself never logged).
        logger.info(
            "[WallClockWatchdog] async monitor task alive: "
            "cap=%.0fs check_interval=%.0fs "
            "skew_warn_threshold=%.0fs (Layer 7 dual-clock authority)",
            cap_s, check_interval_s, skew_threshold_s,
        )
        # Heartbeat every Nth iteration so an operator tailing the
        # log can confirm the task is still running. Default: every
        # 12th tick (60s at default 5s interval). Off when explicitly
        # 0.
        try:
            hb_every = int(os.environ.get(
                "JARVIS_WALL_CLOCK_HEARTBEAT_EVERY", "12",
            ).strip() or "12")
        except (TypeError, ValueError):
            hb_every = 12
        _tick = 0
        _last_skew_warned_at: float = 0.0  # debounce skew warnings
        while True:
            try:
                await asyncio.sleep(check_interval_s)
            except asyncio.CancelledError:
                # Log both clocks for post-mortem diagnostic.
                _mono_at_cancel = time.monotonic() - anchor_monotonic
                _wall_at_cancel = max(
                    0.0, time.time() - anchor_wall,
                )
                logger.info(
                    "[WallClockWatchdog] async monitor task cancelled "
                    "after %.0fs monotonic / %.0fs wall (NEVER fired)",
                    _mono_at_cancel, _wall_at_cancel,
                )
                return
            _tick += 1
            elapsed_monotonic = time.monotonic() - anchor_monotonic
            elapsed_wall = max(0.0, time.time() - anchor_wall)
            # Layer 7 dual-clock cap check — fire on whichever ticks
            # fastest. NTP-rollback safe (wall < monotonic → max
            # picks monotonic). Sleep/suspend safe (monotonic paused
            # → max picks wall). Forward NTP jump fires early
            # (acceptable for soak semantics).
            effective_elapsed = max(elapsed_monotonic, elapsed_wall)
            # Diagnostic: log skew if divergence exceeds threshold
            # (debounced — re-warn only after another threshold-worth
            # of additional drift, so a sustained sleep gap doesn't
            # spam the log every tick).
            skew = elapsed_wall - elapsed_monotonic
            if (
                skew >= skew_threshold_s
                and skew - _last_skew_warned_at >= skew_threshold_s
            ):
                logger.warning(
                    "[WallClockWatchdog] clock skew detected: "
                    "monotonic=%.0fs wall=%.0fs skew=%.0fs "
                    "(host sleep/suspend OR forward NTP jump). "
                    "Treating wall-clock as cap-authoritative.",
                    elapsed_monotonic, elapsed_wall, skew,
                )
                _last_skew_warned_at = skew
            if hb_every > 0 and _tick % hb_every == 0:
                logger.debug(
                    "[WallClockWatchdog] heartbeat: tick=%d "
                    "monotonic=%.0fs wall=%.0fs effective=%.0fs "
                    "remaining=%.0fs",
                    _tick, elapsed_monotonic, elapsed_wall,
                    effective_elapsed,
                    max(0.0, cap_s - effective_elapsed),
                )
            if effective_elapsed >= cap_s:
                break
            # Loop continues; next sleep will wake at +check_interval_s.
        # If another waiter already fired and the shutdown path is in progress,
        # this is a no-op — set() on an already-set event is idempotent, and
        # the FIRST_COMPLETED race has already picked its winner.
        _fired_monotonic = time.monotonic() - anchor_monotonic
        _fired_wall = max(0.0, time.time() - anchor_wall)
        _fired_effective = max(_fired_monotonic, _fired_wall)
        logger.warning(
            "[WallClockWatchdog] fired: monotonic=%.0fs wall=%.0fs "
            "effective=%.0fs >= max_wall_seconds=%.0fs — triggering "
            "graceful shutdown with stop_reason=wall_clock_cap.",
            _fired_monotonic, _fired_wall, _fired_effective, cap_s,
        )
        # Task #94 (2026-05-14) — suspension-likely diagnostic.
        # When wall_clock advances substantially faster than monotonic
        # (process was suspended by the OS), the firing is technically
        # correct (per Ticket A1 Guard 2: effective = max(monotonic,
        # wall) — opaque to activity), but the soak evidence is
        # invalidated.  A graduation/Bar A claim from such a session
        # is unreliable.  Compute the monotonic/wall ratio and warn
        # if it falls below JARVIS_HARNESS_SUSPENSION_WARN_RATIO
        # (default 0.5).  Pure diagnostic — no behavior change to
        # WHEN the watchdog fires.  Surfaced via summary.json's
        # suspension_likely + suspension_ratio fields (additive,
        # schema_version unchanged) so PRD/audit trails can cite
        # structured evidence instead of grepping the log.
        try:
            _susp_thresh = float(os.environ.get(
                "JARVIS_HARNESS_SUSPENSION_WARN_RATIO", "0.5",
            ))
            if not (0.0 < _susp_thresh <= 1.0):
                _susp_thresh = 0.5
        except (TypeError, ValueError):
            _susp_thresh = 0.5
        _susp_ratio: Optional[float] = None
        _susp_likely = False
        if _fired_wall > 0.0:
            _susp_ratio = max(0.0, min(1.0, _fired_monotonic / _fired_wall))
            if _susp_ratio < _susp_thresh:
                _susp_likely = True
                logger.warning(
                    "[WallClockWatchdog] SUSPENSION LIKELY: monotonic/"
                    "wall ratio=%.2f < threshold=%.2f (process was "
                    "suspended ~%.0fs of %.0fs wall window).  "
                    "Graduation / Bar A claims from this session are "
                    "INVALID unless re-run under caffeinate.  "
                    "Surfaced as summary.json suspension_likely=true.",
                    _susp_ratio, _susp_thresh,
                    _fired_wall - _fired_monotonic, _fired_wall,
                )
        # Stash on self for save_summary to read.  Additive field —
        # legacy callers / pre-v1.1c consumers ignore it.
        self._suspension_likely = _susp_likely
        self._suspension_ratio = _susp_ratio
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
                # Task #22 — composed-deadline coherence. The bounded
                # watchdog arms here IN PARALLEL with the autoscore
                # drain that runs later in _shutdown_components; with
                # the bare default_deadline_s() the os._exit(75) can
                # fire BEFORE harness_inject logs the verdict under a
                # heavy session (deep-run bt-2026-05-19-011003). When
                # closed-loop autoscore work is still in flight, extend
                # the watchdog deadline by the autoscore grace + margin
                # so the drain (which the evaluator already reserved
                # via Task #21) completes first. Composes existing
                # env knobs; no hardcode; bare path unchanged otherwise.
                _arm_deadline = _bsw_deadline_s()
                try:
                    from backend.core.ouroboros.governance.swe_bench_pro.harness_inject import (  # noqa: E501
                        autoscore_work_in_flight,
                    )
                    if autoscore_work_in_flight():
                        _grace = float(os.environ.get(
                            "JARVIS_SWE_BENCH_PRO_AUTOSCORE_SHUTDOWN_GRACE_S",
                            "30",
                        ) or "30")
                        _margin = float(os.environ.get(
                            "JARVIS_SWE_BENCH_PRO_EVAL_DRAIN_MARGIN_S",
                            "15",
                        ) or "15")
                        if _grace <= 0:
                            _grace = 30.0
                        if _margin <= 0:
                            _margin = 15.0
                        _arm_deadline = _arm_deadline + _grace + _margin
                except Exception:  # noqa: BLE001 — never block the arm
                    pass
                _wdg.arm(
                    reason="wall_clock_cap", deadline_s=_arm_deadline,
                )
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

    # =====================================================================
    # ProcessMemoryWatchdog (2026-05-18) — process-tree RSS ceiling.
    # Sibling of WallClockWatchdog. Closes the structural blind spot
    # that let a 52GB process tree OOM-kill the host while
    # MemoryPressureGate (system-free-% scoped, new-fanout-only) and
    # SensorGovernor (cost/postmortem only) both reported "healthy".
    # =====================================================================
    def _resolve_process_memory_thresholds(
        self,
    ) -> tuple[float, Optional[float], float]:
        """Resolve (warn_mb, cap_mb, interval_s) from env, adaptively.

        Never hardcodes a byte count. ``JARVIS_PROCESS_MEMORY_CAP_MB``
        is an absolute override; absent it the cap is a fraction of
        total system RAM (``JARVIS_PROCESS_MEMORY_CAP_FRACTION``,
        default 0.75) so the same code protects a 16GB laptop and a
        256GB box without edits. ``cap_mb=None`` means DISABLED (master
        switch off, or total RAM unknowable with no explicit cap) — the
        caller then never arms the watchdog (inert, exactly the
        wall-clock discipline when ``max_wall_seconds`` is None).
        """
        if os.environ.get(
            "JARVIS_PROCESS_MEMORY_WATCHDOG_ENABLED", "true",
        ).strip().lower() == "false":
            return (0.0, None, 0.0)

        def _envf(name: str) -> Optional[float]:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return None
            try:
                v = float(raw)
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None

        # Interval — floor 2s (no busy-probe), ceiling 120s (bound the
        # detection lag on a fast leak).
        interval_s = _envf("JARVIS_PROCESS_MEMORY_WATCHDOG_INTERVAL_S") or 15.0
        interval_s = max(2.0, min(120.0, interval_s))

        cap_mb = _envf("JARVIS_PROCESS_MEMORY_CAP_MB")
        if cap_mb is None:
            try:
                frac_raw = _envf("JARVIS_PROCESS_MEMORY_CAP_FRACTION")
                frac = frac_raw if frac_raw is not None else 0.75
                frac = max(0.10, min(0.95, frac))
                import psutil  # lazy — already a project dependency
                total_mb = psutil.virtual_memory().total / (1024.0 * 1024.0)
                cap_mb = total_mb * frac
            except Exception:  # noqa: BLE001 — psutil missing / probe failed
                # No host-relative cap derivable and no override → stay
                # DISABLED rather than invent a number (no hardcoding).
                return (0.0, None, interval_s)

        warn_mb = _envf("JARVIS_PROCESS_MEMORY_WARN_MB")
        if warn_mb is None:
            warn_mb = cap_mb * 0.85
        # Keep warn strictly below cap so the WARN checkpoint always
        # precedes the CAP stop.
        warn_mb = min(warn_mb, cap_mb * 0.98)
        return (warn_mb, cap_mb, interval_s)

    @staticmethod
    def _probe_process_tree_rss_mb() -> Optional[float]:
        """Sum RSS of THIS process + all descendants, in MB.

        P5 Arc C Slice 5a: the implementation moved verbatim to
        ``governance.process_tree_probe.probe_process_tree_rss_mb``
        (single source of truth shared with MemoryPressureGate — zero
        duplication). This thin staticmethod is preserved so the two
        internal callers + the watchdog test monkeypatch surface stay
        byte-stable; behavior is unchanged.
        """
        from backend.core.ouroboros.governance.process_tree_probe import (
            probe_process_tree_rss_mb,
        )

        return probe_process_tree_rss_mb()

    async def _checkpoint_oracle_best_effort(self) -> None:
        """Persist the Oracle graph now (composes Arc A symmetry +
        Arc B in-build checkpoint). Never raises — durability is an
        enhancement, not a correctness dependency."""
        oracle = getattr(self, "_oracle", None)
        save = getattr(oracle, "_save_cache", None)
        if save is None:
            return
        try:
            await save()
            logger.info("[ProcessMemoryWatchdog] Oracle graph checkpointed.")
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ProcessMemoryWatchdog] Oracle checkpoint degraded: %s",
                exc,
            )

    async def _fire_process_memory_cap(
        self, rss_mb: float, cap_mb: float,
    ) -> None:
        """Graceful CAP fire path — idempotent (``set()`` on an
        already-set event is a no-op and the FIRST_COMPLETED race has
        already chosen a winner if another waiter fired first)."""
        if self._stop_reason in ("unknown", "", None):
            self._stop_reason = "process_memory_cap"
        # Partial summary BEFORE the bounded watchdog can os._exit
        # (mirrors the WallClockWatchdog termination-hook discipline).
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
                cause=_TermCause.PROCESS_MEMORY_CAP,
                session_dir=str(self._session_dir),
                started_at=self._started_at,
                stop_reason=self._stop_reason or "process_memory_cap",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ProcessMemoryWatchdog] termination-hook dispatch "
                "degraded: %s", exc,
            )
        # Arm the bounded shutdown watchdog so a wedged loop still
        # exits (mirrors WallClockWatchdog Slice 1 parallel arm).
        try:
            from backend.core.ouroboros.battle_test.shutdown_watchdog import (
                default_deadline_s as _bsw_deadline_s,
            )
            _wdg = getattr(self, "_shutdown_watchdog", None)
            if _wdg is not None:
                _wdg.arm(
                    reason="process_memory_cap",
                    deadline_s=_bsw_deadline_s(),
                )
        except Exception:  # noqa: BLE001
            pass
        # One last lossless checkpoint, then join the race.
        await self._checkpoint_oracle_best_effort()
        self._process_memory_event.set()
        stop_evt = getattr(
            self, "_process_memory_hard_deadline_stop", None,
        )
        if stop_evt is not None:
            try:
                stop_evt.set()
            except Exception:  # noqa: BLE001
                pass

    async def _monitor_process_memory(
        self, warn_mb: float, cap_mb: float, interval_s: float,
    ) -> None:
        """Async periodic RSS ceiling — sibling of _monitor_wall_clock.

        WARN (debounced, with recovery hysteresis): proactively
        checkpoint the Oracle graph so a subsequent CAP stop loses at
        most one interval of index work. CAP: graceful artifact-
        producing stop via :meth:`_fire_process_memory_cap` BEFORE the
        kernel OOM-kills the tree (an OS kill leaves no summary.json).
        """
        logger.info(
            "[ProcessMemoryWatchdog] async monitor alive: "
            "warn=%.0fMB cap=%.0fMB interval=%.0fs",
            warn_mb, cap_mb, interval_s,
        )
        _warned = False
        _tick = 0
        while True:
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                logger.info(
                    "[ProcessMemoryWatchdog] async monitor cancelled "
                    "(NEVER fired)",
                )
                return
            _tick += 1
            rss_mb = self._probe_process_tree_rss_mb()
            if rss_mb is None:
                continue  # transient probe failure — retry next tick
            if _tick % 12 == 0:
                logger.debug(
                    "[ProcessMemoryWatchdog] heartbeat: tick=%d "
                    "rss=%.0fMB warn=%.0fMB cap=%.0fMB",
                    _tick, rss_mb, warn_mb, cap_mb,
                )
            if rss_mb >= cap_mb:
                logger.warning(
                    "[ProcessMemoryWatchdog] CAP exceeded: rss=%.0fMB "
                    ">= cap=%.0fMB — graceful stop_reason="
                    "process_memory_cap (async path).",
                    rss_mb, cap_mb,
                )
                await self._fire_process_memory_cap(rss_mb, cap_mb)
                return
            if rss_mb >= warn_mb and not _warned:
                _warned = True
                logger.warning(
                    "[ProcessMemoryWatchdog] WARN: rss=%.0fMB >= "
                    "warn=%.0fMB — proactively checkpointing Oracle so "
                    "an imminent cap stop is lossless.",
                    rss_mb, warn_mb,
                )
                await self._checkpoint_oracle_best_effort()
            elif rss_mb < warn_mb * 0.95:
                _warned = False  # re-arm WARN after recovery (hysteresis)

    def _start_process_memory_hard_deadline_thread(
        self, warn_mb: float, cap_mb: float, interval_s: float,
    ) -> None:
        """Thread backstop immune to asyncio starvation.

        Oracle cold-indexing is the documented dominant event-loop
        suffocator — if it wedges the loop the async monitor cannot
        tick. This daemon thread re-probes RSS on its own (slower)
        cadence and on CAP (1) arms the bounded shutdown watchdog
        DIRECTLY (guarantees ``os._exit`` even with a dead loop) and
        (2) best-effort schedules the event set via
        ``call_soon_threadsafe``. It deliberately does NOT touch the
        Oracle from the thread (loop-affinity) — the Arc B in-build
        checkpoint already bounds data loss. The stop event ends it
        cleanly when the async path fired first. NEVER raises.
        """
        try:
            loop = asyncio.get_event_loop()
        except Exception:  # noqa: BLE001
            return
        stop_event = threading.Event()
        self._process_memory_hard_deadline_stop = stop_event
        # Slower than the async path so the graceful async fire wins
        # under normal conditions (no double-stop).
        thread_interval = max(interval_s * 2.0, 10.0)

        def _run() -> None:
            while not stop_event.wait(thread_interval):
                rss_mb = self._probe_process_tree_rss_mb()
                if rss_mb is None or rss_mb < cap_mb:
                    continue
                try:
                    from backend.core.ouroboros.battle_test.shutdown_watchdog import (  # noqa: E501
                        default_deadline_s as _bsw_deadline_s,
                    )
                    _wdg = getattr(self, "_shutdown_watchdog", None)
                    if _wdg is not None:
                        _wdg.arm(
                            reason="process_memory_cap",
                            deadline_s=_bsw_deadline_s(),
                        )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    loop.call_soon_threadsafe(
                        self._process_memory_event.set,
                    )
                except Exception:  # noqa: BLE001
                    pass
                logger.warning(
                    "[ProcessMemoryWatchdog] thread backstop fired: "
                    "rss=%.0fMB >= cap=%.0fMB (asyncio path wedged).",
                    rss_mb, cap_mb,
                )
                return

        threading.Thread(
            target=_run,
            name="process-memory-watchdog",
            daemon=True,
        ).start()

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

    def register_session_liveness_probe(
        self, probe: Callable[[], bool],
    ) -> None:
        """Register a zero-arg probe that returns True while
        background closed-loop work is in flight. The ActivityMonitor
        pokes the idle watchdog while any probe is hot, so
        fire-and-forget work (autoscore parallel_evaluate) cannot be
        idle-reaped. Mirrors the GENERATE stream-activity idea —
        "don't starve the session while real work runs"."""
        if callable(probe):
            self._session_liveness_probes.append(probe)

    def _any_session_liveness_probe_hot(self) -> bool:
        """True if ANY registered probe reports work in flight.
        Per-probe fail-open: a raising probe is treated as not-hot
        and never breaks the ActivityMonitor decision loop."""
        for probe in getattr(self, "_session_liveness_probes", ()):
            try:
                if probe():
                    return True
            except Exception:  # noqa: BLE001 — probe must not break monitor
                continue
        return False

    async def _shutdown_components(self) -> None:
        """Stop all components in reverse boot order.

        Each stop call is wrapped in try/except so that one failure does
        not prevent the remaining components from being cleaned up.
        """
        logger.info("Shutting down session %s ...", self._session_id)

        # ── Slice 12V Phase 1 — WAL-first shutdown ──
        #
        # bt-2026-05-23-192636 (Slice 12U validation soak) closed the
        # LoopDeadman wedge but surfaced that `ShutdownWatchdog` fires
        # `os._exit(75)` 30s into `_shutdown_components` when a network
        # cleanup probe (`dw_heavy_probe`) hangs — bypassing both the
        # clean `_generate_report` path AND the atexit fallback (atexit
        # doesn't run after `os._exit`). Result: `summary.json` carries
        # only the last periodic checkpoint with `operations[]` empty.
        #
        # Slice 12V flips the dependency: BEFORE any cleanup step that
        # could hang on network / disk / locks, synchronously persist
        # the current SessionRecorder state via `_atexit_fallback_write`
        # so `summary.json` is on disk with the latest operations[]
        # snapshot. The clean `_generate_report` path later overwrites
        # this with the richer final report, but if `os._exit` fires
        # mid-shutdown the operator still gets a usable WAL.
        #
        # Wrapped in try/except — telemetry MUST NEVER abort cleanup
        # itself. Master switch
        # `JARVIS_SHUTDOWN_WAL_FIRST_ENABLED` (default TRUE) for
        # byte-identical rollback.
        try:
            _wal_first_raw = os.environ.get(
                "JARVIS_SHUTDOWN_WAL_FIRST_ENABLED", "true",
            ).strip().lower()
            if _wal_first_raw in {"1", "true", "yes", "on"}:
                try:
                    self._atexit_fallback_write(
                        session_outcome="in_flight_shutdown_wal",
                    )
                    logger.info(
                        "[Harness] Slice 12V WAL-first: pre-cleanup "
                        "summary.json persisted (operations[] "
                        "snapshot survives any teardown os._exit)",
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[Harness] Slice 12V WAL-first write raised "
                        "(swallowed) — proceeding with cleanup",
                        exc_info=True,
                    )
        except Exception:  # noqa: BLE001 — defensive
            pass

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

        # 0a. Bounded autoscore drain — give any in-flight closed-loop
        # parallel_evaluate a short grace to land its Phase C/D verdict
        # and close its generator in its OWN coroutine context BEFORE
        # the broker/intake below are torn down. This is the clean
        # counterpart to the liveness probe: probe keeps the session
        # alive while work runs; this drains it deterministically at
        # the end instead of force-cancelling mid-aclose (the v16
        # `aclose(): asynchronous generator is already running`).
        try:
            from backend.core.ouroboros.governance.swe_bench_pro.harness_inject import (  # noqa: E501
                await_autoscore_drain,
            )
            _grace = float(os.environ.get(
                "JARVIS_SWE_BENCH_PRO_AUTOSCORE_SHUTDOWN_GRACE_S", "30",
            ))
            await await_autoscore_drain(grace_s=_grace)
        except asyncio.CancelledError:
            raise
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

        # 0d2. Production Oracle observer (Defect #2 fix 2026-05-03)
        try:
            if (
                hasattr(self, "_production_oracle_monitor_task")
                and self._production_oracle_monitor_task
            ):
                self._production_oracle_monitor_task.cancel()
                try:
                    await self._production_oracle_monitor_task
                except asyncio.CancelledError:
                    pass
        except Exception:
            pass

        # 0d3. EvaluatorTraceObserver (Slice 5 — PR #48711 ignition wire).
        # Stop the structural-probe observer BEFORE broker/intake teardown
        # so its final SSE publish lands cleanly. Master-flag-off boots
        # leave _evaluator_trace_observer as None (gracefully skipped
        # here). CancelledError propagates per asyncio contract; every
        # other exception swallowed defensively (a degraded observer
        # MUST NOT block the rest of shutdown).
        try:
            _eto = getattr(self, "_evaluator_trace_observer", None)
            if _eto is not None:
                try:
                    await _eto.stop()
                except asyncio.CancelledError:
                    raise
                except Exception as _eto_stop_exc:  # noqa: BLE001
                    logger.debug(
                        "[EvaluatorTraceObserver] stop degraded: %s",
                        _eto_stop_exc,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            pass

        # 0d4. ControlPlaneWatchdog (Slice 11A — control-plane
        # starvation early-warning probe). Stop before broker/intake
        # teardown so the final lag_events count lands cleanly.
        try:
            _cpw = getattr(self, "_control_plane_watchdog", None)
            if _cpw is not None:
                try:
                    await _cpw.stop()
                except asyncio.CancelledError:
                    raise
                except Exception as _cpw_stop_exc:  # noqa: BLE001
                    logger.debug(
                        "[ControlPlaneWatchdog] stop degraded: %s",
                        _cpw_stop_exc,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
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
            # First settle the deferred init task — either it finished
            # naturally (fast path) or we cancel it and let cancellation
            # propagate. Either way, no half-initialized Chroma client
            # leaks across shutdown. Bounded wait so a wedged init
            # cannot block clean teardown.
            if (
                self._oracle_init_task is not None
                and not self._oracle_init_task.done()
            ):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._oracle_init_task),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    # Init still running past the grace window — cancel
                    # and consume the resulting CancelledError without
                    # surfacing it as a leak.
                    self._oracle_init_task.cancel()
                    try:
                        await self._oracle_init_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                except Exception as _init_exc:  # noqa: BLE001
                    logger.debug(
                        "Oracle deferred init terminated during "
                        "shutdown: %s", _init_exc, exc_info=True,
                    )
            self._oracle_init_task = None
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
    # Slice 12G-3 — Continuous WAL checkpoint loop
    # ------------------------------------------------------------------

    def _slice12g3_build_checkpoint_state(
        self, *, reason: str,
    ) -> Dict[str, Any]:
        """Snapshot the minimal-but-useful session state for the
        continuous WAL. Mirrors a subset of the fields that
        ``session_recorder.save_summary`` writes at clean shutdown
        — enough that a forensic reader can resume / diagnose
        from a hard-killed session.

        NEVER raises (defensive everywhere — checkpoint failure
        must not block the asyncio loop)."""
        now = time.time()
        duration_s = (now - self._started_at) if self._started_at else 0.0
        try:
            cost_total = float(self._cost_tracker.total_spent)
        except Exception:  # noqa: BLE001
            cost_total = 0.0
        try:
            cost_breakdown = dict(self._cost_tracker.breakdown)
        except Exception:  # noqa: BLE001
            cost_breakdown = {}
        # Slice 12G-3 partial-state shape — mirrors the canonical
        # save_summary fields enough for forensic + audit consumers.
        state: Dict[str, Any] = {
            "schema_version": 2,
            "session_id": self._session_id,
            "session_outcome": "in_flight",  # overridden by clean-shutdown save_summary
            "stop_reason": self._stop_reason,
            "started_at": float(self._started_at) if self._started_at else 0.0,
            "duration_s": float(duration_s),
            "cost_total": cost_total,
            "cost_breakdown": cost_breakdown,
            "last_activity_ts": now,
            "suspension_likely": bool(self._suspension_likely),
            "suspension_ratio": self._suspension_ratio,
            "wal_checkpoint_reason": str(reason)[:128],
        }
        return state

    async def _slice12g3_periodic_checkpoint_loop(self) -> None:
        """Periodic WAL checkpoint task. Cadence env-tunable via
        ``JARVIS_SESSION_WAL_PERIODIC_S`` (default 15s).
        NEVER raises into the asyncio loop — the WAL itself is
        defensive, and the loop swallows any incidental failures."""
        try:
            interval_s = float(os.environ.get(
                "JARVIS_SESSION_WAL_PERIODIC_S", "15.0",
            ))
        except (TypeError, ValueError):
            interval_s = 15.0
        interval_s = max(2.0, min(300.0, interval_s))
        while True:
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                # Clean cancel during shutdown — emit a final
                # checkpoint with reason=shutdown_cancel before
                # returning so the in-flight state is captured.
                try:
                    if self._session_wal is not None:
                        state = self._slice12g3_build_checkpoint_state(
                            reason="shutdown_cancel",
                        )
                        self._session_wal.force_checkpoint(
                            state, "shutdown_cancel",
                        )
                except Exception:  # noqa: BLE001
                    pass
                raise
            try:
                if self._session_wal is None:
                    return  # WAL gone — exit gracefully
                state = self._slice12g3_build_checkpoint_state(
                    reason="periodic",
                )
                self._session_wal.checkpoint(state, "periodic")
            except Exception:  # noqa: BLE001 — defensive
                # Continuous WAL is best-effort; a single failed
                # checkpoint must not break the periodic cadence.
                logger.debug(
                    "[SessionWAL] periodic checkpoint failed "
                    "(continuing)",
                    exc_info=True,
                )

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
                # Task #94 (2026-05-14, v1.1c additive) — suspension diagnostic.
                # Set by WallClockWatchdog when monotonic/wall ratio < threshold
                # (default 0.5). Default False on clean runs.  Audit tooling
                # treats suspension_likely=True as graduation-evidence-invalid.
                suspension_likely=self._suspension_likely,
                suspension_ratio=self._suspension_ratio,
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
        # Layer 7 fix (v2.92, 2026-05-10) — dual-clock skew threshold
        # env knob. Required-literal pin guarantees the skew-warning
        # path (asyncio monitor) doesn't silently regress to
        # monotonic-only enforcement.
        "JARVIS_WALL_CLOCK_SKEW_WARN_THRESHOLD_S",
        "WallClockHardDeadlineThread",
        # Defect #2 fix (2026-05-03) — boot wire-up for the Production
        # Oracle observer's run_periodic loop. Without these markers,
        # the observer is constructed but never starts, leaving its
        # history ring buffer empty and the auto_action_router's
        # oracle veto rule (Rule 1.5) reading current()=None.
        "_production_oracle_monitor_task",
        "production_oracle_observer",
        "run_periodic",
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
            saw_monotonic = False
            saw_wall = False
            for sub in _ast.walk(wall_clock_node):
                if isinstance(sub, _ast.While):
                    if (
                        isinstance(sub.test, _ast.Constant)
                        and sub.test.value is True
                    ):
                        saw_while_true = True
                elif isinstance(sub, _ast.Attribute):
                    # Detect time.monotonic() AND time.time() calls.
                    if (
                        isinstance(sub.value, _ast.Name)
                        and sub.value.id == "time"
                    ):
                        if sub.attr == "monotonic":
                            saw_monotonic = True
                        elif sub.attr == "time":
                            saw_wall = True
            if not saw_while_true:
                violations.append(
                    "_monitor_wall_clock MUST contain a 'while True' "
                    "periodic-check loop (the Defect #1 fix); single-"
                    "sleep regressions caused 22-min fire delay in "
                    "soak v5"
                )
            # Layer 7 pin (v2.92, 2026-05-10) — dual-clock authority:
            # _monitor_wall_clock body MUST reference BOTH
            # `time.monotonic()` AND `time.time()`. Monotonic alone
            # pauses during host sleep on macOS (mach_absolute_time
            # halts when CPU is halted) → soak bt-2026-05-10-093428
            # ran 11h instead of 40min. Composing wall-clock as a
            # second authority catches sleep/suspend gaps and forward
            # NTP jumps while preserving NTP-rollback safety via
            # max() (wall < monotonic falls back to monotonic).
            if not saw_monotonic:
                violations.append(
                    "_monitor_wall_clock MUST call time.monotonic() "
                    "(Layer 7 dual-clock authority); needed for NTP-"
                    "rollback safety as the lower bound of effective "
                    "elapsed"
                )
            if not saw_wall:
                violations.append(
                    "_monitor_wall_clock MUST call time.time() "
                    "(Layer 7 dual-clock authority, v2.92); needed "
                    "to catch host sleep/suspend gaps that pause "
                    "monotonic on macOS — without it, the cap "
                    "silently fails to fire after a sleep event"
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
