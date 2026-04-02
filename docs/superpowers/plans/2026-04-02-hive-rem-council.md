# Hive REM Council — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Hive a heartbeat — three review modules (Health Scanner, Graduation Auditor, Manifesto Compliance) that run during idle REM cycles and produce actionable threads.

**Architecture:** `RemCouncil` orchestrates three independent review modules sequentially within a budget. Each module collects deterministic inputs (metrics, ledger files, git log), then uses the 35B model via `PersonaEngine` to generate Trinity Persona analysis. Critical findings escalate to FLOW. The council runner integrates into `HiveService._rem_poll_loop()`.

**Tech Stack:** Python 3.12, asyncio, psutil (metrics), git CLI (diffs), DoublewordProvider (mocked in tests), existing Hive modules

**Spec:** `docs/superpowers/specs/2026-04-02-hive-rem-council-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `backend/hive/rem_council.py` | `RemCouncil` runner + `RemSessionResult` dataclass |
| `backend/hive/rem_health_scanner.py` | Module 1: collect system metrics, create health threads |
| `backend/hive/rem_graduation_auditor.py` | Module 2: scan Ouroboros ledger for graduation candidates |
| `backend/hive/rem_manifesto_reviewer.py` | Module 3: git diff analysis + Manifesto compliance |
| `tests/test_hive_rem_council.py` | Council runner tests |
| `tests/test_hive_rem_health.py` | Health scanner tests |
| `tests/test_hive_rem_graduation.py` | Graduation auditor tests |
| `tests/test_hive_rem_manifesto.py` | Manifesto reviewer tests |

### Modified Files

| File | Change |
|------|--------|
| `backend/hive/hive_service.py` | Wire `RemCouncil` into `_rem_poll_loop()` after FSM enters REM |

---

## Task 1: RemSessionResult + RemCouncil Runner

**Files:**
- Create: `backend/hive/rem_council.py`
- Create: `tests/test_hive_rem_council.py`

The council runner orchestrates modules and manages the budget. Modules are injected as callables so they can be mocked independently.

- [ ] **1.1: Write failing tests**

```python
# tests/test_hive_rem_council.py
"""Tests for RemCouncil — session lifecycle, budget splitting, escalation."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from backend.hive.rem_council import RemCouncil, RemSessionResult


@pytest.fixture
def mock_health():
    mod = AsyncMock()
    mod.run = AsyncMock(return_value=(["thr_health_1"], 5, False, None))
    return mod


@pytest.fixture
def mock_graduation():
    mod = AsyncMock()
    mod.run = AsyncMock(return_value=([], 3, False, None))
    return mod


@pytest.fixture
def mock_manifesto():
    mod = AsyncMock()
    mod.run = AsyncMock(return_value=(["thr_manifesto_1"], 8, False, None))
    return mod


@pytest.fixture
def council(mock_health, mock_graduation, mock_manifesto):
    return RemCouncil(
        health_scanner=mock_health,
        graduation_auditor=mock_graduation,
        manifesto_reviewer=mock_manifesto,
        max_calls=50,
    )


class TestSessionLifecycle:

    @pytest.mark.asyncio
    async def test_runs_all_three_modules(self, council, mock_health, mock_graduation, mock_manifesto):
        result = await council.run_session()
        mock_health.run.assert_called_once()
        mock_graduation.run.assert_called_once()
        mock_manifesto.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_session_result(self, council):
        result = await council.run_session()
        assert isinstance(result, RemSessionResult)
        assert result.threads_created == ["thr_health_1", "thr_manifesto_1"]
        assert result.calls_used == 16  # 5 + 3 + 8
        assert result.calls_budget == 50
        assert result.should_escalate is False
        assert result.modules_completed == ["health", "graduation", "manifesto"]
        assert result.modules_skipped == []

    @pytest.mark.asyncio
    async def test_sequential_execution_order(self, council, mock_health, mock_graduation, mock_manifesto):
        """Modules execute in order: health → graduation → manifesto."""
        call_order = []
        async def track_health(budget):
            call_order.append("health")
            return ([], 5, False, None)
        async def track_graduation(budget):
            call_order.append("graduation")
            return ([], 3, False, None)
        async def track_manifesto(budget):
            call_order.append("manifesto")
            return ([], 4, False, None)

        mock_health.run = track_health
        mock_graduation.run = track_graduation
        mock_manifesto.run = track_manifesto

        await council.run_session()
        assert call_order == ["health", "graduation", "manifesto"]


class TestBudgetSplitting:

    @pytest.mark.asyncio
    async def test_each_module_gets_fair_share(self, council, mock_health, mock_graduation, mock_manifesto):
        await council.run_session()
        # 50 // 3 = 16 per module, +2 reserve
        health_budget = mock_health.run.call_args[0][0]
        assert health_budget == 16

    @pytest.mark.asyncio
    async def test_budget_exhaustion_skips_remaining_modules(self):
        """If health uses all budget, graduation and manifesto are skipped."""
        health = AsyncMock()
        health.run = AsyncMock(return_value=(["thr_1"], 50, False, None))  # Uses entire budget
        graduation = AsyncMock()
        graduation.run = AsyncMock(return_value=([], 0, False, None))
        manifesto = AsyncMock()
        manifesto.run = AsyncMock(return_value=([], 0, False, None))

        council = RemCouncil(
            health_scanner=health,
            graduation_auditor=graduation,
            manifesto_reviewer=manifesto,
            max_calls=50,
        )
        result = await council.run_session()
        assert result.modules_completed == ["health"]
        assert result.modules_skipped == ["graduation", "manifesto"]
        graduation.run.assert_not_called()
        manifesto.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_remaining_budget_carries_to_next_module(self):
        """If health uses only 2 calls, graduation gets more budget."""
        health = AsyncMock()
        health.run = AsyncMock(return_value=([], 2, False, None))
        graduation = AsyncMock()
        graduation.run = AsyncMock(return_value=([], 5, False, None))
        manifesto = AsyncMock()
        manifesto.run = AsyncMock(return_value=([], 3, False, None))

        council = RemCouncil(
            health_scanner=health,
            graduation_auditor=graduation,
            manifesto_reviewer=manifesto,
            max_calls=50,
        )
        result = await council.run_session()
        # Graduation gets its share + leftover from health
        grad_budget = graduation.run.call_args[0][0]
        assert grad_budget >= 16  # At least its fair share


class TestEscalation:

    @pytest.mark.asyncio
    async def test_escalation_from_health(self):
        health = AsyncMock()
        health.run = AsyncMock(return_value=(["thr_crit"], 10, True, "thr_crit"))
        graduation = AsyncMock()
        graduation.run = AsyncMock(return_value=([], 3, False, None))
        manifesto = AsyncMock()
        manifesto.run = AsyncMock(return_value=([], 5, False, None))

        council = RemCouncil(
            health_scanner=health,
            graduation_auditor=graduation,
            manifesto_reviewer=manifesto,
            max_calls=50,
        )
        result = await council.run_session()
        assert result.should_escalate is True
        assert result.escalation_thread_id == "thr_crit"

    @pytest.mark.asyncio
    async def test_no_escalation_when_all_clean(self, council):
        result = await council.run_session()
        assert result.should_escalate is False
        assert result.escalation_thread_id is None
```

- [ ] **1.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_rem_council.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **1.3: Implement rem_council.py**

```python
# backend/hive/rem_council.py
"""REM Council runner — orchestrates review modules within a budget.

Runs three modules sequentially: Health → Graduation → Manifesto.
Each module receives a call budget and returns (thread_ids, calls_used,
should_escalate, escalation_thread_id). Budget carries forward —
unused calls from earlier modules are available to later ones.

Spec: docs/superpowers/specs/2026-04-02-hive-rem-council-design.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

# Module return type: (thread_ids, calls_used, should_escalate, escalation_thread_id)
ModuleResult = Tuple[List[str], int, bool, Optional[str]]


class ReviewModule(Protocol):
    """Protocol for REM review modules."""
    async def run(self, budget: int) -> ModuleResult: ...


@dataclass
class RemSessionResult:
    """Summary of a REM council session."""
    threads_created: List[str]
    calls_used: int
    calls_budget: int
    should_escalate: bool
    escalation_thread_id: Optional[str]
    modules_completed: List[str]
    modules_skipped: List[str]


class RemCouncil:
    """Orchestrates REM review modules within a call budget."""

    def __init__(
        self,
        health_scanner: Any,
        graduation_auditor: Any,
        manifesto_reviewer: Any,
        max_calls: int = 50,
    ) -> None:
        self._modules = [
            ("health", health_scanner),
            ("graduation", graduation_auditor),
            ("manifesto", manifesto_reviewer),
        ]
        self._max_calls = max_calls

    async def run_session(self) -> RemSessionResult:
        """Execute all modules sequentially within budget."""
        all_threads: List[str] = []
        total_used = 0
        should_escalate = False
        escalation_thread_id: Optional[str] = None
        completed: List[str] = []
        skipped: List[str] = []

        per_module = self._max_calls // len(self._modules)

        for name, module in self._modules:
            remaining = self._max_calls - total_used
            if remaining <= 0:
                skipped.append(name)
                continue

            # Give module its fair share + any leftover from previous modules
            budget = min(remaining, max(per_module, remaining))

            try:
                thread_ids, calls_used, mod_escalate, mod_escalation_id = await module.run(budget)
            except Exception:
                logger.exception("[RemCouncil] Module %s failed", name)
                completed.append(name)
                continue

            all_threads.extend(thread_ids)
            total_used += calls_used
            completed.append(name)

            if mod_escalate and not should_escalate:
                should_escalate = True
                escalation_thread_id = mod_escalation_id

            logger.info(
                "[RemCouncil] Module %s: %d threads, %d calls, escalate=%s",
                name, len(thread_ids), calls_used, mod_escalate,
            )

        return RemSessionResult(
            threads_created=all_threads,
            calls_used=total_used,
            calls_budget=self._max_calls,
            should_escalate=should_escalate,
            escalation_thread_id=escalation_thread_id,
            modules_completed=completed,
            modules_skipped=skipped,
        )
```

- [ ] **1.4: Run tests**

Run: `python3 -m pytest tests/test_hive_rem_council.py -v`
Expected: ALL PASS

- [ ] **1.5: Commit**

```bash
git add backend/hive/rem_council.py tests/test_hive_rem_council.py
git commit -m "feat(hive): add RemCouncil runner with budget management"
```

---

## Task 2: Health Scanner Module

**Files:**
- Create: `backend/hive/rem_health_scanner.py`
- Create: `tests/test_hive_rem_health.py`

- [ ] **2.1: Write failing tests**

```python
# tests/test_hive_rem_health.py
"""Tests for REM Health Scanner module."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from backend.hive.rem_health_scanner import HealthScanner
from backend.hive.thread_manager import ThreadManager
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.persona_engine import PersonaEngine
from backend.hive.thread_models import CognitiveState, ThreadState, PersonaIntent
import json


def _persona_response(reasoning, confidence=0.8, principle=None):
    d = {"reasoning": reasoning, "confidence": confidence}
    if principle:
        d["manifesto_principle"] = principle
    return json.dumps(d)


@pytest.fixture
def mock_engine():
    engine = MagicMock(spec=PersonaEngine)
    engine.generate_reasoning = AsyncMock(side_effect=[
        # JARVIS observe
        MagicMock(
            type="persona_reasoning", persona="jarvis", role="body",
            intent=PersonaIntent.OBSERVE, reasoning="RAM at 88%, trending up.",
            confidence=0.9, token_cost=200, message_id="msg_obs",
            manifesto_principle="$7 Observability", validate_verdict=None,
            to_dict=lambda: {"type": "persona_reasoning", "reasoning": "RAM at 88%"},
        ),
    ])
    return engine


@pytest.fixture
def thread_mgr(tmp_path):
    return ThreadManager(storage_dir=tmp_path / "threads")


@pytest.fixture
def relay():
    r = HudRelayAgent()
    r._ipc_send = AsyncMock()
    return r


@pytest.fixture
def scanner(mock_engine, thread_mgr, relay):
    return HealthScanner(
        persona_engine=mock_engine,
        thread_manager=thread_mgr,
        relay=relay,
    )


class TestHealthMetrics:

    @pytest.mark.asyncio
    async def test_collects_metrics(self, scanner):
        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=65.0)
            mock_ps.cpu_percent.return_value = 20.0
            mock_ps.disk_usage.return_value = MagicMock(percent=45.0)
            metrics = scanner._collect_metrics()
        assert metrics["ram_percent"] == 65.0
        assert metrics["cpu_percent"] == 20.0
        assert metrics["disk_percent"] == 45.0

    @pytest.mark.asyncio
    async def test_healthy_system_creates_summary_thread(self, scanner, thread_mgr):
        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_ps.cpu_percent.return_value = 15.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            thread_ids, calls, escalate, esc_id = await scanner.run(budget=15)
        assert len(thread_ids) == 1  # summary thread
        assert escalate is False

    @pytest.mark.asyncio
    async def test_degraded_system_creates_warning_thread(self, scanner, thread_mgr, mock_engine):
        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=88.0)
            mock_ps.cpu_percent.return_value = 15.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            thread_ids, calls, escalate, esc_id = await scanner.run(budget=15)
        assert len(thread_ids) >= 1
        t = thread_mgr.get_thread(thread_ids[0])
        assert any(m.severity == "warning" for m in t.messages if hasattr(m, "severity"))

    @pytest.mark.asyncio
    async def test_critical_system_escalates(self, scanner, mock_engine):
        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=96.0)
            mock_ps.cpu_percent.return_value = 95.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            thread_ids, calls, escalate, esc_id = await scanner.run(budget=15)
        assert escalate is True
        assert esc_id is not None

    @pytest.mark.asyncio
    async def test_respects_budget(self, scanner):
        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_ps.cpu_percent.return_value = 15.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            _, calls, _, _ = await scanner.run(budget=15)
        assert calls <= 15
```

- [ ] **2.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_rem_health.py -v`
Expected: FAIL

- [ ] **2.3: Implement rem_health_scanner.py**

```python
# backend/hive/rem_health_scanner.py
"""REM Health Scanner — Module 1.

Collects system metrics via psutil, creates Hive threads for degradation.
Uses JARVIS persona to observe/synthesize health narrative.

Severity mapping:
  - RAM/CPU < 70% = info (no thread)
  - RAM/CPU 70-90% = warning (thread created)
  - RAM/CPU > 90% = error (thread + escalation)
  - Disk > 85% = warning, > 95% = error
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)

logger = logging.getLogger(__name__)

_RAM_WARN = 70.0
_RAM_ERROR = 90.0
_CPU_WARN = 70.0
_CPU_ERROR = 90.0
_DISK_WARN = 85.0
_DISK_ERROR = 95.0


class HealthScanner:
    """Collects system metrics and creates health-related Hive threads."""

    def __init__(
        self,
        persona_engine: Any,
        thread_manager: Any,
        relay: Any,
    ) -> None:
        self._engine = persona_engine
        self._tm = thread_manager
        self._relay = relay

    async def run(self, budget: int) -> Tuple[List[str], int, bool, Optional[str]]:
        """Run health scan. Returns (thread_ids, calls_used, should_escalate, escalation_id)."""
        metrics = self._collect_metrics()
        findings = self._assess(metrics)
        calls_used = 0
        thread_ids: List[str] = []
        should_escalate = False
        escalation_id: Optional[str] = None

        if not findings:
            # All healthy — create summary thread
            thread = self._tm.create_thread(
                title="System Health: All Clear",
                trigger_event="rem_health_scanner:healthy",
                cognitive_state=CognitiveState.REM,
            )
            log = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="health_scanner",
                trinity_parent="jarvis",
                severity="info",
                category="system_health",
                payload=metrics,
            )
            self._tm.add_message(thread.thread_id, log)
            await self._relay.project_message(log)
            thread_ids.append(thread.thread_id)
            return (thread_ids, 0, False, None)

        for finding in findings:
            if calls_used >= budget:
                break

            thread = self._tm.create_thread(
                title=f"Health: {finding['metric']} at {finding['value']:.1f}%",
                trigger_event=f"rem_health_scanner:{finding['metric']}",
                cognitive_state=CognitiveState.REM,
            )
            log = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="health_scanner",
                trinity_parent="jarvis",
                severity=finding["severity"],
                category="system_health",
                payload={"metric": finding["metric"], "value": finding["value"], "threshold": finding["threshold"]},
            )
            self._tm.add_message(thread.thread_id, log)
            await self._relay.project_message(log)

            # JARVIS observes
            self._tm.transition(thread.thread_id, ThreadState.DEBATING)
            observe_msg = await self._engine.generate_reasoning(
                "jarvis", PersonaIntent.OBSERVE, thread,
            )
            self._tm.add_message(thread.thread_id, observe_msg)
            await self._relay.project_message(observe_msg)
            calls_used += 1

            thread_ids.append(thread.thread_id)

            if finding["severity"] == "error" and not should_escalate:
                should_escalate = True
                escalation_id = thread.thread_id

        return (thread_ids, calls_used, should_escalate, escalation_id)

    def _collect_metrics(self) -> dict:
        """Collect system metrics via psutil."""
        if psutil is None:
            return {"ram_percent": 0.0, "cpu_percent": 0.0, "disk_percent": 0.0}
        return {
            "ram_percent": psutil.virtual_memory().percent,
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "disk_percent": psutil.disk_usage("/").percent,
        }

    def _assess(self, metrics: dict) -> list:
        """Assess metrics against thresholds. Returns list of findings."""
        findings = []
        checks = [
            ("ram_percent", metrics.get("ram_percent", 0), _RAM_WARN, _RAM_ERROR),
            ("cpu_percent", metrics.get("cpu_percent", 0), _CPU_WARN, _CPU_ERROR),
            ("disk_percent", metrics.get("disk_percent", 0), _DISK_WARN, _DISK_ERROR),
        ]
        for metric, value, warn, error in checks:
            if value >= error:
                findings.append({"metric": metric, "value": value, "severity": "error", "threshold": error})
            elif value >= warn:
                findings.append({"metric": metric, "value": value, "severity": "warning", "threshold": warn})
        return findings
```

- [ ] **2.4: Run tests**

Run: `python3 -m pytest tests/test_hive_rem_health.py -v`
Expected: ALL PASS

- [ ] **2.5: Commit**

```bash
git add backend/hive/rem_health_scanner.py tests/test_hive_rem_health.py
git commit -m "feat(hive): add REM Health Scanner module"
```

---

## Task 3: Graduation Auditor Module

**Files:**
- Create: `backend/hive/rem_graduation_auditor.py`
- Create: `tests/test_hive_rem_graduation.py`

- [ ] **3.1: Write failing tests**

```python
# tests/test_hive_rem_graduation.py
"""Tests for REM Graduation Auditor — Ouroboros ledger scanning."""
import json
import time
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.hive.rem_graduation_auditor import GraduationAuditor
from backend.hive.thread_manager import ThreadManager
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.thread_models import CognitiveState, PersonaIntent


def _make_ledger_entry(op_id, state="completed", wall_time=None):
    return json.dumps({
        "op_id": op_id,
        "state": state,
        "wall_time": wall_time or time.time(),
        "data": {},
    })


@pytest.fixture
def ledger_dir(tmp_path):
    d = tmp_path / "ledger"
    d.mkdir()
    return d


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.generate_reasoning = AsyncMock(return_value=MagicMock(
        type="persona_reasoning", persona="jarvis", role="body",
        intent=PersonaIntent.OBSERVE, reasoning="3 tools ready for graduation.",
        confidence=0.85, token_cost=200, message_id="msg_grad",
        manifesto_principle="$6 Neuroplasticity", validate_verdict=None,
        to_dict=lambda: {"type": "persona_reasoning"},
    ))
    return engine


@pytest.fixture
def thread_mgr(tmp_path):
    return ThreadManager(storage_dir=tmp_path / "threads")


@pytest.fixture
def relay():
    r = HudRelayAgent()
    r._ipc_send = AsyncMock()
    return r


@pytest.fixture
def auditor(mock_engine, thread_mgr, relay, ledger_dir):
    return GraduationAuditor(
        persona_engine=mock_engine,
        thread_manager=thread_mgr,
        relay=relay,
        ledger_dir=ledger_dir,
    )


class TestLedgerScanning:

    def test_empty_ledger_no_candidates(self, auditor, ledger_dir):
        candidates, stale = auditor._scan_ledger()
        assert len(candidates) == 0
        assert len(stale) == 0

    def test_detects_graduation_candidate(self, auditor, ledger_dir):
        """Op with 3+ completed entries = graduation candidate."""
        for i in range(3):
            f = ledger_dir / f"op-test-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-test-{i}", "completed"))
        candidates, _ = auditor._scan_ledger()
        assert candidates["completed"] >= 3

    def test_detects_stale_ops(self, auditor, ledger_dir):
        """Ops with wall_time > 30 days ago = stale."""
        old_time = time.time() - (31 * 86400)
        f = ledger_dir / "op-old-0-jarvis.jsonl"
        f.write_text(_make_ledger_entry("op-old-0", "completed", wall_time=old_time))
        _, stale = auditor._scan_ledger()
        assert len(stale) >= 1


class TestGraduationRun:

    @pytest.mark.asyncio
    async def test_no_candidates_no_threads(self, auditor):
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert thread_ids == []
        assert calls == 0
        assert escalate is False

    @pytest.mark.asyncio
    async def test_candidates_create_thread(self, auditor, ledger_dir):
        for i in range(4):
            f = ledger_dir / f"op-grad-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-grad-{i}", "completed"))
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert len(thread_ids) >= 1
        assert calls >= 1

    @pytest.mark.asyncio
    async def test_strong_candidates_escalate(self, auditor, ledger_dir):
        """5+ completed ops = strong signal → escalation."""
        for i in range(6):
            f = ledger_dir / f"op-strong-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-strong-{i}", "completed"))
        thread_ids, calls, escalate, esc_id = await auditor.run(budget=15)
        assert escalate is True

    @pytest.mark.asyncio
    async def test_respects_budget(self, auditor, ledger_dir):
        for i in range(3):
            f = ledger_dir / f"op-budget-{i}-jarvis.jsonl"
            f.write_text(_make_ledger_entry(f"op-budget-{i}", "completed"))
        _, calls, _, _ = await auditor.run(budget=15)
        assert calls <= 15
```

- [ ] **3.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_rem_graduation.py -v`
Expected: FAIL

- [ ] **3.3: Implement rem_graduation_auditor.py**

```python
# backend/hive/rem_graduation_auditor.py
"""REM Graduation Auditor — Module 2.

Scans the Ouroboros ledger for ephemeral tools eligible for graduation
(count >= 3 completed ops) and stale tools (not used in 30+ days).

Ledger format: ~/.jarvis/ouroboros/ledger/op-{id}-{repo}.jsonl
Each line: {"op_id": str, "state": str, "wall_time": float, "data": dict}
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)

logger = logging.getLogger(__name__)

_GRADUATION_THRESHOLD = 3
_STRONG_SIGNAL_THRESHOLD = 5
_STALE_DAYS = int(os.environ.get("JARVIS_HIVE_TOOL_STALE_DAYS", "30"))


class GraduationAuditor:
    """Scans Ouroboros ledger for graduation candidates and stale tools."""

    def __init__(
        self,
        persona_engine: Any,
        thread_manager: Any,
        relay: Any,
        ledger_dir: Optional[Path] = None,
    ) -> None:
        self._engine = persona_engine
        self._tm = thread_manager
        self._relay = relay
        self._ledger_dir = ledger_dir or Path(
            os.environ.get("JARVIS_OUROBOROS_LEDGER_DIR",
                           str(Path.home() / ".jarvis" / "ouroboros" / "ledger"))
        )

    async def run(self, budget: int) -> Tuple[List[str], int, bool, Optional[str]]:
        """Run graduation audit. Returns (thread_ids, calls_used, should_escalate, escalation_id)."""
        status_counts, stale_ops = self._scan_ledger()
        thread_ids: List[str] = []
        calls_used = 0
        should_escalate = False
        escalation_id: Optional[str] = None

        completed_count = status_counts.get("completed", 0)

        # Check graduation candidates
        if completed_count >= _GRADUATION_THRESHOLD and calls_used < budget:
            thread = self._tm.create_thread(
                title=f"Graduation Candidates: {completed_count} completed ops",
                trigger_event="rem_graduation_auditor:candidates",
                cognitive_state=CognitiveState.REM,
            )
            log = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="graduation_auditor",
                trinity_parent="jarvis",
                severity="info",
                category="graduation",
                payload={"completed_count": completed_count, "threshold": _GRADUATION_THRESHOLD},
            )
            self._tm.add_message(thread.thread_id, log)
            await self._relay.project_message(log)

            self._tm.transition(thread.thread_id, ThreadState.DEBATING)
            observe_msg = await self._engine.generate_reasoning(
                "jarvis", PersonaIntent.OBSERVE, thread,
            )
            self._tm.add_message(thread.thread_id, observe_msg)
            await self._relay.project_message(observe_msg)
            calls_used += 1
            thread_ids.append(thread.thread_id)

            if completed_count >= _STRONG_SIGNAL_THRESHOLD:
                should_escalate = True
                escalation_id = thread.thread_id

        # Check stale tools
        if stale_ops and calls_used < budget:
            thread = self._tm.create_thread(
                title=f"Stale Tools: {len(stale_ops)} ops unused >{_STALE_DAYS}d",
                trigger_event="rem_graduation_auditor:stale",
                cognitive_state=CognitiveState.REM,
            )
            log = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="graduation_auditor",
                trinity_parent="jarvis",
                severity="info",
                category="stale_tools",
                payload={"stale_count": len(stale_ops), "threshold_days": _STALE_DAYS},
            )
            self._tm.add_message(thread.thread_id, log)
            await self._relay.project_message(log)

            self._tm.transition(thread.thread_id, ThreadState.DEBATING)
            observe_msg = await self._engine.generate_reasoning(
                "jarvis", PersonaIntent.OBSERVE, thread,
            )
            self._tm.add_message(thread.thread_id, observe_msg)
            await self._relay.project_message(observe_msg)
            calls_used += 1
            thread_ids.append(thread.thread_id)

        return (thread_ids, calls_used, should_escalate, escalation_id)

    def _scan_ledger(self) -> Tuple[Dict[str, int], List[str]]:
        """Scan ledger directory. Returns (status_counts, stale_op_ids)."""
        status_counts: Dict[str, int] = {}
        stale_ops: List[str] = []
        stale_cutoff = time.time() - (_STALE_DAYS * 86400)

        if not self._ledger_dir.exists():
            return (status_counts, stale_ops)

        for path in self._ledger_dir.glob("op-*.jsonl"):
            latest_wall_time = 0.0
            latest_state = "unknown"
            try:
                for line in path.read_text().strip().split("\n"):
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    state = entry.get("state", "unknown")
                    wt = entry.get("wall_time", 0.0)
                    if wt > latest_wall_time:
                        latest_wall_time = wt
                        latest_state = state
            except (json.JSONDecodeError, OSError):
                continue

            status_counts[latest_state] = status_counts.get(latest_state, 0) + 1
            if latest_wall_time > 0 and latest_wall_time < stale_cutoff:
                stale_ops.append(path.stem)

        return (status_counts, stale_ops)
```

- [ ] **3.4: Run tests**

Run: `python3 -m pytest tests/test_hive_rem_graduation.py -v`
Expected: ALL PASS

- [ ] **3.5: Commit**

```bash
git add backend/hive/rem_graduation_auditor.py tests/test_hive_rem_graduation.py
git commit -m "feat(hive): add REM Graduation Auditor module"
```

---

## Task 4: Manifesto Compliance Reviewer Module

**Files:**
- Create: `backend/hive/rem_manifesto_reviewer.py`
- Create: `tests/test_hive_rem_manifesto.py`

- [ ] **4.1: Write failing tests**

```python
# tests/test_hive_rem_manifesto.py
"""Tests for REM Manifesto Compliance Reviewer — git diff + Manifesto check."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.hive.rem_manifesto_reviewer import ManifestoReviewer
from backend.hive.thread_manager import ThreadManager
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.thread_models import CognitiveState, PersonaIntent


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.generate_reasoning = AsyncMock(return_value=MagicMock(
        type="persona_reasoning", persona="jarvis", role="body",
        intent=PersonaIntent.OBSERVE, reasoning="File follows Boundary Principle.",
        confidence=0.9, token_cost=200, message_id="msg_mani",
        manifesto_principle=None, validate_verdict=None,
        to_dict=lambda: {"type": "persona_reasoning"},
    ))
    return engine


@pytest.fixture
def thread_mgr(tmp_path):
    return ThreadManager(storage_dir=tmp_path / "threads")


@pytest.fixture
def relay():
    r = HudRelayAgent()
    r._ipc_send = AsyncMock()
    return r


@pytest.fixture
def reviewer(mock_engine, thread_mgr, relay, tmp_path):
    state_dir = tmp_path / "hive"
    state_dir.mkdir()
    return ManifestoReviewer(
        persona_engine=mock_engine,
        thread_manager=thread_mgr,
        relay=relay,
        repo_root=tmp_path / "repo",
        state_dir=state_dir,
    )


class TestGitDiffCollection:

    def test_get_changed_files_parses_git_output(self, reviewer):
        git_output = "backend/hive/foo.py\nbackend/hive/bar.py\n"
        files = reviewer._parse_changed_files(git_output)
        assert files == ["backend/hive/foo.py", "backend/hive/bar.py"]

    def test_filters_secret_paths(self, reviewer):
        files = ["backend/foo.py", ".env", "config/credentials.json", "key.pem"]
        filtered = reviewer._filter_secret_paths(files)
        assert filtered == ["backend/foo.py"]

    def test_caps_at_max_files(self, reviewer):
        files = [f"file_{i}.py" for i in range(20)]
        capped = reviewer._cap_files(files, max_files=10)
        assert len(capped) == 10


class TestManifestoRun:

    @pytest.mark.asyncio
    async def test_no_changes_no_threads(self, reviewer):
        with patch.object(reviewer, "_get_changed_files", return_value=[]):
            thread_ids, calls, escalate, esc_id = await reviewer.run(budget=15)
        assert thread_ids == []
        assert calls == 0
        assert escalate is False

    @pytest.mark.asyncio
    async def test_changed_files_create_threads(self, reviewer, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        (repo / "backend").mkdir(exist_ok=True)
        (repo / "backend" / "foo.py").write_text("def hello(): pass\n" * 10)

        with patch.object(reviewer, "_get_changed_files", return_value=["backend/foo.py"]):
            thread_ids, calls, escalate, esc_id = await reviewer.run(budget=15)
        assert len(thread_ids) >= 1
        assert calls >= 1

    @pytest.mark.asyncio
    async def test_respects_budget(self, reviewer, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        for i in range(5):
            d = repo / "backend"
            d.mkdir(exist_ok=True)
            (d / f"file_{i}.py").write_text("x = 1\n" * 10)

        with patch.object(reviewer, "_get_changed_files",
                          return_value=[f"backend/file_{i}.py" for i in range(5)]):
            _, calls, _, _ = await reviewer.run(budget=3)
        assert calls <= 3

    @pytest.mark.asyncio
    async def test_skips_binary_and_secret_files(self, reviewer):
        with patch.object(reviewer, "_get_changed_files",
                          return_value=[".env", "image.png", "credentials.json", "ok.py"]):
            with patch.object(reviewer, "_read_file", return_value="x = 1"):
                thread_ids, _, _, _ = await reviewer.run(budget=15)
        # Should only process ok.py
        assert len(thread_ids) <= 1

    @pytest.mark.asyncio
    async def test_saves_last_rem_timestamp(self, reviewer, tmp_path):
        state_dir = tmp_path / "hive"
        with patch.object(reviewer, "_get_changed_files", return_value=[]):
            await reviewer.run(budget=15)
        assert (state_dir / "last_rem_at").exists()
```

- [ ] **4.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_rem_manifesto.py -v`
Expected: FAIL

- [ ] **4.3: Implement rem_manifesto_reviewer.py**

```python
# backend/hive/rem_manifesto_reviewer.py
"""REM Manifesto Compliance Reviewer — Module 3.

Reviews recent git changes against the Symbiotic AI-Native Manifesto.
Uses git log/diff to find changed files, reads first 200 lines of each,
and has JARVIS assess compliance via the 35B model.

Caps: max 10 files, max 200 lines per file.
Secret denylist: .env, *credentials*, *secret*, *.key, *.pem
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)

logger = logging.getLogger(__name__)

_MAX_FILES = int(os.environ.get("JARVIS_HIVE_REM_MAX_FILES", "10"))
_MAX_LINES = int(os.environ.get("JARVIS_HIVE_REM_MAX_LINES_PER_FILE", "200"))

_SECRET_PATTERNS = [
    r"\.env$", r"credentials", r"secret", r"\.key$", r"\.pem$",
    r"\.p12$", r"\.pfx$", r"\.ssh",
]
_BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".zip", ".tar", ".gz", ".bin", ".exe", ".dll", ".so", ".dylib", ".pyc"}


class ManifestoReviewer:
    """Reviews recent code changes against the Manifesto."""

    def __init__(
        self,
        persona_engine: Any,
        thread_manager: Any,
        relay: Any,
        repo_root: Optional[Path] = None,
        state_dir: Optional[Path] = None,
    ) -> None:
        self._engine = persona_engine
        self._tm = thread_manager
        self._relay = relay
        self._repo_root = repo_root or Path(".")
        self._state_dir = state_dir or Path(
            os.environ.get("JARVIS_HIVE_STATE_DIR", str(Path.home() / ".jarvis" / "hive"))
        )

    async def run(self, budget: int) -> Tuple[List[str], int, bool, Optional[str]]:
        """Run Manifesto compliance review. Returns (thread_ids, calls_used, escalate, esc_id)."""
        changed_files = self._get_changed_files()
        changed_files = self._filter_secret_paths(changed_files)
        changed_files = [f for f in changed_files if not self._is_binary(f)]
        changed_files = self._cap_files(changed_files, _MAX_FILES)

        self._save_last_rem_timestamp()

        if not changed_files:
            return ([], 0, False, None)

        thread_ids: List[str] = []
        calls_used = 0
        should_escalate = False
        escalation_id: Optional[str] = None

        for filepath in changed_files:
            if calls_used >= budget:
                break

            content = self._read_file(filepath)
            if not content:
                continue

            thread = self._tm.create_thread(
                title=f"Manifesto Review: {filepath}",
                trigger_event=f"rem_manifesto_reviewer:{filepath}",
                cognitive_state=CognitiveState.REM,
            )
            log = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="manifesto_reviewer",
                trinity_parent="jarvis",
                severity="info",
                category="manifesto_compliance",
                payload={"file": filepath, "lines": len(content.split("\n"))},
            )
            self._tm.add_message(thread.thread_id, log)
            await self._relay.project_message(log)

            self._tm.transition(thread.thread_id, ThreadState.DEBATING)
            observe_msg = await self._engine.generate_reasoning(
                "jarvis", PersonaIntent.OBSERVE, thread,
            )
            self._tm.add_message(thread.thread_id, observe_msg)
            await self._relay.project_message(observe_msg)
            calls_used += 1
            thread_ids.append(thread.thread_id)

        return (thread_ids, calls_used, should_escalate, escalation_id)

    def _get_changed_files(self) -> List[str]:
        """Get files changed since last REM via git."""
        last_rem = self._load_last_rem_timestamp()
        try:
            if last_rem:
                cmd = f"git -C {self._repo_root} log --since='{last_rem}' --name-only --pretty=format:"
            else:
                cmd = f"git -C {self._repo_root} log -20 --name-only --pretty=format:"

            proc = asyncio.get_event_loop()
            # Use subprocess synchronously (we're in a sync helper)
            import subprocess
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=10,
            )
            return self._parse_changed_files(result.stdout)
        except Exception:
            logger.debug("[ManifestoReviewer] git command failed", exc_info=True)
            return []

    def _parse_changed_files(self, git_output: str) -> List[str]:
        """Parse git log --name-only output into unique file list."""
        files = set()
        for line in git_output.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("commit ") and "/" in line or "." in line:
                files.add(line)
        return sorted(files)

    def _filter_secret_paths(self, files: List[str]) -> List[str]:
        """Remove files matching secret denylist patterns."""
        result = []
        for f in files:
            if any(re.search(pat, f, re.IGNORECASE) for pat in _SECRET_PATTERNS):
                continue
            result.append(f)
        return result

    def _is_binary(self, filepath: str) -> bool:
        """Check if file has a binary extension."""
        return Path(filepath).suffix.lower() in _BINARY_EXTENSIONS

    def _cap_files(self, files: List[str], max_files: int) -> List[str]:
        """Cap file list to max_files."""
        return files[:max_files]

    def _read_file(self, filepath: str) -> str:
        """Read first _MAX_LINES lines of a file."""
        full_path = self._repo_root / filepath
        if not full_path.exists():
            return ""
        try:
            lines = full_path.read_text(errors="replace").split("\n")[:_MAX_LINES]
            return "\n".join(lines)
        except Exception:
            return ""

    def _save_last_rem_timestamp(self) -> None:
        """Save current timestamp as last REM run time."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        ts_file = self._state_dir / "last_rem_at"
        ts_file.write_text(datetime.now(tz=timezone.utc).isoformat())

    def _load_last_rem_timestamp(self) -> Optional[str]:
        """Load last REM timestamp, or None if never run."""
        ts_file = self._state_dir / "last_rem_at"
        if ts_file.exists():
            return ts_file.read_text().strip()
        return None
```

- [ ] **4.4: Run tests**

Run: `python3 -m pytest tests/test_hive_rem_manifesto.py -v`
Expected: ALL PASS

- [ ] **4.5: Commit**

```bash
git add backend/hive/rem_manifesto_reviewer.py tests/test_hive_rem_manifesto.py
git commit -m "feat(hive): add REM Manifesto Compliance Reviewer module"
```

---

## Task 5: Wire RemCouncil into HiveService

**Files:**
- Modify: `backend/hive/hive_service.py`
- Create: `tests/test_hive_rem_integration.py`

- [ ] **5.1: Write failing integration test**

```python
# tests/test_hive_rem_integration.py
"""Integration test: REM poll triggers council, council runs, FSM transitions."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.hive.hive_service import HiveService
from backend.hive.thread_models import CognitiveState, ThreadState
from backend.hive.cognitive_fsm import CognitiveEvent
from backend.neural_mesh.data_models import MessageType


def _response(reasoning, confidence=0.8, principle=None):
    d = {"reasoning": reasoning, "confidence": confidence}
    if principle:
        d["manifesto_principle"] = principle
    return json.dumps(d)


class TestRemIntegration:

    @pytest.mark.asyncio
    async def test_rem_council_runs_and_returns_to_baseline(self, tmp_path):
        """FSM enters REM → council runs → completes → BASELINE."""
        dw = AsyncMock()
        dw.is_available = True
        dw.prompt_only = AsyncMock(return_value=_response("All systems healthy.", 0.9))
        bus = AsyncMock()
        bus.subscribe_broadcast = AsyncMock()

        service = HiveService(bus=bus, governed_loop=None, doubleword=dw, state_dir=tmp_path)
        await service.start()

        # Manually trigger REM
        service._fsm.decide(CognitiveEvent.REM_TRIGGER, idle_seconds=25000, system_load_pct=10.0)
        service._fsm.apply_last_decision()
        assert service._fsm.state == CognitiveState.REM

        # Run council
        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_ps.cpu_percent.return_value = 15.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            await service._run_rem_council()

        # FSM should return to BASELINE
        assert service._fsm.state == CognitiveState.BASELINE

        await service.stop()

    @pytest.mark.asyncio
    async def test_rem_council_escalates_to_flow(self, tmp_path):
        """Critical health finding → council escalates → FLOW."""
        dw = AsyncMock()
        dw.is_available = True
        dw.prompt_only = AsyncMock(side_effect=[
            _response("RAM critical at 96%!", 0.95),  # health observe
            _response("Propose memory cleanup.", 0.87),  # FLOW debate: observe
            _response("Kill stale processes.", 0.85),    # FLOW debate: propose
            _response("Approved.", 0.9, principle="$3"),  # FLOW debate: validate
        ])
        bus = AsyncMock()
        bus.subscribe_broadcast = AsyncMock()
        gl = AsyncMock()
        gl.submit = AsyncMock(return_value=MagicMock())

        service = HiveService(bus=bus, governed_loop=gl, doubleword=dw, state_dir=tmp_path)
        await service.start()

        service._fsm.decide(CognitiveEvent.REM_TRIGGER, idle_seconds=25000, system_load_pct=10.0)
        service._fsm.apply_last_decision()

        with patch("backend.hive.rem_health_scanner.psutil") as mock_ps:
            mock_ps.virtual_memory.return_value = MagicMock(percent=96.0)
            mock_ps.cpu_percent.return_value = 95.0
            mock_ps.disk_usage.return_value = MagicMock(percent=30.0)
            await service._run_rem_council()

        # Should have escalated to FLOW
        # After FLOW debate completes and all threads resolve, should be back at BASELINE
        # (the exact state depends on whether the debate task completed)
        assert service._fsm.state in (CognitiveState.FLOW, CognitiveState.BASELINE)

        await service.stop()
```

- [ ] **5.2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_hive_rem_integration.py -v`
Expected: FAIL (no `_run_rem_council` method yet)

- [ ] **5.3: Wire RemCouncil into HiveService**

Add to `backend/hive/hive_service.py`:

1. Import the new modules at the top:
```python
from backend.hive.rem_council import RemCouncil
from backend.hive.rem_health_scanner import HealthScanner
from backend.hive.rem_graduation_auditor import GraduationAuditor
from backend.hive.rem_manifesto_reviewer import ManifestoReviewer
```

2. Add `_run_rem_council` method:
```python
    async def _run_rem_council(self) -> None:
        """Run the REM Council session with all three review modules."""
        health = HealthScanner(self._persona_engine, self._tm, self._relay)
        graduation = GraduationAuditor(self._persona_engine, self._tm, self._relay)
        manifesto = ManifestoReviewer(
            self._persona_engine, self._tm, self._relay,
            repo_root=Path("."),
            state_dir=self._state_dir,
        )
        council = RemCouncil(
            health_scanner=health,
            graduation_auditor=graduation,
            manifesto_reviewer=manifesto,
            max_calls=int(os.environ.get("JARVIS_HIVE_REM_MAX_CALLS", "50")),
        )
        result = await council.run_session()
        logger.info(
            "[HiveService] REM council complete: %d threads, %d/%d calls, escalate=%s",
            len(result.threads_created), result.calls_used, result.calls_budget, result.should_escalate,
        )

        if result.should_escalate and result.escalation_thread_id:
            decision = self._fsm.decide(CognitiveEvent.COUNCIL_ESCALATION)
            if not decision.noop:
                self._fsm.apply_last_decision()
                await self._relay.project_cognitive_transition(
                    from_state=decision.from_state.value,
                    to_state=decision.to_state.value,
                    reason_code=decision.reason_code,
                )
                self._flow_thread_ids.add(result.escalation_thread_id)
                asyncio.create_task(self._run_debate_round(result.escalation_thread_id))
        else:
            decision = self._fsm.decide(CognitiveEvent.COUNCIL_COMPLETE)
            if not decision.noop:
                self._fsm.apply_last_decision()
                await self._relay.project_cognitive_transition(
                    from_state=decision.from_state.value,
                    to_state=decision.to_state.value,
                    reason_code=decision.reason_code,
                )
```

3. Update `_rem_poll_loop` to call `_run_rem_council` after entering REM:
```python
    # After the existing FSM transition block (around line 425):
                logger.info("REM cycle triggered after %.0fs idle", idle_seconds)
                await self._run_rem_council()
```

- [ ] **5.4: Run tests**

Run: `python3 -m pytest tests/test_hive_rem_integration.py -v`
Expected: ALL PASS

- [ ] **5.5: Run full Hive test suite**

Run: `python3 -m pytest tests/test_hive_*.py -v`
Expected: ALL PASS

- [ ] **5.6: Commit**

```bash
git add backend/hive/hive_service.py tests/test_hive_rem_integration.py
git commit -m "feat(hive): wire RemCouncil into HiveService REM poll loop"
```

---

## Summary

| Task | Component | Dependencies |
|------|-----------|-------------|
| 1 | RemCouncil runner + RemSessionResult | None |
| 2 | Health Scanner module | None |
| 3 | Graduation Auditor module | None |
| 4 | Manifesto Compliance Reviewer module | None |
| 5 | Wire into HiveService + integration test | Tasks 1-4 |

Tasks 1-4 are independent and can be parallelized. Task 5 depends on all of them.
