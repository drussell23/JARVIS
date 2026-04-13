"""
IntakeLayerService — Supervisor Zone 6.9 lifecycle manager.

Owns UnifiedIntakeRouter, all 4 sensors, and the A-narrator (salience-gated
preflight awareness). Mirrors GovernedLoopService pattern: no side effects in
constructor; all async initialization in start().

Delivery semantics: at-least-once intake (WAL) + idempotent execution (dedup_key).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, List, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

logger = logging.getLogger("Ouroboros.IntakeLayer")

# ---------------------------------------------------------------------------
# IntakeServiceState
# ---------------------------------------------------------------------------


class IntakeServiceState(Enum):
    INACTIVE = auto()
    STARTING = auto()
    ACTIVE = auto()
    DEGRADED = auto()
    STOPPING = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# IntakeLayerConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeLayerConfig:
    """Frozen configuration for IntakeLayerService."""

    project_root: Path
    dedup_window_s: float = 60.0
    backlog_scan_interval_s: float = 30.0
    miner_scan_interval_s: float = 300.0
    miner_complexity_threshold: int = 150
    miner_auto_submit_threshold: float = 0.75
    miner_scan_paths: List[str] = field(default_factory=lambda: ["backend/"])
    voice_stt_confidence_threshold: float = 0.70
    a_narrator_enabled: bool = True
    a_narrator_debounce_s: float = 10.0
    test_failure_min_count_for_narration: int = 2
    repo_registry: Optional[RepoRegistry] = None  # TYPE_CHECKING import; multi-repo sensor fan-out

    @classmethod
    def from_env(cls, project_root: Optional[Path] = None) -> "IntakeLayerConfig":
        resolved = project_root or Path(os.getenv("JARVIS_PROJECT_ROOT", os.getcwd()))
        return cls(
            project_root=resolved,
            dedup_window_s=float(os.getenv("JARVIS_INTAKE_DEDUP_WINDOW_S", "60.0")),
            backlog_scan_interval_s=float(
                os.getenv("JARVIS_INTAKE_BACKLOG_SCAN_INTERVAL_S", "30.0")
            ),
            miner_scan_interval_s=float(
                os.getenv("JARVIS_INTAKE_MINER_SCAN_INTERVAL_S", "300.0")
            ),
            miner_complexity_threshold=int(
                os.getenv("JARVIS_INTAKE_MINER_COMPLEXITY_THRESHOLD", "150")
            ),
            miner_auto_submit_threshold=float(
                os.getenv("JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD", "0.75")
            ),
            miner_scan_paths=list(
                filter(
                    None,
                    [
                        p.strip()
                        for p in os.getenv(
                            "JARVIS_INTAKE_MINER_SCAN_PATHS", "backend/,tests/"
                        ).split(",")
                    ],
                )
            ),
            voice_stt_confidence_threshold=float(
                os.getenv("JARVIS_INTAKE_VOICE_STT_THRESHOLD", "0.70")
            ),
            a_narrator_enabled=os.getenv(
                "JARVIS_INTAKE_A_NARRATOR_ENABLED", "true"
            ).lower() not in ("0", "false", "no"),
            a_narrator_debounce_s=float(
                os.getenv("JARVIS_INTAKE_A_NARRATOR_DEBOUNCE_S", "10.0")
            ),
            test_failure_min_count_for_narration=int(
                os.getenv("JARVIS_INTAKE_TF_MIN_COUNT", "2")
            ),
        )


# ---------------------------------------------------------------------------
# IntakeNarrator (A-layer)
# ---------------------------------------------------------------------------

# Sources that always trigger narration
_A_NARRATE_ALWAYS: frozenset[str] = frozenset({"voice_human"})
# Sources that require a count threshold
_A_NARRATE_THRESHOLD: frozenset[str] = frozenset({"test_failure"})
# Sources that are always silent at A-layer
_A_NARRATE_SILENT: frozenset[str] = frozenset({"backlog", "ai_miner"})

_A_TEMPLATES: Dict[str, str] = {
    "voice_human": "Voice command queued: {description}",
    "test_failure": "{count} test failures detected. Investigating.",
}


class IntakeNarrator:
    """A-layer narrator: salience-gated preflight awareness only.

    Language policy: 'detected/queued' — never 'applying/fixing'.
    QoS: debounced per-source; silent for backlog and ai_miner.
    """

    def __init__(
        self,
        say_fn: Callable[..., Coroutine[Any, Any, bool]],
        debounce_s: float = 10.0,
        test_failure_min_count: int = 2,
    ) -> None:
        self._say_fn = say_fn
        self._debounce_s = debounce_s
        self._test_failure_min_count = test_failure_min_count
        self._last_narration: float = float("-inf")
        self._failure_count: int = 0

    async def on_envelope(self, envelope: Any) -> None:
        """Called post-ingest. Filters by salience policy, then debounce."""
        source = envelope.source
        if source in _A_NARRATE_SILENT:
            return

        now = time.monotonic()

        if source in _A_NARRATE_THRESHOLD:
            self._failure_count += 1
            if self._failure_count < self._test_failure_min_count:
                return
            text = _A_TEMPLATES["test_failure"].format(count=self._failure_count)
        elif source in _A_NARRATE_ALWAYS:
            text = _A_TEMPLATES["voice_human"].format(
                description=str(envelope.description)[:80]
            )
        else:
            return  # Unknown source — silent by default

        if (now - self._last_narration) < self._debounce_s:
            return

        try:
            await self._say_fn(text, source="intake_narrator")
            self._last_narration = now
        except Exception:
            logger.debug(
                "[IntakeNarrator] say_fn failed for envelope %s", envelope.causal_id
            )


# ---------------------------------------------------------------------------
# IntakeLayerService
# ---------------------------------------------------------------------------


class IntakeLayerService:
    """Lifecycle manager for router + sensors + A-narrator (Zone 6.9).

    Constructor is side-effect free. All async setup in start().
    """

    def __init__(
        self,
        gls: Any,
        config: IntakeLayerConfig,
        say_fn: Optional[Callable[..., Coroutine[Any, Any, bool]]],
    ) -> None:
        self._gls = gls
        self._config = config
        self._say_fn = say_fn
        self._state = IntakeServiceState.INACTIVE
        self._started_at_monotonic: float = 0.0

        # Built during start()
        self._router: Optional[Any] = None
        self._sensors: List[Any] = []
        self._narrator: Optional[Any] = None  # IntakeNarrator; set in _build_components
        self._voice_sensor: Optional[Any] = None
        self._dead_letter_count: int = 0
        self._per_source_count: Dict[str, int] = {}

    @property
    def state(self) -> IntakeServiceState:
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build and start router, sensors, A-narrator. Idempotent."""
        if self._state in (IntakeServiceState.ACTIVE, IntakeServiceState.DEGRADED):
            return

        self._state = IntakeServiceState.STARTING
        try:
            await self._build_components()
            self._state = IntakeServiceState.ACTIVE
            self._started_at_monotonic = time.monotonic()
            logger.info("[IntakeLayer] Started: state=%s", self._state.name)
        except Exception as exc:
            self._state = IntakeServiceState.FAILED
            logger.error("[IntakeLayer] Start failed: %s", exc, exc_info=True)
            await self._teardown()
            raise

    async def stop(self) -> None:
        """Stop sensors first (drain), then router. Idempotent from INACTIVE."""
        if self._state is IntakeServiceState.INACTIVE:
            return

        self._state = IntakeServiceState.STOPPING

        # Stop sensors first to prevent new envelopes entering router.
        # Sensor stop() methods are synchronous; call directly.
        for sensor in self._sensors:
            try:
                result = sensor.stop()
                # Await if stop() happens to be a coroutine
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("[IntakeLayer] Sensor stop error: %s", exc)

        # Stop reactor consumer
        if hasattr(self, "_reactor_consumer") and self._reactor_consumer is not None:
            try:
                await self._reactor_consumer.stop()
            except Exception as exc:
                logger.warning("[IntakeLayer] ReactorEventConsumer stop error: %s", exc)

        # Stop router (drains in-flight queue)
        if self._router is not None:
            try:
                await self._router.stop()
            except Exception as exc:
                logger.warning("[IntakeLayer] Router stop error: %s", exc)

        # Stop FS bridge (was only stopped in the failed-start _teardown
        # path, leaving watchdog Observer threads alive on graceful
        # budget-exhausted shutdowns — those threads then spammed "Main
        # event loop not running" every 20s for the lifetime of the
        # process). Idempotent and logs-only on failure.
        if hasattr(self, "_fs_bridge") and self._fs_bridge is not None:
            try:
                await self._fs_bridge.stop()
            except Exception as exc:
                logger.warning(
                    "[IntakeLayer] FS bridge stop error: %s", exc,
                )
            self._fs_bridge = None

        self._sensors = []
        self._router = None
        self._narrator = None
        self._started_at_monotonic = 0.0
        self._state = IntakeServiceState.INACTIVE
        logger.info("[IntakeLayer] Stopped.")

    def health(self) -> Dict[str, Any]:
        """Return health metrics for supervisor health checks."""
        queue_depth = 0
        dead_letter_count = 0
        if self._router is not None:
            try:
                # Prefer the public API; fall back to private attr if unavailable.
                if hasattr(self._router, "intake_queue_depth"):
                    queue_depth = self._router.intake_queue_depth()
                else:
                    queue_depth = self._router._queue.qsize()
            except Exception:
                pass
            try:
                if hasattr(self._router, "dead_letter_count"):
                    dead_letter_count = self._router.dead_letter_count()
                elif hasattr(self._router, "_dead_letter"):
                    dead_letter_count = len(self._router._dead_letter)
            except Exception:
                pass

        uptime_s = (
            time.monotonic() - self._started_at_monotonic
            if self._started_at_monotonic > 0
            else 0.0
        )
        per_source_rate: Dict[str, float] = {}
        if uptime_s > 0:
            for src, cnt in self._per_source_count.items():
                per_source_rate[src] = round(cnt / (uptime_s / 60.0), 3)

        return {
            "state": self._state.name.lower(),
            "queue_depth": queue_depth,
            "dead_letter_count": dead_letter_count,
            "wal_entries_pending": 0,
            "per_source_rate": per_source_rate,
            "uptime_s": round(uptime_s, 1),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _build_components(self) -> None:
        """Construct and start router + sensors."""
        from backend.core.ouroboros.governance.intake import (
            IntakeRouterConfig,
            UnifiedIntakeRouter,
        )
        from backend.core.ouroboros.governance.intake.sensors import (
            BacklogSensor,
            OpportunityMinerSensor,
            TestFailureSensor,
            VoiceCommandSensor,
        )
        from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher

        router_config = IntakeRouterConfig(
            project_root=self._config.project_root,
            dedup_window_s=self._config.dedup_window_s,
        )
        # Inject RuntimeTaskOrchestrator if available (for runtime task dispatch)
        _rto = getattr(self._gls, "_runtime_task_orchestrator", None)
        self._router = UnifiedIntakeRouter(
            gls=self._gls,
            config=router_config,
            runtime_orchestrator=_rto,
        )

        # A-narrator: salience-gated preflight awareness
        if self._config.a_narrator_enabled and self._say_fn is not None:
            self._narrator = IntakeNarrator(
                say_fn=self._say_fn,
                debounce_s=self._config.a_narrator_debounce_s,
                test_failure_min_count=self._config.test_failure_min_count_for_narration,
            )
            self._router._on_ingest_hook = self._narrator.on_envelope

        # Build sensors — using actual constructor parameter names.
        # BacklogSensor uses poll_interval_s (not scan_interval_s).
        # VoiceCommandSensor is event-driven (no start/stop); stored separately.

        # Fan out per-repo sensors when a registry is available; fall back to
        # single "jarvis" sensor for backward compatibility.
        registry = self._config.repo_registry
        enabled_repos = list(registry.list_enabled()) if registry is not None else []

        if enabled_repos:
            backlog_sensors: List[Any] = [
                BacklogSensor(
                    backlog_path=rc.local_path / ".jarvis" / "backlog.json",
                    repo_root=rc.local_path,
                    router=self._router,
                    poll_interval_s=self._config.backlog_scan_interval_s,
                )
                for rc in enabled_repos
            ]
            _test_poll_s = float(os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300"))
            test_failure_sensors = [
                TestFailureSensor(
                    repo=rc.name,
                    router=self._router,
                    test_watcher=TestWatcher(
                        repo=rc.name,
                        repo_path=str(rc.local_path),
                        poll_interval_s=_test_poll_s,
                    ),
                )
                for rc in enabled_repos
            ]
            _coalescer = getattr(self._gls, "_graph_coalescer", None)
            miner_sensors = [
                OpportunityMinerSensor(
                    repo_root=rc.local_path,
                    router=self._router,
                    scan_paths=self._config.miner_scan_paths,
                    complexity_threshold=self._config.miner_complexity_threshold,
                    poll_interval_s=self._config.miner_scan_interval_s,
                    repo=rc.name,
                    graph_coalescer=_coalescer,
                )
                for rc in enabled_repos
            ]
        else:
            backlog_sensors = [
                BacklogSensor(
                    backlog_path=self._config.project_root / ".jarvis" / "backlog.json",
                    repo_root=self._config.project_root,
                    router=self._router,
                    poll_interval_s=self._config.backlog_scan_interval_s,
                )
            ]
            _test_poll_s = float(os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300"))
            test_failure_sensors = [
                TestFailureSensor(
                    repo="jarvis",
                    router=self._router,
                    test_watcher=TestWatcher(
                        repo="jarvis",
                        repo_path=str(self._config.project_root),
                        poll_interval_s=_test_poll_s,
                    ),
                )
            ]
            _coalescer = getattr(self._gls, "_graph_coalescer", None)
            miner_sensors = [
                OpportunityMinerSensor(
                    repo_root=self._config.project_root,
                    router=self._router,
                    scan_paths=self._config.miner_scan_paths,
                    complexity_threshold=self._config.miner_complexity_threshold,
                    poll_interval_s=self._config.miner_scan_interval_s,
                    graph_coalescer=_coalescer,
                )
            ]

        # VoiceCommandSensor has no start/stop lifecycle; store as attribute only.
        # Wire UserSignalBus so "JARVIS stop" voice commands trigger FSM preemption.
        _signal_bus = getattr(self._gls, "_user_signal_bus", None)
        self._voice_sensor = VoiceCommandSensor(
            router=self._router,
            repo="jarvis",
            stt_confidence_threshold=self._config.voice_stt_confidence_threshold,
            signal_bus=_signal_bus,
        )

        # Sensors with start/stop lifecycle
        self._sensors = backlog_sensors + test_failure_sensors + miner_sensors

        # ---- ScheduledTriggerSensor (P6) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.scheduled_sensor import (
                ScheduledTriggerSensor,
            )
            _sched = ScheduledTriggerSensor(router=self._router)
            self._sensors.append(_sched)
            logger.info("[IntakeLayer] ScheduledTriggerSensor added")
        except ImportError:
            logger.debug("[IntakeLayer] ScheduledTriggerSensor: croniter not installed")
        except Exception as exc:
            logger.debug("[IntakeLayer] ScheduledTriggerSensor skipped: %s", exc)

        # ---- CapabilityGapSensor (Pillar 6: Neuroplasticity) ----
        # Consumes CapabilityGapEvents from the GapSignalBus (emitted by
        # ApplicationLauncherExecutor, AgentRegistry, etc.) and routes them
        # through the full Ouroboros pipeline for graduation tracking.
        try:
            from backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor import (
                CapabilityGapSensor,
            )
            _gap_sensor = CapabilityGapSensor(
                intake_router=self._router,
                repo="jarvis",
            )
            self._sensors.append(_gap_sensor)
            logger.info("[IntakeLayer] CapabilityGapSensor added (neuroplasticity active)")
        except Exception as exc:
            logger.debug("[IntakeLayer] CapabilityGapSensor skipped: %s", exc)

        # ---- RuntimeHealthSensor (P6 + Boundary Principle) ----
        # Autonomously monitors Python runtime EOL, dependency staleness,
        # security advisories, and legacy compat shims. Deterministic detection,
        # agentic remediation via Ouroboros pipeline.
        try:
            from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (
                RuntimeHealthSensor,
            )
            _health_poll_s = float(
                os.environ.get("JARVIS_RUNTIME_HEALTH_INTERVAL_S", "86400")
            )
            if enabled_repos:
                for rc in enabled_repos:
                    _health_sensor = RuntimeHealthSensor(
                        repo=rc.name,
                        router=self._router,
                        poll_interval_s=_health_poll_s,
                    )
                    self._sensors.append(_health_sensor)
            else:
                _health_sensor = RuntimeHealthSensor(
                    repo="jarvis",
                    router=self._router,
                    poll_interval_s=_health_poll_s,
                )
                self._sensors.append(_health_sensor)
            logger.info("[IntakeLayer] RuntimeHealthSensor added (autonomous dependency monitoring)")
        except Exception as exc:
            logger.debug("[IntakeLayer] RuntimeHealthSensor skipped: %s", exc)

        # ---- WebIntelligenceSensor (P1: proactive CVE/advisory monitoring) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.web_intelligence_sensor import (
                WebIntelligenceSensor,
            )
            _web_intel_poll_s = float(
                os.environ.get("JARVIS_WEB_INTEL_INTERVAL_S", "86400")
            )
            _web_sensor = WebIntelligenceSensor(
                repo="jarvis",
                router=self._router,
                poll_interval_s=_web_intel_poll_s,
                project_root=self._config.project_root,
            )
            self._sensors.append(_web_sensor)
            logger.info("[IntakeLayer] WebIntelligenceSensor added (proactive CVE monitoring)")
        except Exception as exc:
            logger.debug("[IntakeLayer] WebIntelligenceSensor skipped: %s", exc)

        # ---- PerformanceRegressionSensor (P2: continuous benchmarking) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.performance_regression_sensor import (
                PerformanceRegressionSensor,
            )
            _perf_poll_s = float(
                os.environ.get("JARVIS_PERF_REGRESSION_INTERVAL_S", "3600")
            )
            _perf_sensor = PerformanceRegressionSensor(
                repo="jarvis",
                router=self._router,
                poll_interval_s=_perf_poll_s,
            )
            self._sensors.append(_perf_sensor)
            logger.info("[IntakeLayer] PerformanceRegressionSensor added (continuous benchmarking)")
        except Exception as exc:
            logger.debug("[IntakeLayer] PerformanceRegressionSensor skipped: %s", exc)

        # ---- DocStalenessSensor (P2: automatic documentation gaps) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor import (
                DocStalenessSensor,
            )
            _doc_poll_s = float(
                os.environ.get("JARVIS_DOC_STALENESS_INTERVAL_S", "86400")
            )
            _doc_sensor = DocStalenessSensor(
                repo="jarvis",
                router=self._router,
                poll_interval_s=_doc_poll_s,
                project_root=self._config.project_root,
            )
            self._sensors.append(_doc_sensor)
            logger.info("[IntakeLayer] DocStalenessSensor added (documentation gap detection)")
        except Exception as exc:
            logger.debug("[IntakeLayer] DocStalenessSensor skipped: %s", exc)

        # ---- GitHubIssueSensor (auto-resolve issues across Trinity repos) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
                GitHubIssueSensor,
            )
            _gh_poll_s = float(
                os.environ.get("JARVIS_GITHUB_ISSUE_INTERVAL_S", "3600")
            )
            _gh_sensor = GitHubIssueSensor(
                repo="jarvis",
                router=self._router,
                poll_interval_s=_gh_poll_s,
            )
            self._sensors.append(_gh_sensor)
            logger.info("[IntakeLayer] GitHubIssueSensor added (Trinity issue auto-resolution)")
        except Exception as exc:
            logger.debug("[IntakeLayer] GitHubIssueSensor skipped: %s", exc)

        # ---- ProactiveExplorationSensor (P3: curiosity-driven domain exploration) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (
                ProactiveExplorationSensor,
            )
            _explore_sensor = ProactiveExplorationSensor(
                repo="jarvis",
                router=self._router,
                project_root=self._config.project_root,
            )
            self._sensors.append(_explore_sensor)
            logger.info("[IntakeLayer] ProactiveExplorationSensor added (curiosity-driven)")
        except Exception as exc:
            logger.debug("[IntakeLayer] ProactiveExplorationSensor skipped: %s", exc)

        # ---- IntentDiscoverySensor (Manifesto §1: intent-driven exploration) ----
        # Connects StrategicDirection + DreamEngine + Oracle + DW to explore
        # the codebase with purpose, guided by the developer's vision.
        try:
            from backend.core.ouroboros.governance.intake.sensors.intent_discovery_sensor import (
                IntentDiscoverySensor,
            )
            _intent_poll_s = float(
                os.environ.get("JARVIS_INTENT_DISCOVERY_INTERVAL_S", "900")
            )
            _intent_sensor = IntentDiscoverySensor(
                gls=self._gls,
                router=self._router,
                repo="jarvis",
                project_root=self._config.project_root,
                poll_interval_s=_intent_poll_s,
            )
            self._sensors.append(_intent_sensor)
            logger.info("[IntakeLayer] IntentDiscoverySensor added (Manifesto-driven exploration)")
        except Exception as exc:
            logger.debug("[IntakeLayer] IntentDiscoverySensor skipped: %s", exc)

        # ---- CrossRepoDriftSensor (P3: Trinity contract integrity) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.cross_repo_drift_sensor import (
                CrossRepoDriftSensor,
            )
            _drift_sensor = CrossRepoDriftSensor(
                repo="jarvis",
                router=self._router,
                project_root=self._config.project_root,
                repo_registry=self._config.repo_registry,
            )
            self._sensors.append(_drift_sensor)
            logger.info("[IntakeLayer] CrossRepoDriftSensor added (Trinity contract integrity)")
        except Exception as exc:
            logger.debug("[IntakeLayer] CrossRepoDriftSensor skipped: %s", exc)

        # ---- TodoScannerSensor (P3: unfinished work detection) ----
        try:
            from backend.core.ouroboros.governance.intake.sensors.todo_scanner_sensor import (
                TodoScannerSensor,
            )
            _todo_sensor = TodoScannerSensor(
                repo="jarvis",
                router=self._router,
                project_root=self._config.project_root,
            )
            self._sensors.append(_todo_sensor)
            logger.info("[IntakeLayer] TodoScannerSensor added (TODO/FIXME/HACK detection)")
        except Exception as exc:
            logger.debug("[IntakeLayer] TodoScannerSensor skipped: %s", exc)

        # ---- CUExecutionSensor (Pillar 6: Vision Neuroplasticity) ----
        # Event-driven sensor — records fed by ActionDispatcher after CU execution.
        # Singleton re-wiring: CUExecutionSensor.__init__ accepts router= on
        # re-init (if already constructed by get_cu_execution_sensor() elsewhere).
        try:
            from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
                CUExecutionSensor,
            )
            _cu_sensor = CUExecutionSensor(router=self._router, repo="jarvis")
            self._sensors.append(_cu_sensor)
            logger.info("[IntakeLayer] CUExecutionSensor wired (vision neuroplasticity active)")
        except Exception as exc:
            logger.debug("[IntakeLayer] CUExecutionSensor skipped: %s", exc)

        # ---- ReactorEventConsumer (P3) ----
        self._reactor_consumer = None
        try:
            from backend.core.ouroboros.governance.reactor_event_consumer import (
                ReactorEventConsumer,
            )
            from backend.core.ouroboros.cross_repo import CrossRepoEventBus
            _event_bus = CrossRepoEventBus()
            self._reactor_consumer = ReactorEventConsumer(event_bus=_event_bus)
            logger.info("[IntakeLayer] ReactorEventConsumer added")
        except Exception as exc:
            logger.debug("[IntakeLayer] ReactorEventConsumer skipped: %s", exc)

        # ---- Event Spine: FileWatchGuard → TrinityEventBus → Sensors ----
        # Replaces poll-based detection with sub-second event-driven intake.
        # Manifesto §3: "Zero polling. Pure reflex."
        self._fs_bridge: Any = None
        try:
            from backend.core.ouroboros.governance.intake.fs_event_bridge import (
                FileSystemEventBridge,
            )
            from backend.core.trinity_event_bus import get_trinity_event_bus

            _event_bus = await get_trinity_event_bus()
            self._fs_bridge = FileSystemEventBridge(
                project_root=self._config.project_root,
                event_bus=_event_bus,
            )
            await self._fs_bridge.start()

            # Subscribe each event-capable sensor to the bus
            _subscribed = 0
            for sensor in self._sensors:
                if hasattr(sensor, "subscribe_to_bus"):
                    try:
                        await sensor.subscribe_to_bus(_event_bus)
                        _subscribed += 1
                    except Exception as exc:
                        logger.debug(
                            "[IntakeLayer] Sensor bus subscription failed: %s", exc
                        )

            # Subscribe hot-reloader to event bus for event-driven reload
            # (G3: Manifesto §3 zero-polling). Gated by JARVIS_HOT_RELOAD_EVENT_DRIVEN.
            try:
                _orch = getattr(self._gls, "_orchestrator", None)
                _reloader = getattr(_orch, "_hot_reloader", None) if _orch else None
                if _reloader and hasattr(_reloader, "subscribe_to_bus"):
                    await _reloader.subscribe_to_bus(_event_bus)
            except Exception as exc:
                logger.debug(
                    "[IntakeLayer] Hot-reloader bus subscription skipped: %s", exc
                )

            logger.info(
                "[IntakeLayer] Event Spine active: FileWatch → TrinityEventBus → "
                "%d/%d sensors subscribed",
                _subscribed, len(self._sensors),
            )
        except Exception as exc:
            logger.warning(
                "[IntakeLayer] Event Spine failed to start, sensors will use "
                "polling fallback: %s", exc,
            )

        router = self._router
        assert router is not None
        await router.start()
        for sensor in self._sensors:
            await sensor.start()

        # Start reactor consumer after sensors (non-critical, fire-and-forget)
        if self._reactor_consumer is not None:
            try:
                await self._reactor_consumer.start()
            except Exception as exc:
                logger.warning("[IntakeLayer] ReactorEventConsumer start failed: %s", exc)
                self._reactor_consumer = None

    async def _teardown(self) -> None:
        """Best-effort cleanup after failed start."""
        # Stop FileSystemEventBridge first (stops file watcher)
        if hasattr(self, "_fs_bridge") and self._fs_bridge is not None:
            try:
                await self._fs_bridge.stop()
            except Exception:
                pass
            self._fs_bridge = None
        for sensor in self._sensors:
            try:
                result = sensor.stop()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        if self._router is not None:
            try:
                await self._router.stop()
            except Exception:
                pass
        self._sensors = []
        self._router = None
        self._voice_sensor = None
