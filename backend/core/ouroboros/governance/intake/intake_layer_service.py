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
from typing import Any, Callable, Coroutine, Dict, List, Optional

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
    miner_complexity_threshold: int = 10
    miner_scan_paths: List[str] = field(default_factory=lambda: ["backend/", "tests/"])
    voice_stt_confidence_threshold: float = 0.70
    a_narrator_enabled: bool = True
    a_narrator_debounce_s: float = 10.0
    test_failure_min_count_for_narration: int = 2

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
                os.getenv("JARVIS_INTAKE_MINER_COMPLEXITY_THRESHOLD", "10")
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

        # Stop router (drains in-flight queue)
        if self._router is not None:
            try:
                await self._router.stop()
            except Exception as exc:
                logger.warning("[IntakeLayer] Router stop error: %s", exc)

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

        router_config = IntakeRouterConfig(
            project_root=self._config.project_root,
            dedup_window_s=self._config.dedup_window_s,
        )
        self._router = UnifiedIntakeRouter(gls=self._gls, config=router_config)

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
        backlog_path = self._config.project_root / ".jarvis" / "backlog.json"

        backlog_sensor = BacklogSensor(
            backlog_path=backlog_path,
            repo_root=self._config.project_root,
            router=self._router,
            poll_interval_s=self._config.backlog_scan_interval_s,
        )
        test_failure_sensor = TestFailureSensor(
            repo="jarvis",
            router=self._router,
        )
        opportunity_miner_sensor = OpportunityMinerSensor(
            repo_root=self._config.project_root,
            router=self._router,
            scan_paths=self._config.miner_scan_paths,
            complexity_threshold=self._config.miner_complexity_threshold,
            poll_interval_s=self._config.miner_scan_interval_s,
        )

        # VoiceCommandSensor has no start/stop lifecycle; store as attribute only.
        self._voice_sensor = VoiceCommandSensor(
            router=self._router,
            repo="jarvis",
            stt_confidence_threshold=self._config.voice_stt_confidence_threshold,
        )

        # Sensors with start/stop lifecycle
        self._sensors = [backlog_sensor, test_failure_sensor, opportunity_miner_sensor]

        await self._router.start()
        for sensor in self._sensors:
            await sensor.start()

    async def _teardown(self) -> None:
        """Best-effort cleanup after failed start."""
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
