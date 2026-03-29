"""OuroborosDaemon — Zone 7.0 top-level orchestrator.

Orchestrates all three awakening phases in order:

  Phase 1 — VitalScan    : deterministic boot-time invariant checks
  Phase 2 — SpinalCord   : bidirectional event-stream wiring
  Phase 3 — RemSleepDaemon: idle-watch background exploration loop

Design contracts
----------------
* ``awaken()`` is idempotent — subsequent calls return the cached
  :class:`AwakeningReport` without re-running any phase.
* ``awaken()`` never raises — it handles per-phase exceptions gracefully,
  degrading each phase independently so the daemon reaches a partial-but-
  running state rather than a hard crash.
* ``shutdown()`` is always safe to call, even before ``awaken()``.

Usage::

    daemon = OuroborosDaemon(
        oracle=oracle,
        fleet=fleet,
        bg_pool=bg_pool,
        intake_router=intake_router,
        event_stream=event_stream,
        proactive_drive=proactive_drive,
        doubleword=doubleword,
        gls=gls,
        config=DaemonConfig.from_env(),
        health_sensor=health_sensor,    # optional
    )

    report = await daemon.awaken()

    # Later…
    health  = daemon.health()
    metrics = daemon.metrics()
    await daemon.shutdown()
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

_HYPOTHESIS_CACHE_DIR: str = os.path.expanduser("~/.jarvis/ouroboros/roadmap")

from backend.core.ouroboros.daemon_config import DaemonConfig
from backend.core.ouroboros.rem_sleep import RemSleepDaemon
from backend.core.ouroboros.spinal_cord import SpinalCord, SpinalStatus
from backend.core.ouroboros.vital_scan import VitalReport, VitalScan, VitalStatus

logger = logging.getLogger("Ouroboros.Daemon")

# Public re-export so callers can use the canonical name from this module.
OuroborosDaemonConfig = DaemonConfig


# ---------------------------------------------------------------------------
# AwakeningReport
# ---------------------------------------------------------------------------


@dataclass
class AwakeningReport:
    """Snapshot of the three-phase awakening outcome.

    Attributes
    ----------
    vital_status:
        Aggregate status from Phase 1 VitalScan.
    vital_report:
        Full :class:`VitalReport` produced by Phase 1.
    spinal_status:
        CONNECTED or DEGRADED result from Phase 2 SpinalCord wiring.
    rem_started:
        True if Phase 3 RemSleepDaemon was created and its background task
        was started; False if REM was disabled via config or failed to start.
    """

    vital_status: VitalStatus
    vital_report: VitalReport
    spinal_status: SpinalStatus
    rem_started: bool


# ---------------------------------------------------------------------------
# OuroborosDaemon
# ---------------------------------------------------------------------------


class OuroborosDaemon:
    """Top-level Zone 7.0 orchestrator for the three Ouroboros awakening phases.

    Parameters
    ----------
    oracle:
        TheOracle instance for codebase graph queries.
    fleet:
        Agent fleet providing ExplorationSubagents.
    bg_pool:
        Background thread/async pool (currently reserved for future use).
    intake_router:
        Governance intake router for submitting IntentEnvelopes.
    event_stream:
        Object implementing ``broadcast_event(channel, payload)`` — wired
        into SpinalCord for Phase 2.
    proactive_drive:
        ProactiveDrive whose ``on_eligible`` callback is registered by the
        RemSleepDaemon.
    doubleword:
        Optional Doubleword batch-inference provider forwarded to REM epochs.
    gls:
        GovernedLoopService reference (reserved for future cross-phase hooks).
    config:
        Immutable :class:`DaemonConfig` controlling timeouts and feature flags.
    health_sensor:
        Optional RuntimeHealthSensor forwarded to Phase 1 VitalScan.
    """

    def __init__(
        self,
        oracle: Any,
        fleet: Any,
        bg_pool: Any,
        intake_router: Any,
        event_stream: Any,
        proactive_drive: Any,
        doubleword: Any,
        gls: Any,
        config: DaemonConfig,
        health_sensor: Any = None,
    ) -> None:
        # ---- injected dependencies ----------------------------------------
        self._oracle = oracle
        self._fleet = fleet
        self._bg_pool = bg_pool
        self._intake_router = intake_router
        self._event_stream = event_stream
        self._proactive_drive = proactive_drive
        self._doubleword = doubleword
        self._gls = gls
        self._config = config
        self._health_sensor = health_sensor

        # ---- internal state ------------------------------------------------
        self._vital_report: Optional[VitalReport] = None
        self._spinal: Optional[SpinalCord] = None
        self._spinal_status: Optional[SpinalStatus] = None
        self._rem: Optional[RemSleepDaemon] = None
        self._narrator: Optional[Any] = None
        self._roadmap_sensor: Optional[Any] = None
        self._synthesis_engine: Optional[Any] = None
        self._saga_orchestrator: Optional[Any] = None
        self._awakened: bool = False

        # Cached report — set on first successful awaken()
        self._awakening_report: Optional[AwakeningReport] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def awaken(self) -> AwakeningReport:
        """Execute all three awakening phases and return an :class:`AwakeningReport`.

        Idempotent — if already awakened, the cached report is returned
        immediately without re-running any phase.

        Returns
        -------
        AwakeningReport
            Populated with phase statuses regardless of individual phase
            success or failure.
        """
        if self._awakened:
            assert self._awakening_report is not None  # invariant
            logger.debug("OuroborosDaemon.awaken() called again — returning cached report")
            return self._awakening_report

        logger.info("OuroborosDaemon: beginning Zone 7.0 awakening sequence…")

        # ------------------------------------------------------------------
        # Phase 1: VitalScan
        # ------------------------------------------------------------------
        vital_report = await self._run_phase1_vital_scan()
        self._vital_report = vital_report

        # ------------------------------------------------------------------
        # Phase 2: SpinalCord wiring
        # ------------------------------------------------------------------
        spinal_status = await self._run_phase2_spinal_cord()

        # ------------------------------------------------------------------
        # Phase 3: REM Sleep Daemon (conditional)
        # ------------------------------------------------------------------
        rem_started = await self._run_phase3_rem_sleep()

        # ------------------------------------------------------------------
        # Seal and cache the report
        # ------------------------------------------------------------------
        report = AwakeningReport(
            vital_status=vital_report.status,
            vital_report=vital_report,
            spinal_status=spinal_status,
            rem_started=rem_started,
        )
        self._awakening_report = report
        self._awakened = True

        logger.info(
            "OuroborosDaemon: awakening complete — vital=%s spinal=%s rem_started=%s",
            vital_report.status.value,
            spinal_status.value,
            rem_started,
        )
        return report

    async def shutdown(self) -> None:
        """Gracefully stop all running sub-components.

        Safe to call before ``awaken()`` or multiple times.
        """
        if self._rem is not None:
            logger.info("OuroborosDaemon: stopping RemSleepDaemon…")
            try:
                await self._rem.stop()
            except Exception:
                logger.exception("OuroborosDaemon: error stopping RemSleepDaemon")
            finally:
                self._rem = None

        logger.info("OuroborosDaemon: shutdown complete")

    def health(self) -> Dict[str, Any]:
        """Return a health snapshot of the daemon and all sub-components.

        Returns
        -------
        dict with keys:
            awakened       — True once awaken() has completed
            vital_status   — VitalStatus enum value string, or None
            spinal_status  — SpinalStatus enum value string, or None
            rem            — rem health dict from RemSleepDaemon.health(), or None
        """
        rem_health: Optional[Dict[str, Any]] = None
        if self._rem is not None:
            try:
                rem_health = self._rem.health()
            except Exception:
                logger.debug("OuroborosDaemon: error reading rem health")

        return {
            "awakened": self._awakened,
            "vital_status": (
                self._vital_report.status.value if self._vital_report is not None else None
            ),
            "spinal_status": (
                self._spinal_status.value if self._spinal_status is not None else None
            ),
            "rem": rem_health,
        }

    def metrics(self) -> Dict[str, Any]:
        """Return aggregate metrics across all phases.

        Returns
        -------
        dict with keys:
            epoch_count      — cumulative REM epochs completed (0 if no REM)
            total_findings   — cumulative findings across all epochs
            total_envelopes  — cumulative intent envelopes submitted
            vital_findings   — number of findings from the most recent VitalScan
        """
        epoch_count = 0
        total_findings = 0
        total_envelopes = 0

        if self._rem is not None:
            try:
                rem_h = self._rem.health()
                epoch_count = rem_h.get("epoch_count", 0)
                total_findings = rem_h.get("total_findings", 0)
                total_envelopes = rem_h.get("total_envelopes", 0)
            except Exception:
                logger.debug("OuroborosDaemon: error reading rem metrics")

        vital_findings = (
            len(self._vital_report.findings) if self._vital_report is not None else 0
        )

        return {
            "epoch_count": epoch_count,
            "total_findings": total_findings,
            "total_envelopes": total_envelopes,
            "vital_findings": vital_findings,
        }

    # ------------------------------------------------------------------
    # Private phase runners
    # ------------------------------------------------------------------

    async def _run_phase1_vital_scan(self) -> VitalReport:
        """Phase 1: run VitalScan within the configured timeout.

        Never raises — on unexpected error returns a minimal WARN report.
        """
        logger.info(
            "OuroborosDaemon Phase 1: VitalScan (timeout=%.1fs)…",
            self._config.vital_scan_timeout_s,
        )
        try:
            scanner = VitalScan(
                oracle=self._oracle,
                health_sensor=self._health_sensor,
            )
            report = await scanner.run(timeout_s=self._config.vital_scan_timeout_s)
            logger.info(
                "OuroborosDaemon Phase 1 complete: status=%s findings=%d",
                report.status.value,
                len(report.findings),
            )
            return report
        except Exception:
            logger.exception("OuroborosDaemon Phase 1: unexpected error in VitalScan")
            # Return a minimal degraded report so Phase 2/3 can still proceed
            from backend.core.ouroboros.vital_scan import VitalFinding
            return VitalReport(
                status=VitalStatus.WARN,
                findings=[
                    VitalFinding(
                        check="vital_scan_phase_error",
                        severity="warn",
                        detail="VitalScan raised an unexpected exception during Phase 1",
                    )
                ],
                duration_s=0.0,
            )

    async def _run_phase2_spinal_cord(self) -> SpinalStatus:
        """Phase 2: create SpinalCord and wire the event transport.

        Never raises — on unexpected error returns DEGRADED.
        """
        logger.info(
            "OuroborosDaemon Phase 2: SpinalCord wiring (timeout=%.1fs)…",
            self._config.spinal_timeout_s,
        )
        try:
            self._spinal = SpinalCord(self._event_stream)
            status = await self._spinal.wire(timeout_s=self._config.spinal_timeout_s)
            self._spinal_status = status
            logger.info("OuroborosDaemon Phase 2 complete: spinal=%s", status.value)
            return status
        except Exception:
            logger.exception("OuroborosDaemon Phase 2: unexpected error in SpinalCord")
            self._spinal_status = SpinalStatus.DEGRADED
            return SpinalStatus.DEGRADED

    async def _run_phase3_rem_sleep(self) -> bool:
        """Phase 3: create and start RemSleepDaemon if config permits.

        Returns True if the daemon was successfully started, False otherwise.
        Never raises.
        """
        if not self._config.rem_enabled:
            logger.info(
                "OuroborosDaemon Phase 3: REM Sleep disabled (rem_enabled=False) — skipping"
            )
            return False

        # SpinalCord must exist so RemSleepDaemon can await its gate
        if self._spinal is None:
            logger.warning(
                "OuroborosDaemon Phase 3: SpinalCord not wired — skipping REM Sleep"
            )
            return False

        logger.info("OuroborosDaemon Phase 3: starting RemSleepDaemon…")
        try:
            architect = None
            if self._config.architect_enabled:
                try:
                    from backend.core.ouroboros.architect.reasoning_agent import (
                        ArchitectureReasoningAgent,
                        AgentConfig,
                    )
                    architect = ArchitectureReasoningAgent(
                        oracle=self._oracle,
                        doubleword=self._doubleword,
                        config=AgentConfig(max_steps=self._config.architect_max_steps),
                    )
                    logger.info("OuroborosDaemon Phase 3: ArchitectureReasoningAgent initialised")
                except Exception as exc:
                    logger.warning(
                        "[OuroborosDaemon] Architect init failed: %s", exc
                    )

            # DaemonNarrator (voice transparency)
            self._narrator = None
            if self._config.narrator_enabled:
                try:
                    from backend.core.ouroboros.daemon_narrator import DaemonNarrator
                    # Import safe_say — may fail if voice orchestrator not initialized
                    try:
                        from backend.core.supervisor.unified_voice_orchestrator import safe_say
                        say_fn = safe_say
                    except ImportError:
                        say_fn = None

                    if say_fn is not None:
                        self._narrator = DaemonNarrator(
                            say_fn=say_fn,
                            rate_limit_s=self._config.narrator_rate_limit_s,
                            voice=self._config.narrator_voice,
                        )
                        logger.info("[OuroborosDaemon] DaemonNarrator enabled (voice=%s)", self._config.narrator_voice)
                except Exception as exc:
                    logger.warning("[OuroborosDaemon] DaemonNarrator init failed: %s", exc)

            # ---- RoadmapSensor (Clock 1) + FeatureSynthesisEngine (Clock 2) ----
            if self._config.roadmap_enabled:
                try:
                    from backend.core.ouroboros.roadmap.sensor import (
                        RoadmapSensor,
                        RoadmapSensorConfig,
                    )
                    from backend.core.ouroboros.roadmap.synthesis_engine import (
                        FeatureSynthesisEngine,
                        SynthesisConfig,
                    )
                    from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache

                    sensor_config = RoadmapSensorConfig(
                        p1_enabled=self._config.roadmap_p1_enabled,
                        p1_commit_limit=self._config.roadmap_p1_commit_limit,
                        p1_days=self._config.roadmap_p1_days,
                        p2_enabled=self._config.roadmap_p2_enabled,
                        p3_enabled=self._config.roadmap_p3_enabled,
                        refresh_interval_s=self._config.roadmap_refresh_s,
                    )

                    cache = HypothesisCache(cache_dir=Path(_HYPOTHESIS_CACHE_DIR))

                    synthesis_config = SynthesisConfig(
                        min_interval_s=self._config.synthesis_min_interval_s,
                        ttl_s=self._config.synthesis_ttl_s,
                        prompt_version=self._config.synthesis_prompt_version,
                    )

                    self._synthesis_engine = FeatureSynthesisEngine(
                        oracle=self._oracle,
                        doubleword=self._doubleword,
                        cache=cache,
                        config=synthesis_config,
                        narrator=self._narrator,
                    )

                    self._roadmap_sensor = RoadmapSensor(
                        repo_root=Path("."),
                        config=sensor_config,
                        on_snapshot_changed=lambda snapshot: asyncio.ensure_future(
                            self._synthesis_engine.trigger(snapshot)
                        ),
                    )

                    # Initial snapshot refresh
                    self._roadmap_sensor.refresh()
                    logger.info("[OuroborosDaemon] RoadmapSensor + SynthesisEngine wired")

                except Exception as exc:
                    logger.warning("[OuroborosDaemon] Roadmap/Synthesis init failed: %s", exc)

            # ---- SagaOrchestrator ----
            if self._config.architect_enabled:
                try:
                    from backend.core.ouroboros.architect.saga_orchestrator import SagaOrchestrator
                    from backend.core.ouroboros.architect.plan_store import PlanStore
                    from backend.core.ouroboros.architect.acceptance_runner import AcceptanceRunner

                    plan_store = PlanStore(
                        store_dir=Path(os.path.expanduser("~/.jarvis/ouroboros/plans"))
                    )
                    acceptance_runner = AcceptanceRunner()

                    self._saga_orchestrator = SagaOrchestrator(
                        plan_store=plan_store,
                        intake_router=self._intake_router,
                        acceptance_runner=acceptance_runner,
                        saga_dir=Path(os.path.expanduser("~/.jarvis/ouroboros/sagas")),
                        spinal_cord=self._spinal,
                        narrator=self._narrator,
                    )
                    logger.info("[OuroborosDaemon] SagaOrchestrator wired")

                except Exception as exc:
                    logger.warning("[OuroborosDaemon] SagaOrchestrator init failed: %s", exc)

            self._rem = RemSleepDaemon(
                oracle=self._oracle,
                fleet=self._fleet,
                spinal_cord=self._spinal,
                intake_router=self._intake_router,
                proactive_drive=self._proactive_drive,
                doubleword=self._doubleword,
                config=self._config,
                hypothesis_cache_dir=_HYPOTHESIS_CACHE_DIR,
                architect=architect,
                narrator=self._narrator,
                saga_orchestrator=self._saga_orchestrator,
            )
            await self._rem.start()
            logger.info("OuroborosDaemon Phase 3 complete: RemSleepDaemon started")
            return True
        except Exception:
            logger.exception("OuroborosDaemon Phase 3: unexpected error starting RemSleepDaemon")
            self._rem = None
            return False
