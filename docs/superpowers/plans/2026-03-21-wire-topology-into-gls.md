# Wire Topology Package into GLS (Milestone 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Proactive Autonomous Drive (Topology package) into the supervisor and GLS, transforming Ouroboros from reactive (Level 2) to proactive (Level 3) — the system identifies its own capability gaps and proposes exploration targets.

**Architecture:** Three integration layers: (1) A new `ProactiveDriveService` in `backend/core/topology/` that wraps HardwareEnvironmentState, LittlesLawVerifier, ProactiveDrive, and CuriosityEngine with proper async lifecycle. (2) Two hook points in `GovernedLoopService.submit()` that feed queue telemetry to the LittlesLawVerifier. (3) A new Zone 6.12 in `unified_supervisor.py` that starts the service, gated by `JARVIS_PROACTIVE_DRIVE_ENABLED` env var.

**Tech Stack:** Python 3, `asyncio`, `psutil` (already in requirements), `pytest`

**Spec:** `docs/ouroboros-vs-claude-code-gap-analysis.md` — Part 11, Section 11.3 (Milestone 1)

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/core/topology/proactive_drive_service.py` | New: Service wrapper with async start/stop lifecycle, tick loop, hardware discovery, TelemetryBus emission |
| `backend/core/topology/telemetry_schemas.py` | New: Event schema constants and payload builders for `lifecycle.hardware@1.0.0` and `reasoning.proactive_drive@1.0.0` |
| `backend/core/ouroboros/governance/governed_loop_service.py` | Modify: Add LittlesLawVerifier hook at submit() entry/exit (lines ~1107 and ~1637) |
| `unified_supervisor.py` | Modify: Add Zone 6.12 startup block (after Zone 6.11, before closing except at ~86803) |
| `backend/core/telemetry_contract.py` | Modify: Add two new event schemas to V1_EVENT_SCHEMAS list |
| `tests/core/topology/test_proactive_drive_service.py` | New: Tests for service lifecycle, tick behavior, telemetry emission |
| `tests/core/topology/test_gls_verifier_hook.py` | New: Tests for LittlesLawVerifier integration with GLS submit path |

---

### Task 1: Telemetry Schema Registration

**Files:**
- Create: `backend/core/topology/telemetry_schemas.py`
- Modify: `backend/core/telemetry_contract.py` (lines 29-39 — V1_EVENT_SCHEMAS list)
- Create: `tests/core/topology/test_telemetry_schemas.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_telemetry_schemas.py`:

```python
"""Tests for topology telemetry schema registration."""
from backend.core.telemetry_contract import V1_EVENT_SCHEMAS, TelemetryEnvelope
from backend.core.topology.telemetry_schemas import (
    HARDWARE_SCHEMA,
    PROACTIVE_DRIVE_SCHEMA,
    build_hardware_payload,
    build_drive_tick_payload,
)
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState


class TestSchemaRegistration:
    def test_hardware_schema_in_v1_list(self):
        assert HARDWARE_SCHEMA in V1_EVENT_SCHEMAS

    def test_proactive_drive_schema_in_v1_list(self):
        assert PROACTIVE_DRIVE_SCHEMA in V1_EVENT_SCHEMAS

    def test_schema_format(self):
        assert HARDWARE_SCHEMA == "lifecycle.hardware@1.0.0"
        assert PROACTIVE_DRIVE_SCHEMA == "reasoning.proactive_drive@1.0.0"


class TestPayloadBuilders:
    def test_build_hardware_payload(self):
        hw = HardwareEnvironmentState(
            os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
            ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
            hostname="test", python_version="3.11.0",
            max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
        )
        payload = build_hardware_payload(hw)
        assert payload["os_family"] == "darwin"
        assert payload["compute_tier"] == "local_cpu"
        assert payload["cpu_logical_cores"] == 8
        assert payload["gpu_name"] is None

    def test_build_drive_tick_payload(self):
        payload = build_drive_tick_payload(
            state="MEASURING",
            reason="jarvis: L=0.142 < threshold=30.000",
            target_name=None,
            target_domain=None,
        )
        assert payload["state"] == "MEASURING"
        assert payload["reason"] == "jarvis: L=0.142 < threshold=30.000"
        assert payload["target_name"] is None

    def test_build_drive_tick_with_target(self):
        payload = build_drive_tick_payload(
            state="EXPLORING",
            reason="Eligible",
            target_name="parse_parquet",
            target_domain="data_io",
        )
        assert payload["target_name"] == "parse_parquet"
        assert payload["target_domain"] == "data_io"

    def test_hardware_envelope_creates(self):
        hw = HardwareEnvironmentState(
            os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
            ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
            hostname="test", python_version="3.11.0",
            max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
        )
        payload = build_hardware_payload(hw)
        envelope = TelemetryEnvelope.create(
            event_schema=HARDWARE_SCHEMA,
            source="proactive_drive_service",
            trace_id="test-trace",
            span_id="test-span",
            partition_key="lifecycle",
            payload=payload,
        )
        assert envelope.event_schema == HARDWARE_SCHEMA
        assert envelope.payload["compute_tier"] == "local_cpu"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_telemetry_schemas.py -v
```

Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Create telemetry_schemas.py**

```python
"""Telemetry event schemas and payload builders for the Topology package."""
from __future__ import annotations

from typing import Any, Dict, Optional

from backend.core.topology.hardware_env import HardwareEnvironmentState

HARDWARE_SCHEMA = "lifecycle.hardware@1.0.0"
PROACTIVE_DRIVE_SCHEMA = "reasoning.proactive_drive@1.0.0"


def build_hardware_payload(hw: HardwareEnvironmentState) -> Dict[str, Any]:
    """Build TelemetryEnvelope payload from HardwareEnvironmentState."""
    return {
        "os_family": hw.os_family,
        "cpu_logical_cores": hw.cpu_logical_cores,
        "ram_total_mb": hw.ram_total_mb,
        "ram_available_mb": hw.ram_available_mb,
        "compute_tier": hw.compute_tier.value,
        "gpu_name": hw.gpu.name if hw.gpu else None,
        "gpu_vram_total_mb": hw.gpu.vram_total_mb if hw.gpu else None,
        "gpu_vram_free_mb": hw.gpu.vram_free_mb if hw.gpu else None,
        "hostname": hw.hostname,
        "python_version": hw.python_version,
        "max_parallel_inference_tasks": hw.max_parallel_inference_tasks,
        "max_shadow_harness_workers": hw.max_shadow_harness_workers,
    }


def build_drive_tick_payload(
    state: str,
    reason: str,
    target_name: Optional[str] = None,
    target_domain: Optional[str] = None,
) -> Dict[str, Any]:
    """Build TelemetryEnvelope payload from ProactiveDrive.tick() result."""
    return {
        "state": state,
        "reason": reason,
        "target_name": target_name,
        "target_domain": target_domain,
    }
```

- [ ] **Step 4: Register schemas in telemetry_contract.py**

Add to `V1_EVENT_SCHEMAS` list at `backend/core/telemetry_contract.py:29-39`:

```python
V1_EVENT_SCHEMAS: List[str] = [
    "lifecycle.transition@1.0.0",
    "lifecycle.health@1.0.0",
    "lifecycle.hardware@1.0.0",          # NEW
    "reasoning.activation@1.0.0",
    "reasoning.decision@1.0.0",
    "reasoning.proactive_drive@1.0.0",   # NEW
    "scheduler.graph_state@1.0.0",
    "scheduler.unit_state@1.0.0",
    "recovery.attempt@1.0.0",
    "fault.raised@1.0.0",
    "fault.resolved@1.0.0",
]
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_telemetry_schemas.py -v
```

Expected: all PASS.

- [ ] **Step 6: Run existing TUI tests to confirm no regression**

```bash
python3 -m pytest tests/core/test_tui_panels.py -v --tb=short
```

Expected: 30+ PASS (TUI bus_consumer routes by schema prefix, not exact match — new schemas won't break it).

- [ ] **Step 7: Commit**

```bash
git add backend/core/topology/telemetry_schemas.py backend/core/telemetry_contract.py tests/core/topology/test_telemetry_schemas.py
git commit -m "feat(topology): register lifecycle.hardware + reasoning.proactive_drive telemetry schemas

New event schemas for hardware discovery and proactive drive state.
Payload builders for TelemetryEnvelope creation. Added to
V1_EVENT_SCHEMAS for bus routing."
```

---

### Task 2: ProactiveDriveService — Async Lifecycle Wrapper

**Files:**
- Create: `backend/core/topology/proactive_drive_service.py`
- Create: `tests/core/topology/test_proactive_drive_service.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_proactive_drive_service.py`:

```python
"""Tests for ProactiveDriveService async lifecycle."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.topology.proactive_drive_service import (
    ProactiveDriveService,
    ProactiveDriveConfig,
    ServiceState,
)
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState
from backend.core.topology.idle_verifier import LittlesLawVerifier
from backend.core.topology.topology_map import CapabilityNode, TopologyMap


def _make_hardware():
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _make_topology():
    topo = TopologyMap()
    topo.register(CapabilityNode(name="route_ollama", domain="llm", repo_owner="jarvis", active=True))
    topo.register(CapabilityNode(name="route_claude", domain="llm", repo_owner="jarvis", active=False))
    return topo


class TestProactiveDriveConfig:
    def test_from_env_defaults(self):
        config = ProactiveDriveConfig.from_env()
        assert config.tick_interval_seconds == 10.0
        assert config.max_queue_depth == 1000

    def test_from_env_override(self):
        with patch.dict("os.environ", {"JARVIS_PROACTIVE_TICK_INTERVAL": "5.0"}):
            config = ProactiveDriveConfig.from_env()
            assert config.tick_interval_seconds == 5.0


class TestServiceState:
    def test_enum_values(self):
        assert ServiceState.INACTIVE.value == "inactive"
        assert ServiceState.ACTIVE.value == "active"
        assert ServiceState.FAILED.value == "failed"


class TestProactiveDriveService:
    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        assert service.state == ServiceState.INACTIVE
        await service.start()
        assert service.state == ServiceState.ACTIVE
        assert service.hardware is not None
        await service.stop()
        assert service.state == ServiceState.INACTIVE

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        await service.start()  # second call is no-op
        assert service.state == ServiceState.ACTIVE
        await service.stop()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.stop()  # stop without start
        assert service.state == ServiceState.INACTIVE

    @pytest.mark.asyncio
    async def test_hardware_discovered_at_start(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        assert service.hardware is not None
        assert service.hardware.cpu_logical_cores >= 1
        await service.stop()

    @pytest.mark.asyncio
    async def test_verifier_created_for_jarvis(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        assert "jarvis" in service.verifiers
        assert isinstance(service.verifiers["jarvis"], LittlesLawVerifier)
        await service.stop()

    @pytest.mark.asyncio
    async def test_record_sample_feeds_verifier(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        service.record_sample("jarvis", depth=5, latency_ms=100.0)
        assert len(service.verifiers["jarvis"]._samples) == 1
        await service.stop()

    @pytest.mark.asyncio
    async def test_tick_loop_runs(self):
        config = ProactiveDriveConfig(tick_interval_seconds=0.05)
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        await asyncio.sleep(0.15)  # let a few ticks run
        assert service.drive.state in ("REACTIVE", "MEASURING")
        await service.stop()

    @pytest.mark.asyncio
    async def test_health_returns_dict(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        h = service.health()
        assert "state" in h
        assert "drive_state" in h
        assert "hardware_tier" in h
        await service.stop()

    @pytest.mark.asyncio
    async def test_telemetry_emitted_on_tick(self):
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock()
        config = ProactiveDriveConfig(tick_interval_seconds=0.05)
        service = ProactiveDriveService(config=config, telemetry_bus=mock_bus)
        await service.start()
        await asyncio.sleep(0.15)
        await service.stop()
        # Bus should have been called at least once (hardware at start + drive ticks)
        assert mock_bus.emit.call_count >= 1
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_proactive_drive_service.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement ProactiveDriveService**

Create `backend/core/topology/proactive_drive_service.py`:

```python
"""ProactiveDriveService — async lifecycle wrapper for the Proactive Autonomous Drive."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
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
    ) -> None:
        self._config = config
        self._bus = telemetry_bus
        self._topology = topology or TopologyMap()
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
            # Step 1: Discover hardware
            self._hardware = HardwareEnvironmentState.discover()
            logger.info(
                "[ProactiveDrive] Hardware: %s, %d cores, %dMB RAM",
                self._hardware.compute_tier.value,
                self._hardware.cpu_logical_cores,
                self._hardware.ram_total_mb,
            )

            # Step 2: Emit hardware envelope
            self._emit_hardware()

            # Step 3: Create verifiers for each repo
            for repo in self._config.repos:
                self._verifiers[repo] = LittlesLawVerifier(
                    repo, self._config.max_queue_depth
                )

            # Step 4: Create drive and engine
            self._drive = ProactiveDrive(
                self._verifiers.get("jarvis", LittlesLawVerifier("jarvis", self._config.max_queue_depth)),
                self._verifiers.get("prime", LittlesLawVerifier("prime", self._config.max_queue_depth)),
                self._verifiers.get("reactor", LittlesLawVerifier("reactor", self._config.max_queue_depth)),
            )
            self._engine = CuriosityEngine(self._topology, self._hardware)

            # Step 5: Start tick loop
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
            "hardware_tier": self._hardware.compute_tier.value if self._hardware else "unknown",
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
                            "EXPLORING", reason,
                            target_name=target.capability.name,
                            target_domain=target.capability.domain,
                        )
                        # NOTE: Sentinel dispatch will be added in a future milestone.
                        # For now, log the target and transition back to COOLDOWN
                        # to prevent repeated selection.
                        self._drive.begin_exploration()
                        # Placeholder: immediate end (no actual sentinel yet)
                        self._drive.end_exploration()
                        logger.info(
                            "[ProactiveDrive] Exploration cycle complete (sentinel not yet wired). Cooldown."
                        )
            except Exception as exc:
                logger.warning("[ProactiveDrive] Tick error: %s", exc, exc_info=True)

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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_proactive_drive_service.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/proactive_drive_service.py tests/core/topology/test_proactive_drive_service.py
git commit -m "feat(topology): add ProactiveDriveService async lifecycle wrapper

Wraps HardwareEnvironmentState discovery, LittlesLawVerifier creation,
ProactiveDrive tick loop, and CuriosityEngine target selection with
proper async start/stop. Emits lifecycle.hardware@1.0.0 at boot and
reasoning.proactive_drive@1.0.0 on each tick."
```

---

### Task 3: Hook LittlesLawVerifier into GLS submit()

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (lines ~1107 and ~1637)
- Create: `tests/core/topology/test_gls_verifier_hook.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_gls_verifier_hook.py`:

```python
"""Tests for LittlesLawVerifier hook in GLS submit path."""
import pytest

from backend.core.topology.idle_verifier import LittlesLawVerifier


class TestGLSVerifierHookContract:
    """Test the contract that GLS should call when it has a proactive drive service.

    These tests verify the hook behavior in isolation — they don't require
    a full GLS instance. The actual integration is verified by the import
    chain test below.
    """

    def test_record_accepts_depth_and_latency(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=1000)
        v.record(depth=5, processing_latency_ms=100.0)
        assert len(v._samples) == 1

    def test_record_depth_from_active_ops_set(self):
        """GLS uses len(self._active_ops) as queue depth."""
        v = LittlesLawVerifier("jarvis", max_queue_depth=1000)
        active_ops = {"op-1", "op-2", "op-3"}
        v.record(depth=len(active_ops), processing_latency_ms=50.0)
        assert v._samples[0].depth == 3

    def test_proactive_drive_service_record_sample_method(self):
        """ProactiveDriveService.record_sample() is the GLS hook entry point."""
        from backend.core.topology.proactive_drive_service import (
            ProactiveDriveConfig,
            ProactiveDriveService,
        )
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        # Manually create verifier (normally done in start())
        service._verifiers["jarvis"] = LittlesLawVerifier("jarvis", 1000)
        service.record_sample("jarvis", depth=5, latency_ms=100.0)
        assert len(service._verifiers["jarvis"]._samples) == 1

    def test_record_sample_unknown_repo_is_noop(self):
        from backend.core.topology.proactive_drive_service import (
            ProactiveDriveConfig,
            ProactiveDriveService,
        )
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        service.record_sample("nonexistent", depth=5, latency_ms=100.0)
        # Should not raise
```

- [ ] **Step 2: Run tests to confirm they pass** (these test the contract, not the wiring)

```bash
python3 -m pytest tests/core/topology/test_gls_verifier_hook.py -v
```

Expected: all PASS (these test the hook contract in isolation).

- [ ] **Step 3: Add hook to GLS submit()**

In `backend/core/ouroboros/governance/governed_loop_service.py`, add the hook at two points.

**At line ~1107** (after `self._active_ops.add(dedupe_key)`), add:

```python
        self._active_ops.add(dedupe_key)
        # --- Proactive Drive telemetry hook ---
        _proactive_svc = getattr(self, "_proactive_drive_service", None)
        if _proactive_svc is not None:
            _proactive_svc.record_sample(
                "jarvis", depth=len(self._active_ops), latency_ms=0.0
            )
```

**At line ~1637** (in the `finally` block, after `self._active_ops.discard(dedupe_key)`), add:

```python
            self._active_ops.discard(dedupe_key)
            # --- Proactive Drive telemetry hook (completion) ---
            _proactive_svc = getattr(self, "_proactive_drive_service", None)
            if _proactive_svc is not None:
                _elapsed_ms = (time.monotonic() - _submit_start_mono) * 1000.0 if '_submit_start_mono' in dir() else 0.0
                _proactive_svc.record_sample(
                    "jarvis", depth=len(self._active_ops), latency_ms=_elapsed_ms
                )
```

Also add `_submit_start_mono = time.monotonic()` at the top of `submit()` (around line ~1036) to capture the start time:

```python
    async def submit(self, ctx, trigger_source="unknown") -> OperationResult:
        _submit_start_mono = time.monotonic()
```

- [ ] **Step 4: Run GLS-related tests to confirm no regression**

```bash
python3 -m pytest tests/core/topology/ -v --tb=short
python3 -m pytest tests/ -k "governance" --tb=short -q 2>&1 | tail -5
```

Expected: all topology tests pass. Governance tests should not regress (the hook is guarded by `getattr` returning None).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py tests/core/topology/test_gls_verifier_hook.py
git commit -m "feat(topology): hook LittlesLawVerifier into GLS submit() path

Records queue depth and processing latency at operation entry and
exit in governed_loop_service.py. Uses getattr guard so the hook
is a no-op when ProactiveDriveService is not wired."
```

---

### Task 4: Zone 6.12 in unified_supervisor.py

**Files:**
- Modify: `unified_supervisor.py` (after Zone 6.11 block, before line ~86803)

- [ ] **Step 1: Read current Zone 6.11 end to confirm insertion point**

```bash
grep -n "Zone 6.11 trinity consciousness SKIPPED\|Zone 6.11 trinity consciousness:" unified_supervisor.py
```

Confirm insertion point is after the Zone 6.11 except block.

- [ ] **Step 2: Add self._proactive_drive attribute to __init__**

Find the line `self._iteration_service: Optional[Any] = None  # Zone 6.10` (around line 67021) and add after it:

```python
        self._proactive_drive: Optional[Any] = None  # Zone 6.12: ProactiveDriveService
```

- [ ] **Step 3: Add Zone 6.12 block**

After the Zone 6.11 closing `except` block (after `"[Kernel] Zone 6.11 trinity consciousness SKIPPED: %s"` line ~86801), insert:

```python
                            # ---- Zone 6.12: Proactive Autonomous Drive ----
                            if (
                                os.environ.get("JARVIS_PROACTIVE_DRIVE_ENABLED", "false").lower()
                                in ("true", "1", "yes")
                                and self._governed_loop is not None
                            ):
                                try:
                                    from backend.core.topology.proactive_drive_service import (
                                        ProactiveDriveConfig,
                                        ProactiveDriveService,
                                    )
                                    from backend.core.telemetry_contract import get_telemetry_bus

                                    _pd_config = ProactiveDriveConfig.from_env()
                                    _pd_bus = get_telemetry_bus()

                                    self._proactive_drive = ProactiveDriveService(
                                        config=_pd_config,
                                        telemetry_bus=_pd_bus,
                                    )
                                    await asyncio.wait_for(
                                        asyncio.shield(self._proactive_drive.start()),
                                        timeout=10.0,
                                    )
                                    # Wire the GLS hook so submit() feeds queue telemetry
                                    self._governed_loop._proactive_drive_service = self._proactive_drive
                                    self.logger.info(
                                        "[Kernel] Zone 6.12 proactive drive: %s",
                                        self._proactive_drive.health(),
                                    )
                                except (asyncio.CancelledError, KeyboardInterrupt):
                                    raise
                                except BaseException as exc:
                                    self._proactive_drive = None
                                    self.logger.warning(
                                        "[Kernel] Zone 6.12 proactive drive SKIPPED: %s", exc,
                                    )
```

- [ ] **Step 4: Add shutdown in the supervisor's stop sequence**

Find where `self._iteration_service` is stopped in the shutdown path and add after it:

```python
                if self._proactive_drive is not None:
                    try:
                        await asyncio.wait_for(self._proactive_drive.stop(), timeout=5.0)
                    except Exception:
                        pass
                    self._proactive_drive = None
```

- [ ] **Step 5: Verify import chain**

```bash
python3 -c "
from backend.core.topology.proactive_drive_service import ProactiveDriveService, ProactiveDriveConfig
print('ProactiveDriveService imports OK')
"
```

Expected: `ProactiveDriveService imports OK`

- [ ] **Step 6: Run full topology test suite**

```bash
python3 -m pytest tests/core/topology/ -v --tb=short
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(supervisor): add Zone 6.12 Proactive Autonomous Drive

Gated by JARVIS_PROACTIVE_DRIVE_ENABLED env var. Starts
ProactiveDriveService at boot, wires GLS submit() hook for
queue telemetry. Hardware discovered dynamically, drive tick
emits reasoning.proactive_drive@1.0.0 every 10 seconds."
```

---

### Task 5: Full Regression + Documentation

- [ ] **Step 1: Run complete topology test suite**

```bash
python3 -m pytest tests/core/topology/ -v --tb=short
```

Expected: all tests PASS across all test files.

- [ ] **Step 2: Run TUI test suite (telemetry schema change)**

```bash
python3 -m pytest tests/core/test_tui_panels.py -v --tb=short
```

Expected: all PASS.

- [ ] **Step 3: Verify end-to-end import chain**

```bash
python3 -c "
from backend.core.topology import (
    ComputeTier, GPUState, HardwareEnvironmentState,
    CapabilityNode, TopologyMap,
    LittlesLawVerifier, ProactiveDrive, QueueSample,
    CuriosityEngine, CuriosityTarget,
    PIDController, ResourceGovernor,
    DeadEndClass, DeadEndClassifier, ExplorationSentinel, SentinelOutcome,
    ArchitecturalProposal, ShadowTestResult,
)
from backend.core.topology.proactive_drive_service import ProactiveDriveService
from backend.core.topology.telemetry_schemas import HARDWARE_SCHEMA, PROACTIVE_DRIVE_SCHEMA
print('All topology + service symbols imported OK')
"
```

- [ ] **Step 4: Verify no circular imports with supervisor**

```bash
python3 -c "
import sys
sys.modules['unified_supervisor'] = type(sys)('fake')  # prevent full supervisor load
from backend.core.topology.proactive_drive_service import ProactiveDriveService
print('No circular import with supervisor')
"
```

- [ ] **Step 5: Commit final**

```bash
git add -A
git commit -m "feat(topology): Milestone 1 complete — Proactive Drive wired into GLS

ProactiveDriveService with async lifecycle, TelemetryBus integration,
and GLS submit() hook. Zone 6.12 in unified_supervisor.py gated by
JARVIS_PROACTIVE_DRIVE_ENABLED. Hardware discovery at boot emits
lifecycle.hardware@1.0.0. Drive tick emits reasoning.proactive_drive@1.0.0.

Level 2 (GOVERNED) -> Level 3 (PROACTIVE GOVERNED) bridge complete.
Activate with: JARVIS_PROACTIVE_DRIVE_ENABLED=true in .env"
```

---

## YAGNI Guard — Out of Scope

Do NOT implement these:
- Sentinel dispatch (ExplorationSentinel) — the tick loop logs the target and enters COOLDOWN. Actual sandbox execution is a separate milestone.
- TopologyMap population from scheduler envelopes — start with empty topology; populate manually or via future integration.
- Capability discovery automation — use hand-curated CapabilityNode registration initially.
- TUI Proactive Drive panel — the telemetry envelopes are emitted; TUI can render them in a future task.
- ProposalDeliveryService (git commit to proposals/ branch) — depends on Sentinel being wired.
- Prime/Reactor LittlesLawVerifier wiring — only JARVIS verifier is hooked in this milestone. Prime and Reactor verifiers will be hooked when cross-repo IPC is implemented.
