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
        try:
            # Boot sequence
            await self.boot_oracle()
            await self.boot_governance_stack()
            await self.boot_governed_loop_service()
            await self.boot_jarvis_tiers()
            self._branch_name = await self.create_branch()
            await self.boot_intake()
            await self.boot_graduation()

            logger.info(
                "Ouroboros is alive — session %s | budget=$%.2f | idle=%ds",
                self._session_id,
                self._config.cost_cap_usd,
                int(self._config.idle_timeout_s),
            )
            # Detect active subsystems for banner
            _gls = self._governed_loop_service
            _has_consciousness = (
                _gls is not None
                and getattr(_gls, "_consciousness_bridge", None) is not None
            )
            _has_goal_memory = (
                _gls is not None
                and getattr(_gls, "_goal_memory_bridge", None) is not None
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

            _on = "\033[92mON\033[0m"
            _off = "\033[2mOFF\033[0m"

            _n_principles = 0
            if _has_strategic:
                _n_principles = len(_gls._strategic_direction.principles)

            print(
                f"\n"
                f"\033[1m\033[96m"
                f"      \U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\U0001f40d\n"
                f"\033[0m"
                f"\033[1m\033[96m"
                f"      O U R O B O R O S  +  V E N O M\n"
                f"      The Self-Developing Organism\n"
                f"\033[0m"
                f"\033[2m"
                f"      {'─' * 52}\n"
                f"\033[0m"
                f"\n"
                f"  \U0001f9ec  Session    {self._session_id}\n"
                f"  \U0001f333  Branch     {self._branch_name or 'N/A'}\n"
                f"  \U0001f4b0  Budget     ${self._config.cost_cap_usd:.2f}\n"
                f"  \u23f3  Idle       {int(self._config.idle_timeout_s)}s\n"
                f"  \U0001f6e1\ufe0f   Mode       Governed (SAFE_AUTO auto-apply)\n"
                f"\n"
                f"\033[2m"
                f"      {'─' * 52}\n"
                f"\033[0m"
                f"\033[1m  6-Layer Organism Status:\033[0m\n"
                f"\n"
                f"  \U0001f9ed  Strategic Direction   [{_on if _has_strategic else _off}]"
                f"  {_n_principles} Manifesto principles\n"
                f"  \U0001f9e0  Consciousness         [{_on if _has_consciousness else _off}]"
                f"  Memory + Prophecy + Health\n"
                f"  \U0001f4e1  Event Spine           [{_on}]"
                f"  FileWatch \u2192 TrinityBus \u2192 sensors\n"
                f"  \u2699\ufe0f   Ouroboros Pipeline    [{_on}]"
                f"  {'parallel (' + str(getattr(_gls._bg_pool, '_pool_size', 2)) + ' workers)' if _has_bg_pool else 'sequential'}\n"
                f"  \U0001f40d  Venom Agentic Loop    [{_on if _has_tool_loop else _off}]"
                f"  {'bash + web + tests + L2' if _has_l2 else 'tools active'}\n"
                f"  \U0001f4dd  Thought Log           [{_on}]"
                f"  .jarvis/ouroboros_thoughts.jsonl\n"
                f"\n"
                f"\033[2m"
                f"      {'─' * 52}\n"
                f"\033[0m"
                f"  \U0001f50b Organism is alive. Sensors scanning...\n"
                f"  \u2328\ufe0f  Press Ctrl+C to stop.\n"
                f"\n"
            )

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
            # Inject BattleDiffTransport for live colored diffs in CLI
            try:
                from backend.core.ouroboros.battle_test.diff_display import BattleDiffTransport
                if hasattr(self._governance_stack, "comm") and self._governance_stack.comm is not None:
                    diff_transport = BattleDiffTransport(repo_path=self._config.repo_path)
                    self._governance_stack.comm._transports.append(diff_transport)
                    logger.info("BattleDiffTransport wired (live colored diffs in CLI)")
            except Exception as exc:
                logger.debug("BattleDiffTransport not available: %s", exc)

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

                _memory = MemoryEngine()
                _cortex = HealthCortex()
                _prophecy = ProphecyEngine(memory_engine=_memory)

                _consciousness = TrinityConsciousness(
                    health_cortex=_cortex,
                    memory_engine=_memory,
                    dream_engine=None,
                    prophecy_engine=_prophecy,
                    config=None,
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
    # GLS Activity Monitor
    # ------------------------------------------------------------------

    async def _monitor_gls_activity(self) -> None:
        """Background task: pokes idle watchdog whenever GLS has in-flight ops.

        The Doubleword batch API can take minutes per operation. Without this,
        the idle watchdog fires while batches are still in flight. This monitor
        checks every 5 seconds whether the GLS has active operations and pokes
        the watchdog if so — keeping the session alive during long batches.
        """
        try:
            while True:
                await asyncio.sleep(5.0)
                if self._governed_loop_service is not None:
                    try:
                        active = getattr(self._governed_loop_service, "_active_ops", set())
                        if active:
                            self._idle_watchdog.poke()
                            logger.debug(
                                "[ActivityMonitor] %d ops in flight, poked watchdog",
                                len(active),
                            )
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

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

        # 0b. Cost monitor
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
            print(terminal_summary)
        except Exception as exc:
            logger.warning("Failed to format terminal summary: %s", exc)

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
