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
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

logger = logging.getLogger(__name__)


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
    branch_prefix: str = "ouroboros/battle-test"
    session_dir: Optional[Path] = None
    notebook_output_dir: Optional[Path] = None

    @classmethod
    def from_env(cls) -> HarnessConfig:
        """Build a HarnessConfig from environment variables.

        Reads:
        - ``OUROBOROS_BATTLE_COST_CAP``
        - ``OUROBOROS_BATTLE_IDLE_TIMEOUT``
        - ``OUROBOROS_BATTLE_BRANCH_PREFIX``
        - ``JARVIS_REPO_PATH``
        """
        return cls(
            repo_path=Path(os.environ.get("JARVIS_REPO_PATH", ".")),
            cost_cap_usd=float(os.environ.get("OUROBOROS_BATTLE_COST_CAP", "0.50")),
            idle_timeout_s=float(os.environ.get("OUROBOROS_BATTLE_IDLE_TIMEOUT", "600.0")),
            branch_prefix=os.environ.get("OUROBOROS_BATTLE_BRANCH_PREFIX", "ouroboros/battle-test"),
        )


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

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """Unique session identifier in ``bt-YYYY-MM-DD-HHMMSS`` format."""
        return self._session_id

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

            done, pending = await asyncio.wait(
                [shutdown_waiter, budget_waiter, idle_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel the pending waiters
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # Determine stop reason
            if shutdown_waiter in done:
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
        /risk, /budget, /goal.
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
        self._repl_print(
            f"[bold]Status:[/bold]  active={active}  completed={completed}  "
            f"failed={failed}  cost=${cost:.4f}/${cap:.2f}{paused_tag}"
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

        else:
            self._repl_print(
                "[dim]Usage: /goal | /goal all | /goal tree | "
                "/goal show <id> | /goal add [--parent <id>] <desc> | "
                "/goal remove <id> | /goal pause|resume|complete <id> | "
                "/goal purge[/dim]"
            )

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
            self._repl_print(f"[bold]{mem.type.value}:{mem.name}[/bold]  [dim]({mem.id})[/dim]")
            self._repl_print(f"  [dim]description:[/dim] {mem.description}")
            if mem.why:
                self._repl_print(f"  [dim]why:[/dim] {mem.why}")
            if mem.how_to_apply:
                self._repl_print(f"  [dim]how:[/dim] {mem.how_to_apply}")
            if mem.tags:
                self._repl_print(f"  [dim]tags:[/dim] {', '.join(mem.tags)}")
            if mem.paths:
                self._repl_print(f"  [dim]paths:[/dim] {', '.join(mem.paths)}")
            if mem.content:
                self._repl_print(f"  [dim]content:[/dim] {mem.content[:200]}")
            return

        self._repl_print(
            "[dim]Usage: /memory | /memory list [type] | /memory add <type> "
            "<name> | <desc> | /memory rm <id> | /memory forbid <path> | "
            "/memory show <id>[/dim]"
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

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def register_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register SIGINT/SIGTERM handlers to trigger clean shutdown.

        Catches ``NotImplementedError`` on Windows where
        ``loop.add_signal_handler`` is not supported.
        """
        try:
            loop.add_signal_handler(signal.SIGINT, self._shutdown_event.set)
            loop.add_signal_handler(signal.SIGTERM, self._shutdown_event.set)
        except NotImplementedError:
            logger.warning("Signal handlers not supported on this platform")

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
                        if last_transition is None:
                            progressing_count += 1
                            _stale_since.pop(dedupe_key, None)
                            continue

                        elapsed_s = (now - last_transition).total_seconds()

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

        Runs every 5 seconds. Computes *incremental* cost since the last poll
        and calls CostTracker.record() so the battle test's --cost-cap flag
        actually triggers budget_event when real API spend is reached.
        """
        _last_dw_cost: float = 0.0
        _last_claude_cost: float = 0.0
        try:
            while True:
                await asyncio.sleep(5.0)
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
            )
            logger.info("Summary written to %s", summary_path)
        except Exception as exc:
            logger.warning("Failed to save session summary: %s", exc)

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
