# Proactive Autonomous Drive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Proactive Autonomous Drive (Synthetic Curiosity Engine) — a zero-LLM-dependency system that uses queuing theory, information theory, and control theory to autonomously discover and explore capability gaps.

**Architecture:** All components live in `backend/core/topology/` (new package). Seven source files implement the five structural challenges from the spec: HardwareEnvironmentState (dynamic discovery), TopologyMap (capability DAG), LittlesLawVerifier + ProactiveDrive (idle detection state machine), CuriosityEngine (Shannon Entropy + UCB1), PIDController + ResourceGovernor (resource throttling), ExplorationSentinel + DeadEndClassifier (sandboxed execution), and ArchitecturalProposal (output contract). TDD throughout — tests in `tests/core/topology/`.

**Tech Stack:** Python 3, `psutil` (already in requirements), `pytest`, `asyncio`

**Spec:** `docs/ouroboros-vs-claude-code-gap-analysis.md` — Part 10: Proactive Autonomous Drive

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/core/topology/__init__.py` | Package init, public API exports |
| `backend/core/topology/hardware_env.py` | `ComputeTier`, `GPUState`, `HardwareEnvironmentState` — dynamic hardware discovery |
| `backend/core/topology/topology_map.py` | `CapabilityNode`, `TopologyMap` — capability DAG with Shannon Entropy |
| `backend/core/topology/idle_verifier.py` | `QueueSample`, `LittlesLawVerifier`, `ProactiveDrive` — idle detection + state machine |
| `backend/core/topology/curiosity_engine.py` | `CuriosityTarget`, `CuriosityEngine` — UCB1 capability gap selection |
| `backend/core/topology/resource_governor.py` | `PIDController`, `ResourceGovernor` — CPU throttling via PID control |
| `backend/core/topology/sentinel.py` | `DeadEndClass`, `SentinelOutcome`, `DeadEndClassifier`, `ExplorationSentinel` — sandboxed exploration |
| `backend/core/topology/architectural_proposal.py` | `ShadowTestResult`, `ArchitecturalProposal` — frozen output contract |
| `tests/core/topology/__init__.py` | Test package init |
| `tests/core/topology/test_hardware_env.py` | Tests for hardware discovery + tier classification |
| `tests/core/topology/test_topology_map.py` | Tests for capability DAG + entropy math |
| `tests/core/topology/test_idle_verifier.py` | Tests for Little's Law + ProactiveDrive state machine |
| `tests/core/topology/test_curiosity_engine.py` | Tests for UCB1 scoring + target selection |
| `tests/core/topology/test_resource_governor.py` | Tests for PID controller math + governor lifecycle |
| `tests/core/topology/test_sentinel.py` | Tests for failure classification + sentinel async lifecycle |
| `tests/core/topology/test_architectural_proposal.py` | Tests for proposal creation + integrity hash |

---

### Task 1: Package Init + HardwareEnvironmentState

**Files:**
- Create: `backend/core/topology/__init__.py`
- Create: `backend/core/topology/hardware_env.py`
- Create: `tests/core/topology/__init__.py`
- Create: `tests/core/topology/test_hardware_env.py`

- [ ] **Step 1: Create test package init**

Create `tests/core/topology/__init__.py` (empty).

- [ ] **Step 2: Write failing tests for HardwareEnvironmentState**

Create `tests/core/topology/test_hardware_env.py`:

```python
"""Tests for HardwareEnvironmentState dynamic discovery."""
import platform
from unittest.mock import patch, MagicMock

import pytest

from backend.core.topology.hardware_env import (
    ComputeTier,
    GPUState,
    HardwareEnvironmentState,
)


class TestComputeTier:
    def test_enum_values(self):
        assert ComputeTier.CLOUD_GPU == "cloud_gpu"
        assert ComputeTier.CLOUD_CPU == "cloud_cpu"
        assert ComputeTier.LOCAL_GPU == "local_gpu"
        assert ComputeTier.LOCAL_CPU == "local_cpu"
        assert ComputeTier.UNKNOWN == "unknown"


class TestGPUState:
    def test_frozen(self):
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        with pytest.raises(AttributeError):
            gpu.name = "A100"

    def test_fields(self):
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        assert gpu.name == "L4"
        assert gpu.vram_total_mb == 24576


class TestHardwareEnvironmentState:
    def test_frozen(self):
        state = HardwareEnvironmentState(
            os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
            ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
            hostname="test", python_version="3.11.0",
            max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
        )
        with pytest.raises(AttributeError):
            state.os_family = "linux"

    def test_discover_returns_valid_state(self):
        state = HardwareEnvironmentState.discover()
        assert state.os_family == platform.system().lower()
        assert state.cpu_logical_cores >= 1
        assert state.ram_total_mb > 0
        assert state.ram_available_mb > 0
        assert state.max_parallel_inference_tasks >= 1
        assert state.max_shadow_harness_workers >= 1
        assert isinstance(state.compute_tier, ComputeTier)

    def test_discover_no_hardcoded_tier(self):
        """Tier must be derived from runtime probing, never hardcoded."""
        state = HardwareEnvironmentState.discover()
        # On this dev machine (macOS, no nvidia-smi), expect LOCAL_CPU
        if platform.system().lower() == "darwin":
            assert state.gpu is None
            assert state.compute_tier in (ComputeTier.LOCAL_CPU, ComputeTier.LOCAL_GPU)

    def test_classify_tier_cloud_gpu(self):
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "jarvis-473803"}):
            tier = HardwareEnvironmentState._classify_tier(gpu, 4, 16384)
        assert tier == ComputeTier.CLOUD_GPU

    def test_classify_tier_cloud_cpu(self):
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}):
            tier = HardwareEnvironmentState._classify_tier(None, 4, 16384)
        assert tier == ComputeTier.CLOUD_CPU

    def test_classify_tier_local_gpu(self):
        gpu = GPUState(name="RTX4090", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        with patch.dict("os.environ", {}, clear=True):
            tier = HardwareEnvironmentState._classify_tier(gpu, 8, 32768)
        assert tier == ComputeTier.LOCAL_GPU

    def test_classify_tier_local_cpu(self):
        with patch.dict("os.environ", {}, clear=True):
            tier = HardwareEnvironmentState._classify_tier(None, 8, 16384)
        assert tier == ComputeTier.LOCAL_CPU

    def test_probe_gpu_returns_none_without_nvidia(self):
        """nvidia-smi not present on macOS dev machine."""
        result = HardwareEnvironmentState._probe_gpu()
        if platform.system().lower() == "darwin":
            assert result is None

    def test_max_parallel_inference_derived(self):
        state = HardwareEnvironmentState.discover()
        expected = max(1, state.ram_available_mb // 2048)
        assert state.max_parallel_inference_tasks == expected

    def test_max_shadow_workers_derived(self):
        state = HardwareEnvironmentState.discover()
        expected = max(1, state.cpu_logical_cores // 2)
        assert state.max_shadow_harness_workers == expected
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_hardware_env.py -v
```

Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 4: Implement HardwareEnvironmentState**

Create `backend/core/topology/__init__.py`:

```python
"""Topology package — dynamic hardware discovery, capability DAG, and proactive drive."""
```

Create `backend/core/topology/hardware_env.py` with the full implementation from the spec (Part 10 Challenge 1): `ComputeTier`, `GPUState`, `HardwareEnvironmentState` with `discover()`, `_probe_gpu()`, `_classify_tier()`.

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_hardware_env.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/core/topology/__init__.py backend/core/topology/hardware_env.py tests/core/topology/__init__.py tests/core/topology/test_hardware_env.py
git commit -m "feat(topology): add HardwareEnvironmentState dynamic discovery (Challenge 1)

ComputeTier enum, GPUState frozen dataclass, HardwareEnvironmentState
with discover() classmethod using psutil + nvidia-smi. No hardcoded
IS_LOCAL_MAC — tier derived from runtime probing."
```

---

### Task 2: TopologyMap — Capability DAG with Shannon Entropy

**Files:**
- Create: `backend/core/topology/topology_map.py`
- Create: `tests/core/topology/test_topology_map.py`

- [ ] **Step 1: Write failing tests for TopologyMap**

Create `tests/core/topology/test_topology_map.py`:

```python
"""Tests for TopologyMap capability DAG and Shannon Entropy."""
import math

import pytest

from backend.core.topology.topology_map import CapabilityNode, TopologyMap
from backend.core.topology.hardware_env import (
    ComputeTier,
    GPUState,
    HardwareEnvironmentState,
)


def _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU):
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=tier, gpu=gpu,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


class TestCapabilityNode:
    def test_defaults(self):
        node = CapabilityNode(name="route_ollama", domain="llm_routing", repo_owner="jarvis")
        assert node.active is False
        assert node.coverage_score == 0.0
        assert node.exploration_attempts == 0


class TestTopologyMap:
    def test_register_and_lookup(self):
        topo = TopologyMap()
        node = CapabilityNode(name="parse_csv", domain="data_io", repo_owner="reactor")
        topo.register(node)
        assert "parse_csv" in topo.nodes
        assert topo.nodes["parse_csv"].domain == "data_io"

    def test_edges_initialized_on_register(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="jarvis"))
        assert "a" in topo.edges
        assert topo.edges["a"] == set()

    def test_all_domains(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="llm", repo_owner="jarvis"))
        topo.register(CapabilityNode(name="b", domain="vision", repo_owner="prime"))
        assert topo.all_domains() == frozenset({"llm", "vision"})

    def test_domain_coverage_empty_domain(self):
        topo = TopologyMap()
        assert topo.domain_coverage("nonexistent") == 1.0

    def test_domain_coverage_all_active(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=True))
        assert topo.domain_coverage("d") == 1.0

    def test_domain_coverage_half(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=False))
        assert topo.domain_coverage("d") == 0.5

    def test_domain_coverage_none_active(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=False))
        assert topo.domain_coverage("d") == 0.0

    def test_entropy_fully_known(self):
        """H=0 when all capabilities are active (p=1.0)."""
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        assert topo.entropy_over_domain("d") == 0.0

    def test_entropy_fully_unknown(self):
        """H=0 when no capabilities are active (p=0.0)."""
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=False))
        assert topo.entropy_over_domain("d") == 0.0

    def test_entropy_maximum_at_half(self):
        """H=1.0 when exactly half are active (maximum ignorance)."""
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=False))
        assert topo.entropy_over_domain("d") == pytest.approx(1.0)

    def test_entropy_nonexistent_domain(self):
        topo = TopologyMap()
        assert topo.entropy_over_domain("nope") == 0.0

    def test_entropy_intermediate(self):
        """H between 0 and 1 for partial coverage (e.g., 1/3)."""
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=False))
        topo.register(CapabilityNode(name="c", domain="d", repo_owner="j", active=False))
        p = 1.0 / 3.0
        expected_h = -p * math.log2(p) - (1 - p) * math.log2(1 - p)
        assert topo.entropy_over_domain("d") == pytest.approx(expected_h)

    def test_feasible_cpu_only_rejects_gpu_capability(self):
        topo = TopologyMap()
        gpu_node = CapabilityNode(name="vision_gpu_ocr", domain="vision", repo_owner="prime")
        hw = _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU)
        assert topo.feasible_for_hardware(gpu_node, hw) is False

    def test_feasible_with_gpu(self):
        topo = TopologyMap()
        gpu_node = CapabilityNode(name="vision_gpu_ocr", domain="vision", repo_owner="prime")
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        hw = _make_hardware(gpu=gpu, tier=ComputeTier.CLOUD_GPU)
        assert topo.feasible_for_hardware(gpu_node, hw) is True

    def test_feasible_low_vram_rejects(self):
        topo = TopologyMap()
        gpu_node = CapabilityNode(name="vision_gpu_ocr", domain="vision", repo_owner="prime")
        gpu = GPUState(name="L4", vram_total_mb=4096, vram_free_mb=2000, driver_version="535.0")
        hw = _make_hardware(gpu=gpu, tier=ComputeTier.CLOUD_GPU)
        assert topo.feasible_for_hardware(gpu_node, hw) is False

    def test_feasible_cpu_capability_always_ok(self):
        topo = TopologyMap()
        cpu_node = CapabilityNode(name="parse_csv", domain="data_io", repo_owner="reactor")
        hw = _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU)
        assert topo.feasible_for_hardware(cpu_node, hw) is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_topology_map.py -v
```

Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement TopologyMap**

Create `backend/core/topology/topology_map.py` with the full implementation from the spec: `CapabilityNode` dataclass, `TopologyMap` with `register()`, `domain_coverage()`, `entropy_over_domain()`, `all_domains()`, `feasible_for_hardware()`.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_topology_map.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/topology_map.py tests/core/topology/test_topology_map.py
git commit -m "feat(topology): add TopologyMap capability DAG with Shannon Entropy (Challenge 1)

CapabilityNode dataclass, TopologyMap with domain_coverage(),
entropy_over_domain() (Shannon binary entropy), feasible_for_hardware()
hardware gate. Substrate for the CuriosityEngine."
```

---

### Task 3: LittlesLawVerifier + ProactiveDrive State Machine

**Files:**
- Create: `backend/core/topology/idle_verifier.py`
- Create: `tests/core/topology/test_idle_verifier.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_idle_verifier.py`:

```python
"""Tests for Little's Law idle verifier and ProactiveDrive state machine."""
import time
from unittest.mock import patch

import pytest

from backend.core.topology.idle_verifier import (
    LittlesLawVerifier,
    ProactiveDrive,
    QueueSample,
)


class TestQueueSample:
    def test_fields(self):
        s = QueueSample(timestamp=1.0, depth=5, processing_latency_ms=100.0)
        assert s.depth == 5
        assert s.processing_latency_ms == 100.0


class TestLittlesLawVerifier:
    def test_insufficient_samples_returns_none(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        assert v.compute_L() is None

    def test_insufficient_samples_not_idle(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        idle, reason = v.is_idle()
        assert idle is False
        assert "insufficient" in reason

    def test_idle_with_low_load(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        now = time.monotonic()
        for i in range(15):
            v._samples.append(QueueSample(
                timestamp=now + i * 1.0,
                depth=2,
                processing_latency_ms=10.0,
            ))
        L = v.compute_L()
        assert L is not None
        assert L < 0.30 * 100  # well below threshold
        idle, reason = v.is_idle()
        assert idle is True

    def test_busy_with_high_load(self):
        v = LittlesLawVerifier("prime", max_queue_depth=100)
        now = time.monotonic()
        for i in range(15):
            v._samples.append(QueueSample(
                timestamp=now + i * 0.1,  # fast arrival rate
                depth=80,
                processing_latency_ms=5000.0,  # slow processing
            ))
        L = v.compute_L()
        assert L is not None
        assert L >= 0.30 * 100
        idle, reason = v.is_idle()
        assert idle is False

    def test_record_prunes_old_samples(self):
        v = LittlesLawVerifier("reactor", max_queue_depth=100)
        old_time = time.monotonic() - 200.0  # outside 120s window
        v._samples.append(QueueSample(timestamp=old_time, depth=1, processing_latency_ms=10.0))
        v.record(depth=1, processing_latency_ms=10.0)
        assert all(s.timestamp > old_time for s in v._samples)

    def test_zero_window_returns_none(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=100)
        now = time.monotonic()
        for _ in range(15):
            v._samples.append(QueueSample(timestamp=now, depth=1, processing_latency_ms=10.0))
        assert v.compute_L() is None


class TestProactiveDrive:
    def _make_idle_verifiers(self, idle=True):
        verifiers = {}
        for name in ("jarvis", "prime", "reactor"):
            v = LittlesLawVerifier(name, max_queue_depth=100)
            now = time.monotonic()
            if idle:
                for i in range(15):
                    v._samples.append(QueueSample(
                        timestamp=now + i * 1.0, depth=1, processing_latency_ms=5.0,
                    ))
            verifiers[name] = v
        return verifiers

    def test_initial_state_is_reactive(self):
        vs = self._make_idle_verifiers(idle=False)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        assert drive.state == "REACTIVE"

    def test_tick_not_idle_goes_to_measuring(self):
        vs = self._make_idle_verifiers(idle=False)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        state, reason = drive.tick()
        assert state == "MEASURING"
        assert "insufficient" in reason or "Not idle" in reason

    def test_tick_all_idle_starts_eligibility(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        state, reason = drive.tick()
        assert state == "MEASURING"
        assert "eligibility" in reason.lower() or "idle" in reason.lower()

    def test_becomes_eligible_after_min_seconds(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive.tick()  # starts eligibility timer
        # Simulate time passing past MIN_ELIGIBLE_SECONDS
        drive._eligible_since = time.monotonic() - drive.MIN_ELIGIBLE_SECONDS - 1
        state, reason = drive.tick()
        assert state == "ELIGIBLE"

    def test_begin_exploration_from_eligible(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "ELIGIBLE"
        drive.begin_exploration()
        assert drive.state == "EXPLORING"

    def test_begin_exploration_from_wrong_state_raises(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        with pytest.raises(AssertionError):
            drive.begin_exploration()

    def test_end_exploration_enters_cooldown(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "EXPLORING"
        drive.end_exploration()
        assert drive.state == "COOLDOWN"

    def test_cooldown_expires_to_reactive(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "COOLDOWN"
        drive._last_exploration_end = time.monotonic() - drive.COOLDOWN_SECONDS - 1
        state, reason = drive.tick()
        assert state == "REACTIVE"

    def test_exploring_state_waits_for_sentinel(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive._state = "EXPLORING"
        state, reason = drive.tick()
        assert state == "EXPLORING"
        assert "Sentinel active" in reason

    def test_idle_interrupted_resets_eligibility(self):
        vs = self._make_idle_verifiers(idle=True)
        drive = ProactiveDrive(vs["jarvis"], vs["prime"], vs["reactor"])
        drive.tick()  # start eligibility timer
        assert drive._eligible_since is not None
        # Now make one verifier not idle by clearing samples
        vs["prime"]._samples.clear()
        state, reason = drive.tick()
        assert state == "MEASURING"
        assert drive._eligible_since is None
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_idle_verifier.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement LittlesLawVerifier and ProactiveDrive**

Create `backend/core/topology/idle_verifier.py` with the full implementation from the spec (Part 10 Challenge 2): `QueueSample`, `LittlesLawVerifier` with `record()`, `compute_L()`, `is_idle()`, and `ProactiveDrive` state machine with `tick()`, `begin_exploration()`, `end_exploration()`.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_idle_verifier.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/idle_verifier.py tests/core/topology/test_idle_verifier.py
git commit -m "feat(topology): add LittlesLawVerifier + ProactiveDrive state machine (Challenge 2)

Little's Law (L=lambda*W) over 120s rolling window to mathematically
prove idle capacity. ProactiveDrive FSM: REACTIVE -> MEASURING ->
ELIGIBLE -> EXPLORING -> COOLDOWN. No cron jobs, no asyncio.sleep hacks."
```

---

### Task 4: CuriosityEngine — Shannon Entropy + UCB1

**Files:**
- Create: `backend/core/topology/curiosity_engine.py`
- Create: `tests/core/topology/test_curiosity_engine.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_curiosity_engine.py`:

```python
"""Tests for CuriosityEngine — Shannon Entropy + UCB1 target selection."""
import math

import pytest

from backend.core.topology.curiosity_engine import (
    CuriosityEngine,
    CuriosityTarget,
    UCB_EXPLORATION_CONSTANT,
)
from backend.core.topology.topology_map import CapabilityNode, TopologyMap
from backend.core.topology.hardware_env import (
    ComputeTier,
    GPUState,
    HardwareEnvironmentState,
)


def _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU):
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=tier, gpu=gpu,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _populated_topology():
    topo = TopologyMap()
    topo.register(CapabilityNode(name="route_ollama", domain="llm_routing", repo_owner="jarvis", active=True))
    topo.register(CapabilityNode(name="route_claude", domain="llm_routing", repo_owner="jarvis", active=False))
    topo.register(CapabilityNode(name="parse_csv", domain="data_io", repo_owner="reactor", active=True))
    topo.register(CapabilityNode(name="parse_parquet", domain="data_io", repo_owner="reactor", active=False))
    topo.register(CapabilityNode(name="vision_ocr", domain="vision", repo_owner="prime", active=False))
    return topo


class TestCuriosityTarget:
    def test_frozen(self):
        node = CapabilityNode(name="a", domain="d", repo_owner="j")
        target = CuriosityTarget(
            capability=node, ucb_score=1.5, entropy_score=0.8,
            feasibility_score=1.0, rationale="test",
        )
        with pytest.raises(AttributeError):
            target.ucb_score = 2.0


class TestCuriosityEngine:
    def test_select_target_returns_highest_ucb(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        target = engine.select_target()
        assert target is not None
        assert isinstance(target, CuriosityTarget)
        assert target.capability.active is False

    def test_select_target_skips_active_capabilities(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        for node, score in scored:
            assert node.active is False

    def test_select_target_skips_infeasible(self):
        """vision_ocr requires GPU; LOCAL_CPU hardware should exclude it."""
        topo = _populated_topology()
        hw = _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU)
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        names = [n.name for n, _ in scored]
        assert "vision_ocr" not in names

    def test_select_target_includes_gpu_cap_with_gpu(self):
        topo = _populated_topology()
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        hw = _make_hardware(gpu=gpu, tier=ComputeTier.CLOUD_GPU)
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        names = [n.name for n, _ in scored]
        assert "vision_ocr" in names

    def test_select_target_none_when_all_active(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        assert engine.select_target() is None

    def test_select_target_none_when_empty(self):
        topo = TopologyMap()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        assert engine.select_target() is None

    def test_ucb_exploration_bonus_decreases_with_attempts(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(
            name="a", domain="d", repo_owner="j", active=False, exploration_attempts=1,
        ))
        topo.register(CapabilityNode(
            name="b", domain="d", repo_owner="j", active=False, exploration_attempts=10,
        ))
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = dict(engine.score_all())
        # 'a' has fewer attempts so should have higher UCB score
        assert scored[topo.nodes["a"]] > scored[topo.nodes["b"]]

    def test_rationale_contains_math(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        target = engine.select_target()
        assert "Shannon Entropy" in target.rationale
        assert "UCB=" in target.rationale
        assert "coverage=" in target.rationale

    def test_score_all_sorted_descending(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        scores = [s for _, s in scored]
        assert scores == sorted(scores, reverse=True)

    def test_laplace_smoothing_prevents_div_zero(self):
        """Brand new system: total_attempts=0, exploration_attempts=0."""
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=False))
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        target = engine.select_target()
        assert target is not None
        assert math.isfinite(target.ucb_score)

    def test_dependency_feasibility(self):
        """Capability with unmet dependencies gets lower feasibility score."""
        topo = TopologyMap()
        topo.register(CapabilityNode(name="base", domain="d", repo_owner="j", active=False))
        topo.register(CapabilityNode(name="dependent", domain="d", repo_owner="j", active=False))
        topo.edges["dependent"] = {"base"}
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = dict((n.name, s) for n, s in engine.score_all())
        # "base" has no deps (feasibility=1.0), "dependent" has unmet dep (feasibility=0.0)
        assert scored["base"] > scored.get("dependent", 0.0)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_curiosity_engine.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement CuriosityEngine**

Create `backend/core/topology/curiosity_engine.py` with the full implementation from the spec (Part 10 Challenge 3): `CuriosityTarget` frozen dataclass, `CuriosityEngine` with `_entropy()`, `_feasibility()`, `_ucb_score()`, `score_all()`, `select_target()`.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_curiosity_engine.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/curiosity_engine.py tests/core/topology/test_curiosity_engine.py
git commit -m "feat(topology): add CuriosityEngine with Shannon Entropy + UCB1 (Challenge 3)

Deterministic capability gap selection. Shannon Entropy quantifies
domain ignorance; UCB1 balances exploitation vs exploration.
Laplace-smoothed to prevent div-by-zero on brand-new systems."
```

---

### Task 5: PIDController + ResourceGovernor

**Files:**
- Create: `backend/core/topology/resource_governor.py`
- Create: `tests/core/topology/test_resource_governor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_resource_governor.py`:

```python
"""Tests for PIDController and ResourceGovernor."""
import asyncio
import time

import pytest

from backend.core.topology.resource_governor import PIDController, ResourceGovernor


class TestPIDController:
    def test_default_params(self):
        pid = PIDController()
        assert pid.target_cpu_fraction == 0.40
        assert pid.Kp == 0.5
        assert pid.Ki == 0.1
        assert pid.Kd == 0.05
        assert pid.min_concurrency == 1
        assert pid.max_concurrency == 8

    def test_underloaded_increases_concurrency(self):
        pid = PIDController()
        # CPU at 10% (well below 40% target) -> should increase concurrency
        result = pid.update(0.10)
        assert result >= pid.min_concurrency
        assert result <= pid.max_concurrency
        # Should be above baseline (4)
        baseline = (pid.min_concurrency + pid.max_concurrency) // 2
        assert result >= baseline

    def test_overloaded_decreases_concurrency(self):
        pid = PIDController()
        # CPU at 80% (well above 40% target) -> should decrease concurrency
        result = pid.update(0.80)
        assert result >= pid.min_concurrency
        assert result <= pid.max_concurrency
        baseline = (pid.min_concurrency + pid.max_concurrency) // 2
        assert result <= baseline

    def test_at_target_stays_near_baseline(self):
        pid = PIDController()
        result = pid.update(0.40)
        baseline = (pid.min_concurrency + pid.max_concurrency) // 2
        assert abs(result - baseline) <= 1

    def test_never_exceeds_bounds(self):
        pid = PIDController()
        for cpu in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            result = pid.update(cpu)
            assert pid.min_concurrency <= result <= pid.max_concurrency

    def test_anti_windup_clamp(self):
        pid = PIDController()
        # Sustained overload should not cause integral to blow up
        for _ in range(1000):
            pid.update(1.0)  # 100% CPU for many iterations
        assert -10.0 <= pid._integral <= 10.0

    def test_integral_accumulates_on_sustained_error(self):
        pid = PIDController()
        pid.update(0.10)  # underloaded
        time.sleep(0.01)
        pid.update(0.10)  # still underloaded
        assert pid._integral > 0  # positive error accumulates

    def test_custom_params(self):
        pid = PIDController(
            target_cpu_fraction=0.60, Kp=1.0, Ki=0.2, Kd=0.1,
            min_concurrency=2, max_concurrency=16,
        )
        assert pid.target_cpu_fraction == 0.60
        assert pid.max_concurrency == 16


class TestResourceGovernor:
    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem)
        await gov.start()
        assert gov._task is not None
        assert not gov._task.done()
        await gov.stop()
        assert gov._task is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem)
        await gov.stop()  # stop without start should not raise
        assert gov._task is None

    @pytest.mark.asyncio
    async def test_governor_adjusts_concurrency(self):
        """Governor should call pid.update() in its loop."""
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem, poll_interval=0.05)
        await gov.start()
        await asyncio.sleep(0.15)  # let a few cycles run
        await gov.stop()
        # PID should have been called at least once
        assert pid._prev_time > 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_resource_governor.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement PIDController and ResourceGovernor**

Create `backend/core/topology/resource_governor.py` with `PIDController` (from spec Challenge 4) and `ResourceGovernor` with `start()`, `stop()`, `_loop()`. Add a `poll_interval` parameter (default 5.0) to make the governor testable. The `_loop()` reads `psutil.cpu_percent()` and calls `pid.update()`.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_resource_governor.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/resource_governor.py tests/core/topology/test_resource_governor.py
git commit -m "feat(topology): add PIDController + ResourceGovernor (Challenge 4)

PID controller (Kp=0.5, Ki=0.1, Kd=0.05) with anti-windup clamp.
ResourceGovernor wraps PID with async measurement loop, controls
Sentinel concurrency via semaphore. Physically prevents CPU melt."
```

---

### Task 6: ExplorationSentinel + DeadEndClassifier

**Files:**
- Create: `backend/core/topology/sentinel.py`
- Create: `tests/core/topology/test_sentinel.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_sentinel.py`:

```python
"""Tests for DeadEndClassifier, SentinelOutcome, and ExplorationSentinel."""
import asyncio
import os
import shutil
import tempfile

import pytest

from backend.core.topology.sentinel import (
    DeadEndClass,
    DeadEndClassifier,
    ExplorationSentinel,
    SentinelOutcome,
)
from backend.core.topology.curiosity_engine import CuriosityTarget
from backend.core.topology.topology_map import CapabilityNode
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState


def _make_hardware():
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _make_target():
    node = CapabilityNode(name="test_cap", domain="test_domain", repo_owner="jarvis")
    return CuriosityTarget(
        capability=node, ucb_score=1.5, entropy_score=0.8,
        feasibility_score=1.0, rationale="test rationale",
    )


class TestDeadEndClass:
    def test_enum_values(self):
        assert DeadEndClass.PAYWALL == "paywall"
        assert DeadEndClass.TIMEOUT == "timeout"
        assert DeadEndClass.CLEAN_SUCCESS == "clean_success"
        assert DeadEndClass.SANDBOX_VIOLATION == "sandbox_violation"


class TestSentinelOutcome:
    def test_frozen(self):
        outcome = SentinelOutcome(
            dead_end_class=DeadEndClass.CLEAN_SUCCESS,
            capability_name="test", elapsed_seconds=10.0,
            partial_findings="found stuff", unwind_actions_taken=["none"],
        )
        with pytest.raises(AttributeError):
            outcome.dead_end_class = DeadEndClass.TIMEOUT


class TestDeadEndClassifier:
    def test_classify_402_as_paywall(self):
        assert DeadEndClassifier.classify_http_error(402) == DeadEndClass.PAYWALL

    def test_classify_403_as_paywall(self):
        assert DeadEndClassifier.classify_http_error(403) == DeadEndClass.PAYWALL

    def test_classify_410_as_deprecated(self):
        assert DeadEndClassifier.classify_http_error(410) == DeadEndClass.DEPRECATED_API

    def test_classify_200_returns_none(self):
        assert DeadEndClassifier.classify_http_error(200) is None

    def test_classify_500_returns_none(self):
        assert DeadEndClassifier.classify_http_error(500) is None

    def test_classify_memory_error(self):
        assert DeadEndClassifier.classify_exception(MemoryError()) == DeadEndClass.RESOURCE_EXHAUSTION

    def test_classify_timeout_error(self):
        assert DeadEndClassifier.classify_exception(TimeoutError()) == DeadEndClass.TIMEOUT

    def test_classify_cancelled_error(self):
        assert DeadEndClassifier.classify_exception(asyncio.CancelledError()) == DeadEndClass.TIMEOUT

    def test_classify_permission_error(self):
        assert DeadEndClassifier.classify_exception(PermissionError()) == DeadEndClass.SANDBOX_VIOLATION

    def test_classify_unknown_defaults_to_timeout(self):
        assert DeadEndClassifier.classify_exception(ValueError("something")) == DeadEndClass.TIMEOUT


class TestExplorationSentinel:
    @pytest.mark.asyncio
    async def test_context_manager_lifecycle(self):
        target = _make_target()
        hw = _make_hardware()
        async with ExplorationSentinel(target, hw, max_runtime_seconds=5.0) as sentinel:
            assert sentinel._governor is not None

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_outcome(self):
        target = _make_target()
        hw = _make_hardware()
        sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=0.1)
        # Override _explore to hang
        async def hang():
            await asyncio.sleep(100)
            return ""
        sentinel._explore = hang
        async with sentinel:
            outcome = await sentinel.run()
        assert outcome.dead_end_class == DeadEndClass.TIMEOUT
        assert outcome.capability_name == "test_cap"

    @pytest.mark.asyncio
    async def test_success_returns_clean_success(self):
        target = _make_target()
        hw = _make_hardware()
        sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=5.0)
        # Override _explore to succeed immediately
        async def succeed():
            return "found integration docs"
        sentinel._explore = succeed
        async with sentinel:
            outcome = await sentinel.run()
        assert outcome.dead_end_class == DeadEndClass.CLEAN_SUCCESS
        assert outcome.partial_findings == "found integration docs"

    @pytest.mark.asyncio
    async def test_exception_returns_classified_outcome(self):
        target = _make_target()
        hw = _make_hardware()
        sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=5.0)
        async def explode():
            raise MemoryError("OOM")
        sentinel._explore = explode
        async with sentinel:
            outcome = await sentinel.run()
        assert outcome.dead_end_class == DeadEndClass.RESOURCE_EXHAUSTION

    @pytest.mark.asyncio
    async def test_cleanup_scratch_on_exit(self):
        target = _make_target()
        hw = _make_hardware()
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=5.0)
            sentinel._scratch_path = os.path.join(tmpdir, "scratch")
            os.makedirs(sentinel._scratch_path)
            async def fail():
                raise ValueError("test failure")
            sentinel._explore = fail
            async with sentinel:
                await sentinel.run()
            assert not os.path.exists(sentinel._scratch_path)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_sentinel.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement sentinel module**

Create `backend/core/topology/sentinel.py` with the full implementation from the spec (Part 10 Challenge 4): `DeadEndClass` enum, `SentinelOutcome` frozen dataclass, `DeadEndClassifier`, `ExplorationSentinel` async context manager with `run()`, `_explore()` (raises NotImplementedError), `_cleanup_scratch()`.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_sentinel.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/sentinel.py tests/core/topology/test_sentinel.py
git commit -m "feat(topology): add ExplorationSentinel + DeadEndClassifier (Challenge 4)

Sandboxed async context manager with ResourceGovernor PID throttling.
DeadEndClassifier deterministically classifies failure modes.
Domain allowlist enforced. Clean async unwind on all exit paths."
```

---

### Task 7: ArchitecturalProposal Output Contract

**Files:**
- Create: `backend/core/topology/architectural_proposal.py`
- Create: `tests/core/topology/test_architectural_proposal.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/topology/test_architectural_proposal.py`:

```python
"""Tests for ArchitecturalProposal output contract."""
import json
import os
import tempfile

import pytest

from backend.core.topology.architectural_proposal import (
    ArchitecturalProposal,
    ShadowTestResult,
)
from backend.core.topology.curiosity_engine import CuriosityTarget
from backend.core.topology.topology_map import CapabilityNode
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState


def _make_hardware():
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _make_target():
    node = CapabilityNode(name="parse_parquet", domain="data_io", repo_owner="reactor")
    return CuriosityTarget(
        capability=node, ucb_score=1.5, entropy_score=0.8,
        feasibility_score=1.0, rationale="Domain 'data_io' has Shannon Entropy H=0.918",
    )


class TestShadowTestResult:
    def test_frozen(self):
        r = ShadowTestResult(test_name="test_parse", passed=True, duration_ms=50.0, output="ok")
        with pytest.raises(AttributeError):
            r.passed = False


class TestArchitecturalProposal:
    def test_create_with_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "parser.py")
            with open(f1, "w") as fh:
                fh.write("def parse(): pass\n")
            target = _make_target()
            hw = _make_hardware()
            results = [
                ShadowTestResult(test_name="test_parse", passed=True, duration_ms=50.0, output="ok"),
            ]
            proposal = ArchitecturalProposal.create(
                target=target, hardware=hw,
                generated_files=[f1], shadow_results=results,
                sentinel_elapsed=120.5,
            )
            assert proposal.capability_name == "parse_parquet"
            assert proposal.capability_domain == "data_io"
            assert proposal.repo_owner == "reactor"
            assert proposal.all_tests_passed is True
            assert len(proposal.proposal_id) > 0
            assert len(proposal.content_hash) == 64  # SHA-256 hex

    def test_frozen(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[],
            sentinel_elapsed=10.0,
        )
        with pytest.raises(AttributeError):
            proposal.capability_name = "changed"

    def test_to_json_valid(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[],
            sentinel_elapsed=10.0,
        )
        data = json.loads(proposal.to_json())
        assert data["capability_name"] == "parse_parquet"
        assert "proposal_id" in data
        assert "content_hash" in data

    def test_summary_contains_key_info(self):
        target = _make_target()
        hw = _make_hardware()
        results = [
            ShadowTestResult(test_name="t1", passed=True, duration_ms=50.0, output="ok"),
            ShadowTestResult(test_name="t2", passed=False, duration_ms=30.0, output="fail"),
        ]
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=["a.py", "b.py"], shadow_results=results,
            sentinel_elapsed=300.0,
        )
        summary = proposal.summary()
        assert "parse_parquet" in summary
        assert "reactor" in summary
        assert "1/2" in summary  # 1 of 2 tests passing
        assert "2 file(s)" in summary
        assert proposal.all_tests_passed is False

    def test_content_hash_deterministic(self):
        """Same files -> same hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "a.py")
            with open(f1, "w") as fh:
                fh.write("x = 1\n")
            target = _make_target()
            hw = _make_hardware()
            p1 = ArchitecturalProposal.create(
                target=target, hardware=hw,
                generated_files=[f1], shadow_results=[], sentinel_elapsed=10.0,
            )
            p2 = ArchitecturalProposal.create(
                target=target, hardware=hw,
                generated_files=[f1], shadow_results=[], sentinel_elapsed=10.0,
            )
            assert p1.content_hash == p2.content_hash

    def test_hardware_context_captured(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[], sentinel_elapsed=10.0,
        )
        assert proposal.hardware_tier == "local_cpu"
        assert proposal.ram_available_mb == 8192
        assert proposal.gpu_vram_free_mb == 0

    def test_curiosity_provenance(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[], sentinel_elapsed=10.0,
        )
        assert proposal.ucb_score == 1.5
        assert proposal.entropy_score == 0.8
        assert "Shannon Entropy" in proposal.curiosity_rationale
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/core/topology/test_architectural_proposal.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement ArchitecturalProposal**

Create `backend/core/topology/architectural_proposal.py` with the full implementation from the spec (Part 10 Challenge 5): `ShadowTestResult` frozen dataclass, `ArchitecturalProposal` frozen dataclass with `create()` classmethod, `to_json()`, `summary()`.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/core/topology/test_architectural_proposal.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/architectural_proposal.py tests/core/topology/test_architectural_proposal.py
git commit -m "feat(topology): add ArchitecturalProposal output contract (Challenge 5)

Frozen versioned dataclass with SHA-256 content hash, curiosity
provenance (UCB/entropy/feasibility), shadow test results, and
hardware context. Committed to proposals/ branch, never to main."
```

---

### Task 8: Package Init Exports + Full Regression

**Files:**
- Modify: `backend/core/topology/__init__.py`

- [ ] **Step 1: Update __init__.py with public API exports**

```python
"""
Topology package — Proactive Autonomous Drive
==============================================

Dynamic hardware discovery, capability DAG with Shannon Entropy,
Little's Law idle verification, UCB1 curiosity engine, PID resource
governor, sandboxed exploration sentinel, and architectural proposal
output contract.

Zero LLM dependency. Pure systems engineering, mathematics, and
control theory.
"""
from backend.core.topology.hardware_env import (
    ComputeTier,
    GPUState,
    HardwareEnvironmentState,
)
from backend.core.topology.topology_map import CapabilityNode, TopologyMap
from backend.core.topology.idle_verifier import (
    LittlesLawVerifier,
    ProactiveDrive,
    QueueSample,
)
from backend.core.topology.curiosity_engine import CuriosityEngine, CuriosityTarget
from backend.core.topology.resource_governor import PIDController, ResourceGovernor
from backend.core.topology.sentinel import (
    DeadEndClass,
    DeadEndClassifier,
    ExplorationSentinel,
    SentinelOutcome,
)
from backend.core.topology.architectural_proposal import (
    ArchitecturalProposal,
    ShadowTestResult,
)

__all__ = [
    "ComputeTier", "GPUState", "HardwareEnvironmentState",
    "CapabilityNode", "TopologyMap",
    "LittlesLawVerifier", "ProactiveDrive", "QueueSample",
    "CuriosityEngine", "CuriosityTarget",
    "PIDController", "ResourceGovernor",
    "DeadEndClass", "DeadEndClassifier", "ExplorationSentinel", "SentinelOutcome",
    "ArchitecturalProposal", "ShadowTestResult",
]
```

- [ ] **Step 2: Run full topology test suite**

```bash
python3 -m pytest tests/core/topology/ -v --tb=short
```

Expected: all tests PASS across all 7 test files.

- [ ] **Step 3: Verify import chain**

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
print(f'All 16 topology symbols imported OK')
hw = HardwareEnvironmentState.discover()
print(f'Hardware: {hw.compute_tier.value}, {hw.cpu_logical_cores} cores, {hw.ram_total_mb}MB RAM')
"
```

- [ ] **Step 4: Verify no supervisor/governance reverse-imports**

```bash
grep -rn "unified_supervisor\|unified_command_processor" backend/core/topology/
```

Expected: zero matches.

- [ ] **Step 5: Commit**

```bash
git add backend/core/topology/__init__.py
git commit -m "feat(topology): Proactive Autonomous Drive complete — all 5 challenges

Package exports: 16 symbols covering all 5 structural challenges.
HardwareEnvironmentState (dynamic discovery), TopologyMap (capability DAG),
LittlesLawVerifier + ProactiveDrive (Little's Law idle detection),
CuriosityEngine (Shannon Entropy + UCB1), PIDController + ResourceGovernor
(PID throttling), ExplorationSentinel + DeadEndClassifier (sandboxed
execution), ArchitecturalProposal (frozen output contract).

Zero LLM dependency. Pure systems engineering."
```

---

## YAGNI Guard — Out of Scope

Do NOT implement these:
- Supervisor Zone wiring (separate plan for integration)
- TelemetryBus `lifecycle.hardware@1.0.0` envelope emission (integration concern)
- `ouroboros propose accept/reject` CLI commands (separate feature)
- Actual Sentinel research logic (`_explore()` stays NotImplementedError — capability-specific)
- Cross-repo network communication (deployment concern)
- ProposalDeliveryService (git commit to proposals/ branch — separate feature)
- Dynamic semaphore resize in ResourceGovernor (complex; track concurrency delta externally)
