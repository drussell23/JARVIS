"""ProactiveDriveService — async lifecycle wrapper for the Proactive Autonomous Drive."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from backend.core.topology.hardware_env import HardwareEnvironmentState
from backend.core.topology.idle_verifier import LittlesLawVerifier, ProactiveDrive
from backend.core.topology.curiosity_engine import CuriosityEngine
from backend.core.topology.topology_map import TopologyMap
from backend.core.topology.telemetry_schemas import (
    HARDWARE_SCHEMA,
    PROACTIVE_DRIVE_SCHEMA,
    build_hardware_payload,
    build_drive_tick_payload,
)

logger = logging.getLogger(__name__)


class ServiceState(str, Enum):
    INACTIVE = "inactive"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class ProactiveDriveConfig:
    tick_interval_seconds: float = 10.0
    max_queue_depth: int = 1000
    repos: tuple = ("jarvis", "prime", "reactor")

    @classmethod
    def from_env(cls) -> ProactiveDriveConfig:
        return cls(
            tick_interval_seconds=float(
                os.environ.get("JARVIS_PROACTIVE_TICK_INTERVAL", "10.0")
            ),
            max_queue_depth=int(
                os.environ.get("JARVIS_PROACTIVE_MAX_QUEUE_DEPTH", "1000")
            ),
        )


class ProactiveDriveService:
    """Async service wrapping the Proactive Autonomous Drive.

    Lifecycle: INACTIVE -> STARTING -> ACTIVE -> STOPPING -> INACTIVE
    Discovers hardware at start, creates verifiers for each repo,
    runs a tick loop that checks idle state and emits telemetry.
    """

    def __init__(
        self,
        config: ProactiveDriveConfig,
        telemetry_bus: Any = None,
        topology: Optional[TopologyMap] = None,
        prime_client: Any = None,
        repo_registry: Any = None,
        comm_protocol: Any = None,
        web_tool: Any = None,
    ) -> None:
        self._config = config
        self._bus = telemetry_bus
        self._topology = topology or TopologyMap()
        self._prime_client = prime_client
        self._repo_registry = repo_registry
        self._comm_protocol = comm_protocol
        self._web_tool = web_tool
        self._state = ServiceState.INACTIVE
        self._hardware: Optional[HardwareEnvironmentState] = None
        self._verifiers: Dict[str, LittlesLawVerifier] = {}
        self._drive: Optional[ProactiveDrive] = None
        self._engine: Optional[CuriosityEngine] = None
        self._tick_task: Optional[asyncio.Task] = None

    @property
    def state(self) -> ServiceState:
        return self._state

    @property
    def hardware(self) -> Optional[HardwareEnvironmentState]:
        return self._hardware

    @property
    def verifiers(self) -> Dict[str, LittlesLawVerifier]:
        return self._verifiers

    @property
    def drive(self) -> Optional[ProactiveDrive]:
        return self._drive

    async def start(self) -> None:
        if self._state in (ServiceState.ACTIVE, ServiceState.STARTING):
            return
        self._state = ServiceState.STARTING
        try:
            self._hardware = HardwareEnvironmentState.discover()
            logger.info(
                "[ProactiveDrive] Hardware: %s, %d cores, %dMB RAM",
                self._hardware.compute_tier.value,
                self._hardware.cpu_logical_cores,
                self._hardware.ram_total_mb,
            )
            self._emit_hardware()

            for repo in self._config.repos:
                self._verifiers[repo] = LittlesLawVerifier(
                    repo, self._config.max_queue_depth
                )

            self._drive = ProactiveDrive(
                self._verifiers.get(
                    "jarvis",
                    LittlesLawVerifier("jarvis", self._config.max_queue_depth),
                ),
                self._verifiers.get(
                    "prime",
                    LittlesLawVerifier("prime", self._config.max_queue_depth),
                ),
                self._verifiers.get(
                    "reactor",
                    LittlesLawVerifier("reactor", self._config.max_queue_depth),
                ),
            )
            self._engine = CuriosityEngine(self._topology, self._hardware)

            self._tick_task = asyncio.create_task(
                self._tick_loop(), name="proactive_drive_tick"
            )

            self._state = ServiceState.ACTIVE
            logger.info("[ProactiveDrive] Started: state=%s", self._state.value)
        except Exception as exc:
            self._state = ServiceState.FAILED
            logger.error("[ProactiveDrive] Start failed: %s", exc, exc_info=True)
            raise

    async def stop(self) -> None:
        if self._state == ServiceState.INACTIVE:
            return
        self._state = ServiceState.STOPPING

        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

        self._verifiers.clear()
        self._drive = None
        self._engine = None
        self._state = ServiceState.INACTIVE
        logger.info("[ProactiveDrive] Stopped")

    def record_sample(self, repo: str, depth: int, latency_ms: float) -> None:
        """Called by GLS at submit entry/exit to feed queue telemetry."""
        verifier = self._verifiers.get(repo)
        if verifier:
            verifier.record(depth, latency_ms)

    def health(self) -> Dict[str, Any]:
        return {
            "state": self._state.value,
            "drive_state": self._drive.state if self._drive else "N/A",
            "hardware_tier": (
                self._hardware.compute_tier.value if self._hardware else "unknown"
            ),
            "verifier_samples": {
                repo: len(v._samples) for repo, v in self._verifiers.items()
            },
        }

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.tick_interval_seconds)
            if self._drive is None:
                continue
            try:
                state, reason = self._drive.tick()
                self._emit_drive_tick(state, reason)

                if state == "ELIGIBLE" and self._engine:
                    target = self._engine.select_target()
                    if target:
                        logger.info(
                            "[ProactiveDrive] Target selected: %s (UCB=%.4f, H=%.3f)",
                            target.capability.name,
                            target.ucb_score,
                            target.entropy_score,
                        )
                        self._emit_drive_tick(
                            "EXPLORING",
                            reason,
                            target_name=target.capability.name,
                            target_domain=target.capability.domain,
                        )
                        self._drive.begin_exploration()
                        await self._run_exploration(target)
                        self._drive.end_exploration()
            except Exception as exc:
                logger.warning(
                    "[ProactiveDrive] Tick error: %s", exc, exc_info=True
                )

    async def _run_exploration(self, target: Any) -> None:
        """Spawn ExplorationSentinel with strategy, run, log outcome."""
        from backend.core.topology.sentinel import ExplorationSentinel
        from backend.core.topology.exploration_strategy import (
            ExplorationConfig,
            ExplorationStrategy,
        )
        from backend.core.topology.architectural_proposal import (
            ArchitecturalProposal,
            ShadowTestResult,
        )

        scratch = f".jarvis/ouroboros/exploration_sandbox/{target.capability.name}/"
        strategy = ExplorationStrategy(
            config=ExplorationConfig.from_env(),
            scratch_path=scratch,
            web_tool=self._web_tool,
            prime_client=self._prime_client,
            repo_registry=self._repo_registry,
            comm_protocol=self._comm_protocol,
        )

        try:
            async with ExplorationSentinel(
                target=target,
                hardware=self._hardware,
                strategy=strategy,
            ) as sentinel:
                outcome = await sentinel.run()

            logger.info(
                "[ProactiveDrive] Exploration complete: %s -> %s (%.1fs)",
                target.capability.name,
                outcome.dead_end_class.value,
                outcome.elapsed_seconds,
            )

            # On success: build ArchitecturalProposal
            if outcome.dead_end_class.value == "clean_success" and outcome.partial_findings:
                import json as _json
                try:
                    findings = _json.loads(outcome.partial_findings)
                    generated = findings.get("generated_files", [])
                    test_passed = findings.get("test_passed", False)

                    # Build shadow test results from findings
                    shadow_results = []
                    if test_passed is not None:
                        shadow_results.append(ShadowTestResult(
                            test_name="exploration_tests",
                            passed=bool(test_passed),
                            duration_ms=outcome.elapsed_seconds * 1000,
                            output=findings.get("explanation", ""),
                        ))

                    proposal = ArchitecturalProposal.create(
                        target=target,
                        hardware=self._hardware,
                        generated_files=[f"{scratch}{f}" for f in generated],
                        shadow_results=shadow_results,
                        sentinel_elapsed=outcome.elapsed_seconds,
                    )
                    logger.info(
                        "[ProactiveDrive] Proposal created: %s",
                        proposal.summary(),
                    )
                except (_json.JSONDecodeError, Exception) as e:
                    logger.warning("[ProactiveDrive] Proposal creation failed: %s", e)
            else:
                logger.info(
                    "[ProactiveDrive] Exploration did not succeed: %s — %s",
                    outcome.dead_end_class.value,
                    outcome.partial_findings[:200] if outcome.partial_findings else "no findings",
                )

            # Increment exploration attempts on the topology node
            node = self._topology.nodes.get(target.capability.name)
            if node is not None:
                node.exploration_attempts += 1

        except Exception as exc:
            logger.error(
                "[ProactiveDrive] Sentinel dispatch failed for %s: %s",
                target.capability.name, exc, exc_info=True,
            )

    def _emit_hardware(self) -> None:
        if self._bus is None or self._hardware is None:
            return
        from backend.core.telemetry_contract import TelemetryEnvelope

        envelope = TelemetryEnvelope.create(
            event_schema=HARDWARE_SCHEMA,
            source="proactive_drive_service",
            trace_id="boot",
            span_id="hardware_discovery",
            partition_key="lifecycle",
            payload=build_hardware_payload(self._hardware),
        )
        self._bus.emit(envelope)

    def _emit_drive_tick(
        self,
        state: str,
        reason: str,
        target_name: Optional[str] = None,
        target_domain: Optional[str] = None,
    ) -> None:
        if self._bus is None:
            return
        from backend.core.telemetry_contract import TelemetryEnvelope

        envelope = TelemetryEnvelope.create(
            event_schema=PROACTIVE_DRIVE_SCHEMA,
            source="proactive_drive_service",
            trace_id="proactive",
            span_id="tick",
            partition_key="reasoning",
            payload=build_drive_tick_payload(state, reason, target_name, target_domain),
        )
        self._bus.emit(envelope)
