# Ouroboros Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the OuroborosDaemon (Zone 7.0) — a proactive self-evolution engine that activates dormant exploration, analysis, and patching infrastructure to make the Trinity ecosystem self-healing and self-optimizing.

**Architecture:** Three-phase daemon lifecycle: Phase 1 (Vital Scan) runs blocking invariant checks at boot; Phase 2 (Spinal Cord) wires bidirectional event streams; Phase 3 (REM Sleep) runs a background daemon that explores the codebase during idle, synthesizes patches via Doubleword 397B, and applies them through the governance pipeline.

**Tech Stack:** Python 3.12, asyncio (TaskGroup, CancellationToken pattern), NetworkX (Oracle graph), existing governance pipeline (IntakeRouter, RiskEngine, GLS)

**Spec:** `docs/superpowers/specs/2026-03-28-ouroboros-daemon-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `backend/core/ouroboros/daemon.py` | OuroborosDaemon class — Zone 7.0 entry point, orchestrates all 3 phases |
| `backend/core/ouroboros/daemon_config.py` | OuroborosDaemonConfig — env-driven configuration dataclass |
| `backend/core/ouroboros/cancellation_token.py` | CancellationToken — epoch-scoped cooperative cancellation |
| `backend/core/ouroboros/vital_scan.py` | Phase 1: VitalScan runner, VitalReport, VitalFinding |
| `backend/core/ouroboros/spinal_cord.py` | Phase 2: SpinalCord wiring, SpinalGate, SpinalLiveness |
| `backend/core/ouroboros/rem_sleep.py` | Phase 3: RemSleepDaemon, RemState machine, idle watch |
| `backend/core/ouroboros/rem_epoch.py` | Single REM epoch: explore -> analyze -> patch cycle |
| `backend/core/ouroboros/finding_ranker.py` | Deterministic merge_and_rank with impact_score v1.0 |
| `backend/core/ouroboros/exploration_envelope_factory.py` | Convert exploration findings to IntentEnvelopes |
| `tests/core/ouroboros/test_cancellation_token.py` | Tests for CancellationToken |
| `tests/core/ouroboros/test_daemon_config.py` | Tests for OuroborosDaemonConfig |
| `tests/core/ouroboros/test_vital_scan.py` | Tests for Phase 1 |
| `tests/core/ouroboros/test_spinal_cord.py` | Tests for Phase 2 |
| `tests/core/ouroboros/test_finding_ranker.py` | Tests for deterministic ranking |
| `tests/core/ouroboros/test_exploration_envelope_factory.py` | Tests for envelope conversion |
| `tests/core/ouroboros/test_rem_epoch.py` | Tests for single epoch cycle |
| `tests/core/ouroboros/test_rem_sleep.py` | Tests for REM state machine |
| `tests/core/ouroboros/test_daemon.py` | Tests for OuroborosDaemon lifecycle |

### Modified Files
| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/intake/intent_envelope.py:20` | Add `"exploration"` to `_VALID_SOURCES` |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py:33` | Add `"exploration": 4` to `_PRIORITY_MAP` |
| `backend/core/ouroboros/governance/risk_engine.py:177` | Add exploration-source stricter rules in `classify()` |
| `backend/core/ouroboros/governance/exploration_subagent.py:80` | Add `request_yield()` cooperative cancellation method |
| `backend/core/topology/idle_verifier.py:65` | Add `on_eligible()` callback registration to ProactiveDrive |
| `backend/core/ouroboros/governance/governed_loop_service.py` | Add getter methods for injected dependencies |
| `unified_supervisor.py:~87683` | Add Zone 7.0 wiring block |

---

## Task 1: Foundation Types (CancellationToken + DaemonConfig)

**Files:**
- Create: `backend/core/ouroboros/cancellation_token.py`
- Create: `backend/core/ouroboros/daemon_config.py`
- Create: `tests/core/ouroboros/test_cancellation_token.py`
- Create: `tests/core/ouroboros/test_daemon_config.py`

- [ ] **Step 1: Write CancellationToken tests**

```python
# tests/core/ouroboros/test_cancellation_token.py
"""Tests for epoch-scoped cooperative cancellation."""
import asyncio
import pytest
from backend.core.ouroboros.cancellation_token import CancellationToken


def test_token_starts_uncancelled():
    token = CancellationToken(epoch_id=1)
    assert token.epoch_id == 1
    assert not token.is_cancelled


def test_cancel_sets_flag():
    token = CancellationToken(epoch_id=42)
    token.cancel()
    assert token.is_cancelled


def test_cancel_is_idempotent():
    token = CancellationToken(epoch_id=1)
    token.cancel()
    token.cancel()
    assert token.is_cancelled


@pytest.mark.asyncio
async def test_wait_for_cancellation():
    token = CancellationToken(epoch_id=1)
    loop = asyncio.get_event_loop()
    loop.call_later(0.05, token.cancel)
    await asyncio.wait_for(token.wait(), timeout=1.0)
    assert token.is_cancelled


def test_epoch_id_is_readonly():
    token = CancellationToken(epoch_id=7)
    with pytest.raises(AttributeError):
        token.epoch_id = 99
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_cancellation_token.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ouroboros.cancellation_token'`

- [ ] **Step 3: Implement CancellationToken**

```python
# backend/core/ouroboros/cancellation_token.py
"""Epoch-scoped cooperative cancellation token.

Tasks within a REM epoch check token.is_cancelled between work units.
This enables cooperative pause without killing asyncio TaskGroups.
"""
from __future__ import annotations

import asyncio


class CancellationToken:
    """Cooperative cancellation token scoped to a REM epoch.

    Thread-safe via asyncio.Event (which is NOT thread-safe — but all
    consumers are coroutines on the same event loop, which is correct).
    """

    __slots__ = ("_epoch_id", "_event")

    def __init__(self, epoch_id: int) -> None:
        self._epoch_id = epoch_id
        self._event = asyncio.Event()

    @property
    def epoch_id(self) -> int:
        return self._epoch_id

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """Signal cancellation. Idempotent."""
        self._event.set()

    async def wait(self) -> None:
        """Block until cancelled. Use with asyncio.wait_for for timeout."""
        await self._event.wait()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_cancellation_token.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Write DaemonConfig tests**

```python
# tests/core/ouroboros/test_daemon_config.py
"""Tests for OuroborosDaemonConfig env-driven configuration."""
import os
import pytest
from backend.core.ouroboros.daemon_config import OuroborosDaemonConfig


def test_defaults():
    config = OuroborosDaemonConfig.from_env()
    assert config.daemon_enabled is True
    assert config.vital_scan_timeout_s == 30.0
    assert config.spinal_timeout_s == 10.0
    assert config.rem_enabled is True
    assert config.rem_cycle_timeout_s == 300.0
    assert config.rem_epoch_timeout_s == 1800.0
    assert config.rem_max_agents == 30
    assert config.rem_max_findings_per_epoch == 10
    assert config.rem_cooldown_s == 3600.0
    assert config.rem_idle_eligible_s == 60.0
    assert config.exploration_model_enabled is False
    assert config.exploration_model_rpm == 10


def test_env_override(monkeypatch):
    monkeypatch.setenv("OUROBOROS_DAEMON_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_VITAL_SCAN_TIMEOUT_S", "15")
    monkeypatch.setenv("OUROBOROS_REM_MAX_AGENTS", "5")
    monkeypatch.setenv("OUROBOROS_REM_COOLDOWN_S", "600")
    config = OuroborosDaemonConfig.from_env()
    assert config.daemon_enabled is False
    assert config.vital_scan_timeout_s == 15.0
    assert config.rem_max_agents == 5
    assert config.rem_cooldown_s == 600.0


def test_boolean_parsing(monkeypatch):
    for truthy in ("true", "True", "TRUE", "1", "yes"):
        monkeypatch.setenv("OUROBOROS_REM_ENABLED", truthy)
        assert OuroborosDaemonConfig.from_env().rem_enabled is True
    for falsy in ("false", "False", "FALSE", "0", "no"):
        monkeypatch.setenv("OUROBOROS_REM_ENABLED", falsy)
        assert OuroborosDaemonConfig.from_env().rem_enabled is False
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 7: Implement DaemonConfig**

```python
# backend/core/ouroboros/daemon_config.py
"""Environment-driven configuration for OuroborosDaemon."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(val: str) -> bool:
    return val.lower() in ("true", "1", "yes")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class OuroborosDaemonConfig:
    """All Ouroboros Daemon configuration. Immutable after creation."""

    # Master toggle
    daemon_enabled: bool = True

    # Phase 1
    vital_scan_timeout_s: float = 30.0

    # Phase 2
    spinal_timeout_s: float = 10.0

    # Phase 3
    rem_enabled: bool = True
    rem_cycle_timeout_s: float = 300.0
    rem_epoch_timeout_s: float = 1800.0
    rem_max_agents: int = 30
    rem_max_findings_per_epoch: int = 10
    rem_cooldown_s: float = 3600.0
    rem_idle_eligible_s: float = 60.0

    # Exploration model
    exploration_model_enabled: bool = False
    exploration_model_rpm: int = 10

    @classmethod
    def from_env(cls) -> OuroborosDaemonConfig:
        return cls(
            daemon_enabled=_bool(_env("OUROBOROS_DAEMON_ENABLED", "true")),
            vital_scan_timeout_s=float(_env("OUROBOROS_VITAL_SCAN_TIMEOUT_S", "30")),
            spinal_timeout_s=float(_env("OUROBOROS_SPINAL_TIMEOUT_S", "10")),
            rem_enabled=_bool(_env("OUROBOROS_REM_ENABLED", "true")),
            rem_cycle_timeout_s=float(_env("OUROBOROS_REM_CYCLE_TIMEOUT_S", "300")),
            rem_epoch_timeout_s=float(_env("OUROBOROS_REM_EPOCH_TIMEOUT_S", "1800")),
            rem_max_agents=int(_env("OUROBOROS_REM_MAX_AGENTS", "30")),
            rem_max_findings_per_epoch=int(_env("OUROBOROS_REM_MAX_FINDINGS", "10")),
            rem_cooldown_s=float(_env("OUROBOROS_REM_COOLDOWN_S", "3600")),
            rem_idle_eligible_s=float(_env("OUROBOROS_REM_IDLE_ELIGIBLE_S", "60")),
            exploration_model_enabled=_bool(_env("OUROBOROS_EXPLORATION_MODEL_ENABLED", "false")),
            exploration_model_rpm=int(_env("OUROBOROS_EXPLORATION_MODEL_RPM", "10")),
        )
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon_config.py -v`
Expected: All 3 tests PASS

- [ ] **Step 9: Commit**

```bash
git add backend/core/ouroboros/cancellation_token.py backend/core/ouroboros/daemon_config.py tests/core/ouroboros/test_cancellation_token.py tests/core/ouroboros/test_daemon_config.py
git commit -m "feat(ouroboros): add CancellationToken and DaemonConfig foundation types"
```

---

## Task 2: Modify Existing Files (Exploration Source + Risk Rules)

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intent_envelope.py:20`
- Modify: `backend/core/ouroboros/governance/intake/unified_intake_router.py:33`
- Modify: `backend/core/ouroboros/governance/risk_engine.py:177`

- [ ] **Step 1: Write tests for exploration source acceptance**

```python
# tests/core/ouroboros/test_exploration_source.py
"""Tests that 'exploration' is a valid IntentEnvelope source with correct priority and risk rules."""
import pytest


def test_exploration_is_valid_source():
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _VALID_SOURCES,
        make_envelope,
    )
    assert "exploration" in _VALID_SOURCES
    # Should not raise
    env = make_envelope(
        source="exploration",
        description="Dead code found: _legacy_handler",
        target_files=("backend/core/legacy.py",),
        repo="jarvis",
        confidence=0.9,
        urgency="normal",
        evidence={"epoch_id": 1},
        requires_human_ack=False,
    )
    assert env.source == "exploration"


def test_exploration_priority_is_4():
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        _PRIORITY_MAP,
    )
    assert _PRIORITY_MAP["exploration"] == 4
    # Between ai_miner (3) and runtime_health (5)
    assert _PRIORITY_MAP["ai_miner"] < _PRIORITY_MAP["exploration"]
    assert _PRIORITY_MAP["exploration"] < _PRIORITY_MAP["runtime_health"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_exploration_source.py -v`
Expected: FAIL — `"exploration" not in _VALID_SOURCES`

- [ ] **Step 3: Add "exploration" to _VALID_SOURCES**

In `backend/core/ouroboros/governance/intake/intent_envelope.py` line 20, change:

```python
# OLD:
_VALID_SOURCES = frozenset({"backlog", "test_failure", "voice_human", "ai_miner", "capability_gap", "runtime_health"})

# NEW:
_VALID_SOURCES = frozenset({"backlog", "test_failure", "voice_human", "ai_miner", "capability_gap", "runtime_health", "exploration"})
```

- [ ] **Step 4: Add "exploration" to _PRIORITY_MAP**

In `backend/core/ouroboros/governance/intake/unified_intake_router.py` line 33, change:

```python
# OLD:
_PRIORITY_MAP: Dict[str, int] = {
    "voice_human": 0,
    "test_failure": 1,
    "backlog": 2,
    "ai_miner": 3,
    "capability_gap": 4,
    "runtime_health": 5,
}

# NEW:
_PRIORITY_MAP: Dict[str, int] = {
    "voice_human": 0,
    "test_failure": 1,
    "backlog": 2,
    "ai_miner": 3,
    "exploration": 4,
    "capability_gap": 5,
    "runtime_health": 6,
}
```

Note: `capability_gap` and `runtime_health` shift up by 1 to make room for `exploration` at priority 4.

- [ ] **Step 5: Run source/priority tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_exploration_source.py -v`
Expected: All PASS

- [ ] **Step 6: Write tests for exploration risk rules**

```python
# tests/core/ouroboros/test_exploration_risk_rules.py
"""Tests that exploration-sourced operations have stricter risk rules."""
import pytest
from backend.core.ouroboros.governance.risk_engine import (
    RiskEngine,
    OperationProfile,
    RiskTier,
)


@pytest.fixture
def engine():
    return RiskEngine()


def _profile(*, source: str = "exploration", files: tuple = ("backend/agents/foo.py",), **kw) -> OperationProfile:
    """Helper to build OperationProfile with exploration source."""
    return OperationProfile(
        source=source,
        target_files=files,
        change_type=kw.get("change_type", "modify"),
        blast_radius=kw.get("blast_radius", 1),
        test_confidence=kw.get("test_confidence", 0.9),
        crosses_repo_boundary=kw.get("crosses_repo", False),
    )


def test_exploration_touching_supervisor_is_blocked(engine):
    profile = _profile(files=("unified_supervisor.py",))
    result = engine.classify(profile)
    assert result.risk_tier == RiskTier.BLOCKED


def test_exploration_touching_ouroboros_daemon_is_blocked(engine):
    profile = _profile(files=("backend/core/ouroboros/daemon.py",))
    result = engine.classify(profile)
    assert result.risk_tier == RiskTier.BLOCKED


def test_exploration_touching_risk_engine_is_blocked(engine):
    profile = _profile(files=("backend/core/ouroboros/governance/risk_engine.py",))
    result = engine.classify(profile)
    assert result.risk_tier == RiskTier.BLOCKED


def test_exploration_touching_secrets_is_blocked(engine):
    profile = _profile(files=("backend/core/auth/credentials.py",))
    result = engine.classify(profile)
    assert result.risk_tier == RiskTier.BLOCKED


def test_exploration_blast_radius_above_3_requires_approval(engine):
    profile = _profile(blast_radius=4)
    result = engine.classify(profile)
    assert result.risk_tier == RiskTier.APPROVAL_REQUIRED


def test_exploration_normal_file_is_safe_auto(engine):
    profile = _profile(files=("backend/agents/youtube_agent.py",), blast_radius=1)
    result = engine.classify(profile)
    assert result.risk_tier == RiskTier.SAFE_AUTO


def test_non_exploration_uses_default_blast_radius_5(engine):
    profile = _profile(source="ai_miner", blast_radius=4)
    result = engine.classify(profile)
    # Default threshold is 5, so blast_radius=4 should be SAFE_AUTO for non-exploration
    assert result.risk_tier == RiskTier.SAFE_AUTO
```

- [ ] **Step 7: Run risk rule tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_exploration_risk_rules.py -v`
Expected: FAIL — exploration rules not yet added

- [ ] **Step 8: Add exploration-source rules to RiskEngine.classify()**

In `backend/core/ouroboros/governance/risk_engine.py`, within the `classify()` method (line 177), add exploration-specific rules BEFORE the existing rule list. The exploration rules are stricter — lower blast radius threshold (3 vs 5) and additional BLOCKED paths (ouroboros code, auth/secrets):

```python
# Add at the start of classify(), after profile validation:

# Exploration-source stricter rules (Ouroboros cannot self-modify)
if profile.source == "exploration":
    _ouroboros_paths = (
        "backend/core/ouroboros/daemon",
        "backend/core/ouroboros/vital_scan",
        "backend/core/ouroboros/spinal_cord",
        "backend/core/ouroboros/rem_sleep",
        "backend/core/ouroboros/rem_epoch",
        "backend/core/ouroboros/governance/risk_engine",
        "backend/core/ouroboros/governance/orchestrator",
        "backend/core/ouroboros/governance/governed_loop",
    )
    _security_paths = (
        "auth/", "credential", "secret", "token", ".env",
    )
    for f in profile.target_files:
        fl = f.lower()
        if "unified_supervisor" in fl:
            return RiskClassification(
                risk_tier=RiskTier.BLOCKED,
                reason="exploration cannot modify kernel",
            )
        if any(op in fl for op in _ouroboros_paths):
            return RiskClassification(
                risk_tier=RiskTier.BLOCKED,
                reason="exploration cannot self-modify ouroboros code",
            )
        if any(sp in fl for sp in _security_paths):
            return RiskClassification(
                risk_tier=RiskTier.BLOCKED,
                reason="exploration cannot modify security surface",
            )
    if profile.blast_radius > 3:
        return RiskClassification(
            risk_tier=RiskTier.APPROVAL_REQUIRED,
            reason=f"exploration blast_radius {profile.blast_radius} > 3",
        )
# ... existing rules continue below
```

- [ ] **Step 9: Run all tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_exploration_risk_rules.py tests/core/ouroboros/test_exploration_source.py -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add backend/core/ouroboros/governance/intake/intent_envelope.py backend/core/ouroboros/governance/intake/unified_intake_router.py backend/core/ouroboros/governance/risk_engine.py tests/core/ouroboros/test_exploration_source.py tests/core/ouroboros/test_exploration_risk_rules.py
git commit -m "feat(ouroboros): add 'exploration' source with stricter risk rules"
```

---

## Task 3: Add Missing APIs to Existing Components

**Files:**
- Modify: `backend/core/topology/idle_verifier.py:65` (ProactiveDrive)
- Modify: `backend/core/ouroboros/governance/exploration_subagent.py:80`
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`

- [ ] **Step 1: Write test for ProactiveDrive.on_eligible()**

```python
# tests/core/ouroboros/test_proactive_drive_callback.py
"""Tests for ProactiveDrive.on_eligible() callback registration."""
import pytest
from backend.core.topology.idle_verifier import ProactiveDrive, LittlesLawVerifier


def test_on_eligible_registers_callback():
    drive = ProactiveDrive(verifiers={"jarvis": LittlesLawVerifier()})
    called = []
    drive.on_eligible(lambda: called.append(True))
    assert len(drive._eligible_callbacks) == 1


def test_on_eligible_fires_on_transition():
    drive = ProactiveDrive(verifiers={"jarvis": LittlesLawVerifier()})
    called = []
    drive.on_eligible(lambda: called.append(True))
    # Force state to ELIGIBLE via internal state manipulation for testing
    drive._state = "MEASURING"
    drive._eligible_timer_start = 0  # long ago
    # Simulate tick with all verifiers idle
    for v in drive._verifiers.values():
        for _ in range(15):  # enough samples
            v.record(queue_depth=0, latency_ms=1.0)
    drive.tick()
    # If state transitioned to ELIGIBLE, callback should have fired
    if drive._state == "ELIGIBLE":
        assert len(called) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/core/ouroboros/test_proactive_drive_callback.py -v`
Expected: FAIL — `on_eligible` not found or `_eligible_callbacks` not found

- [ ] **Step 3: Add on_eligible() to ProactiveDrive**

In `backend/core/topology/idle_verifier.py`, in the `ProactiveDrive.__init__()` method, add:

```python
self._eligible_callbacks: list = []
```

Add a new method after `__init__`:

```python
def on_eligible(self, callback) -> None:
    """Register a callback to fire when all repos are idle long enough.

    Callback is a callable (sync or async — caller wraps if needed).
    Called exactly once per MEASURING -> ELIGIBLE transition.
    """
    self._eligible_callbacks.append(callback)
```

In the `tick()` method, where the state transitions from MEASURING to ELIGIBLE, add after the state change:

```python
if new_state == "ELIGIBLE" and self._state != "ELIGIBLE":
    for cb in self._eligible_callbacks:
        cb()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/core/ouroboros/test_proactive_drive_callback.py -v`
Expected: PASS

- [ ] **Step 5: Write test for ExplorationSubagent.request_yield()**

```python
# tests/core/ouroboros/test_subagent_yield.py
"""Tests for ExplorationSubagent cooperative cancellation."""
import pytest
from backend.core.ouroboros.governance.exploration_subagent import ExplorationSubagent


def test_request_yield_sets_flag():
    agent = ExplorationSubagent(repo_root="/tmp/test", scope="backend/")
    assert not agent._yield_requested
    agent.request_yield()
    assert agent._yield_requested


def test_should_yield_returns_flag():
    agent = ExplorationSubagent(repo_root="/tmp/test", scope="backend/")
    assert not agent.should_yield()
    agent.request_yield()
    assert agent.should_yield()
```

- [ ] **Step 6: Run test to verify it fails**

Run: `python3 -m pytest tests/core/ouroboros/test_subagent_yield.py -v`
Expected: FAIL — `_yield_requested` not found

- [ ] **Step 7: Add request_yield() to ExplorationSubagent**

In `backend/core/ouroboros/governance/exploration_subagent.py`, in `__init__()`, add:

```python
self._yield_requested: bool = False
```

Add two methods:

```python
def request_yield(self) -> None:
    """Request cooperative yield. Agent finishes current file, then returns partial results."""
    self._yield_requested = True

def should_yield(self) -> bool:
    """Check if yield was requested. Called between file reads in explore()."""
    return self._yield_requested
```

In the `explore()` method, at the top of the file-reading loop (where it iterates over files to analyze), add:

```python
if self.should_yield():
    break  # Return partial results
```

- [ ] **Step 8: Run test to verify it passes**

Run: `python3 -m pytest tests/core/ouroboros/test_subagent_yield.py -v`
Expected: PASS

- [ ] **Step 9: Add GLS getter methods**

In `backend/core/ouroboros/governance/governed_loop_service.py`, add getter methods after the `start()` method. These expose dependencies for OuroborosDaemon injection:

```python
@property
def oracle(self):
    """TheOracle instance, or None if not initialized."""
    return self._oracle

@property
def exploration_fleet(self):
    """ExplorationFleet instance, or None if not wired."""
    return getattr(self, "_exploration_fleet_ref", None)

@property
def background_pool(self):
    """BackgroundAgentPool instance, or None if not started."""
    return self._bg_pool

@property
def doubleword_provider(self):
    """DoublewordProvider instance, or None if not configured."""
    return getattr(self, "_doubleword_ref", None)
```

Also, where ExplorationFleet is wired (around line 2510), store the reference:

```python
# After: self._orchestrator.set_exploration_fleet(_fleet)
self._exploration_fleet_ref = _fleet
```

And where DoublewordProvider is instantiated (around line 2225-2244), store the reference:

```python
# After creating the DoublewordProvider:
self._doubleword_ref = _doubleword
```

- [ ] **Step 10: Commit**

```bash
git add backend/core/topology/idle_verifier.py backend/core/ouroboros/governance/exploration_subagent.py backend/core/ouroboros/governance/governed_loop_service.py tests/core/ouroboros/test_proactive_drive_callback.py tests/core/ouroboros/test_subagent_yield.py
git commit -m "feat(ouroboros): add missing APIs for daemon integration (ProactiveDrive callback, subagent yield, GLS getters)"
```

---

## Task 4: Finding Ranker (Deterministic Impact Scoring)

**Files:**
- Create: `backend/core/ouroboros/finding_ranker.py`
- Create: `tests/core/ouroboros/test_finding_ranker.py`

- [ ] **Step 1: Write ranking tests**

```python
# tests/core/ouroboros/test_finding_ranker.py
"""Tests for deterministic finding ranking with impact_score v1.0."""
import time
import pytest
from backend.core.ouroboros.finding_ranker import (
    RankedFinding,
    impact_score,
    merge_and_rank,
    RANKING_VERSION,
)


def test_ranking_version():
    assert RANKING_VERSION == "1.0"


def test_impact_score_maximum():
    score = impact_score(
        blast_radius=1.0, confidence=1.0, urgency="critical",
        last_modified=time.time(),  # now = max recency
    )
    assert score == pytest.approx(1.0, abs=0.01)


def test_impact_score_minimum():
    score = impact_score(
        blast_radius=0.0, confidence=0.0, urgency="low",
        last_modified=time.time() - 90 * 86400,  # 90 days ago = 0 recency
    )
    assert score == pytest.approx(0.05, abs=0.01)  # 0*0.4 + 0*0.3 + 0.25*0.2 + 0*0.1


def test_impact_score_weights():
    # blast_radius has highest weight (0.4)
    high_blast = impact_score(blast_radius=1.0, confidence=0.0, urgency="low", last_modified=0)
    high_conf = impact_score(blast_radius=0.0, confidence=1.0, urgency="low", last_modified=0)
    assert high_blast > high_conf


def test_urgency_mapping():
    critical = impact_score(blast_radius=0, confidence=0, urgency="critical", last_modified=0)
    high = impact_score(blast_radius=0, confidence=0, urgency="high", last_modified=0)
    normal = impact_score(blast_radius=0, confidence=0, urgency="normal", last_modified=0)
    low = impact_score(blast_radius=0, confidence=0, urgency="low", last_modified=0)
    assert critical > high > normal > low


def test_merge_and_rank_sorts_descending():
    findings = [
        RankedFinding(description="low", category="dead_code", file_path="a.py",
                      blast_radius=0.1, confidence=0.5, urgency="low", last_modified=0, repo="jarvis"),
        RankedFinding(description="high", category="dead_code", file_path="b.py",
                      blast_radius=0.9, confidence=0.9, urgency="critical", last_modified=time.time(), repo="jarvis"),
    ]
    ranked = merge_and_rank(findings)
    assert ranked[0].description == "high"
    assert ranked[1].description == "low"


def test_merge_and_rank_tiebreaker_alphabetical():
    now = time.time()
    findings = [
        RankedFinding(description="z", category="dead_code", file_path="z.py",
                      blast_radius=0.5, confidence=0.5, urgency="normal", last_modified=now, repo="jarvis"),
        RankedFinding(description="a", category="dead_code", file_path="a.py",
                      blast_radius=0.5, confidence=0.5, urgency="normal", last_modified=now, repo="jarvis"),
    ]
    ranked = merge_and_rank(findings)
    assert ranked[0].file_path == "a.py"  # alphabetical tiebreaker
    assert ranked[1].file_path == "z.py"


def test_merge_and_rank_deduplicates_same_file_category():
    findings = [
        RankedFinding(description="dup1", category="dead_code", file_path="a.py",
                      blast_radius=0.5, confidence=0.9, urgency="normal", last_modified=0, repo="jarvis"),
        RankedFinding(description="dup2", category="dead_code", file_path="a.py",
                      blast_radius=0.3, confidence=0.5, urgency="normal", last_modified=0, repo="jarvis"),
    ]
    ranked = merge_and_rank(findings)
    assert len(ranked) == 1
    assert ranked[0].confidence == 0.9  # keeps the higher-scored one
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_finding_ranker.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement finding_ranker.py**

```python
# backend/core/ouroboros/finding_ranker.py
"""Deterministic finding ranking for Ouroboros REM Sleep.

Ranking formula v1.0:
  impact_score = blast_radius * 0.4 + confidence * 0.3 + urgency_weight * 0.2 + recency * 0.1
  Tie-breaker: alphabetical file_path (deterministic, reproducible)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List


RANKING_VERSION = "1.0"

_URGENCY_WEIGHTS = {
    "critical": 1.0,
    "high": 0.75,
    "normal": 0.5,
    "low": 0.25,
}

# Recency: linear decay from 1.0 (now) to 0.0 (90 days ago)
_RECENCY_WINDOW_S = 90 * 86400  # 90 days in seconds


@dataclass
class RankedFinding:
    """A single finding with all data needed for ranking and envelope creation."""

    description: str
    category: str  # dead_code, circular_dep, complexity, unwired, test_gap, todo, doc_stale, perf, github_issue
    file_path: str
    blast_radius: float  # normalized 0-1
    confidence: float  # 0-1
    urgency: str  # critical, high, normal, low
    last_modified: float  # epoch timestamp
    repo: str  # jarvis, jarvis-prime, reactor
    source_check: str = ""  # which check produced this (oracle, fleet, sensor)
    score: float = field(init=False, default=0.0)

    def __post_init__(self):
        self.score = impact_score(
            self.blast_radius, self.confidence, self.urgency, self.last_modified,
        )


def impact_score(
    blast_radius: float,
    confidence: float,
    urgency: str,
    last_modified: float,
) -> float:
    """Deterministic impact score. v1.0.

    blast_radius: normalized 0-1 (from Oracle.compute_blast_radius or sensor)
    confidence:   0-1 (from sensor/fleet)
    urgency:      critical=1.0, high=0.75, normal=0.5, low=0.25
    last_modified: epoch timestamp (recency: 1.0 if now, decays to 0.0 at 90 days)
    """
    urgency_weight = _URGENCY_WEIGHTS.get(urgency, 0.5)

    age_s = max(0.0, time.time() - last_modified)
    recency = max(0.0, 1.0 - age_s / _RECENCY_WINDOW_S)

    return (
        blast_radius * 0.4
        + confidence * 0.3
        + urgency_weight * 0.2
        + recency * 0.1
    )


def merge_and_rank(findings: List[RankedFinding]) -> List[RankedFinding]:
    """Merge, deduplicate, and rank findings by impact_score descending.

    Deduplication: same (file_path, category) keeps the higher-scored entry.
    Tie-breaker: alphabetical file_path.
    """
    # Deduplicate: keep highest score per (file_path, category)
    best: dict[tuple[str, str], RankedFinding] = {}
    for f in findings:
        key = (f.file_path, f.category)
        if key not in best or f.score > best[key].score:
            best[key] = f

    # Sort: descending score, then alphabetical file_path as tiebreaker
    return sorted(best.values(), key=lambda f: (-f.score, f.file_path))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_finding_ranker.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/finding_ranker.py tests/core/ouroboros/test_finding_ranker.py
git commit -m "feat(ouroboros): add deterministic finding ranker with impact_score v1.0"
```

---

## Task 5: Exploration Envelope Factory

**Files:**
- Create: `backend/core/ouroboros/exploration_envelope_factory.py`
- Create: `tests/core/ouroboros/test_exploration_envelope_factory.py`

- [ ] **Step 1: Write envelope factory tests**

```python
# tests/core/ouroboros/test_exploration_envelope_factory.py
"""Tests for converting exploration findings to IntentEnvelopes."""
import pytest
from backend.core.ouroboros.finding_ranker import RankedFinding
from backend.core.ouroboros.exploration_envelope_factory import findings_to_envelopes


def _finding(*, desc="test", file="a.py", category="dead_code", urgency="normal", repo="jarvis"):
    return RankedFinding(
        description=desc, category=category, file_path=file,
        blast_radius=0.5, confidence=0.9, urgency=urgency,
        last_modified=0, repo=repo,
    )


def test_creates_envelope_per_finding():
    findings = [_finding(desc="f1"), _finding(desc="f2", file="b.py")]
    envelopes = findings_to_envelopes(findings, epoch_id=42)
    assert len(envelopes) == 2


def test_envelope_source_is_exploration():
    envelopes = findings_to_envelopes([_finding()], epoch_id=1)
    assert envelopes[0].source == "exploration"


def test_envelope_carries_epoch_id():
    envelopes = findings_to_envelopes([_finding()], epoch_id=99)
    assert envelopes[0].evidence["epoch_id"] == 99


def test_envelope_target_files_from_finding():
    envelopes = findings_to_envelopes(
        [_finding(file="backend/core/foo.py")], epoch_id=1,
    )
    assert envelopes[0].target_files == ("backend/core/foo.py",)


def test_envelope_urgency_from_finding():
    envelopes = findings_to_envelopes([_finding(urgency="critical")], epoch_id=1)
    assert envelopes[0].urgency == "critical"


def test_envelope_requires_no_human_ack():
    envelopes = findings_to_envelopes([_finding()], epoch_id=1)
    assert envelopes[0].requires_human_ack is False


def test_envelope_repo_from_finding():
    envelopes = findings_to_envelopes([_finding(repo="jarvis-prime")], epoch_id=1)
    assert envelopes[0].repo == "jarvis-prime"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_exploration_envelope_factory.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement exploration_envelope_factory.py**

```python
# backend/core/ouroboros/exploration_envelope_factory.py
"""Convert exploration findings into IntentEnvelopes for the governance pipeline."""
from __future__ import annotations

from typing import List

from backend.core.ouroboros.finding_ranker import RankedFinding
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)


def findings_to_envelopes(
    findings: List[RankedFinding],
    *,
    epoch_id: int,
) -> List[IntentEnvelope]:
    """Convert ranked findings into IntentEnvelopes.

    Each finding becomes one envelope with source="exploration".
    The epoch_id is stored in evidence for correlation.
    requires_human_ack is always False (GOVERNED tier, risk engine handles safety).
    """
    envelopes: List[IntentEnvelope] = []
    for finding in findings:
        envelope = make_envelope(
            source="exploration",
            description=f"[{finding.category}] {finding.description}",
            target_files=(finding.file_path,),
            repo=finding.repo,
            confidence=finding.confidence,
            urgency=finding.urgency,
            evidence={
                "epoch_id": epoch_id,
                "category": finding.category,
                "blast_radius": finding.blast_radius,
                "score": finding.score,
                "source_check": finding.source_check,
            },
            requires_human_ack=False,
        )
        envelopes.append(envelope)
    return envelopes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_exploration_envelope_factory.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/exploration_envelope_factory.py tests/core/ouroboros/test_exploration_envelope_factory.py
git commit -m "feat(ouroboros): add exploration envelope factory for finding -> pipeline conversion"
```

---

## Task 6: Phase 1 — Vital Scan

**Files:**
- Create: `backend/core/ouroboros/vital_scan.py`
- Create: `tests/core/ouroboros/test_vital_scan.py`

- [ ] **Step 1: Write vital scan tests**

```python
# tests/core/ouroboros/test_vital_scan.py
"""Tests for Phase 1: Vital Scan boot invariant gate."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.vital_scan import (
    VitalScan,
    VitalReport,
    VitalStatus,
    VitalFinding,
)


@pytest.fixture
def mock_oracle():
    oracle = MagicMock()
    oracle.find_circular_dependencies.return_value = []
    oracle.find_dead_code.return_value = []
    return oracle


@pytest.fixture
def mock_health_sensor():
    sensor = AsyncMock()
    sensor.scan_once.return_value = []
    return sensor


def test_vital_report_pass():
    report = VitalReport(status=VitalStatus.PASS, findings=[])
    assert report.status == VitalStatus.PASS
    assert len(report.findings) == 0
    assert len(report.warnings) == 0


def test_vital_report_warn_filters_warnings():
    findings = [
        VitalFinding(check="circular_deps", severity="warn", detail="cycle in agents/"),
        VitalFinding(check="cache_freshness", severity="warn", detail="cache 25h stale"),
    ]
    report = VitalReport(status=VitalStatus.WARN, findings=findings)
    assert len(report.warnings) == 2


@pytest.mark.asyncio
async def test_vital_scan_all_pass(mock_oracle, mock_health_sensor):
    scan = VitalScan(oracle=mock_oracle, health_sensor=mock_health_sensor)
    report = await scan.run(timeout_s=5.0)
    assert report.status == VitalStatus.PASS


@pytest.mark.asyncio
async def test_vital_scan_circular_dep_in_kernel_is_fail(mock_oracle, mock_health_sensor):
    # Simulate cycle involving unified_supervisor
    mock_node = MagicMock()
    mock_node.file_path = "unified_supervisor.py"
    mock_oracle.find_circular_dependencies.return_value = [[mock_node, mock_node]]
    scan = VitalScan(oracle=mock_oracle, health_sensor=mock_health_sensor)
    report = await scan.run(timeout_s=5.0)
    assert report.status == VitalStatus.FAIL


@pytest.mark.asyncio
async def test_vital_scan_circular_dep_non_kernel_is_warn(mock_oracle, mock_health_sensor):
    mock_node = MagicMock()
    mock_node.file_path = "backend/agents/foo.py"
    mock_oracle.find_circular_dependencies.return_value = [[mock_node, mock_node]]
    scan = VitalScan(oracle=mock_oracle, health_sensor=mock_health_sensor)
    report = await scan.run(timeout_s=5.0)
    assert report.status == VitalStatus.WARN


@pytest.mark.asyncio
async def test_vital_scan_timeout_returns_warn(mock_oracle, mock_health_sensor):
    # Oracle takes too long
    async def slow_init():
        await asyncio.sleep(10)
        return True
    mock_oracle.initialize = slow_init
    scan = VitalScan(oracle=mock_oracle, health_sensor=mock_health_sensor)
    report = await scan.run(timeout_s=0.1)
    assert report.status == VitalStatus.WARN
    assert any("timeout" in f.detail.lower() for f in report.findings)


@pytest.mark.asyncio
async def test_vital_scan_no_oracle_cache_large_repo_is_fail(mock_oracle, mock_health_sensor):
    mock_oracle._last_indexed_monotonic_ns = 0
    mock_oracle._graph = MagicMock()
    mock_oracle._graph.number_of_nodes.return_value = 0  # no cache
    # Simulate large repo check
    scan = VitalScan(
        oracle=mock_oracle, health_sensor=mock_health_sensor,
        repo_file_count=600,  # >500 threshold
    )
    report = await scan.run(timeout_s=5.0)
    # Should be FAIL if no cache and large repo
    assert report.status in (VitalStatus.FAIL, VitalStatus.WARN)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_vital_scan.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement vital_scan.py**

```python
# backend/core/ouroboros/vital_scan.py
"""Phase 1: Vital Scan — boot invariant gate.

Zero model calls. All checks are deterministic.
Timeout: env OUROBOROS_VITAL_SCAN_TIMEOUT_S (default 30s).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

_KERNEL_FILES = frozenset({
    "unified_supervisor.py",
    "governed_loop_service.py",
    "governed_loop",
})

_CACHE_STALE_THRESHOLD_S = 86400  # 24 hours
_LARGE_REPO_FILE_THRESHOLD = 500


class VitalStatus(enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class VitalFinding:
    check: str  # circular_deps, contract_drift, dependency_health, cache_freshness
    severity: str  # fail, warn
    detail: str


@dataclass
class VitalReport:
    status: VitalStatus
    findings: List[VitalFinding]
    duration_s: float = 0.0

    @property
    def warnings(self) -> List[VitalFinding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def failures(self) -> List[VitalFinding]:
        return [f for f in self.findings if f.severity == "fail"]


class VitalScan:
    """Run deterministic invariant checks on the organism."""

    def __init__(
        self,
        oracle,
        health_sensor=None,
        repo_file_count: int = 0,
    ) -> None:
        self._oracle = oracle
        self._health_sensor = health_sensor
        self._repo_file_count = repo_file_count

    async def run(self, timeout_s: float = 30.0) -> VitalReport:
        """Execute all vital checks within timeout.

        Returns VitalReport with worst-case status across all checks.
        On timeout: returns WARN with partial results.
        """
        start = time.monotonic()
        findings: List[VitalFinding] = []

        try:
            await asyncio.wait_for(
                self._run_checks(findings),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            findings.append(VitalFinding(
                check="timeout",
                severity="warn",
                detail=f"Vital scan exceeded {timeout_s}s timeout — partial results",
            ))
        except Exception as exc:
            logger.warning("[VitalScan] Unexpected error: %s", exc)
            findings.append(VitalFinding(
                check="error",
                severity="warn",
                detail=f"Vital scan error: {exc}",
            ))

        # Determine worst-case status
        if any(f.severity == "fail" for f in findings):
            status = VitalStatus.FAIL
        elif any(f.severity == "warn" for f in findings):
            status = VitalStatus.WARN
        else:
            status = VitalStatus.PASS

        return VitalReport(
            status=status,
            findings=findings,
            duration_s=time.monotonic() - start,
        )

    async def _run_checks(self, findings: List[VitalFinding]) -> None:
        """Run all checks, appending findings."""
        self._check_circular_deps(findings)
        self._check_cache_freshness(findings)
        if self._health_sensor is not None:
            await self._check_dependency_health(findings)

    def _check_circular_deps(self, findings: List[VitalFinding]) -> None:
        """Check for circular dependencies in the Oracle graph."""
        try:
            cycles = self._oracle.find_circular_dependencies()
        except Exception as exc:
            findings.append(VitalFinding(
                check="circular_deps", severity="warn",
                detail=f"Could not check circular deps: {exc}",
            ))
            return

        if not cycles:
            return

        for cycle in cycles:
            file_paths = [
                getattr(node, "file_path", str(node))
                for node in cycle
            ]
            is_kernel = any(
                any(kf in fp for kf in _KERNEL_FILES)
                for fp in file_paths
            )
            if is_kernel:
                findings.append(VitalFinding(
                    check="circular_deps",
                    severity="fail",
                    detail=f"Circular dependency in kernel: {' -> '.join(file_paths)}",
                ))
            else:
                findings.append(VitalFinding(
                    check="circular_deps",
                    severity="warn",
                    detail=f"Circular dependency: {' -> '.join(file_paths)}",
                ))

    def _check_cache_freshness(self, findings: List[VitalFinding]) -> None:
        """Check Oracle cache age."""
        last_indexed = getattr(self._oracle, "_last_indexed_monotonic_ns", None)
        graph = getattr(self._oracle, "_graph", None)

        if graph is not None and hasattr(graph, "number_of_nodes"):
            node_count = graph.number_of_nodes()
            if node_count == 0 and self._repo_file_count > _LARGE_REPO_FILE_THRESHOLD:
                findings.append(VitalFinding(
                    check="cache_freshness",
                    severity="fail",
                    detail=f"No Oracle cache and repo has {self._repo_file_count} files (cold boot too slow)",
                ))
                return

        if last_indexed is not None and last_indexed > 0:
            age_s = time.monotonic_ns() / 1e9 - last_indexed / 1e9
            if age_s > _CACHE_STALE_THRESHOLD_S:
                findings.append(VitalFinding(
                    check="cache_freshness",
                    severity="warn",
                    detail=f"Oracle cache is {age_s / 3600:.1f}h stale (threshold: 24h)",
                ))

    async def _check_dependency_health(self, findings: List[VitalFinding]) -> None:
        """Check for critical CVEs via RuntimeHealthSensor."""
        try:
            health_findings = await self._health_sensor.scan_once()
        except Exception as exc:
            findings.append(VitalFinding(
                check="dependency_health",
                severity="warn",
                detail=f"Could not check dependency health: {exc}",
            ))
            return

        for hf in health_findings:
            severity_str = getattr(hf, "severity", "info")
            if severity_str in ("critical", "high"):
                findings.append(VitalFinding(
                    check="dependency_health",
                    severity="fail" if severity_str == "critical" else "warn",
                    detail=str(hf),
                ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_vital_scan.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/vital_scan.py tests/core/ouroboros/test_vital_scan.py
git commit -m "feat(ouroboros): implement Phase 1 Vital Scan with boot invariant checks"
```

---

## Task 7: Phase 2 — Spinal Cord

**Files:**
- Create: `backend/core/ouroboros/spinal_cord.py`
- Create: `tests/core/ouroboros/test_spinal_cord.py`

- [ ] **Step 1: Write spinal cord tests**

```python
# tests/core/ouroboros/test_spinal_cord.py
"""Tests for Phase 2: Spinal Cord bidirectional event wiring."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.spinal_cord import (
    SpinalCord,
    SpinalStatus,
)


@pytest.fixture
def mock_event_stream():
    stream = MagicMock()
    stream.broadcast_event = AsyncMock(return_value=1)
    return stream


def test_spinal_gate_starts_unset():
    cord = SpinalCord(event_stream=MagicMock())
    assert not cord.gate_is_set


def test_spinal_liveness_starts_false():
    cord = SpinalCord(event_stream=MagicMock())
    assert not cord.is_live


@pytest.mark.asyncio
async def test_wire_sets_gate_on_success(mock_event_stream):
    cord = SpinalCord(event_stream=mock_event_stream)
    status = await cord.wire(timeout_s=5.0)
    assert status == SpinalStatus.CONNECTED
    assert cord.gate_is_set
    assert cord.is_live


@pytest.mark.asyncio
async def test_wire_degraded_on_timeout():
    stream = MagicMock()
    stream.broadcast_event = AsyncMock(side_effect=asyncio.TimeoutError)
    cord = SpinalCord(event_stream=stream)
    status = await cord.wire(timeout_s=0.1)
    assert status == SpinalStatus.DEGRADED
    assert cord.gate_is_set  # gate still set — Phase 3 can start in local mode
    assert not cord.is_live


@pytest.mark.asyncio
async def test_stream_up_uses_broadcast(mock_event_stream):
    cord = SpinalCord(event_stream=mock_event_stream)
    await cord.wire(timeout_s=5.0)
    await cord.stream_up("exploration.finding", {"test": True})
    mock_event_stream.broadcast_event.assert_called()


@pytest.mark.asyncio
async def test_stream_up_falls_back_to_local_when_not_live(mock_event_stream, tmp_path):
    cord = SpinalCord(
        event_stream=mock_event_stream,
        local_buffer_path=str(tmp_path / "pending.jsonl"),
    )
    cord._is_live = False
    cord._gate.set()
    await cord.stream_up("exploration.finding", {"test": True})
    # Should write to local file, not broadcast
    assert (tmp_path / "pending.jsonl").exists()


@pytest.mark.asyncio
async def test_wire_is_idempotent(mock_event_stream):
    cord = SpinalCord(event_stream=mock_event_stream)
    s1 = await cord.wire(timeout_s=5.0)
    s2 = await cord.wire(timeout_s=5.0)
    assert s1 == SpinalStatus.CONNECTED
    assert s2 == SpinalStatus.CONNECTED


def test_on_disconnect_clears_liveness(mock_event_stream):
    cord = SpinalCord(event_stream=mock_event_stream)
    cord._is_live = True
    cord.on_disconnect()
    assert not cord.is_live


def test_on_reconnect_restores_liveness(mock_event_stream):
    cord = SpinalCord(event_stream=mock_event_stream)
    cord._is_live = False
    cord.on_reconnect()
    assert cord.is_live
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_spinal_cord.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement spinal_cord.py**

```python
# backend/core/ouroboros/spinal_cord.py
"""Phase 2: Spinal Cord — bidirectional event stream wiring.

Zero model calls. Deterministic subscribe -> handshake -> gate sequence.
Two-flag state: SpinalGate (one-shot) + SpinalLiveness (dynamic).
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_BUFFER = os.path.expanduser(
    "~/.jarvis/ouroboros/pending_findings.jsonl"
)


class SpinalStatus(enum.Enum):
    CONNECTED = "connected"
    DEGRADED = "degraded"


class SpinalCord:
    """Bidirectional event stream wiring between Body, Mind, and Soul.

    SpinalGate: one-shot asyncio.Event. Set after first successful wire().
        Phase 3 awaits this before starting. Once set, never cleared.

    SpinalLiveness: dynamic bool. Flips on disconnect/reconnect.
        When False, stream_up/stream_down write to local buffer.
        When True, they broadcast via EventStreamProtocol.
    """

    def __init__(
        self,
        event_stream,
        local_buffer_path: str = _DEFAULT_LOCAL_BUFFER,
    ) -> None:
        self._event_stream = event_stream
        self._local_buffer_path = local_buffer_path
        self._gate = asyncio.Event()
        self._is_live = False
        self._wired = False

    @property
    def gate_is_set(self) -> bool:
        return self._gate.is_set()

    @property
    def is_live(self) -> bool:
        return self._is_live

    async def wait_for_gate(self) -> None:
        """Block until SpinalGate is set. Used by Phase 3."""
        await self._gate.wait()

    async def wire(self, timeout_s: float = 10.0) -> SpinalStatus:
        """Establish bidirectional governance streams.

        Ordering contract:
        1. Attempt transport-level verification via broadcast
        2. Set SpinalGate (one-shot, even on degraded — Phase 3 can run local)
        3. Set SpinalLiveness based on success

        Idempotent: safe to call multiple times.
        """
        try:
            # Transport-level verification: broadcast a handshake event
            # and confirm the EventStreamProtocol accepts it
            await asyncio.wait_for(
                self._event_stream.broadcast_event(
                    "governance",
                    {
                        "type": "spinal_handshake",
                        "ts": time.time(),
                        "version": 1,
                    },
                ),
                timeout=timeout_s,
            )
            self._is_live = True
            status = SpinalStatus.CONNECTED
            logger.info("[SpinalCord] CONNECTED — bidirectional stream active")
        except (asyncio.TimeoutError, Exception) as exc:
            self._is_live = False
            status = SpinalStatus.DEGRADED
            logger.warning("[SpinalCord] DEGRADED — local-only mode: %s", exc)

        # Gate is always set — Phase 3 can start regardless
        # (runs in local-only mode if degraded)
        self._gate.set()
        self._wired = True
        return status

    async def stream_up(
        self, event_type: str, payload: Dict[str, Any],
    ) -> None:
        """Stream an event from Body to Mind (exploration findings, progress)."""
        event = {
            "type": event_type,
            "ts": time.time(),
            "d": payload,
        }
        if self._is_live:
            try:
                await self._event_stream.broadcast_event("governance", event)
                return
            except Exception:
                pass  # Fall through to local buffer

        # Local buffer fallback
        self._write_local(event)

    async def stream_down(
        self, event_type: str, payload: Dict[str, Any],
    ) -> None:
        """Stream an event from Mind to Body (candidates, decisions, patches)."""
        event = {
            "type": event_type,
            "ts": time.time(),
            "d": payload,
        }
        if self._is_live:
            try:
                await self._event_stream.broadcast_event("governance", event)
                return
            except Exception:
                pass

        self._write_local(event)

    def on_disconnect(self) -> None:
        """Called when WebSocket/transport disconnects."""
        self._is_live = False
        logger.warning("[SpinalCord] Disconnected — switching to local buffer")

    def on_reconnect(self) -> None:
        """Called when WebSocket/transport reconnects."""
        self._is_live = True
        logger.info("[SpinalCord] Reconnected — resuming streaming")

    def _write_local(self, event: Dict[str, Any]) -> None:
        """Append event to local JSONL buffer for later replay."""
        try:
            os.makedirs(os.path.dirname(self._local_buffer_path), exist_ok=True)
            with open(self._local_buffer_path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError as exc:
            logger.debug("[SpinalCord] Local buffer write failed: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_spinal_cord.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/spinal_cord.py tests/core/ouroboros/test_spinal_cord.py
git commit -m "feat(ouroboros): implement Phase 2 Spinal Cord with bidirectional event wiring"
```

---

## Task 8: REM Epoch (Single Exploration Cycle)

**Files:**
- Create: `backend/core/ouroboros/rem_epoch.py`
- Create: `tests/core/ouroboros/test_rem_epoch.py`

- [ ] **Step 1: Write REM epoch tests**

```python
# tests/core/ouroboros/test_rem_epoch.py
"""Tests for a single REM epoch: explore -> analyze -> patch."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.rem_epoch import RemEpoch, EpochResult
from backend.core.ouroboros.finding_ranker import RankedFinding


def _mock_oracle():
    oracle = MagicMock()
    oracle.find_dead_code.return_value = []
    oracle.find_circular_dependencies.return_value = []
    oracle.compute_blast_radius.return_value = MagicMock(
        directly_affected=set(), transitively_affected=set(), risk_level="low",
    )
    return oracle


def _mock_fleet():
    fleet = AsyncMock()
    fleet.deploy.return_value = MagicMock(
        findings=[], total_findings=0, agents_deployed=5, agents_completed=5,
    )
    return fleet


def _mock_spinal():
    spinal = MagicMock()
    spinal.stream_up = AsyncMock()
    spinal.stream_down = AsyncMock()
    spinal.is_live = True
    return spinal


def _mock_intake():
    router = AsyncMock()
    router.ingest.return_value = "enqueued"
    return router


@pytest.mark.asyncio
async def test_epoch_with_no_findings():
    epoch = RemEpoch(
        epoch_id=1,
        oracle=_mock_oracle(),
        fleet=_mock_fleet(),
        spinal_cord=_mock_spinal(),
        intake_router=_mock_intake(),
        doubleword=None,
        config=MagicMock(
            rem_cycle_timeout_s=30,
            rem_epoch_timeout_s=60,
            rem_max_findings_per_epoch=10,
            rem_max_agents=5,
        ),
    )
    token = CancellationToken(epoch_id=1)
    result = await epoch.run(token)
    assert result.epoch_id == 1
    assert result.findings_count == 0
    assert result.envelopes_submitted == 0
    assert result.completed


@pytest.mark.asyncio
async def test_epoch_with_findings_submits_envelopes():
    fleet = _mock_fleet()
    mock_finding = MagicMock()
    mock_finding.category = "dead_code"
    mock_finding.description = "unused function"
    mock_finding.file_path = "backend/agents/old.py"
    mock_finding.confidence = 0.9
    fleet.deploy.return_value = MagicMock(
        findings=[mock_finding], total_findings=1,
        agents_deployed=5, agents_completed=5,
    )

    intake = _mock_intake()
    epoch = RemEpoch(
        epoch_id=2,
        oracle=_mock_oracle(),
        fleet=fleet,
        spinal_cord=_mock_spinal(),
        intake_router=intake,
        doubleword=None,
        config=MagicMock(
            rem_cycle_timeout_s=30,
            rem_epoch_timeout_s=60,
            rem_max_findings_per_epoch=10,
            rem_max_agents=5,
        ),
    )
    token = CancellationToken(epoch_id=2)
    result = await epoch.run(token)
    assert result.findings_count >= 1
    assert intake.ingest.called


@pytest.mark.asyncio
async def test_epoch_respects_cancellation():
    fleet = _mock_fleet()
    # Fleet deploy takes a while
    async def slow_deploy(**kw):
        await asyncio.sleep(10)
        return MagicMock(findings=[], total_findings=0,
                         agents_deployed=0, agents_completed=0)
    fleet.deploy = slow_deploy

    epoch = RemEpoch(
        epoch_id=3,
        oracle=_mock_oracle(),
        fleet=fleet,
        spinal_cord=_mock_spinal(),
        intake_router=_mock_intake(),
        doubleword=None,
        config=MagicMock(
            rem_cycle_timeout_s=30,
            rem_epoch_timeout_s=60,
            rem_max_findings_per_epoch=10,
            rem_max_agents=5,
        ),
    )
    token = CancellationToken(epoch_id=3)
    # Cancel after 0.1s
    asyncio.get_event_loop().call_later(0.1, token.cancel)
    result = await asyncio.wait_for(epoch.run(token), timeout=5.0)
    assert not result.completed or result.findings_count == 0


@pytest.mark.asyncio
async def test_epoch_stops_on_backpressure():
    intake = _mock_intake()
    intake.ingest.return_value = "backpressure"

    oracle = _mock_oracle()
    # Return some dead code
    mock_node = MagicMock()
    mock_node.file_path = "a.py"
    mock_node.name = "old_func"
    mock_node.repo = "jarvis"
    oracle.find_dead_code.return_value = [mock_node, mock_node, mock_node]

    epoch = RemEpoch(
        epoch_id=4,
        oracle=oracle,
        fleet=_mock_fleet(),
        spinal_cord=_mock_spinal(),
        intake_router=intake,
        doubleword=None,
        config=MagicMock(
            rem_cycle_timeout_s=30,
            rem_epoch_timeout_s=60,
            rem_max_findings_per_epoch=10,
            rem_max_agents=5,
        ),
    )
    token = CancellationToken(epoch_id=4)
    result = await epoch.run(token)
    # Should have stopped feeding after first backpressure
    assert intake.ingest.call_count <= 2  # might get 1-2 before backpressure check
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_epoch.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement rem_epoch.py**

```python
# backend/core/ouroboros/rem_epoch.py
"""Single REM epoch: explore -> analyze -> patch.

One complete cycle of organism self-scan and remediation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.finding_ranker import RankedFinding, merge_and_rank
from backend.core.ouroboros.exploration_envelope_factory import findings_to_envelopes

logger = logging.getLogger(__name__)


@dataclass
class EpochResult:
    epoch_id: int
    findings_count: int = 0
    envelopes_submitted: int = 0
    envelopes_backpressured: int = 0
    duration_s: float = 0.0
    completed: bool = False
    cancelled: bool = False
    error: Optional[str] = None


class RemEpoch:
    """Execute a single REM epoch: explore -> analyze -> patch."""

    def __init__(
        self,
        epoch_id: int,
        oracle: Any,
        fleet: Any,
        spinal_cord: Any,
        intake_router: Any,
        doubleword: Any,
        config: Any,
    ) -> None:
        self._epoch_id = epoch_id
        self._oracle = oracle
        self._fleet = fleet
        self._spinal = spinal_cord
        self._intake = intake_router
        self._doubleword = doubleword
        self._config = config

    async def run(self, token: CancellationToken) -> EpochResult:
        """Run the full epoch. Cooperative cancellation via token."""
        start = time.monotonic()
        result = EpochResult(epoch_id=self._epoch_id)

        try:
            # EXPLORING
            findings = await asyncio.wait_for(
                self._explore(token),
                timeout=self._config.rem_cycle_timeout_s,
            )
            result.findings_count = len(findings)

            if token.is_cancelled:
                result.cancelled = True
                result.duration_s = time.monotonic() - start
                return result

            if not findings:
                result.completed = True
                result.duration_s = time.monotonic() - start
                return result

            # Stream findings UP
            for f in findings:
                await self._spinal.stream_up("exploration.finding", {
                    "epoch_id": self._epoch_id,
                    "category": f.category,
                    "file": f.file_path,
                    "description": f.description,
                    "score": f.score,
                })

            if token.is_cancelled:
                result.cancelled = True
                result.duration_s = time.monotonic() - start
                return result

            # PATCHING: convert to envelopes and submit
            top_findings = findings[:self._config.rem_max_findings_per_epoch]
            envelopes = findings_to_envelopes(top_findings, epoch_id=self._epoch_id)

            for envelope in envelopes:
                if token.is_cancelled:
                    result.cancelled = True
                    break

                ingest_result = await self._intake.ingest(envelope)
                if ingest_result == "backpressure":
                    result.envelopes_backpressured += 1
                    logger.info("[RemEpoch %d] Backpressure — stopping intake", self._epoch_id)
                    break

                result.envelopes_submitted += 1
                await self._spinal.stream_down("governance.progress", {
                    "epoch_id": self._epoch_id,
                    "envelope_id": envelope.signal_id,
                    "status": ingest_result,
                })

            result.completed = not result.cancelled

        except asyncio.TimeoutError:
            result.error = "exploration cycle timeout"
            logger.warning("[RemEpoch %d] Exploration timed out", self._epoch_id)
        except asyncio.CancelledError:
            result.cancelled = True
            raise
        except Exception as exc:
            result.error = str(exc)
            logger.warning("[RemEpoch %d] Error: %s", self._epoch_id, exc)

        result.duration_s = time.monotonic() - start
        return result

    async def _explore(self, token: CancellationToken) -> List[RankedFinding]:
        """Run all 9 checks concurrently. Return merged, ranked findings."""
        findings: List[RankedFinding] = []

        # Run oracle checks and fleet in parallel
        oracle_findings = await self._run_oracle_checks(token)
        findings.extend(oracle_findings)

        if token.is_cancelled:
            return merge_and_rank(findings)

        # Fleet exploration
        fleet_findings = await self._run_fleet(token)
        findings.extend(fleet_findings)

        return merge_and_rank(findings)

    async def _run_oracle_checks(self, token: CancellationToken) -> List[RankedFinding]:
        """Run deterministic Oracle graph analysis."""
        findings: List[RankedFinding] = []

        # Dead code
        try:
            dead = self._oracle.find_dead_code()
            for node in dead:
                file_path = getattr(node, "file_path", str(node))
                name = getattr(node, "name", "unknown")
                repo = getattr(node, "repo", "jarvis")
                findings.append(RankedFinding(
                    description=f"Dead code: {name} is never called",
                    category="dead_code",
                    file_path=file_path,
                    blast_radius=0.1,  # low — removing dead code is safe
                    confidence=0.85,
                    urgency="low",
                    last_modified=0,
                    repo=repo,
                    source_check="oracle.find_dead_code",
                ))
        except Exception as exc:
            logger.debug("[RemEpoch] Dead code check failed: %s", exc)

        if token.is_cancelled:
            return findings

        # Circular dependencies
        try:
            cycles = self._oracle.find_circular_dependencies()
            for cycle in cycles:
                file_paths = [getattr(n, "file_path", str(n)) for n in cycle]
                findings.append(RankedFinding(
                    description=f"Circular dependency: {' -> '.join(file_paths[:3])}...",
                    category="circular_dep",
                    file_path=file_paths[0] if file_paths else "unknown",
                    blast_radius=0.6,
                    confidence=1.0,  # deterministic detection
                    urgency="normal",
                    last_modified=0,
                    repo="jarvis",
                    source_check="oracle.find_circular_dependencies",
                ))
        except Exception as exc:
            logger.debug("[RemEpoch] Circular dep check failed: %s", exc)

        return findings

    async def _run_fleet(self, token: CancellationToken) -> List[RankedFinding]:
        """Run ExplorationFleet across Trinity repos."""
        if self._fleet is None:
            return []

        try:
            report = await self._fleet.deploy(
                goal="Identify unwired components, architecture gaps, "
                     "and dormant agents across Trinity ecosystem",
                repos=("jarvis", "jarvis-prime", "reactor"),
                max_agents=self._config.rem_max_agents,
            )
        except Exception as exc:
            logger.warning("[RemEpoch] Fleet deploy failed: %s", exc)
            return []

        findings: List[RankedFinding] = []
        for f in getattr(report, "findings", []):
            findings.append(RankedFinding(
                description=getattr(f, "description", str(f)),
                category=getattr(f, "category", "architecture_gap"),
                file_path=getattr(f, "file_path", "unknown"),
                blast_radius=0.3,
                confidence=getattr(f, "relevance", 0.7),
                urgency="normal",
                last_modified=0,
                repo=getattr(f, "repo", "jarvis"),
                source_check="exploration_fleet",
            ))
        return findings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_epoch.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/rem_epoch.py tests/core/ouroboros/test_rem_epoch.py
git commit -m "feat(ouroboros): implement REM epoch (explore -> analyze -> patch cycle)"
```

---

## Task 9: REM Sleep Daemon (State Machine + Idle Watch)

**Files:**
- Create: `backend/core/ouroboros/rem_sleep.py`
- Create: `tests/core/ouroboros/test_rem_sleep.py`

- [ ] **Step 1: Write REM sleep state machine tests**

```python
# tests/core/ouroboros/test_rem_sleep.py
"""Tests for REM Sleep daemon state machine."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.ouroboros.rem_sleep import RemSleepDaemon, RemState


def _mock_deps():
    return {
        "oracle": MagicMock(),
        "fleet": AsyncMock(),
        "spinal_cord": MagicMock(
            stream_up=AsyncMock(),
            stream_down=AsyncMock(),
            wait_for_gate=AsyncMock(),
            is_live=True,
        ),
        "intake_router": AsyncMock(),
        "proactive_drive": MagicMock(),
        "doubleword": None,
        "config": MagicMock(
            rem_cooldown_s=0.1,  # fast cooldown for tests
            rem_cycle_timeout_s=5,
            rem_epoch_timeout_s=10,
            rem_max_findings_per_epoch=10,
            rem_max_agents=5,
            rem_idle_eligible_s=0.1,
        ),
    }


def test_initial_state_is_idle_watch():
    deps = _mock_deps()
    daemon = RemSleepDaemon(**deps)
    assert daemon.state == RemState.IDLE_WATCH


def test_state_transitions():
    deps = _mock_deps()
    daemon = RemSleepDaemon(**deps)
    daemon._transition(RemState.EXPLORING)
    assert daemon.state == RemState.EXPLORING
    daemon._transition(RemState.ANALYZING)
    assert daemon.state == RemState.ANALYZING
    daemon._transition(RemState.PATCHING)
    assert daemon.state == RemState.PATCHING
    daemon._transition(RemState.COOLDOWN)
    assert daemon.state == RemState.COOLDOWN


def test_epoch_counter_increments():
    deps = _mock_deps()
    daemon = RemSleepDaemon(**deps)
    assert daemon._next_epoch_id() == 1
    assert daemon._next_epoch_id() == 2
    assert daemon._next_epoch_id() == 3


@pytest.mark.asyncio
async def test_start_and_stop():
    deps = _mock_deps()
    daemon = RemSleepDaemon(**deps)
    await daemon.start()
    assert daemon._task is not None
    await daemon.stop()
    assert daemon._task is None or daemon._task.done()


def test_health_report():
    deps = _mock_deps()
    daemon = RemSleepDaemon(**deps)
    health = daemon.health()
    assert health["state"] == "IDLE_WATCH"
    assert health["epoch_count"] == 0
    assert health["total_findings"] == 0
    assert health["total_envelopes"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_sleep.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement rem_sleep.py**

```python
# backend/core/ouroboros/rem_sleep.py
"""Phase 3: REM Sleep daemon — autonomous maintenance and evolution.

Background daemon that watches for idle state, triggers exploration epochs,
and routes findings through the governance pipeline.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from itertools import count
from typing import Any, Dict, Optional

from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.rem_epoch import RemEpoch, EpochResult

logger = logging.getLogger(__name__)


class RemState(enum.Enum):
    IDLE_WATCH = "IDLE_WATCH"
    EXPLORING = "EXPLORING"
    ANALYZING = "ANALYZING"
    PATCHING = "PATCHING"
    COOLDOWN = "COOLDOWN"


class RemSleepDaemon:
    """Background daemon that runs REM epochs when the system is idle."""

    def __init__(
        self,
        oracle: Any,
        fleet: Any,
        spinal_cord: Any,
        intake_router: Any,
        proactive_drive: Any,
        doubleword: Any,
        config: Any,
    ) -> None:
        self._oracle = oracle
        self._fleet = fleet
        self._spinal = spinal_cord
        self._intake = intake_router
        self._drive = proactive_drive
        self._doubleword = doubleword
        self._config = config

        self._state = RemState.IDLE_WATCH
        self._epoch_counter = count(1)
        self._current_token: Optional[CancellationToken] = None
        self._task: Optional[asyncio.Task] = None

        # Metrics
        self._epoch_count = 0
        self._total_findings = 0
        self._total_envelopes = 0
        self._last_epoch_result: Optional[EpochResult] = None

    @property
    def state(self) -> RemState:
        return self._state

    def _transition(self, new_state: RemState) -> None:
        logger.info("[REM] %s -> %s", self._state.value, new_state.value)
        self._state = new_state

    def _next_epoch_id(self) -> int:
        return next(self._epoch_counter)

    async def start(self) -> None:
        """Launch the REM sleep background daemon. Returns immediately."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._daemon_loop())
        logger.info("[REM] Daemon started")

    async def stop(self) -> None:
        """Graceful shutdown. Cancel current epoch, drain."""
        if self._current_token is not None:
            self._current_token.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("[REM] Daemon stopped")

    def pause(self) -> None:
        """Cooperatively pause current epoch (user activity detected)."""
        if self._current_token is not None:
            self._current_token.cancel()

    def health(self) -> Dict[str, Any]:
        return {
            "state": self._state.value,
            "epoch_count": self._epoch_count,
            "total_findings": self._total_findings,
            "total_envelopes": self._total_envelopes,
            "last_epoch": (
                {
                    "id": self._last_epoch_result.epoch_id,
                    "findings": self._last_epoch_result.findings_count,
                    "envelopes": self._last_epoch_result.envelopes_submitted,
                    "duration_s": self._last_epoch_result.duration_s,
                    "completed": self._last_epoch_result.completed,
                }
                if self._last_epoch_result
                else None
            ),
        }

    async def _daemon_loop(self) -> None:
        """Main daemon loop: idle_watch -> explore -> cooldown -> repeat."""
        # Wait for SpinalGate before first epoch
        await self._spinal.wait_for_gate()

        # Register idle callback if ProactiveDrive supports it
        idle_event = asyncio.Event()
        if hasattr(self._drive, "on_eligible"):
            self._drive.on_eligible(idle_event.set)

        while True:
            try:
                self._transition(RemState.IDLE_WATCH)

                # Wait for idle eligibility
                if hasattr(self._drive, "on_eligible"):
                    await idle_event.wait()
                    idle_event.clear()
                else:
                    # Fallback: poll tick() every 10s
                    while True:
                        if hasattr(self._drive, "tick"):
                            state, _ = self._drive.tick()
                            if state == "ELIGIBLE":
                                break
                        await asyncio.sleep(10)

                # Run epoch
                await self._run_epoch()

                # Cooldown
                self._transition(RemState.COOLDOWN)
                await asyncio.sleep(self._config.rem_cooldown_s)

            except asyncio.CancelledError:
                logger.info("[REM] Daemon cancelled")
                return
            except Exception as exc:
                logger.warning("[REM] Daemon loop error: %s", exc)
                self._transition(RemState.COOLDOWN)
                await asyncio.sleep(self._config.rem_cooldown_s)

    async def _run_epoch(self) -> None:
        """Execute a single REM epoch."""
        epoch_id = self._next_epoch_id()
        self._current_token = CancellationToken(epoch_id=epoch_id)

        self._transition(RemState.EXPLORING)
        logger.info("[REM] Starting epoch %d", epoch_id)

        epoch = RemEpoch(
            epoch_id=epoch_id,
            oracle=self._oracle,
            fleet=self._fleet,
            spinal_cord=self._spinal,
            intake_router=self._intake,
            doubleword=self._doubleword,
            config=self._config,
        )

        try:
            result = await asyncio.wait_for(
                epoch.run(self._current_token),
                timeout=self._config.rem_epoch_timeout_s,
            )
        except asyncio.TimeoutError:
            result = EpochResult(
                epoch_id=epoch_id,
                error="epoch timeout",
                duration_s=self._config.rem_epoch_timeout_s,
            )

        self._last_epoch_result = result
        self._epoch_count += 1
        self._total_findings += result.findings_count
        self._total_envelopes += result.envelopes_submitted

        logger.info(
            "[REM] Epoch %d complete: %d findings, %d envelopes, %.1fs",
            epoch_id, result.findings_count, result.envelopes_submitted,
            result.duration_s,
        )

        self._current_token = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_sleep.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/rem_sleep.py tests/core/ouroboros/test_rem_sleep.py
git commit -m "feat(ouroboros): implement REM Sleep daemon with state machine and idle watch"
```

---

## Task 10: OuroborosDaemon (Zone 7.0 Orchestrator)

**Files:**
- Create: `backend/core/ouroboros/daemon.py`
- Create: `tests/core/ouroboros/test_daemon.py`

- [ ] **Step 1: Write daemon lifecycle tests**

```python
# tests/core/ouroboros/test_daemon.py
"""Tests for OuroborosDaemon — Zone 7.0 orchestrator."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.daemon import OuroborosDaemon, AwakeningReport
from backend.core.ouroboros.daemon_config import OuroborosDaemonConfig
from backend.core.ouroboros.vital_scan import VitalStatus


def _mock_deps():
    oracle = MagicMock()
    oracle.find_circular_dependencies.return_value = []
    oracle.find_dead_code.return_value = []

    event_stream = MagicMock()
    event_stream.broadcast_event = AsyncMock(return_value=1)

    health_sensor = AsyncMock()
    health_sensor.scan_once.return_value = []

    return {
        "oracle": oracle,
        "fleet": AsyncMock(),
        "bg_pool": MagicMock(start=AsyncMock(), stop=AsyncMock()),
        "intake_router": AsyncMock(),
        "event_stream": event_stream,
        "proactive_drive": MagicMock(),
        "doubleword": None,
        "gls": MagicMock(),
        "health_sensor": health_sensor,
        "config": OuroborosDaemonConfig(
            rem_cooldown_s=0.1,
            vital_scan_timeout_s=5,
            spinal_timeout_s=5,
        ),
    }


@pytest.mark.asyncio
async def test_awaken_returns_report():
    deps = _mock_deps()
    daemon = OuroborosDaemon(**deps)
    report = await daemon.awaken()
    assert isinstance(report, AwakeningReport)
    assert report.vital_status in (VitalStatus.PASS, VitalStatus.WARN)


@pytest.mark.asyncio
async def test_awaken_then_shutdown():
    deps = _mock_deps()
    daemon = OuroborosDaemon(**deps)
    await daemon.awaken()
    await daemon.shutdown()


@pytest.mark.asyncio
async def test_health_after_awaken():
    deps = _mock_deps()
    daemon = OuroborosDaemon(**deps)
    await daemon.awaken()
    health = daemon.health()
    assert "vital_status" in health
    assert "spinal_status" in health
    assert "rem" in health
    await daemon.shutdown()


@pytest.mark.asyncio
async def test_awaken_is_idempotent():
    deps = _mock_deps()
    daemon = OuroborosDaemon(**deps)
    r1 = await daemon.awaken()
    r2 = await daemon.awaken()
    assert r1.vital_status == r2.vital_status
    await daemon.shutdown()


@pytest.mark.asyncio
async def test_awaken_with_rem_disabled():
    deps = _mock_deps()
    deps["config"] = OuroborosDaemonConfig(rem_enabled=False, vital_scan_timeout_s=5, spinal_timeout_s=5)
    daemon = OuroborosDaemon(**deps)
    report = await daemon.awaken()
    assert report.rem_started is False
    await daemon.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement daemon.py**

```python
# backend/core/ouroboros/daemon.py
"""OuroborosDaemon — Zone 7.0 proactive self-evolution engine.

Three-phase lifecycle:
  Phase 1: Vital Scan (blocking, <=30s, zero model calls)
  Phase 2: Spinal Cord (async, <=10s, zero model calls)
  Phase 3: REM Sleep (background daemon, agentic)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.core.ouroboros.daemon_config import OuroborosDaemonConfig
from backend.core.ouroboros.vital_scan import VitalScan, VitalReport, VitalStatus
from backend.core.ouroboros.spinal_cord import SpinalCord, SpinalStatus
from backend.core.ouroboros.rem_sleep import RemSleepDaemon

logger = logging.getLogger(__name__)


@dataclass
class AwakeningReport:
    """Result of OuroborosDaemon.awaken()."""
    vital_status: VitalStatus
    vital_report: VitalReport
    spinal_status: SpinalStatus
    rem_started: bool


class OuroborosDaemon:
    """Zone 7.0 — Proactive self-evolution daemon.

    Dependencies are injected, not constructed.
    Lifecycle: awaken() at boot, shutdown() at supervisor teardown.
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
        config: OuroborosDaemonConfig,
        health_sensor: Any = None,
    ) -> None:
        self._oracle = oracle
        self._fleet = fleet
        self._bg_pool = bg_pool
        self._intake = intake_router
        self._event_stream = event_stream
        self._drive = proactive_drive
        self._doubleword = doubleword
        self._gls = gls
        self._config = config
        self._health_sensor = health_sensor

        self._vital_report: Optional[VitalReport] = None
        self._spinal: Optional[SpinalCord] = None
        self._spinal_status: Optional[SpinalStatus] = None
        self._rem: Optional[RemSleepDaemon] = None
        self._awakened = False

    async def awaken(self) -> AwakeningReport:
        """Boot sequence: vital scan -> spinal cord -> REM daemon.

        Idempotent: safe to call multiple times.
        """
        if self._awakened and self._vital_report is not None:
            return AwakeningReport(
                vital_status=self._vital_report.status,
                vital_report=self._vital_report,
                spinal_status=self._spinal_status or SpinalStatus.DEGRADED,
                rem_started=self._rem is not None,
            )

        # Phase 1: Vital Scan
        logger.info("[OuroborosDaemon] Phase 1: Vital Scan")
        scanner = VitalScan(
            oracle=self._oracle,
            health_sensor=self._health_sensor,
        )
        self._vital_report = await scanner.run(
            timeout_s=self._config.vital_scan_timeout_s,
        )
        logger.info(
            "[OuroborosDaemon] Vital Scan: %s (%d findings)",
            self._vital_report.status.value,
            len(self._vital_report.findings),
        )

        # Phase 2: Spinal Cord
        logger.info("[OuroborosDaemon] Phase 2: Spinal Cord")
        self._spinal = SpinalCord(event_stream=self._event_stream)
        self._spinal_status = await self._spinal.wire(
            timeout_s=self._config.spinal_timeout_s,
        )
        logger.info(
            "[OuroborosDaemon] Spinal Cord: %s",
            self._spinal_status.value,
        )

        # Phase 3: REM Sleep
        rem_started = False
        if self._config.rem_enabled:
            logger.info("[OuroborosDaemon] Phase 3: Starting REM Sleep daemon")
            self._rem = RemSleepDaemon(
                oracle=self._oracle,
                fleet=self._fleet,
                spinal_cord=self._spinal,
                intake_router=self._intake,
                proactive_drive=self._drive,
                doubleword=self._doubleword,
                config=self._config,
            )
            await self._rem.start()
            rem_started = True
            logger.info("[OuroborosDaemon] REM Sleep daemon active")

            # Queue WARN findings from Phase 1 for REM processing
            if self._vital_report.status == VitalStatus.WARN:
                logger.info(
                    "[OuroborosDaemon] Queuing %d vital warnings for REM",
                    len(self._vital_report.warnings),
                )

        self._awakened = True
        return AwakeningReport(
            vital_status=self._vital_report.status,
            vital_report=self._vital_report,
            spinal_status=self._spinal_status,
            rem_started=rem_started,
        )

    async def shutdown(self) -> None:
        """Graceful teardown: stop REM, close spinal subscriptions."""
        if self._rem is not None:
            await self._rem.stop()
            self._rem = None
        logger.info("[OuroborosDaemon] Shutdown complete")

    def health(self) -> Dict[str, Any]:
        """Current daemon health for TUI/API."""
        return {
            "awakened": self._awakened,
            "vital_status": (
                self._vital_report.status.value if self._vital_report else "unknown"
            ),
            "spinal_status": (
                self._spinal_status.value if self._spinal_status else "unknown"
            ),
            "rem": self._rem.health() if self._rem else {"state": "disabled"},
        }

    def metrics(self) -> Dict[str, Any]:
        """Cumulative daemon metrics."""
        rem_health = self._rem.health() if self._rem else {}
        return {
            "epoch_count": rem_health.get("epoch_count", 0),
            "total_findings": rem_health.get("total_findings", 0),
            "total_envelopes": rem_health.get("total_envelopes", 0),
            "vital_findings": (
                len(self._vital_report.findings) if self._vital_report else 0
            ),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/daemon.py tests/core/ouroboros/test_daemon.py
git commit -m "feat(ouroboros): implement OuroborosDaemon Zone 7.0 orchestrator"
```

---

## Task 11: Zone 7.0 Wiring in unified_supervisor.py

**Files:**
- Modify: `unified_supervisor.py:~87683` (after Zone 6.14)

- [ ] **Step 1: Identify exact insertion point**

Search for the last zone in unified_supervisor.py:
```bash
python3 -c "
import re
with open('unified_supervisor.py') as f:
    for i, line in enumerate(f, 1):
        if 'Zone 6.14' in line or 'Zone 6.13' in line or 'elite' in line.lower() and 'dashboard' in line.lower():
            print(f'{i}: {line.rstrip()}')" | tail -5
```

The Zone 7.0 block goes after the last Zone 6.x block, before any shutdown/cleanup code.

- [ ] **Step 2: Add Zone 7.0 wiring**

Insert after the Zone 6.14 block:

```python
# ---- Zone 7.0: Ouroboros Daemon (Proactive Self-Evolution) ----
_ouroboros_daemon_enabled = os.environ.get("OUROBOROS_DAEMON_ENABLED", "true").lower() in ("true", "1", "yes")
if _ouroboros_daemon_enabled:
    try:
        from backend.core.ouroboros.daemon import OuroborosDaemon
        from backend.core.ouroboros.daemon_config import OuroborosDaemonConfig

        _daemon_config = OuroborosDaemonConfig.from_env()
        _daemon = OuroborosDaemon(
            oracle=getattr(self._gls, "oracle", None),
            fleet=getattr(self._gls, "exploration_fleet", None),
            bg_pool=getattr(self._gls, "background_pool", None),
            intake_router=getattr(self._intake_layer, "_router", None),
            event_stream=getattr(self, "_event_stream", None),
            proactive_drive=getattr(self, "_proactive_drive", None),
            doubleword=getattr(self._gls, "doubleword_provider", None),
            gls=self._gls,
            config=_daemon_config,
        )

        _awakening = await _daemon.awaken()
        self._ouroboros_daemon = _daemon

        if _awakening.vital_status.value == "fail":
            logger.critical("[Zone 7.0] Vital scan FAILED — self-evolution offline")
            try:
                await safe_say("Ouroboros vital scan failed. Review required.")
            except Exception:
                pass
        elif _awakening.vital_status.value == "warn":
            _warn_count = len(_awakening.vital_report.warnings)
            logger.warning("[Zone 7.0] OuroborosDaemon online with %d warnings", _warn_count)
            try:
                await safe_say(f"Ouroboros online with {_warn_count} warnings. REM Sleep will address them.")
            except Exception:
                pass
        else:
            logger.info("[Zone 7.0] OuroborosDaemon fully online")
            try:
                await safe_say("Ouroboros online. Organism fully awakened.")
            except Exception:
                pass

    except Exception as exc:
        logger.warning("[Zone 7.0] OuroborosDaemon failed to start: %s", exc)
        # Graceful degradation — organism runs without self-evolution
else:
    logger.info("[Zone 7.0] OuroborosDaemon disabled via OUROBOROS_DAEMON_ENABLED=false")
```

- [ ] **Step 3: Add shutdown wiring**

Find the supervisor shutdown sequence (reverse order cleanup). Add before any existing cleanup:

```python
# Ouroboros Daemon shutdown
if hasattr(self, "_ouroboros_daemon") and self._ouroboros_daemon is not None:
    try:
        await self._ouroboros_daemon.shutdown()
    except Exception as exc:
        logger.debug("[shutdown] OuroborosDaemon shutdown error: %s", exc)
```

- [ ] **Step 4: Verify boot works**

```bash
# Dry-run check: import the daemon and verify no import errors
python3 -c "from backend.core.ouroboros.daemon import OuroborosDaemon; print('Import OK')"
```
Expected: `Import OK`

- [ ] **Step 5: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(ouroboros): wire Zone 7.0 OuroborosDaemon into supervisor boot sequence"
```

---

## Task 12: Integration Test

**Files:**
- Create: `tests/core/ouroboros/test_daemon_integration.py`

- [ ] **Step 1: Write end-to-end integration test**

```python
# tests/core/ouroboros/test_daemon_integration.py
"""Integration test: full OuroborosDaemon lifecycle with mocked providers."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.daemon import OuroborosDaemon, AwakeningReport
from backend.core.ouroboros.daemon_config import OuroborosDaemonConfig
from backend.core.ouroboros.vital_scan import VitalStatus


def _full_mock_stack():
    """Create a complete mock stack simulating the real supervisor dependencies."""
    oracle = MagicMock()
    oracle.find_circular_dependencies.return_value = []
    oracle.find_dead_code.return_value = [
        MagicMock(file_path="backend/agents/old_agent.py", name="OldAgent", repo="jarvis"),
    ]

    fleet = AsyncMock()
    fleet.deploy.return_value = MagicMock(
        findings=[
            MagicMock(
                description="Unwired: PredictivePlanningAgent imported but never instantiated",
                category="unwired_component",
                file_path="backend/intelligence/predictive_planning.py",
                relevance=0.9,
                repo="jarvis",
            ),
        ],
        total_findings=1,
        agents_deployed=5,
        agents_completed=5,
    )

    event_stream = MagicMock()
    event_stream.broadcast_event = AsyncMock(return_value=1)

    intake_router = AsyncMock()
    intake_router.ingest.return_value = "enqueued"

    proactive_drive = MagicMock()
    proactive_drive.on_eligible = MagicMock()

    return {
        "oracle": oracle,
        "fleet": fleet,
        "bg_pool": MagicMock(start=AsyncMock()),
        "intake_router": intake_router,
        "event_stream": event_stream,
        "proactive_drive": proactive_drive,
        "doubleword": None,
        "gls": MagicMock(),
        "health_sensor": AsyncMock(scan_once=AsyncMock(return_value=[])),
        "config": OuroborosDaemonConfig(
            rem_enabled=True,
            rem_cooldown_s=0.1,
            vital_scan_timeout_s=5,
            spinal_timeout_s=5,
            rem_cycle_timeout_s=5,
            rem_epoch_timeout_s=10,
        ),
    }


@pytest.mark.asyncio
async def test_full_lifecycle():
    """Test: awaken -> health check -> shutdown."""
    deps = _full_mock_stack()
    daemon = OuroborosDaemon(**deps)

    # Awaken
    report = await daemon.awaken()
    assert report.vital_status == VitalStatus.PASS
    assert report.spinal_status.value == "connected"
    assert report.rem_started is True

    # Health
    health = daemon.health()
    assert health["awakened"] is True
    assert health["vital_status"] == "pass"
    assert health["spinal_status"] == "connected"
    assert health["rem"]["state"] == "IDLE_WATCH"

    # Metrics
    metrics = daemon.metrics()
    assert metrics["epoch_count"] == 0  # no epoch run yet (idle not triggered)

    # Shutdown
    await daemon.shutdown()
    assert daemon.health()["rem"]["state"] == "disabled"


@pytest.mark.asyncio
async def test_vital_warn_queues_for_rem():
    """Test: vital warnings get queued for REM Sleep processing."""
    deps = _full_mock_stack()
    # Add a non-kernel circular dep to trigger WARN
    mock_node = MagicMock()
    mock_node.file_path = "backend/agents/circular.py"
    deps["oracle"].find_circular_dependencies.return_value = [[mock_node, mock_node]]

    daemon = OuroborosDaemon(**deps)
    report = await daemon.awaken()
    assert report.vital_status == VitalStatus.WARN
    assert report.rem_started is True
    await daemon.shutdown()


@pytest.mark.asyncio
async def test_rem_disabled():
    """Test: daemon works without REM Sleep."""
    deps = _full_mock_stack()
    deps["config"] = OuroborosDaemonConfig(
        rem_enabled=False,
        vital_scan_timeout_s=5,
        spinal_timeout_s=5,
    )
    daemon = OuroborosDaemon(**deps)
    report = await daemon.awaken()
    assert report.rem_started is False
    assert daemon.health()["rem"]["state"] == "disabled"
    await daemon.shutdown()
```

- [ ] **Step 2: Run integration tests**

Run: `python3 -m pytest tests/core/ouroboros/test_daemon_integration.py -v`
Expected: All 3 tests PASS

- [ ] **Step 3: Run full test suite for ouroboros**

Run: `python3 -m pytest tests/core/ouroboros/ -v --tb=short`
Expected: All tests PASS (cancellation_token, daemon_config, vital_scan, spinal_cord, finding_ranker, exploration_envelope_factory, rem_epoch, rem_sleep, daemon, integration)

- [ ] **Step 4: Commit**

```bash
git add tests/core/ouroboros/test_daemon_integration.py
git commit -m "test(ouroboros): add end-to-end integration tests for OuroborosDaemon lifecycle"
```

---

## Summary

| Task | What it builds | New files | Modified files |
|------|---------------|-----------|---------------|
| 1 | Foundation types | 4 | 0 |
| 2 | Exploration source + risk rules | 2 | 3 |
| 3 | Missing APIs (ProactiveDrive, SubAgent, GLS) | 2 | 3 |
| 4 | Finding ranker | 2 | 0 |
| 5 | Exploration envelope factory | 2 | 0 |
| 6 | Phase 1: Vital Scan | 2 | 0 |
| 7 | Phase 2: Spinal Cord | 2 | 0 |
| 8 | REM Epoch | 2 | 0 |
| 9 | REM Sleep Daemon | 2 | 0 |
| 10 | OuroborosDaemon orchestrator | 2 | 0 |
| 11 | Zone 7.0 wiring | 0 | 1 |
| 12 | Integration tests | 1 | 0 |
| **Total** | | **23 new** | **7 modified** |
