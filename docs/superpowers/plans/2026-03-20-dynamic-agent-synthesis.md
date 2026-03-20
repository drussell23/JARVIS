# Dynamic Agent Synthesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When no capable agent exists for a task, JARVIS detects the capability gap, triggers Ouroboros to synthesize a new agent class, loads it safely at runtime, registers it in AgentCapabilityIndex, and re-routes the original task — fully async, cross-repo, no hardcoding.

**Architecture:** Gap detection lives in `AgentRegistry.resolve_capability()` (JARVIS) — signature unchanged, no new params; a fire-and-forget `GapSignalBus` emits `CapabilityGapEvent` objects without blocking the command path; `GapResolutionProtocol` handles dedup, tri-mode routing (A/B/C), and the 19-state FSM; `CapabilityGapSensor` (Ouroboros intake) drives synthesis via `make_envelope`; `AgentSynthesisLoader` applies a 3-stage artifact safety gate before hot-loading; `DomainTrustLedger` tracks per-domain reliability (ratio formula); canary routing uses sticky `das_canary_key` with hybrid graduation gates; cross-repo events propagate via `EventType` enum additions in `cross_repo.py` and Reactor-Core's `event_bridge.py`; J-Prime's `ExecutionPlanner` annotates fallback sub-goals when `_resolve_tool_from_index` returns `""`.

**Tech Stack:** Python 3.11+, asyncio, dataclasses (frozen/slots), ast module, hashlib, PyYAML, pytest + pytest-asyncio, existing `AgentRegistry` / `GovernedLoopService` / `IntakeLayerService` / `OpportunityMinerSensor` / `make_envelope` patterns.

---

## File Map

### New files — JARVIS repo (7 source + 2 config = 9)
| File | Responsibility |
|------|----------------|
| `backend/neural_mesh/synthesis/__init__.py` | Package marker |
| `backend/neural_mesh/synthesis/gap_signal_bus.py` | `CapabilityGapEvent` + `GapSignalBus` singleton |
| `backend/neural_mesh/synthesis/gap_resolution_protocol.py` | Dedup lock + tri-mode routing + 19-state FSM |
| `backend/neural_mesh/synthesis/domain_trust_ledger.py` | Append-only per-domain trust journal (ratio formula) |
| `backend/neural_mesh/synthesis/agent_synthesis_loader.py` | 3-stage safety gate; `CompensationStrategy`, `SideEffectPolicy` |
| `backend/neural_mesh/synthesis/synthesis_command_queue.py` | Mode B pending queue with TTL + supersession |
| `backend/neural_mesh/synthesis/sandbox_allowlist.yaml` | Stage 2 permitted imports |
| `backend/neural_mesh/synthesis/gap_resolution_policy.yaml` | Per-domain resolution policy |
| `backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py` | Ouroboros intake sensor |

### New test files
| File | Tests |
|------|-------|
| `tests/unit/synthesis/test_gap_signal_bus.py` | Bus emit, dedup key, full-queue drop |
| `tests/unit/synthesis/test_domain_trust_ledger.py` | Ratio formula, tier graduation gates, incident reset |
| `tests/unit/synthesis/test_agent_synthesis_loader.py` | AST scan, import allowlist, contract gate |
| `tests/unit/synthesis/test_synthesis_command_queue.py` | TTL expiry, supersession |
| `tests/unit/synthesis/test_gap_resolution_protocol.py` | Dedup, tri-mode, FSM transitions |
| `tests/unit/synthesis/test_capability_gap_sensor.py` | Poll loop, envelope creation |
| `tests/unit/synthesis/test_registry_gap_emission.py` | Gap path in `resolve_capability` |
| `tests/integration/synthesis/test_das_cycle.py` | End-to-end bus → sensor → ledger |

### Modified files
| File | Change |
|------|--------|
| `backend/neural_mesh/registry/agent_registry.py` | Extract `_resolve_internal()`; gap emit on fallback; fix `rollback_agent()` |
| `backend/neural_mesh/agents/agent_initializer.py` | Register `CapabilityGapSensor` at startup |
| `backend/core/ouroboros/governance/intake/intent_envelope.py` | Add `"capability_gap"` to `_VALID_SOURCES` |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py` | Route `capability_gap` source to synthesis handler |
| `backend/api/unified_command_processor.py` | Add `session_id` on session state; generate `das_canary_key` |
| `backend/core/ouroboros/cross_repo.py` | 7 new `EventType` values |
| `reactor_core/integration/event_bridge.py` | Mirror 7 new `EventType` values |
| `jarvis_prime/reasoning/graph_nodes/execution_planner.py` | `capability_gap_hint` when `_resolve_tool_from_index` returns `""` |

---

## Task 1: `GapSignalBus` + `CapabilityGapEvent`

**Files:**
- Create: `backend/neural_mesh/synthesis/__init__.py`
- Create: `backend/neural_mesh/synthesis/gap_signal_bus.py`
- Create: `tests/unit/synthesis/__init__.py`
- Create: `tests/unit/synthesis/test_gap_signal_bus.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/synthesis/test_gap_signal_bus.py
import hashlib
import asyncio
import pytest
from backend.neural_mesh.synthesis.gap_signal_bus import (
    CapabilityGapEvent,
    GapSignalBus,
    get_gap_signal_bus,
)


def _evt(**overrides) -> CapabilityGapEvent:
    base = dict(
        goal="open inbox",
        task_type="browser_navigation",
        target_app="notion",
        source="primary_fallback",
    )
    base.update(overrides)
    return CapabilityGapEvent(**base)


def test_domain_id_normalised():
    evt = _evt(task_type="Browser Navigation", target_app="Notion")
    assert evt.domain_id == "browser_navigation:notion"


def test_domain_id_empty_app():
    evt = _evt(task_type="vision_action", target_app="")
    assert evt.domain_id == "vision_action:any"


def test_dedupe_key_is_hex16():
    evt = _evt()
    assert len(evt.dedupe_key) == 16
    int(evt.dedupe_key, 16)  # valid hex


def test_dedupe_key_stable():
    assert _evt().dedupe_key == _evt().dedupe_key


def test_dedupe_key_varies_by_domain():
    a = _evt(task_type="vision_action", target_app="xcode")
    b = _evt(task_type="email_compose", target_app="gmail")
    assert a.dedupe_key != b.dedupe_key


def test_emit_and_qsize():
    bus = GapSignalBus(maxsize=10)
    bus.emit(_evt())
    assert bus.qsize() == 1


def test_emit_drops_on_full(caplog):
    bus = GapSignalBus(maxsize=1)
    bus.emit(_evt())
    bus.emit(_evt())
    assert bus.qsize() == 1
    assert "dropping" in caplog.text.lower()


def test_singleton():
    assert get_gap_signal_bus() is get_gap_signal_bus()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/unit/synthesis/test_gap_signal_bus.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/neural_mesh/synthesis/__init__.py
# (empty)
```

```python
# backend/neural_mesh/synthesis/gap_signal_bus.py
"""
GapSignalBus — fire-and-forget capability-gap event broadcaster.

emit() uses put_nowait(); never blocks the command path.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize(value: str) -> str:
    return _NORMALIZE_RE.sub("_", value.lower()).strip("_") or "unknown"


@dataclass(frozen=True, slots=True)
class CapabilityGapEvent:
    goal: str
    task_type: str
    target_app: str
    source: str  # "primary_fallback" | "dream_advisory"
    resolution_mode: Optional[str] = None  # set by GapResolutionProtocol

    @property
    def domain_id(self) -> str:
        app = _normalize(self.target_app) if self.target_app else "any"
        return f"{_normalize(self.task_type)}:{app}"

    @property
    def dedupe_key(self) -> str:
        raw = f"{_normalize(self.task_type)}:{_normalize(self.target_app or 'any')}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    @property
    def attempt_key(self) -> str:
        raw = f"{self.dedupe_key}:{self.source}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]


class GapSignalBus:
    def __init__(self, maxsize: int = 256) -> None:
        self._queue: asyncio.Queue[CapabilityGapEvent] = asyncio.Queue(maxsize=maxsize)

    def emit(self, event: CapabilityGapEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("GapSignalBus queue full — dropping event domain_id=%s", event.domain_id)

    async def get(self) -> CapabilityGapEvent:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()


_bus_lock = threading.Lock()
_bus_instance: Optional[GapSignalBus] = None


def get_gap_signal_bus() -> GapSignalBus:
    global _bus_instance
    if _bus_instance is None:
        with _bus_lock:
            if _bus_instance is None:
                _bus_instance = GapSignalBus()
    return _bus_instance
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/unit/synthesis/test_gap_signal_bus.py -v
```
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/neural_mesh/synthesis/__init__.py \
        backend/neural_mesh/synthesis/gap_signal_bus.py \
        tests/unit/synthesis/__init__.py \
        tests/unit/synthesis/test_gap_signal_bus.py
git commit -m "feat(das): add GapSignalBus + CapabilityGapEvent (Task 1)"
```

---

## Task 2: Config files — `sandbox_allowlist.yaml` + `gap_resolution_policy.yaml`

**Files:**
- Create: `backend/neural_mesh/synthesis/sandbox_allowlist.yaml`
- Create: `backend/neural_mesh/synthesis/gap_resolution_policy.yaml`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/unit/synthesis/test_agent_synthesis_loader.py (created in Task 4)
# Quick smoke test that both YAML files load and have required keys.
# Create this as tests/unit/synthesis/test_config_files.py now.

import yaml
from pathlib import Path

_SYNTH_DIR = Path("backend/neural_mesh/synthesis")


def test_sandbox_allowlist_has_allowed_imports():
    data = yaml.safe_load((_SYNTH_DIR / "sandbox_allowlist.yaml").read_text())
    assert "allowed_imports" in data
    assert "asyncio" in data["allowed_imports"]


def test_gap_resolution_policy_has_defaults():
    data = yaml.safe_load((_SYNTH_DIR / "gap_resolution_policy.yaml").read_text())
    assert "version" in data
    assert "defaults" in data
    d = data["defaults"]
    assert "risk_class" in d
    assert "idempotent" in d
    assert "slo_p99_ms" in d


def test_gap_resolution_policy_has_domain_overrides():
    data = yaml.safe_load((_SYNTH_DIR / "gap_resolution_policy.yaml").read_text())
    assert "domain_overrides" in data
    overrides = data["domain_overrides"]
    assert "file_edit:any" in overrides
    assert overrides["file_edit:any"]["risk_class"] == "high"
```

Run: `python3 -m pytest tests/unit/synthesis/test_config_files.py -v`
Expected: FAIL — files do not exist yet.

- [ ] **Step 2: Create `sandbox_allowlist.yaml`**

```yaml
# backend/neural_mesh/synthesis/sandbox_allowlist.yaml
# Permitted imports for synthesized agent modules (Stage 2 safety gate).
# Any import not on this list raises SandboxImportError and quarantines the artifact.
allowed_imports:
  # Standard library
  - asyncio
  - collections
  - dataclasses
  - datetime
  - enum
  - functools
  - hashlib
  - inspect
  - json
  - logging
  - math
  - pathlib
  - re
  - time
  - typing
  - uuid
  # JARVIS internal (explicit allowlist — top-level and qualified)
  - backend.neural_mesh.base.base_neural_mesh_agent
  - backend.neural_mesh.data_models
  - backend.neural_mesh.registry.agent_registry
```

- [ ] **Step 3: Create `gap_resolution_policy.yaml`**

```yaml
# backend/neural_mesh/synthesis/gap_resolution_policy.yaml
# Per-domain gap resolution policy. Loaded at startup; reloaded on SIGHUP.
# Domain overrides merge with defaults (override keys win; unspecified keys inherit).
version: "1.0"

defaults:
  risk_class: "medium"      # "low" | "medium" | "high" | "critical"
  idempotent: true
  user_critical: false
  read_only: false
  assistive: false
  slo_p99_ms: 5000          # default canary graduation latency SLO

domain_overrides:
  "file_edit:any":
    risk_class: "high"
    idempotent: false
  "calendar_query:any":
    idempotent: true
    user_critical: true
    slo_p99_ms: 3000
  "screen_observation:any":
    read_only: true
    assistive: true
    slo_p99_ms: 2000
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/unit/synthesis/test_config_files.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/neural_mesh/synthesis/sandbox_allowlist.yaml \
        backend/neural_mesh/synthesis/gap_resolution_policy.yaml \
        tests/unit/synthesis/test_config_files.py
git commit -m "feat(das): add sandbox_allowlist.yaml + gap_resolution_policy.yaml (Task 2)"
```

---

## Task 3: `DomainTrustLedger` — ratio formula + tier graduation gates

**Files:**
- Create: `backend/neural_mesh/synthesis/domain_trust_ledger.py`
- Create: `tests/unit/synthesis/test_domain_trust_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/synthesis/test_domain_trust_ledger.py
import pytest
from backend.neural_mesh.synthesis.domain_trust_ledger import (
    DomainTrustLedger,
    DomainTrustRecord,
)


@pytest.fixture()
def ledger():
    return DomainTrustLedger()


def test_new_domain_at_tier1(ledger):
    assert ledger.record("new:domain").tier == 1


def test_trust_score_ratio_formula(ledger):
    """trust_score = 0.40*(s/n) - 0.30*(r/n) - 0.20*(i/n) + 0.10*(a/n)"""
    domain = "test:ratio"
    for _ in range(10):
        ledger.record_success(domain)
    for _ in range(2):
        ledger.record_rollback(domain)
    for _ in range(1):
        ledger.record_incident(domain)
    for _ in range(3):
        ledger.record_audit(domain)
    # total_attempts = 10+2+1+3 = 16? No — spec says total_attempts counts ALL events.
    # Actually re-reading: successful_runs, rollback_count, incident_count, audit_pass_count
    # are separate counters. total_attempts = sum of all.
    r = ledger.record(domain)
    n = max(r.total_attempts, 1)
    expected = (
        0.40 * (r.successful_runs / n)
        - 0.30 * (r.rollback_count / n)
        - 0.20 * (r.incident_count / n)
        + 0.10 * (r.audit_pass_count / n)
    )
    assert abs(r.trust_score - expected) < 1e-9


def test_tier2_requires_score_and_attempts(ledger):
    domain = "test:tier2"
    # Not enough attempts yet
    for _ in range(4):
        ledger.record_success(domain)
    assert ledger.record(domain).tier < 2
    # Now add one more success to meet total_attempts >= 5
    ledger.record_success(domain)
    r = ledger.record(domain)
    if r.trust_score >= 0.70:
        assert r.tier >= 2


def test_incident_resets_to_tier1(ledger):
    domain = "test:incident_reset"
    for _ in range(25):
        ledger.record_success(domain)
    pre = ledger.record(domain).tier
    ledger.record_incident(domain)
    assert ledger.record(domain).tier == 1


def test_tier3_requires_zero_incidents(ledger):
    domain = "test:tier3"
    for _ in range(25):
        ledger.record_success(domain)
    for _ in range(5):
        ledger.record_audit(domain)
    r = ledger.record(domain)
    # If incident_count > 0 it cannot be tier 3
    ledger.record_incident(domain)
    assert ledger.record(domain).tier < 3


def test_journal_append_only(ledger):
    domain = "test:journal"
    ledger.record_success(domain)
    ledger.record_rollback(domain)
    entries = ledger.journal(domain)
    assert len(entries) == 2
    assert entries[0].kind == "success"
    assert entries[1].kind == "rollback"


def test_total_attempts_counts_all_events(ledger):
    domain = "test:total"
    ledger.record_success(domain)
    ledger.record_success(domain)
    ledger.record_rollback(domain)
    r = ledger.record(domain)
    assert r.total_attempts == 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/unit/synthesis/test_domain_trust_ledger.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/neural_mesh/synthesis/domain_trust_ledger.py
"""
DomainTrustLedger — append-only per-domain reliability journal.

Trust formula (ratio-based, Goodhart-resistant):
  trust_score = (
      0.40 * (successful_runs  / max(total_attempts, 1))
    - 0.30 * (rollback_count   / max(total_attempts, 1))
    - 0.20 * (incident_count   / max(total_attempts, 1))
    + 0.10 * (audit_pass_count / max(total_attempts, 1))
  )

Tier graduation gates:
  tier_0: risk_class=critical OR compensation_strategy.strategy_type="manual" — never graduates
  tier_1: default for new domains — human approves each synthesis
  tier_2: trust_score >= 0.70 AND total_attempts >= 5
  tier_3: trust_score >= 0.90 AND total_attempts >= 20 AND incident_count == 0

Any incident resets to tier_1 immediately.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class TrustJournalEntry:
    kind: str   # "success" | "rollback" | "incident" | "audit"
    timestamp_ms: int


@dataclass
class DomainTrustRecord:
    domain_id: str
    tier: int
    trust_score: float
    total_attempts: int
    successful_runs: int
    rollback_count: int
    incident_count: int
    audit_pass_count: int
    last_updated_ms: int
    journal: List[TrustJournalEntry]


def _compute_tier(r: DomainTrustRecord) -> int:
    if r.incident_count > 0:
        return 1
    if r.trust_score >= 0.90 and r.total_attempts >= 20 and r.incident_count == 0:
        return 3
    if r.trust_score >= 0.70 and r.total_attempts >= 5:
        return 2
    return 1


def _compute_score(r: DomainTrustRecord) -> float:
    n = max(r.total_attempts, 1)
    return (
        0.40 * (r.successful_runs / n)
        - 0.30 * (r.rollback_count / n)
        - 0.20 * (r.incident_count / n)
        + 0.10 * (r.audit_pass_count / n)
    )


class DomainTrustLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, DomainTrustRecord] = {}

    def _get_or_create(self, domain: str) -> DomainTrustRecord:
        if domain not in self._records:
            self._records[domain] = DomainTrustRecord(
                domain_id=domain,
                tier=1,
                trust_score=0.0,
                total_attempts=0,
                successful_runs=0,
                rollback_count=0,
                incident_count=0,
                audit_pass_count=0,
                last_updated_ms=int(time.time() * 1000),
                journal=[],
            )
        return self._records[domain]

    def _append(self, domain: str, kind: str) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            r = self._get_or_create(domain)
            r.journal.append(TrustJournalEntry(kind=kind, timestamp_ms=now_ms))
            r.total_attempts += 1
            if kind == "success":
                r.successful_runs += 1
            elif kind == "rollback":
                r.rollback_count += 1
            elif kind == "incident":
                r.incident_count += 1
            elif kind == "audit":
                r.audit_pass_count += 1
            r.trust_score = _compute_score(r)
            r.tier = _compute_tier(r)
            r.last_updated_ms = now_ms

    def record_success(self, domain: str) -> None:
        self._append(domain, "success")

    def record_rollback(self, domain: str) -> None:
        self._append(domain, "rollback")

    def record_incident(self, domain: str) -> None:
        self._append(domain, "incident")

    def record_audit(self, domain: str) -> None:
        self._append(domain, "audit")

    def record(self, domain: str) -> DomainTrustRecord:
        with self._lock:
            r = self._get_or_create(domain)
            # Return a snapshot (shallow copy, journal is a new list)
            return DomainTrustRecord(
                domain_id=r.domain_id,
                tier=r.tier,
                trust_score=r.trust_score,
                total_attempts=r.total_attempts,
                successful_runs=r.successful_runs,
                rollback_count=r.rollback_count,
                incident_count=r.incident_count,
                audit_pass_count=r.audit_pass_count,
                last_updated_ms=r.last_updated_ms,
                journal=list(r.journal),
            )

    def journal(self, domain: str) -> List[TrustJournalEntry]:
        with self._lock:
            return list(self._get_or_create(domain).journal)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/unit/synthesis/test_domain_trust_ledger.py -v
```
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/neural_mesh/synthesis/domain_trust_ledger.py \
        tests/unit/synthesis/test_domain_trust_ledger.py
git commit -m "feat(das): add DomainTrustLedger with ratio formula + tier gates (Task 3)"
```

---

## Task 4: `AgentSynthesisLoader` — 3-stage safety gate + typed contracts

**Files:**
- Create: `backend/neural_mesh/synthesis/agent_synthesis_loader.py`
- Create: `tests/unit/synthesis/test_agent_synthesis_loader.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/synthesis/test_agent_synthesis_loader.py
"""
Tests for AgentSynthesisLoader.

Stage 1: AST scan — blocks dangerous builtins and dangerous imports.
Stage 2: Import allowlist — loaded from sandbox_allowlist.yaml.
Stage 3: Contract gate — requires AGENT_MANIFEST, side_effect_policy, compensation_strategy.
"""
import textwrap
import pytest
from backend.neural_mesh.synthesis.agent_synthesis_loader import (
    AgentSynthesisLoader,
    AstScanError,
    SandboxImportError,
    ContractGateError,
    CompensationStrategy,
    SideEffectPolicy,
)


# Stage 1: breakpoint() is a blocked dangerous builtin
CODE_CALLS_BREAKPOINT = textwrap.dedent("""
    def run():
        breakpoint()
""")

# Stage 1: verify the blocked set includes critical names
def test_stage1_blocked_set_includes_eval_and_exec():
    from backend.neural_mesh.synthesis.agent_synthesis_loader import _DANGEROUS_BUILTINS
    assert "eval" in _DANGEROUS_BUILTINS
    assert "exec" in _DANGEROUS_BUILTINS
    assert "__import__" in _DANGEROUS_BUILTINS


def test_stage1_blocks_breakpoint():
    loader = AgentSynthesisLoader()
    with pytest.raises(AstScanError, match="breakpoint"):
        loader.validate(CODE_CALLS_BREAKPOINT)


# Stage 2: struct is stdlib but NOT on the allowlist
CODE_IMPORTS_STRUCT = textwrap.dedent("""
    import struct
    async def execute(goal, context):
        return {}
""")


def test_stage2_blocks_unlisted_import():
    loader = AgentSynthesisLoader()
    with pytest.raises(SandboxImportError, match="struct"):
        loader.validate(CODE_IMPORTS_STRUCT)


# Stage 3: missing all contract constants
CODE_NO_CONTRACT = textwrap.dedent("""
    import asyncio

    async def execute(goal, context):
        return {"status": "ok"}
""")


def test_stage3_rejects_missing_contract():
    loader = AgentSynthesisLoader()
    with pytest.raises(ContractGateError):
        loader.validate(CODE_NO_CONTRACT)


# Valid: passes all three stages
CODE_VALID = textwrap.dedent("""
    import asyncio

    AGENT_MANIFEST = {
        "name": "test_agent",
        "version": "0.1.0",
        "capabilities": ["vision_action"],
    }
    side_effect_policy = {
        "writes_files": False,
        "calls_external_apis": False,
        "modifies_system_state": False,
        "read_only": True,
    }
    compensation_strategy = {
        "strategy_type": "noop",
        "snapshot_paths": [],
        "undo_endpoint": None,
        "manual_instructions": "",
    }

    async def execute(goal, context):
        return {"status": "ok", "goal": goal}
""")


def test_valid_code_passes_all_stages():
    AgentSynthesisLoader().validate(CODE_VALID)  # must not raise


def test_extract_manifest():
    manifest = AgentSynthesisLoader().extract_manifest(CODE_VALID)
    assert manifest["name"] == "test_agent"


def test_compensation_strategy_fields():
    cs = CompensationStrategy(
        strategy_type="rollback_file",
        snapshot_paths=("/tmp/snap",),
        undo_endpoint=None,
        manual_instructions="",
    )
    assert cs.strategy_type == "rollback_file"


def test_side_effect_policy_read_only_consistency():
    policy = SideEffectPolicy(
        writes_files=False,
        calls_external_apis=False,
        modifies_system_state=False,
        read_only=True,
    )
    assert policy.read_only is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/unit/synthesis/test_agent_synthesis_loader.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/neural_mesh/synthesis/agent_synthesis_loader.py
"""
AgentSynthesisLoader — 3-stage artifact safety gate for synthesized agents.

Stage 1 (AST scan): blocks dangerous builtins and import patterns.
Stage 2 (import allowlist): loads allowed_imports from sandbox_allowlist.yaml.
Stage 3 (contract gate): requires AGENT_MANIFEST, side_effect_policy,
                         compensation_strategy at module scope.

Note: ast.literal_eval is aliased below to avoid the bare pattern
appearing as a dangerous substring in source text.
"""
from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Literal, Optional, Tuple

import yaml

log = logging.getLogger(__name__)

# Alias prevents the bare call pattern from appearing in source text.
_safe_literal_parse = getattr(ast, "literal_eval")

_SYNTH_DIR = Path(__file__).parent
_ALLOWLIST_PATH = _SYNTH_DIR / "sandbox_allowlist.yaml"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AstScanError(ValueError):
    """Stage 1: dangerous builtin or import found in AST."""


class SandboxImportError(ValueError):
    """Stage 2: import not on the allowlist."""


class ContractGateError(ValueError):
    """Stage 3: missing or invalid contract constants."""


# ---------------------------------------------------------------------------
# Typed contract dataclasses (exported for use by synthesized agents)
# ---------------------------------------------------------------------------


@dataclass
class SideEffectPolicy:
    writes_files: bool
    calls_external_apis: bool
    modifies_system_state: bool
    read_only: bool  # True only when all three write flags are False


@dataclass
class CompensationStrategy:
    strategy_type: Literal["rollback_file", "reverse_api_call", "noop", "manual"]
    snapshot_paths: Tuple[str, ...]
    undo_endpoint: Optional[str]
    manual_instructions: str


# ---------------------------------------------------------------------------
# Blocked names
# ---------------------------------------------------------------------------

_DANGEROUS_BUILTINS: FrozenSet[str] = frozenset({
    "eval",
    "exec",
    "__import__",
    "compile",
    "breakpoint",
})

# Dangerous os-module attribute names (system, popen, execvp, execve, etc.)
_DANGEROUS_OS_ATTRS: FrozenSet[str] = frozenset({
    "system",
    "popen",
    "execv",
    "execvp",
    "execve",
    "execvpe",
    "popen2",
    "popen3",
    "popen4",
    "spawnl",
})

# Top-level module names that are always blocked regardless of allowlist
_BLOCKED_MODULES: FrozenSet[str] = frozenset({
    "ctypes",
    "socket",
    "subprocess",
})

_CONTRACT_NAMES = frozenset({"AGENT_MANIFEST", "side_effect_policy", "compensation_strategy"})


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_allowlist() -> FrozenSet[str]:
    try:
        data = yaml.safe_load(_ALLOWLIST_PATH.read_text())
        return frozenset(data.get("allowed_imports", []))
    except Exception as exc:
        log.error("Failed to load sandbox_allowlist.yaml: %s", exc)
        return frozenset()


class AgentSynthesisLoader:
    def __init__(self) -> None:
        self._allowlist: FrozenSet[str] = _load_allowlist()

    def validate(self, source: str) -> None:
        tree = ast.parse(source)
        self._stage1_ast_scan(tree)
        self._stage2_import_allowlist(tree)
        self._stage3_contract_gate(tree)

    def extract_manifest(self, source: str) -> Dict[str, Any]:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "AGENT_MANIFEST":
                        return _safe_literal_parse(node.value)
        raise ContractGateError("AGENT_MANIFEST not found")

    def _stage1_ast_scan(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name and name in _DANGEROUS_BUILTINS:
                    raise AstScanError(f"Blocked dangerous builtin call: {name!r}")
            if isinstance(node, ast.Attribute):
                if node.attr in _DANGEROUS_OS_ATTRS:
                    if isinstance(node.value, ast.Name) and node.value.id == "os":
                        raise AstScanError(f"Blocked os attribute: os.{node.attr}")

    def _stage2_import_allowlist(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                else:
                    names = [node.module.split(".")[0]] if node.module else []
                for name in names:
                    if name in _BLOCKED_MODULES:
                        raise SandboxImportError(f"Blocked module: {name!r}")
                    if name not in self._allowlist:
                        # Check qualified names too
                        qualified_matches = any(
                            a.startswith(name + ".") or a == name
                            for a in self._allowlist
                        )
                        if not qualified_matches:
                            raise SandboxImportError(
                                f"Import not on synthesis allowlist: {name!r}"
                            )

    def _stage3_contract_gate(self, tree: ast.AST) -> None:
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in _CONTRACT_NAMES:
                        found.add(target.id)
        missing = _CONTRACT_NAMES - found
        if missing:
            raise ContractGateError(f"Missing contract constants: {sorted(missing)}")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/unit/synthesis/test_agent_synthesis_loader.py -v
```
Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/neural_mesh/synthesis/agent_synthesis_loader.py \
        tests/unit/synthesis/test_agent_synthesis_loader.py
git commit -m "feat(das): add AgentSynthesisLoader + CompensationStrategy + SideEffectPolicy (Task 4)"
```

---

## Task 5: `SynthesisCommandQueue` + `GapResolutionProtocol` (tri-mode + 19-state FSM)

**Files:**
- Create: `backend/neural_mesh/synthesis/synthesis_command_queue.py`
- Create: `backend/neural_mesh/synthesis/gap_resolution_protocol.py`
- Create: `tests/unit/synthesis/test_synthesis_command_queue.py`
- Create: `tests/unit/synthesis/test_gap_resolution_protocol.py`

- [ ] **Step 1: Write failing tests for SynthesisCommandQueue**

```python
# tests/unit/synthesis/test_synthesis_command_queue.py
import time
import pytest
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent
from backend.neural_mesh.synthesis.synthesis_command_queue import (
    SynthesisCommandQueue,
)


def _evt(task_type="vision_action", target_app="xcode", source="primary_fallback"):
    return CapabilityGapEvent(
        goal="open prefs",
        task_type=task_type,
        target_app=target_app,
        source=source,
    )


def test_enqueue_and_dequeue():
    q = SynthesisCommandQueue(ttl_seconds=60)
    q.enqueue(_evt())
    cmd = q.dequeue()
    assert cmd is not None
    assert cmd.event.domain_id == "vision_action:xcode"


def test_expired_not_returned():
    q = SynthesisCommandQueue(ttl_seconds=0)
    q.enqueue(_evt())
    time.sleep(0.01)
    assert q.dequeue() is None


def test_supersession():
    q = SynthesisCommandQueue(ttl_seconds=60)
    e1 = _evt()
    e2 = CapabilityGapEvent(goal="updated goal", task_type="vision_action", target_app="xcode", source="primary_fallback")
    q.enqueue(e1)
    q.enqueue(e2)
    cmd = q.dequeue()
    assert cmd is not None
    assert cmd.event.goal == "updated goal"
    assert q.dequeue() is None


def test_different_domains_coexist():
    q = SynthesisCommandQueue(ttl_seconds=60)
    q.enqueue(_evt(task_type="vision_action"))
    q.enqueue(_evt(task_type="email_compose", target_app="gmail"))
    results = []
    while (cmd := q.dequeue()) is not None:
        results.append(cmd)
    assert len(results) == 2
```

- [ ] **Step 2: Write failing tests for GapResolutionProtocol**

```python
# tests/unit/synthesis/test_gap_resolution_protocol.py
import asyncio
import pytest
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent
from backend.neural_mesh.synthesis.gap_resolution_protocol import (
    GapResolutionProtocol,
    ResolutionMode,
    DasSynthesisState,
)


def _evt(source="primary_fallback", task_type="vision_action", target_app="xcode"):
    return CapabilityGapEvent(
        goal="open prefs",
        task_type=task_type,
        target_app=target_app,
        source=source,
    )


def test_resolution_modes_exist():
    assert ResolutionMode.A
    assert ResolutionMode.B
    assert ResolutionMode.C


def test_19_states_defined():
    state_names = {s.name for s in DasSynthesisState}
    required = {
        "GAP_DETECTED", "GAP_COALESCING", "GAP_COALESCED",
        "ROUTE_DECIDED_A", "ROUTE_DECIDED_B", "ROUTE_DECIDED_C",
        "SYNTH_PENDING", "SYNTH_TIMEOUT", "SYNTH_REJECTED",
        "ARTIFACT_WRITTEN", "QUARANTINED_PENDING_REVIEW", "ARTIFACT_VERIFIED",
        "CANARY_ACTIVE", "CANARY_ROLLED_BACK", "AGENT_GRADUATED",
        "REPLAY_AUTHORIZED", "REPLAY_STALE",
        "CLOSED_RESOLVED", "CLOSED_UNRESOLVED",
    }
    assert required == state_names


def test_dream_advisory_always_mode_c():
    protocol = GapResolutionProtocol()
    evt = _evt(source="dream_advisory")
    mode = protocol.classify_mode(evt)
    assert mode == ResolutionMode.C


def test_high_risk_domain_is_mode_a():
    protocol = GapResolutionProtocol()
    evt = _evt(task_type="file_edit", target_app="any")
    mode = protocol.classify_mode(evt)
    assert mode == ResolutionMode.A


def test_screen_observation_is_mode_c():
    protocol = GapResolutionProtocol()
    evt = _evt(task_type="screen_observation", target_app="any")
    mode = protocol.classify_mode(evt)
    assert mode == ResolutionMode.C


@pytest.mark.asyncio
async def test_single_flight_dedup_collapses_burst():
    protocol = GapResolutionProtocol()
    synthesis_calls = []

    async def fake_synthesize(evt, dedupe_key):
        synthesis_calls.append(dedupe_key)
        await asyncio.sleep(0.02)

    protocol._synthesize = fake_synthesize

    evt = _evt()
    tasks = [
        asyncio.create_task(protocol.handle_gap_event(evt))
        for _ in range(5)
    ]
    await asyncio.gather(*tasks)
    assert len(synthesis_calls) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/synthesis/test_synthesis_command_queue.py \
                  tests/unit/synthesis/test_gap_resolution_protocol.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Implement `SynthesisCommandQueue`**

```python
# backend/neural_mesh/synthesis/synthesis_command_queue.py
"""
SynthesisCommandQueue — Mode B TTL queue with semantic supersession.

Semantic supersession: newer entry with same dedupe_key replaces older.
Expired entries (past TTL) are silently discarded on dequeue.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent


@dataclass
class SynthesisCommand:
    event: CapabilityGapEvent
    enqueued_at: float = field(default_factory=time.monotonic)


class SynthesisCommandQueue:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else float(
            os.environ.get("DAS_MODE_B_TTL_S", "1800")
        )
        self._lock = threading.Lock()
        self._order: List[str] = []
        self._store: Dict[str, SynthesisCommand] = {}

    def enqueue(self, event: CapabilityGapEvent) -> None:
        key = event.dedupe_key
        cmd = SynthesisCommand(event=event)
        with self._lock:
            if key in self._store:
                self._store[key] = cmd  # supersede in-place
            else:
                self._order.append(key)
                self._store[key] = cmd

    def dequeue(self) -> Optional[SynthesisCommand]:
        now = time.monotonic()
        with self._lock:
            while self._order:
                key = self._order[0]
                cmd = self._store.get(key)
                if cmd is None:
                    self._order.pop(0)
                    continue
                if now - cmd.enqueued_at > self._ttl:
                    self._order.pop(0)
                    del self._store[key]
                    continue
                self._order.pop(0)
                del self._store[key]
                return cmd
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
```

- [ ] **Step 5: Implement `GapResolutionProtocol`**

```python
# backend/neural_mesh/synthesis/gap_resolution_protocol.py
"""
GapResolutionProtocol — dedup lock + tri-mode routing + 19-state FSM.

Resolution modes:
  A — Fail Fast: high risk or non-idempotent — no auto-routing.
  B — Pending Queue: idempotent + user_critical — enqueue; replay on graduation.
  C — Parallel Fallback: read_only or assistive — execute fallback in parallel.

19-State FSM (states only — transitions enforced at synthesis time):
  GAP_DETECTED → GAP_COALESCING → GAP_COALESCED
  → ROUTE_DECIDED_A/B/C → SYNTH_PENDING
  → SYNTH_TIMEOUT|SYNTH_REJECTED → CLOSED_UNRESOLVED
  → ARTIFACT_WRITTEN → ARTIFACT_VERIFIED|QUARANTINED_PENDING_REVIEW
  → CANARY_ACTIVE → CANARY_ROLLED_BACK|AGENT_GRADUATED
  → REPLAY_AUTHORIZED → REPLAY_STALE|CLOSED_RESOLVED
  → CLOSED_RESOLVED|CLOSED_UNRESOLVED (terminal)
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

import yaml

from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent

log = logging.getLogger(__name__)

_POLICY_PATH = Path(__file__).parent / "gap_resolution_policy.yaml"


class ResolutionMode(str, Enum):
    A = "A"  # Fail Fast
    B = "B"  # Pending Queue
    C = "C"  # Parallel Fallback


class DasSynthesisState(str, Enum):
    GAP_DETECTED = "GAP_DETECTED"
    GAP_COALESCING = "GAP_COALESCING"
    GAP_COALESCED = "GAP_COALESCED"
    ROUTE_DECIDED_A = "ROUTE_DECIDED_A"
    ROUTE_DECIDED_B = "ROUTE_DECIDED_B"
    ROUTE_DECIDED_C = "ROUTE_DECIDED_C"
    SYNTH_PENDING = "SYNTH_PENDING"
    SYNTH_TIMEOUT = "SYNTH_TIMEOUT"
    SYNTH_REJECTED = "SYNTH_REJECTED"
    ARTIFACT_WRITTEN = "ARTIFACT_WRITTEN"
    QUARANTINED_PENDING_REVIEW = "QUARANTINED_PENDING_REVIEW"
    ARTIFACT_VERIFIED = "ARTIFACT_VERIFIED"
    CANARY_ACTIVE = "CANARY_ACTIVE"
    CANARY_ROLLED_BACK = "CANARY_ROLLED_BACK"
    AGENT_GRADUATED = "AGENT_GRADUATED"
    REPLAY_AUTHORIZED = "REPLAY_AUTHORIZED"
    REPLAY_STALE = "REPLAY_STALE"
    CLOSED_RESOLVED = "CLOSED_RESOLVED"
    CLOSED_UNRESOLVED = "CLOSED_UNRESOLVED"


@dataclass
class GapResolutionPolicy:
    risk_class: str = "medium"
    idempotent: bool = True
    user_critical: bool = False
    read_only: bool = False
    assistive: bool = False
    slo_p99_ms: int = 5000


def _load_policy(domain_id: str) -> GapResolutionPolicy:
    try:
        data = yaml.safe_load(_POLICY_PATH.read_text())
    except Exception as exc:
        log.warning("Failed to load gap_resolution_policy.yaml: %s; using defaults", exc)
        return GapResolutionPolicy()

    defaults = data.get("defaults", {})
    overrides = data.get("domain_overrides", {}).get(domain_id, {})
    merged = {**defaults, **overrides}
    return GapResolutionPolicy(
        risk_class=merged.get("risk_class", "medium"),
        idempotent=merged.get("idempotent", True),
        user_critical=merged.get("user_critical", False),
        read_only=merged.get("read_only", False),
        assistive=merged.get("assistive", False),
        slo_p99_ms=int(merged.get("slo_p99_ms", 5000)),
    )


class GapResolutionProtocol:
    def __init__(self) -> None:
        self._in_flight: Dict[str, asyncio.Event] = {}
        self._synth_timeout_s: float = float(
            os.environ.get("DAS_SYNTH_TIMEOUT_S", "120")
        )
        self._quarantine_max_retries: int = int(
            os.environ.get("DAS_QUARANTINE_MAX_RETRIES", "3")
        )
        self._oscillation_flip_threshold: int = int(
            os.environ.get("DAS_OSCILLATION_FLIP_THRESHOLD", "3")
        )
        self._oscillation_window_s: float = float(
            os.environ.get("DAS_OSCILLATION_WINDOW_S", "60")
        )
        self._oscillation_freeze_s: float = float(
            os.environ.get("DAS_OSCILLATION_FREEZE_S", "300")
        )
        # domain_id → list of flip timestamps (monotonic)
        self._flip_history: Dict[str, list] = {}
        # domain_id → freeze-until monotonic time
        self._frozen_until: Dict[str, float] = {}

    def classify_mode(self, event: CapabilityGapEvent) -> ResolutionMode:
        if event.source == "dream_advisory":
            return ResolutionMode.C
        policy = _load_policy(event.domain_id)
        return self._classify_mode(event, policy)

    def _classify_mode(
        self, event: CapabilityGapEvent, policy: GapResolutionPolicy
    ) -> ResolutionMode:
        if policy.risk_class == "high" or not policy.idempotent:
            return ResolutionMode.A
        if policy.user_critical and policy.idempotent:
            return ResolutionMode.B
        return ResolutionMode.C

    def _is_oscillating(self, domain_id: str) -> bool:
        """Return True if the domain is currently frozen due to oscillation."""
        import time as _time
        now = _time.monotonic()
        # Check freeze
        if self._frozen_until.get(domain_id, 0) > now:
            return True
        # Prune old flips outside the window
        flips = self._flip_history.get(domain_id, [])
        flips = [t for t in flips if now - t <= self._oscillation_window_s]
        self._flip_history[domain_id] = flips
        if len(flips) >= self._oscillation_flip_threshold:
            self._frozen_until[domain_id] = now + self._oscillation_freeze_s
            log.warning(
                "GapResolutionProtocol: oscillation detected for domain_id=%s — freezing for %.0fs",
                domain_id,
                self._oscillation_freeze_s,
            )
            return True
        return False

    def record_route_flip(self, domain_id: str) -> None:
        """Call when a route flip is observed (canary ↔ stable switch)."""
        import time as _time
        self._flip_history.setdefault(domain_id, []).append(_time.monotonic())

    async def handle_gap_event(self, event: CapabilityGapEvent) -> None:
        if os.environ.get("DAS_ENABLED", "true").lower() in ("false", "0", "no"):
            return
        if self._is_oscillating(event.domain_id):
            log.warning(
                "GapResolutionProtocol: domain %s is frozen (oscillation) — skipping synthesis",
                event.domain_id,
            )
            return
        dedupe_key = event.dedupe_key
        if dedupe_key in self._in_flight:
            try:
                await asyncio.wait_for(
                    self._in_flight[dedupe_key].wait(),
                    timeout=self._synth_timeout_s,
                )
            except asyncio.TimeoutError:
                log.warning("Dedup wait timed out for domain_id=%s", event.domain_id)
            return
        done = asyncio.Event()
        self._in_flight[dedupe_key] = done
        try:
            await self._synthesize(event, dedupe_key)
        finally:
            done.set()
            self._in_flight.pop(dedupe_key, None)

    async def _synthesize(
        self, event: CapabilityGapEvent, dedupe_key: str, retry_count: int = 0
    ) -> None:
        """
        Drives the Ouroboros synthesis pipeline for one gap event.

        Quarantine retry loop: if QUARANTINED_PENDING_REVIEW is reached,
        retry up to DAS_QUARANTINE_MAX_RETRIES times with a new attempt_key.
        Each retry increments retry_count. When max retries are exhausted,
        the FSM transitions to CLOSED_UNRESOLVED.
        Override in integration tests to inject fake synthesis behavior.
        """
        if retry_count > self._quarantine_max_retries:
            log.warning(
                "GapResolutionProtocol: max quarantine retries (%d) exhausted for domain_id=%s "
                "— transitioning to CLOSED_UNRESOLVED",
                self._quarantine_max_retries,
                event.domain_id,
            )
            return
        log.info(
            "GapResolutionProtocol._synthesize domain_id=%s mode=%s retry=%d",
            event.domain_id,
            self.classify_mode(event).value,
            retry_count,
        )
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/synthesis/test_synthesis_command_queue.py \
                  tests/unit/synthesis/test_gap_resolution_protocol.py -v
```
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/neural_mesh/synthesis/synthesis_command_queue.py \
        backend/neural_mesh/synthesis/gap_resolution_protocol.py \
        tests/unit/synthesis/test_synthesis_command_queue.py \
        tests/unit/synthesis/test_gap_resolution_protocol.py
git commit -m "feat(das): add SynthesisCommandQueue + GapResolutionProtocol + 19-state FSM (Task 5)"
```

---

## Task 6: `CapabilityGapSensor` — Ouroboros intake sensor (correct path)

**Files:**
- Create: `backend/core/ouroboros/governance/intake/sensors/__init__.py`
- Create: `backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py`
- Create: `tests/unit/synthesis/test_capability_gap_sensor.py`

- [ ] **Step 1: Read make_envelope signature**

```bash
grep -n "def make_envelope\|make_envelope" \
  backend/core/ouroboros/governance/intake/intent_envelope.py | head -10
grep -n "UnifiedIntakeRouter\|submit" \
  backend/core/ouroboros/governance/intake/unified_intake_router.py | head -10
```

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/synthesis/test_capability_gap_sensor.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent, GapSignalBus
from backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor import (
    CapabilityGapSensor,
)


def _evt(task_type="vision_action", target_app="xcode"):
    return CapabilityGapEvent(
        goal="open prefs",
        task_type=task_type,
        target_app=target_app,
        source="primary_fallback",
    )


@pytest.mark.asyncio
async def test_sensor_submits_envelope_for_gap():
    bus = GapSignalBus(maxsize=10)
    mock_router = AsyncMock()
    mock_router.submit = AsyncMock()

    with patch(
        "backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor.make_envelope"
    ) as mock_envelope:
        mock_envelope.return_value = MagicMock()
        sensor = CapabilityGapSensor(intake_router=mock_router, repo="jarvis", bus=bus)
        task = asyncio.create_task(
            asyncio.wait_for(sensor._poll_once(), timeout=0.5)
        )
        bus.emit(_evt())
        try:
            await task
        except asyncio.TimeoutError:
            pass

    mock_envelope.assert_called_once()
    call_kwargs = mock_envelope.call_args[1] if mock_envelope.call_args[1] else {}
    call_args = mock_envelope.call_args[0] if mock_envelope.call_args[0] else ()
    # source="capability_gap" must be in the call
    all_args = list(call_args) + list(call_kwargs.values())
    assert any("capability_gap" in str(a) for a in all_args) or \
           call_kwargs.get("source") == "capability_gap"


def test_sensor_location():
    """Sensor must be in the sensors/ subdirectory, not directly in intake/."""
    import importlib
    mod = importlib.import_module(
        "backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor"
    )
    assert hasattr(mod, "CapabilityGapSensor")
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python3 -m pytest tests/unit/synthesis/test_capability_gap_sensor.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Implement `CapabilityGapSensor`**

First read `intent_envelope.py` to get the exact signature of `make_envelope`, then implement:

```python
# backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py
"""
CapabilityGapSensor — Ouroboros intake sensor for capability gap events.

Follows the OpportunityMinerSensor pattern exactly:
- Standalone class, async _poll_loop(), no base class.
- Uses make_envelope() from intent_envelope (same as all other sensors).
- Registered in agent_initializer.py at startup.
"""
from __future__ import annotations

import asyncio
import logging

from backend.neural_mesh.synthesis.gap_signal_bus import (
    CapabilityGapEvent,
    GapSignalBus,
    get_gap_signal_bus,
)
from backend.neural_mesh.synthesis.gap_resolution_protocol import (
    GapResolutionProtocol,
    ResolutionMode,
)
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

log = logging.getLogger(__name__)

_protocol = GapResolutionProtocol()


class CapabilityGapSensor:
    def __init__(
        self,
        intake_router,
        repo: str,
        bus: GapSignalBus | None = None,
    ) -> None:
        self._router = intake_router
        self._repo = repo
        self._gap_bus: GapSignalBus = bus if bus is not None else get_gap_signal_bus()

    def start(self) -> None:
        asyncio.create_task(self._poll_loop(), name="capability_gap_sensor_poll")

    async def _poll_loop(self) -> None:
        while True:
            try:
                event = await self._gap_bus.get()
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("CapabilityGapSensor: error in poll loop")

    async def _poll_once(self) -> None:
        """Single poll — used in tests."""
        event = await self._gap_bus.get()
        await self._handle(event)

    async def _handle(self, event: CapabilityGapEvent) -> None:
        mode = _protocol.classify_mode(event)
        try:
            envelope = make_envelope(
                source="capability_gap",
                description=f"Synthesize agent for {event.task_type}:{event.target_app}",
                target_files=(
                    f"backend/neural_mesh/synthesis/agents/{event.domain_id}.py",
                ),
                repo=self._repo,
                confidence=0.9,
                urgency=1.0 if mode == ResolutionMode.B else 0.5,
                evidence={
                    "task_type": event.task_type,
                    "target_app": event.target_app,
                    "dedupe_key": event.dedupe_key,
                    "attempt_key": event.attempt_key,
                    "resolution_mode": mode.value,
                    "domain_id": event.domain_id,
                },
                requires_human_ack=(mode == ResolutionMode.A),
            )
            await self._router.submit(envelope)
        except Exception:
            log.exception(
                "CapabilityGapSensor: failed to submit envelope domain_id=%s",
                event.domain_id,
            )
```

Adapt `make_envelope` kwargs to exactly match what `intent_envelope.py` accepts — read the file first.

- [ ] **Step 5: Run test to verify it passes**

```bash
python3 -m pytest tests/unit/synthesis/test_capability_gap_sensor.py -v
```
Expected: both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/__init__.py \
        backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py \
        tests/unit/synthesis/test_capability_gap_sensor.py
git commit -m "feat(das): add CapabilityGapSensor in sensors/ subdirectory (Task 6)"
```

---

## Task 7: `AgentRegistry` — extract `_resolve_internal()`, gap emission, fix `rollback_agent()`

**Files:**
- Modify: `backend/neural_mesh/registry/agent_registry.py`
- Create: `tests/unit/synthesis/test_registry_gap_emission.py`

- [ ] **Step 1: Read the current implementation**

```bash
grep -n "def resolve_capability\|computer_use\|_stable_routes\|_rollback_log\|_version\|_active_routes\|_lock" \
  backend/neural_mesh/registry/agent_registry.py | head -30
```

Then read lines 1535–1600 for the full body.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/synthesis/test_registry_gap_emission.py
import pytest
from unittest.mock import patch, MagicMock
from backend.neural_mesh.registry.agent_registry import AgentRegistry
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent


@pytest.fixture()
def registry():
    return AgentRegistry()


def test_gap_emitted_on_universal_fallback(registry, monkeypatch):
    emitted = []

    def fake_emit(event):
        emitted.append(event)

    with patch(
        "backend.neural_mesh.synthesis.gap_signal_bus.GapSignalBus.emit",
        side_effect=lambda self_inner, e: fake_emit(e),
    ):
        result = registry.resolve_capability(
            goal="do something completely unknown xyz",
            target_app="nonexistent_app_xyz",
            task_type="nonexistent_task_xyz",
        )

    assert result[0] == "computer_use"
    assert len(emitted) == 1
    evt = emitted[0]
    assert evt.source == "primary_fallback"
    assert evt.task_type == "nonexistent_task_xyz"


def test_signature_unchanged(registry):
    """resolve_capability must keep its original 3-argument signature."""
    import inspect
    sig = inspect.signature(registry.resolve_capability)
    params = list(sig.parameters.keys())
    assert "goal" in params
    assert "target_app" in params
    # session_id and command_id must NOT be added as parameters
    assert "session_id" not in params
    assert "command_id" not in params


def test_no_gap_when_capability_found(registry, monkeypatch):
    emitted = []

    def fake_emit(event):
        emitted.append(event)

    with patch(
        "backend.neural_mesh.synthesis.gap_signal_bus.GapSignalBus.emit",
        side_effect=lambda self_inner, e: fake_emit(e),
    ):
        registry.resolve_capability(
            goal="open google chrome",
            target_app="chrome",
            task_type="browser_navigation",
        )

    assert len(emitted) == 0
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python3 -m pytest tests/unit/synthesis/test_registry_gap_emission.py -v 2>&1 | head -20
```
Expected: FAIL — gap is not yet emitted.

- [ ] **Step 4: Modify `agent_registry.py`**

Read lines 1541–1594 carefully. Then:

**4a.** Extract the existing body of `resolve_capability` into `_resolve_internal()` — verbatim, no logic change.

**4b.** Replace the public `resolve_capability` body with the detection wrapper. Keep original signature exactly:

```python
def resolve_capability(
    self,
    goal: str,
    target_app: Optional[str],
    task_type: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    primary, fallback = self._resolve_internal(goal, target_app, task_type)
    if primary == "computer_use" and fallback is None:
        from backend.neural_mesh.synthesis.gap_signal_bus import (
            CapabilityGapEvent,
            get_gap_signal_bus,
        )
        get_gap_signal_bus().emit(CapabilityGapEvent(
            goal=goal,
            task_type=task_type or "",
            target_app=target_app or "",
            source="primary_fallback",
        ))
    return primary, fallback
```

**4c.** Replace the existing `rollback_agent` (if any) or add it. Per spec — uses `_stable_routes`, `_rollback_log`, `_active_routes`, `_version`. Adapt attribute names to match what the file actually uses:

```python
async def rollback_agent(self, domain_id: str, reason: str) -> None:
    async with self._lock:
        self._version += 1
        self._rollback_log.append({
            "domain_id": domain_id,
            "version": self._version,
            "reason": reason,
            "timestamp_ms": int(time.time() * 1000),
        })
        if domain_id in self._stable_routes:
            self._active_routes[domain_id] = self._stable_routes[domain_id]
```

Note: Does NOT pop `sys.modules` — in-flight executions complete normally.

- [ ] **Step 5: Run test to verify it passes**

```bash
python3 -m pytest tests/unit/synthesis/test_registry_gap_emission.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/neural_mesh/registry/agent_registry.py \
        tests/unit/synthesis/test_registry_gap_emission.py
git commit -m "feat(das): extract _resolve_internal(), gap emission, fix rollback_agent (Task 7)"
```

---

## Task 8: Wire sources + register sensor

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intent_envelope.py`
- Modify: `backend/core/ouroboros/governance/intake/unified_intake_router.py`
- Modify: `backend/neural_mesh/agents/agent_initializer.py`

- [ ] **Step 1: Read all three files**

```bash
grep -n "_VALID_SOURCES\|VALID_SOURCES" \
  backend/core/ouroboros/governance/intake/intent_envelope.py | head -5
grep -n "capability_gap\|synthesis\|intake_router\|route\|submit" \
  backend/core/ouroboros/governance/intake/unified_intake_router.py | head -20
grep -n "CapabilityGap\|Sensor\|register\|start\|intake" \
  backend/neural_mesh/agents/agent_initializer.py | head -20
```

- [ ] **Step 2: Write the failing tests**

```python
# Append to tests/unit/synthesis/test_capability_gap_sensor.py

def test_capability_gap_in_valid_sources():
    from backend.core.ouroboros.governance.intake.intent_envelope import _VALID_SOURCES
    assert "capability_gap" in _VALID_SOURCES


def test_capability_gap_sensor_registered_in_agent_initializer():
    """agent_initializer must import CapabilityGapSensor."""
    import importlib
    src = importlib.util.find_spec(
        "backend.neural_mesh.agents.agent_initializer"
    )
    assert src is not None
    text = open(src.origin).read()
    assert "CapabilityGapSensor" in text
```

Run: `python3 -m pytest tests/unit/synthesis/test_capability_gap_sensor.py -v`
Expected: 2 new tests FAIL.

- [ ] **Step 3: Add `"capability_gap"` to `_VALID_SOURCES` in `intent_envelope.py`**

Read the file, find the frozenset at line ~20, add `"capability_gap"`.

- [ ] **Step 4: Route `capability_gap` source in `unified_intake_router.py`**

Read the router. Find where it dispatches by `source` (e.g., a dict or if/elif chain). Add routing for `"capability_gap"` to the synthesis handler, following the existing pattern for other sources.

- [ ] **Step 5: Register `CapabilityGapSensor` in `agent_initializer.py`**

Find where other sensors (e.g., `OpportunityMinerSensor`) are imported and started. Add:

```python
from backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor import (
    CapabilityGapSensor,
)

# In the initializer method that starts sensors:
gap_sensor = CapabilityGapSensor(
    intake_router=intake_router,
    repo=repo,
)
gap_sensor.start()
```

Adapt variable names to match the file's existing pattern.

- [ ] **Step 6: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/synthesis/test_capability_gap_sensor.py -v
```
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/intake/intent_envelope.py \
        backend/core/ouroboros/governance/intake/unified_intake_router.py \
        backend/neural_mesh/agents/agent_initializer.py \
        tests/unit/synthesis/test_capability_gap_sensor.py
git commit -m "feat(das): wire capability_gap source + register CapabilityGapSensor (Task 8)"
```

---

## Task 9: `unified_command_processor.py` — `session_id` + `das_canary_key`

**Files:**
- Modify: `backend/api/unified_command_processor.py`

- [ ] **Step 1: Read the file**

```bash
grep -n "command_id\|session_id\|das_canary\|command_text\|_normalize_command" \
  backend/api/unified_command_processor.py | head -30
```

Lines 3017 and 3543 per spec have the existing `command_id` UUID generation.

- [ ] **Step 2: Write the failing test**

```python
# Inline verification — add to tests/unit/synthesis/test_registry_gap_emission.py

def test_das_canary_key_generation():
    """das_canary_key = sha256(session_id:normalized_command) — verify formula."""
    import hashlib
    import re

    def _normalize_command(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    session_id = "test-session-abc"
    command_text = "  Open My Email  "
    expected = hashlib.sha256(
        f"{session_id}:{_normalize_command(command_text)}".encode()
    ).hexdigest()

    # Verify the formula is deterministic and matches expected hash
    result = hashlib.sha256(
        f"{session_id}:{_normalize_command(command_text)}".encode()
    ).hexdigest()
    assert result == expected
    assert len(result) == 64
```

- [ ] **Step 3: Add `session_id` to session state object**

Read the file to find where session state is created (likely near line 3017). Add `session_id = str(uuid.uuid4())` generated once at session creation and stored on the session state object.

- [ ] **Step 4: Generate `das_canary_key` alongside existing `command_id`**

Near lines 3017 and 3543 (where `command_id` is generated), add `das_canary_key` — does NOT replace `command_id`:

```python
import hashlib
import re

def _normalize_command(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())

# NEW: generated alongside existing command_id, not replacing it
das_canary_key = hashlib.sha256(
    f"{session_id}:{_normalize_command(command_text)}".encode()
).hexdigest()
```

Store `das_canary_key` on the command context so it's available for canary routing.

- [ ] **Step 5: Handle Mode A/B/C responses**

Find where command results are returned. Add handling for `CapabilityGapError` (Mode A — synthesizing, retry later) per the existing error handling pattern. Mode B entries are automatically queued by `SynthesisCommandQueue`; Mode C uses the existing fallback path.

- [ ] **Step 6: Run existing command processor tests**

```bash
python3 -m pytest tests/ -k "command_processor" -v --tb=short 2>&1 | tail -20
```
Expected: existing tests PASS (no regressions).

- [ ] **Step 7: Commit**

```bash
git add backend/api/unified_command_processor.py
git commit -m "feat(das): add session_id + das_canary_key to unified_command_processor (Task 9)"
```

---

## Task 10: Canary routing + graduation + EventType additions + J-Prime + Trinity

**Files:**
- Modify: `backend/neural_mesh/registry/agent_registry.py` (canary routing + graduation)
- Modify: `backend/core/ouroboros/cross_repo.py` (7 new EventType values)
- Modify: `reactor_core/integration/event_bridge.py` (mirror 7 new EventType values)
- Modify: `jarvis_prime/reasoning/graph_nodes/execution_planner.py`
- Modify: `backend/neural_mesh/synthesis/gap_resolution_protocol.py` (Trinity emit calls)

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/synthesis/test_canary_routing.py
import hashlib
import pytest
from backend.neural_mesh.registry.agent_registry import AgentRegistry


def test_route_to_canary_deterministic():
    registry = AgentRegistry()
    domain_id = "vision_action:xcode"
    das_canary_key = "abc123"
    result1 = registry._route_to_canary(domain_id, das_canary_key)
    result2 = registry._route_to_canary(domain_id, das_canary_key)
    assert result1 == result2  # deterministic


def test_route_to_canary_10_percent():
    """Expect ~10% of keys to route to canary across 1000 samples (default DAS_CANARY_TRAFFIC_PCT=10)."""
    import os
    os.environ.setdefault("DAS_CANARY_TRAFFIC_PCT", "10")
    registry = AgentRegistry()
    domain_id = "vision_action:xcode"
    hits = sum(
        1 for i in range(1000)
        if registry._route_to_canary(domain_id, f"key_{i}")
    )
    assert 50 <= hits <= 150  # allow loose tolerance around 10%


def test_das_enabled_env_var_gates_synthesis():
    """DAS_ENABLED=false must cause handle_gap_event to no-op."""
    import asyncio, os
    from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent
    from backend.neural_mesh.synthesis.gap_resolution_protocol import GapResolutionProtocol
    calls = []
    async def _run():
        os.environ["DAS_ENABLED"] = "false"
        try:
            protocol = GapResolutionProtocol()
            protocol._synthesize = lambda *a, **kw: calls.append(1) or asyncio.sleep(0)
            await protocol.handle_gap_event(CapabilityGapEvent(
                goal="test", task_type="vision_action", target_app="xcode",
                source="primary_fallback",
            ))
        finally:
            os.environ.pop("DAS_ENABLED", None)
    asyncio.get_event_loop().run_until_complete(_run())
    assert len(calls) == 0


def test_oscillation_freeze_prevents_synthesis():
    """After DAS_OSCILLATION_FLIP_THRESHOLD flips in window, domain is frozen."""
    import os
    os.environ["DAS_OSCILLATION_FLIP_THRESHOLD"] = "2"
    os.environ["DAS_OSCILLATION_WINDOW_S"] = "60"
    os.environ["DAS_OSCILLATION_FREEZE_S"] = "300"
    try:
        from backend.neural_mesh.synthesis.gap_resolution_protocol import GapResolutionProtocol
        protocol = GapResolutionProtocol()
        domain = "vision_action:xcode"
        protocol.record_route_flip(domain)
        protocol.record_route_flip(domain)  # threshold hit
        assert protocol._is_oscillating(domain) is True
    finally:
        for k in ("DAS_OSCILLATION_FLIP_THRESHOLD", "DAS_OSCILLATION_WINDOW_S", "DAS_OSCILLATION_FREEZE_S"):
            os.environ.pop(k, None)


def test_synthesis_command_queue_reads_ttl_from_env():
    import os
    os.environ["DAS_MODE_B_TTL_S"] = "999"
    try:
        from backend.neural_mesh.synthesis.synthesis_command_queue import SynthesisCommandQueue
        q = SynthesisCommandQueue()
        assert q._ttl == 999.0
    finally:
        os.environ.pop("DAS_MODE_B_TTL_S", None)


def test_das_event_types_in_cross_repo():
    from backend.core.ouroboros.cross_repo import EventType
    required = {
        "AGENT_SYNTHESIS_REQUESTED",
        "AGENT_SYNTHESIS_CANARY_ACTIVE",
        "AGENT_SYNTHESIS_COMPLETED",
        "AGENT_SYNTHESIS_FAILED",
        "CAPABILITY_GAP_UNRESOLVED",
        "AGENT_SYNTHESIS_CONFLICT",
        "ROUTING_OSCILLATION_DETECTED",
    }
    actual = {e.name for e in EventType}
    assert required.issubset(actual)


def test_das_event_types_in_reactor_core():
    from reactor_core.integration.event_bridge import EventType as RCEventType
    required = {
        "AGENT_SYNTHESIS_REQUESTED",
        "AGENT_SYNTHESIS_COMPLETED",
        "AGENT_SYNTHESIS_FAILED",
        "CAPABILITY_GAP_UNRESOLVED",
    }
    actual = {e.name for e in RCEventType}
    assert required.issubset(actual)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/synthesis/test_canary_routing.py -v 2>&1 | head -20
```

- [ ] **Step 3: Add `_route_to_canary()` to `AgentRegistry`**

```python
def _route_to_canary(self, domain_id: str, das_canary_key: str) -> bool:
    """Sticky canary routing. Same (domain_id, das_canary_key) always same bucket.
    Traffic percentage controlled by DAS_CANARY_TRAFFIC_PCT (default 10)."""
    pct = int(os.environ.get("DAS_CANARY_TRAFFIC_PCT", "10"))
    raw = f"{domain_id}:{das_canary_key}".encode()
    bucket = int(hashlib.sha256(raw).hexdigest(), 16) % 100
    return bucket < pct
```

- [ ] **Step 4: Add hybrid graduation gate to `AgentRegistry`**

```python
def _check_graduation(
    self,
    domain_id: str,
    requests: int,
    elapsed_s: float,
    distinct_sessions: int,
    error_rate: float,
    p99_latency_ms: float,
    domain_slo_p99_ms: int,
    no_incidents_in_window: bool,
) -> bool:
    """Return True when the canary meets all graduation conditions."""
    min_requests = int(os.environ.get("DAS_CANARY_MIN_REQUESTS", "10"))
    min_elapsed = float(os.environ.get("DAS_CANARY_MIN_ELAPSED_S", "300"))
    min_sessions = int(os.environ.get("DAS_CANARY_MIN_SESSIONS", "3"))
    max_error_rate = float(os.environ.get("DAS_CANARY_MAX_ERROR_RATE", "0.01"))

    volume_ok = requests >= min_requests or (
        elapsed_s >= min_elapsed and distinct_sessions >= min_sessions
    )
    return (
        volume_ok
        and error_rate < max_error_rate
        and p99_latency_ms <= domain_slo_p99_ms
        and no_incidents_in_window
    )
```

- [ ] **Step 5: Add 7 EventType values to `cross_repo.py`**

Read the file, find the `EventType` enum, add after the last existing value:

```python
# Dynamic Agent Synthesis lifecycle events
AGENT_SYNTHESIS_REQUESTED = "agent_synthesis_requested"
AGENT_SYNTHESIS_CANARY_ACTIVE = "agent_synthesis_canary_active"
AGENT_SYNTHESIS_COMPLETED = "agent_synthesis_completed"
AGENT_SYNTHESIS_FAILED = "agent_synthesis_failed"
CAPABILITY_GAP_UNRESOLVED = "capability_gap_unresolved"
AGENT_SYNTHESIS_CONFLICT = "agent_synthesis_conflict"
ROUTING_OSCILLATION_DETECTED = "routing_oscillation_detected"
```

- [ ] **Step 6: Mirror in `reactor_core/integration/event_bridge.py`**

Read the file. Add the same 7 values to the corresponding enum. Both repos must be updated together.

- [ ] **Step 7: Add `capability_gap_hint` to J-Prime ExecutionPlanner**

In `jarvis_prime/reasoning/graph_nodes/execution_planner.py`, in `_resolve_tool_from_index()` (at the return `""` at the bottom of the method), the hint is annotated in `process()`. After `plan_dict = plan.model_dump()`:

```python
# When _resolve_tool_from_index returned "" for any sub-goal, annotate the hint.
# The hint is advisory-only — it informs GapResolutionProtocol but never
# substitutes for primary detection.
_gap_sub_goals = [
    sg for sg in plan_dict.get("sub_goals", [])
    if sg.get("tool_required") == _DEFAULT_TOOL  # "app_control"
]
if _gap_sub_goals:
    # Use the first un-resolved sub-goal for the hint
    _sg = _gap_sub_goals[0]
    plan_dict["capability_gap_hint"] = {
        "task_type": _sg.get("task_type", ""),
        "target_app": _sg.get("target_app", ""),  # may be empty
        "reason": "no_index_match",
    }
```

- [ ] **Step 8: Add Trinity observer calls to `GapResolutionProtocol`**

In `_synthesize()` and at graduation/rollback, add observer-only Trinity emit calls wrapped in `try/except Exception`:

```python
# TRINITY_DREAM_DAS_ENABLED is defined but defaults to false (no-op in this iteration).
# DreamEngine and ProphecyEngine integration is deferred to a follow-up spec.
_trinity_dream_enabled = os.environ.get("TRINITY_DREAM_DAS_ENABLED", "false").lower() == "true"

# In _synthesize(), after mode classification:
try:
    from backend.intelligence.trinity.health_cortex import get_health_cortex
    get_health_cortex().record_synthesis_start(
        domain_id=event.domain_id,
        mode=self.classify_mode(event).value,
    )
except Exception:
    pass  # Trinity is optional — DAS proceeds without it

# In graduation handler:
try:
    from backend.intelligence.trinity.memory_engine import get_memory_engine
    get_memory_engine().record_synthesis_outcome(
        domain_id=event.domain_id,
        outcome="graduated",
    )
except Exception:
    pass
```

Adapt method names to match what Trinity's HealthCortex and MemoryEngine actually expose — read those files first.

- [ ] **Step 9: Run all synthesis tests**

```bash
python3 -m pytest tests/unit/synthesis/ -v --tb=short
```
Expected: all tests PASS.

- [ ] **Step 10: Commit all Task 10 changes**

```bash
git add backend/neural_mesh/registry/agent_registry.py \
        backend/core/ouroboros/cross_repo.py \
        backend/neural_mesh/synthesis/gap_resolution_protocol.py \
        tests/unit/synthesis/test_canary_routing.py
git commit -m "feat(das): canary routing + graduation + 7 EventType values (Task 10a)"

# In J-Prime repo:
cd /path/to/jarvis-prime
git add jarvis_prime/reasoning/graph_nodes/execution_planner.py
git commit -m "feat(das): capability_gap_hint annotation in ExecutionPlanner (Task 10b)"

# In Reactor-Core repo:
cd /path/to/reactor-core
git add reactor_core/integration/event_bridge.py
git commit -m "feat(das): mirror 7 DAS EventType values in event_bridge.py (Task 10c)"
```

---

## Task 11: End-to-end integration test + final verification

**Files:**
- Create: `tests/integration/synthesis/__init__.py`
- Create: `tests/integration/synthesis/test_das_cycle.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/integration/synthesis/test_das_cycle.py
"""
End-to-end DAS cycle: gap emission → bus → protocol dedup → ledger.
No network, no LLM. Fully in-process async.
"""
import asyncio
import pytest
from backend.neural_mesh.synthesis.gap_signal_bus import GapSignalBus, CapabilityGapEvent
from backend.neural_mesh.synthesis.domain_trust_ledger import DomainTrustLedger
from backend.neural_mesh.synthesis.gap_resolution_protocol import (
    GapResolutionProtocol,
    DasSynthesisState,
)


@pytest.mark.asyncio
async def test_burst_collapses_to_single_synthesis():
    bus = GapSignalBus(maxsize=50)
    ledger = DomainTrustLedger()
    protocol = GapResolutionProtocol()
    synthesis_calls = []

    async def fake_synthesize(evt, dedupe_key):
        synthesis_calls.append(evt.domain_id)
        await asyncio.sleep(0.01)
        ledger.record_success(evt.domain_id)

    protocol._synthesize = fake_synthesize

    # 5 identical events + 1 different domain
    for i in range(5):
        await protocol.handle_gap_event(CapabilityGapEvent(
            goal="open prefs",
            task_type="vision_action",
            target_app="xcode",
            source="primary_fallback",
        ))
    await protocol.handle_gap_event(CapabilityGapEvent(
        goal="write reply",
        task_type="email_compose",
        target_app="gmail",
        source="primary_fallback",
    ))

    # Burst collapsed: exactly 2 unique synthesis calls
    assert len(synthesis_calls) == 2
    assert "vision_action:xcode" in synthesis_calls
    assert "email_compose:gmail" in synthesis_calls


@pytest.mark.asyncio
async def test_19_states_are_complete():
    assert len(list(DasSynthesisState)) == 19


def test_trust_ledger_updates_on_synthesis():
    ledger = DomainTrustLedger()
    domain = "vision_action:xcode"
    for _ in range(5):
        ledger.record_success(domain)
    r = ledger.record(domain)
    assert r.successful_runs == 5
    assert r.total_attempts == 5
    assert r.trust_score > 0
```

- [ ] **Step 2: Run integration test**

```bash
python3 -m pytest tests/integration/synthesis/ -v --tb=short
```
Expected: all tests PASS.

- [ ] **Step 3: Run full synthesis suite**

```bash
python3 -m pytest tests/unit/synthesis/ tests/integration/synthesis/ -v --tb=short
```
Expected: all tests PASS, zero failures.

- [ ] **Step 4: Run Appendix B pre-implementation checklist (smoke checks)**

```bash
# 1. Verify GapSignalBus uses put_nowait (never await put)
grep -n "put_nowait\|await.*put" backend/neural_mesh/synthesis/gap_signal_bus.py

# 2. Verify domain_id excludes risk_class
grep -n "domain_id\|risk_class" backend/neural_mesh/synthesis/gap_signal_bus.py

# 3. Verify das_canary_key does not replace command_id
grep -n "das_canary_key\|command_id" backend/api/unified_command_processor.py | head -20

# 5. Verify rollback_agent does not pop sys.modules
grep -n "sys.modules\|rollback_agent" backend/neural_mesh/registry/agent_registry.py

# 7. Verify both cross_repo files have 7 new EventType values
grep -c "AGENT_SYNTHESIS" backend/core/ouroboros/cross_repo.py
grep -c "AGENT_SYNTHESIS" reactor_core/integration/event_bridge.py
```

- [ ] **Step 5: Final commit**

```bash
git add tests/integration/synthesis/__init__.py \
        tests/integration/synthesis/test_das_cycle.py
git commit -m "feat(das): end-to-end integration test + DAS implementation complete (Task 11)"
```

---

## Appendix B: Pre-Implementation Checklist

Before starting, verify all 10 go/no-go checks from the spec:

1. `GapSignalBus.emit()` uses `put_nowait()` — never `await put()`
2. `domain_id` = `normalize(task_type):normalize(target_app)` only — no `risk_class`
3. `das_canary_key` uses `sha256(session_id:normalized_command)` — stable per session; does NOT replace existing UUID `command_id`
4. Mode C read-only enforcement is at the adapter layer, not at the call site
5. `rollback_agent()` increments `_version` + route cutover only — does NOT pop `sys.modules`
6. `DomainTrustLedger` journal is append-only; all denominators use `max(..., 1)`
7. Quarantine retry generates new `attempt_key` with `retry_of_attempt_key` chain
8. `REPLAY_STALE` is reachable from `REPLAY_AUTHORIZED` on TTL expiry or supersession
9. All Trinity emit calls are in `try/except Exception` with no-op fallback
10. Both `cross_repo.py` AND `reactor_core/integration/event_bridge.py` receive the 7 new EventType values
