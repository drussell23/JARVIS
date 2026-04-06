# Ouroboros Battle Test Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone headless daemon that boots the full Ouroboros governance brain and autonomously improves the JARVIS codebase, proving the organism works.

**Architecture:** A `BattleTestHarness` boots GovernanceStack, GovernedLoopService, IntakeLayerService, TheOracle, and all JARVIS-level tiers. It creates an accumulation git branch, lets sensors find work, auto-applies SAFE_AUTO operations, tracks RSI convergence data, and stops on budget/idle/SIGINT. On shutdown it prints a terminal summary and generates a Jupyter notebook.

**Tech Stack:** Python 3.9+, asyncio, argparse, subprocess (git), nbformat (notebook generation), matplotlib/seaborn (optional for notebook), existing Ouroboros governance stack

**Spec:** `docs/superpowers/specs/2026-04-06-ouroboros-battle-test-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|---|---|
| `scripts/ouroboros_battle_test.py` | CLI entry point: argparse, signal handling, asyncio.run |
| `backend/core/ouroboros/battle_test/harness.py` | BattleTestHarness: boot, run, shutdown orchestration |
| `backend/core/ouroboros/battle_test/cost_tracker.py` | CostTracker: session budget monitoring with asyncio.Event |
| `backend/core/ouroboros/battle_test/branch_manager.py` | BranchManager: accumulation branch git operations |
| `backend/core/ouroboros/battle_test/idle_watchdog.py` | IdleWatchdog: fires event after N seconds of no activity |
| `backend/core/ouroboros/battle_test/session_recorder.py` | SessionRecorder: collects stats, prints summary, writes JSON |
| `backend/core/ouroboros/battle_test/notebook_generator.py` | NotebookGenerator: creates .ipynb or Markdown fallback |
| `backend/core/ouroboros/battle_test/__init__.py` | Package init |
| `tests/test_ouroboros_governance/test_battle_test_cost_tracker.py` | Tests for CostTracker |
| `tests/test_ouroboros_governance/test_battle_test_branch_manager.py` | Tests for BranchManager |
| `tests/test_ouroboros_governance/test_battle_test_idle_watchdog.py` | Tests for IdleWatchdog |
| `tests/test_ouroboros_governance/test_battle_test_session_recorder.py` | Tests for SessionRecorder |
| `tests/test_ouroboros_governance/test_battle_test_notebook_generator.py` | Tests for NotebookGenerator |
| `tests/test_ouroboros_governance/test_battle_test_harness.py` | Tests for BattleTestHarness |

### Modified Files

None expected. If discovery reveals GovernedLoopService needs a pre-dequeue budget hook, one small addition to `governed_loop_service.py` is allowed per spec.

---

## Task 1: CostTracker

**Files:**
- Create: `backend/core/ouroboros/battle_test/__init__.py`
- Create: `backend/core/ouroboros/battle_test/cost_tracker.py`
- Test: `tests/test_ouroboros_governance/test_battle_test_cost_tracker.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for CostTracker — session budget enforcement."""
from __future__ import annotations

import asyncio
import json
import pytest


@pytest.mark.asyncio
async def test_initial_state():
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    tracker = CostTracker(budget_usd=0.50)
    assert tracker.total_spent == 0.0
    assert not tracker.exhausted
    assert tracker.remaining == 0.50


@pytest.mark.asyncio
async def test_record_cost():
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    tracker = CostTracker(budget_usd=0.50)
    tracker.record(provider="doubleword_397b", cost_usd=0.10)
    assert tracker.total_spent == pytest.approx(0.10)
    assert tracker.remaining == pytest.approx(0.40)
    assert not tracker.exhausted


@pytest.mark.asyncio
async def test_budget_exhausted_fires_event():
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    tracker = CostTracker(budget_usd=0.05)
    tracker.record(provider="doubleword_397b", cost_usd=0.03)
    assert not tracker.exhausted
    tracker.record(provider="doubleword_397b", cost_usd=0.03)
    assert tracker.exhausted
    assert tracker.budget_event.is_set()


@pytest.mark.asyncio
async def test_breakdown_by_provider():
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    tracker = CostTracker(budget_usd=1.00)
    tracker.record(provider="doubleword_397b", cost_usd=0.10)
    tracker.record(provider="doubleword_35b", cost_usd=0.02)
    tracker.record(provider="claude_sonnet", cost_usd=0.15)
    breakdown = tracker.breakdown
    assert breakdown["doubleword_397b"] == pytest.approx(0.10)
    assert breakdown["doubleword_35b"] == pytest.approx(0.02)
    assert breakdown["claude_sonnet"] == pytest.approx(0.15)
    assert tracker.total_spent == pytest.approx(0.27)


@pytest.mark.asyncio
async def test_persistence_roundtrip(tmp_path):
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    path = tmp_path / "cost_state.json"
    t1 = CostTracker(budget_usd=0.50, persist_path=path)
    t1.record(provider="doubleword_397b", cost_usd=0.20)
    t1.save()

    t2 = CostTracker(budget_usd=0.50, persist_path=path)
    assert t2.total_spent == pytest.approx(0.20)
    assert t2.remaining == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_zero_cost_ignored():
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    tracker = CostTracker(budget_usd=0.50)
    tracker.record(provider="doubleword_397b", cost_usd=0.0)
    tracker.record(provider="doubleword_397b", cost_usd=-1.0)
    assert tracker.total_spent == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_cost_tracker.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

Create `backend/core/ouroboros/battle_test/__init__.py`:
```python
"""Ouroboros Battle Test — standalone headless governance runner."""
```

Create `backend/core/ouroboros/battle_test/cost_tracker.py`:
```python
"""CostTracker — per-session budget enforcement for battle test runs.

Monitors cumulative API spend and fires an asyncio.Event when the session
budget is exhausted. Budget is per-session (not calendar day) to avoid
timezone ambiguity.

Persists state to JSON so crash-and-restart within the same session
resumes the existing budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CostTracker:
    """Tracks cumulative API spend against a session budget."""

    def __init__(
        self,
        budget_usd: float = 0.50,
        persist_path: Optional[Path] = None,
    ) -> None:
        self._budget = budget_usd
        self._persist_path = persist_path
        self._by_provider: Dict[str, float] = defaultdict(float)
        self._total: float = 0.0
        self.budget_event = asyncio.Event()
        self._load()

    @property
    def total_spent(self) -> float:
        return self._total

    @property
    def remaining(self) -> float:
        return max(0.0, self._budget - self._total)

    @property
    def exhausted(self) -> bool:
        return self._total >= self._budget

    @property
    def breakdown(self) -> Dict[str, float]:
        return dict(self._by_provider)

    def record(self, provider: str, cost_usd: float) -> None:
        """Record a cost. Fires budget_event if budget exhausted."""
        if cost_usd <= 0.0:
            return
        self._by_provider[provider] += cost_usd
        self._total += cost_usd
        if self._total >= self._budget and not self.budget_event.is_set():
            self.budget_event.set()
            logger.info(
                "[CostTracker] Budget exhausted: $%.4f / $%.2f",
                self._total, self._budget,
            )

    def save(self) -> None:
        """Persist state to disk."""
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "budget_usd": self._budget,
                "total_spent": self._total,
                "by_provider": dict(self._by_provider),
            }
            self._persist_path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.debug("CostTracker: save failed", exc_info=True)

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            self._total = data.get("total_spent", 0.0)
            for k, v in data.get("by_provider", {}).items():
                self._by_provider[k] = v
            if self._total >= self._budget:
                self.budget_event.set()
        except Exception:
            logger.debug("CostTracker: load failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_cost_tracker.py -v --timeout=15 -x`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/battle_test/__init__.py backend/core/ouroboros/battle_test/cost_tracker.py tests/test_ouroboros_governance/test_battle_test_cost_tracker.py
git commit -m "feat(battle-test): add CostTracker with session budget enforcement"
```

---

## Task 2: BranchManager

**Files:**
- Create: `backend/core/ouroboros/battle_test/branch_manager.py`
- Test: `tests/test_ouroboros_governance/test_battle_test_branch_manager.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for BranchManager — accumulation branch git operations."""
from __future__ import annotations

import os
import subprocess
import pytest
from pathlib import Path


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo for testing."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


def test_create_branch(tmp_path):
    from backend.core.ouroboros.battle_test.branch_manager import BranchManager
    _init_git_repo(tmp_path)
    mgr = BranchManager(repo_path=tmp_path, branch_prefix="ouroboros/test")
    branch_name = mgr.create_branch()
    assert branch_name.startswith("ouroboros/test-")
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.stdout.strip() == branch_name


def test_commit_changes(tmp_path):
    from backend.core.ouroboros.battle_test.branch_manager import BranchManager
    _init_git_repo(tmp_path)
    mgr = BranchManager(repo_path=tmp_path, branch_prefix="ouroboros/test")
    mgr.create_branch()
    (tmp_path / "new_file.py").write_text("print('hello')\n")
    sha = mgr.commit_operation(
        files=["new_file.py"],
        sensor="OpportunityMinerSensor",
        description="Add greeting",
        op_id="op-001",
        risk_tier="SAFE_AUTO",
        composite_score=0.35,
        technique="module_mutation",
    )
    assert len(sha) >= 7
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "OpportunityMinerSensor" in log.stdout


def test_diff_stats(tmp_path):
    from backend.core.ouroboros.battle_test.branch_manager import BranchManager
    _init_git_repo(tmp_path)
    mgr = BranchManager(repo_path=tmp_path, branch_prefix="ouroboros/test")
    mgr.create_branch()
    (tmp_path / "file.py").write_text("x = 1\n")
    mgr.commit_operation(
        files=["file.py"], sensor="test", description="add",
        op_id="op-001", risk_tier="SAFE_AUTO", composite_score=0.3, technique="test",
    )
    stats = mgr.get_diff_stats()
    assert stats["commits"] >= 1
    assert stats["files_changed"] >= 1
    assert stats["insertions"] >= 1


def test_dirty_repo_aborts(tmp_path):
    from backend.core.ouroboros.battle_test.branch_manager import BranchManager
    _init_git_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted")
    subprocess.run(["git", "add", "dirty.txt"], cwd=tmp_path, capture_output=True)
    mgr = BranchManager(repo_path=tmp_path, branch_prefix="ouroboros/test")
    with pytest.raises(RuntimeError, match="clean"):
        mgr.create_branch()


def test_branch_name_uniqueness(tmp_path):
    from backend.core.ouroboros.battle_test.branch_manager import BranchManager
    _init_git_repo(tmp_path)
    mgr1 = BranchManager(repo_path=tmp_path, branch_prefix="ouroboros/test")
    name1 = mgr1.create_branch()
    # Switch back to main to create another branch
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, capture_output=True)
    mgr2 = BranchManager(repo_path=tmp_path, branch_prefix="ouroboros/test")
    name2 = mgr2.create_branch()
    assert name1 != name2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_branch_manager.py -v --timeout=30 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""BranchManager — accumulation branch lifecycle for battle test runs.

Creates a timestamped branch, commits auto-applied operations with
structured messages, and provides diff stats for the terminal summary.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class BranchManager:
    """Manages the accumulation branch for a battle test session."""

    def __init__(
        self,
        repo_path: Path,
        branch_prefix: str = "ouroboros/battle-test",
    ) -> None:
        self._repo = repo_path
        self._prefix = branch_prefix
        self._branch: Optional[str] = None
        self._base_branch: Optional[str] = None
        self._commit_count = 0

    @property
    def branch_name(self) -> Optional[str]:
        return self._branch

    @property
    def commit_count(self) -> int:
        return self._commit_count

    def create_branch(self) -> str:
        """Create accumulation branch. Repo must be clean."""
        # Check clean working tree
        status = self._git("status", "--porcelain").strip()
        if status:
            raise RuntimeError(
                "Working tree is not clean. Commit or stash changes before running the battle test."
            )
        self._base_branch = self._git("branch", "--show-current").strip()
        timestamp = time.strftime("%Y-%m-%d-%H%M%S")
        self._branch = f"{self._prefix}-{timestamp}"
        # Ensure unique
        existing = self._git("branch", "--list", self._branch).strip()
        if existing:
            self._branch = f"{self._branch}-{int(time.time()) % 10000}"
        self._git("checkout", "-b", self._branch)
        logger.info("[BranchManager] Created branch: %s", self._branch)
        return self._branch

    def commit_operation(
        self,
        files: List[str],
        sensor: str,
        description: str,
        op_id: str,
        risk_tier: str,
        composite_score: float,
        technique: str,
    ) -> str:
        """Commit auto-applied files with structured message. Returns SHA."""
        for f in files:
            self._git("add", f)
        msg = (
            f"ouroboros({sensor}): {description}\n\n"
            f"Operation: {op_id}\n"
            f"Risk: {risk_tier}\n"
            f"Composite Score: {composite_score:.4f}\n"
            f"Technique: {technique}\n"
            f"Auto-applied: true"
        )
        self._git("commit", "-m", msg, "--allow-empty")
        sha = self._git("rev-parse", "--short", "HEAD").strip()
        self._commit_count += 1
        return sha

    def get_diff_stats(self) -> Dict[str, int]:
        """Get diff stats between base branch and current HEAD."""
        if not self._base_branch or not self._branch:
            return {"commits": 0, "files_changed": 0, "insertions": 0, "deletions": 0}
        try:
            stat = self._git("diff", "--stat", f"{self._base_branch}..{self._branch}")
            lines = stat.strip().split("\n")
            # Parse last line: " N files changed, M insertions(+), K deletions(-)"
            commits = int(self._git(
                "rev-list", "--count", f"{self._base_branch}..{self._branch}"
            ).strip())
            files_changed = 0
            insertions = 0
            deletions = 0
            if lines and "changed" in lines[-1]:
                import re
                m_files = re.search(r"(\d+) files? changed", lines[-1])
                m_ins = re.search(r"(\d+) insertions?", lines[-1])
                m_del = re.search(r"(\d+) deletions?", lines[-1])
                files_changed = int(m_files.group(1)) if m_files else 0
                insertions = int(m_ins.group(1)) if m_ins else 0
                deletions = int(m_del.group(1)) if m_del else 0
            return {
                "commits": commits,
                "files_changed": files_changed,
                "insertions": insertions,
                "deletions": deletions,
            }
        except Exception:
            return {"commits": self._commit_count, "files_changed": 0, "insertions": 0, "deletions": 0}

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self._repo,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and "checkout" not in args[0:1]:
            logger.debug("git %s failed: %s", " ".join(args), result.stderr)
        return result.stdout
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_branch_manager.py -v --timeout=30 -x`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/battle_test/branch_manager.py tests/test_ouroboros_governance/test_battle_test_branch_manager.py
git commit -m "feat(battle-test): add BranchManager with accumulation branch lifecycle"
```

---

## Task 3: IdleWatchdog

**Files:**
- Create: `backend/core/ouroboros/battle_test/idle_watchdog.py`
- Test: `tests/test_ouroboros_governance/test_battle_test_idle_watchdog.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for IdleWatchdog — idle timeout detection."""
from __future__ import annotations

import asyncio
import pytest


@pytest.mark.asyncio
async def test_fires_after_timeout():
    from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
    watchdog = IdleWatchdog(timeout_s=0.2)
    await watchdog.start()
    # Don't poke — let it time out
    await asyncio.sleep(0.4)
    assert watchdog.idle_event.is_set()
    watchdog.stop()


@pytest.mark.asyncio
async def test_poke_resets_timer():
    from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
    watchdog = IdleWatchdog(timeout_s=0.3)
    await watchdog.start()
    await asyncio.sleep(0.15)
    watchdog.poke()  # Reset timer
    await asyncio.sleep(0.15)
    assert not watchdog.idle_event.is_set()  # Should not have fired yet
    await asyncio.sleep(0.25)
    assert watchdog.idle_event.is_set()  # Now it should fire
    watchdog.stop()


@pytest.mark.asyncio
async def test_stop_cancels():
    from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
    watchdog = IdleWatchdog(timeout_s=0.2)
    await watchdog.start()
    watchdog.stop()
    await asyncio.sleep(0.4)
    assert not watchdog.idle_event.is_set()


@pytest.mark.asyncio
async def test_poke_count():
    from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
    watchdog = IdleWatchdog(timeout_s=1.0)
    await watchdog.start()
    watchdog.poke()
    watchdog.poke()
    watchdog.poke()
    assert watchdog.poke_count == 3
    watchdog.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_idle_watchdog.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""IdleWatchdog — fires an event after N seconds of no activity.

The battle test harness pokes the watchdog on every operation completion.
If no poke arrives within timeout_s, the idle_event fires and the harness
begins shutdown.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class IdleWatchdog:
    """Fires idle_event if no poke() within timeout_s seconds."""

    def __init__(self, timeout_s: float = 600.0) -> None:
        self._timeout = timeout_s
        self._last_poke = time.monotonic()
        self._poke_count = 0
        self._task: asyncio.Task | None = None
        self.idle_event = asyncio.Event()

    @property
    def poke_count(self) -> int:
        return self._poke_count

    def poke(self) -> None:
        """Reset the idle timer. Call on every operation completion."""
        self._last_poke = time.monotonic()
        self._poke_count += 1

    async def start(self) -> None:
        """Start the watchdog background task."""
        self._last_poke = time.monotonic()
        self._task = asyncio.create_task(self._watch())

    def stop(self) -> None:
        """Cancel the watchdog. Does NOT fire idle_event."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    async def _watch(self) -> None:
        try:
            while True:
                elapsed = time.monotonic() - self._last_poke
                remaining = self._timeout - elapsed
                if remaining <= 0:
                    self.idle_event.set()
                    logger.info(
                        "[IdleWatchdog] No activity for %.0fs. Firing idle event.",
                        self._timeout,
                    )
                    return
                await asyncio.sleep(min(remaining, 1.0))
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_idle_watchdog.py -v --timeout=15 -x`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/battle_test/idle_watchdog.py tests/test_ouroboros_governance/test_battle_test_idle_watchdog.py
git commit -m "feat(battle-test): add IdleWatchdog with poke-based idle detection"
```

---

## Task 4: SessionRecorder

**Files:**
- Create: `backend/core/ouroboros/battle_test/session_recorder.py`
- Test: `tests/test_ouroboros_governance/test_battle_test_session_recorder.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for SessionRecorder — session stats collection and terminal summary."""
from __future__ import annotations

import json
import time
import pytest


def test_record_operation():
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
    recorder = SessionRecorder(session_id="bt-test-001")
    recorder.record_operation(
        op_id="op-1", status="completed", sensor="TestFailureSensor",
        technique="module_mutation", composite_score=0.35, elapsed_s=2.5,
    )
    assert recorder.stats["attempted"] == 1
    assert recorder.stats["completed"] == 1


def test_multiple_statuses():
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
    recorder = SessionRecorder(session_id="bt-test-002")
    recorder.record_operation(op_id="op-1", status="completed", sensor="s1", technique="t1", composite_score=0.3, elapsed_s=1.0)
    recorder.record_operation(op_id="op-2", status="failed", sensor="s2", technique="t2", composite_score=0.8, elapsed_s=1.0)
    recorder.record_operation(op_id="op-3", status="cancelled", sensor="s1", technique="t1", composite_score=0.5, elapsed_s=1.0)
    recorder.record_operation(op_id="op-4", status="queued", sensor="s3", technique="t3", composite_score=0.4, elapsed_s=1.0)
    assert recorder.stats["completed"] == 1
    assert recorder.stats["failed"] == 1
    assert recorder.stats["cancelled"] == 1
    assert recorder.stats["queued"] == 1
    assert recorder.stats["attempted"] == 4


def test_sensor_counts():
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
    recorder = SessionRecorder(session_id="bt-test-003")
    recorder.record_operation(op_id="op-1", status="completed", sensor="TestFailureSensor", technique="t", composite_score=0.3, elapsed_s=1.0)
    recorder.record_operation(op_id="op-2", status="completed", sensor="TestFailureSensor", technique="t", composite_score=0.3, elapsed_s=1.0)
    recorder.record_operation(op_id="op-3", status="completed", sensor="OpportunityMinerSensor", technique="t", composite_score=0.3, elapsed_s=1.0)
    top = recorder.top_sensors(2)
    assert top[0] == ("TestFailureSensor", 2)
    assert top[1] == ("OpportunityMinerSensor", 1)


def test_save_summary(tmp_path):
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
    recorder = SessionRecorder(session_id="bt-test-004")
    recorder.record_operation(op_id="op-1", status="completed", sensor="s1", technique="t1", composite_score=0.35, elapsed_s=2.0)
    recorder.save_summary(
        output_dir=tmp_path,
        stop_reason="idle",
        duration_s=120.0,
        cost_total=0.15,
        cost_breakdown={"doubleword_397b": 0.15},
        branch_stats={"commits": 1, "files_changed": 2, "insertions": 10, "deletions": 3},
        convergence_state="improving",
        convergence_slope=-0.01,
        convergence_r2=0.65,
    )
    summary_path = tmp_path / "summary.json"
    assert summary_path.exists()
    data = json.loads(summary_path.read_text())
    assert data["session_id"] == "bt-test-004"
    assert data["stop_reason"] == "idle"
    assert data["operations"]["completed"] == 1


def test_format_terminal_summary():
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
    recorder = SessionRecorder(session_id="bt-test-005")
    recorder.record_operation(op_id="op-1", status="completed", sensor="s1", technique="t1", composite_score=0.3, elapsed_s=1.0)
    text = recorder.format_terminal_summary(
        stop_reason="budget", duration_s=300.0,
        cost_total=0.48, cost_breakdown={"doubleword_397b": 0.48},
        branch_name="ouroboros/battle-test-2026-04-06",
        branch_stats={"commits": 1, "files_changed": 1, "insertions": 5, "deletions": 0},
        convergence_state="improving", convergence_slope=-0.01, convergence_r2=0.7,
    )
    assert "SESSION COMPLETE" in text
    assert "bt-test-005" in text
    assert "improving" in text.lower() or "IMPROVING" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_session_recorder.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""SessionRecorder — collects battle test session stats and generates summary.

Records every operation outcome, produces terminal-formatted summary text
and persists a JSON summary file for notebook consumption.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SessionRecorder:
    """Collects session stats for terminal summary and notebook generation."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._operations: List[Dict[str, Any]] = []
        self._sensor_counts: Dict[str, int] = defaultdict(int)
        self._technique_counts: Dict[str, int] = defaultdict(int)
        self._status_counts: Dict[str, int] = defaultdict(int)

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "attempted": len(self._operations),
            "completed": self._status_counts.get("completed", 0),
            "failed": self._status_counts.get("failed", 0),
            "cancelled": self._status_counts.get("cancelled", 0),
            "queued": self._status_counts.get("queued", 0),
        }

    def record_operation(
        self,
        op_id: str,
        status: str,
        sensor: str,
        technique: str,
        composite_score: float,
        elapsed_s: float,
    ) -> None:
        self._operations.append({
            "op_id": op_id, "status": status, "sensor": sensor,
            "technique": technique, "composite_score": composite_score,
            "elapsed_s": elapsed_s, "timestamp": time.time(),
        })
        self._status_counts[status] += 1
        self._sensor_counts[sensor] += 1
        self._technique_counts[technique] += 1

    def top_sensors(self, n: int = 5) -> List[Tuple[str, int]]:
        return sorted(self._sensor_counts.items(), key=lambda x: x[1], reverse=True)[:n]

    def top_techniques(self, n: int = 5) -> List[Tuple[str, int]]:
        return sorted(self._technique_counts.items(), key=lambda x: x[1], reverse=True)[:n]

    def save_summary(
        self,
        output_dir: Path,
        stop_reason: str,
        duration_s: float,
        cost_total: float,
        cost_breakdown: Dict[str, float],
        branch_stats: Dict[str, int],
        convergence_state: str,
        convergence_slope: float,
        convergence_r2: float,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "summary.json"
        data = {
            "session_id": self.session_id,
            "stop_reason": stop_reason,
            "duration_s": round(duration_s, 1),
            "operations": self.stats,
            "cost": {"total": cost_total, "breakdown": cost_breakdown},
            "branch": branch_stats,
            "convergence": {
                "state": convergence_state,
                "slope": convergence_slope,
                "r_squared_log": convergence_r2,
            },
            "top_sensors": self.top_sensors(),
            "top_techniques": self.top_techniques(),
            "operation_log": self._operations,
        }
        path.write_text(json.dumps(data, indent=2))
        return path

    def format_terminal_summary(
        self,
        stop_reason: str,
        duration_s: float,
        cost_total: float,
        cost_breakdown: Dict[str, float],
        branch_name: str,
        branch_stats: Dict[str, int],
        convergence_state: str,
        convergence_slope: float,
        convergence_r2: float,
    ) -> str:
        s = self.stats
        mins = int(duration_s // 60)
        secs = int(duration_s % 60)
        total = max(s["attempted"], 1)

        lines = [
            "",
            "=" * 60,
            "  OUROBOROS BATTLE TEST — SESSION COMPLETE",
            "=" * 60,
            "",
            f"  Session ID:    {self.session_id}",
            f"  Duration:      {mins}m {secs}s",
            f"  Stop reason:   {stop_reason}",
            "",
            "  OPERATIONS",
            "  ----------",
            f"  Attempted:     {s['attempted']}",
            f"  Completed:     {s['completed']}  ({100*s['completed']/total:.1f}%)",
            f"  Failed:        {s['failed']}  ({100*s['failed']/total:.1f}%)",
            f"  Cancelled:     {s['cancelled']}  ({100*s['cancelled']/total:.1f}%)",
            f"  Queued (approval):  {s['queued']}",
            "",
            "  CONVERGENCE",
            "  -----------",
            f"  State:         {convergence_state.upper()}",
            f"  Slope:         {convergence_slope:.4f}",
            f"  R^2 (log fit): {convergence_r2:.2f}",
            "",
            "  COST",
            "  ----",
        ]
        for provider, amount in sorted(cost_breakdown.items()):
            lines.append(f"  {provider}: ${amount:.4f}")
        lines.append(f"  Total:         ${cost_total:.4f}")
        lines.append("")
        lines.append("  TOP SENSORS")
        lines.append("  -----------")
        for i, (sensor, count) in enumerate(self.top_sensors(5), 1):
            lines.append(f"  {i}. {sensor:<30} {count} operations")
        lines.append("")
        lines.append("  BRANCH")
        lines.append("  ------")
        lines.append(f"  Branch:     {branch_name}")
        lines.append(f"  Commits:    {branch_stats.get('commits', 0)}")
        lines.append(f"  Files:      {branch_stats.get('files_changed', 0)} changed")
        lines.append(f"  Insertions: +{branch_stats.get('insertions', 0)}")
        lines.append(f"  Deletions:  -{branch_stats.get('deletions', 0)}")
        lines.append("")
        lines.append("  Next steps:")
        lines.append(f"    git diff main..{branch_name}")
        lines.append(f"    jupyter notebook notebooks/ouroboros_battle_test_analysis.ipynb")
        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_session_recorder.py -v --timeout=15 -x`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/battle_test/session_recorder.py tests/test_ouroboros_governance/test_battle_test_session_recorder.py
git commit -m "feat(battle-test): add SessionRecorder with terminal summary and JSON export"
```

---

## Task 5: NotebookGenerator

**Files:**
- Create: `backend/core/ouroboros/battle_test/notebook_generator.py`
- Test: `tests/test_ouroboros_governance/test_battle_test_notebook_generator.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for NotebookGenerator — creates analysis notebook or Markdown fallback."""
from __future__ import annotations

import json
import pytest
from pathlib import Path


def _make_summary(tmp_path: Path) -> Path:
    """Create a minimal summary.json for testing."""
    data = {
        "session_id": "bt-test-001",
        "stop_reason": "budget",
        "duration_s": 300.0,
        "operations": {"attempted": 10, "completed": 8, "failed": 1, "cancelled": 1, "queued": 2},
        "cost": {"total": 0.48, "breakdown": {"doubleword_397b": 0.41, "doubleword_35b": 0.07}},
        "branch": {"commits": 8, "files_changed": 12, "insertions": 200, "deletions": 50},
        "convergence": {"state": "improving", "slope": -0.014, "r_squared_log": 0.73},
        "top_sensors": [["OpportunityMinerSensor", 5], ["TestFailureSensor", 3]],
        "top_techniques": [["module_mutation", 6], ["metrics_feedback", 2]],
        "operation_log": [
            {"op_id": f"op-{i}", "status": "completed", "sensor": "s", "technique": "t",
             "composite_score": 0.8 - i * 0.03, "elapsed_s": 1.0, "timestamp": 1000.0 + i}
            for i in range(8)
        ],
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(data))
    return summary_path


def test_generate_markdown_fallback(tmp_path):
    from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator
    summary_path = _make_summary(tmp_path)
    gen = NotebookGenerator(summary_path=summary_path)
    output = gen.generate_markdown(output_dir=tmp_path)
    assert output.exists()
    content = output.read_text()
    assert "bt-test-001" in content
    assert "improving" in content.lower()
    assert "OpportunityMinerSensor" in content


def test_generate_notebook(tmp_path):
    from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator
    summary_path = _make_summary(tmp_path)
    gen = NotebookGenerator(summary_path=summary_path)
    output = gen.generate_notebook(output_path=tmp_path / "analysis.ipynb")
    assert output.exists()
    assert output.suffix == ".ipynb"
    # Verify it's valid JSON (nbformat)
    data = json.loads(output.read_text())
    assert "cells" in data
    assert len(data["cells"]) >= 5


def test_generate_auto_detects(tmp_path):
    from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator
    summary_path = _make_summary(tmp_path)
    gen = NotebookGenerator(summary_path=summary_path)
    output = gen.generate(output_dir=tmp_path)
    assert output.exists()
    # Should produce either .ipynb or .md depending on nbformat availability
    assert output.suffix in (".ipynb", ".md")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_notebook_generator.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""NotebookGenerator — creates Jupyter notebook or Markdown fallback from battle test data.

Generates pre-populated analysis notebooks with composite score trends,
convergence analysis, transition heatmaps, and operation breakdowns.
Falls back to Markdown if nbformat is not installed.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class NotebookGenerator:
    """Generates analysis notebook or Markdown report from session summary."""

    def __init__(self, summary_path: Path) -> None:
        self._summary_path = summary_path
        self._data: Dict[str, Any] = json.loads(summary_path.read_text())

    def generate(self, output_dir: Path) -> Path:
        """Auto-detect: notebook if nbformat available, else Markdown."""
        try:
            import nbformat  # noqa: F401
            return self.generate_notebook(output_path=output_dir / "ouroboros_battle_test_analysis.ipynb")
        except ImportError:
            return self.generate_markdown(output_dir=output_dir)

    def generate_notebook(self, output_path: Path) -> Path:
        """Generate a Jupyter notebook with pre-populated analysis cells."""
        import nbformat
        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        }

        summary_json = json.dumps(self._data, indent=2)

        nb.cells = [
            nbformat.v4.new_markdown_cell(
                f"# Ouroboros Battle Test Analysis\n\n"
                f"**Session:** {self._data['session_id']}  \n"
                f"**Duration:** {self._data['duration_s']:.0f}s  \n"
                f"**Stop reason:** {self._data['stop_reason']}"
            ),
            nbformat.v4.new_code_cell(
                "import json\n"
                "import matplotlib.pyplot as plt\n"
                "import numpy as np\n"
                "\n"
                f"summary = json.loads('''{summary_json}''')\n"
                "ops = summary['operation_log']\n"
                "scores = [op['composite_score'] for op in ops if op['status'] == 'completed']\n"
                "print(f'Loaded {len(ops)} operations, {len(scores)} completed with scores')"
            ),
            nbformat.v4.new_markdown_cell("## Composite Score Trend"),
            nbformat.v4.new_code_cell(
                "if scores:\n"
                "    fig, ax = plt.subplots(figsize=(10, 5))\n"
                "    ax.plot(range(len(scores)), scores, 'b-o', markersize=4, label='Composite Score')\n"
                "    # Logarithmic fit overlay\n"
                "    if len(scores) >= 3:\n"
                "        t = np.arange(1, len(scores) + 1)\n"
                "        log_t = np.log(t)\n"
                "        coeffs = np.polyfit(log_t, scores, 1)\n"
                "        fit = coeffs[0] * log_t + coeffs[1]\n"
                "        ax.plot(range(len(scores)), fit, 'r--', alpha=0.7, label=f'Log fit (a={coeffs[0]:.3f})')\n"
                "    ax.set_xlabel('Operation #')\n"
                "    ax.set_ylabel('Composite Score (lower = better)')\n"
                "    ax.set_title('RSI Convergence — Composite Score Trend')\n"
                "    ax.legend()\n"
                "    ax.grid(True, alpha=0.3)\n"
                "    plt.tight_layout()\n"
                "    plt.show()\n"
                "else:\n"
                "    print('No completed operations with scores.')"
            ),
            nbformat.v4.new_markdown_cell("## Convergence State"),
            nbformat.v4.new_code_cell(
                "conv = summary['convergence']\n"
                "print(f\"State:    {conv['state'].upper()}\")\n"
                "print(f\"Slope:    {conv['slope']:.4f}\")\n"
                "print(f\"R^2 log:  {conv['r_squared_log']:.2f}\")\n"
                "print()\n"
                "if conv['state'] == 'improving':\n"
                "    print('Pipeline is converging. Composite scores trending downward.')\n"
                "elif conv['state'] == 'logarithmic':\n"
                "    print('Matches Wang O(log n) prediction. Healthy convergence confirmed.')\n"
                "elif conv['state'] == 'plateaued':\n"
                "    print('Pipeline has plateaued. Consider triggering Dynamic Re-Planning.')\n"
                "elif conv['state'] == 'oscillating':\n"
                "    print('Pipeline is oscillating. Tighten negative constraints.')\n"
                "elif conv['state'] == 'degrading':\n"
                "    print('WARNING: Pipeline is degrading. Investigate immediately.')"
            ),
            nbformat.v4.new_markdown_cell("## Operations Breakdown"),
            nbformat.v4.new_code_cell(
                "op_stats = summary['operations']\n"
                "labels = ['Completed', 'Failed', 'Cancelled', 'Queued']\n"
                "values = [op_stats.get('completed',0), op_stats.get('failed',0),\n"
                "          op_stats.get('cancelled',0), op_stats.get('queued',0)]\n"
                "colors = ['#2ecc71', '#e74c3c', '#95a5a6', '#f39c12']\n"
                "fig, ax = plt.subplots(figsize=(6, 6))\n"
                "ax.pie([v for v in values if v > 0],\n"
                "       labels=[l for l, v in zip(labels, values) if v > 0],\n"
                "       colors=[c for c, v in zip(colors, values) if v > 0],\n"
                "       autopct='%1.1f%%', startangle=90)\n"
                "ax.set_title(f'Operations ({op_stats[\"attempted\"]} total)')\n"
                "plt.show()"
            ),
            nbformat.v4.new_markdown_cell("## Sensor Activation"),
            nbformat.v4.new_code_cell(
                "sensors = summary['top_sensors']\n"
                "if sensors:\n"
                "    names = [s[0] for s in sensors]\n"
                "    counts = [s[1] for s in sensors]\n"
                "    fig, ax = plt.subplots(figsize=(10, 4))\n"
                "    ax.barh(names, counts, color='#3498db')\n"
                "    ax.set_xlabel('Operations')\n"
                "    ax.set_title('Sensor Activation Frequency')\n"
                "    plt.tight_layout()\n"
                "    plt.show()"
            ),
            nbformat.v4.new_markdown_cell("## Cost & Branch Summary"),
            nbformat.v4.new_code_cell(
                "cost = summary['cost']\n"
                "branch = summary['branch']\n"
                "print(f\"Total cost:    ${cost['total']:.4f}\")\n"
                "for provider, amount in cost['breakdown'].items():\n"
                "    print(f\"  {provider}: ${amount:.4f}\")\n"
                "print()\n"
                "print(f\"Commits:       {branch['commits']}\")\n"
                "print(f\"Files changed: {branch['files_changed']}\")\n"
                "print(f\"Insertions:    +{branch['insertions']}\")\n"
                "print(f\"Deletions:     -{branch['deletions']}\")"
            ),
        ]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            nbformat.write(nb, f)
        return output_path

    def generate_markdown(self, output_dir: Path) -> Path:
        """Generate Markdown report as fallback when nbformat is not available."""
        d = self._data
        ops = d.get("operation_log", [])
        scores = [op["composite_score"] for op in ops if op.get("status") == "completed"]

        lines = [
            f"# Ouroboros Battle Test Report",
            f"",
            f"**Session:** {d['session_id']}",
            f"**Duration:** {d['duration_s']:.0f}s",
            f"**Stop reason:** {d['stop_reason']}",
            f"",
            f"## Operations",
            f"",
            f"| Status | Count |",
            f"|---|---|",
        ]
        for k, v in d.get("operations", {}).items():
            lines.append(f"| {k} | {v} |")

        lines.extend([
            f"",
            f"## Convergence",
            f"",
            f"- State: **{d['convergence']['state'].upper()}**",
            f"- Slope: {d['convergence']['slope']:.4f}",
            f"- R^2 (log fit): {d['convergence']['r_squared_log']:.2f}",
            f"",
            f"## Composite Scores",
            f"",
        ])
        if scores:
            lines.append(f"Scores (chronological): {', '.join(f'{s:.3f}' for s in scores)}")
        else:
            lines.append("No completed operations with scores.")

        lines.extend([
            f"",
            f"## Top Sensors",
            f"",
        ])
        for sensor, count in d.get("top_sensors", []):
            lines.append(f"- {sensor}: {count} operations")

        lines.extend([
            f"",
            f"## Cost",
            f"",
            f"Total: ${d['cost']['total']:.4f}",
            f"",
        ])
        for provider, amount in d.get("cost", {}).get("breakdown", {}).items():
            lines.append(f"- {provider}: ${amount:.4f}")

        lines.extend([
            f"",
            f"## Branch",
            f"",
            f"- Commits: {d['branch']['commits']}",
            f"- Files changed: {d['branch']['files_changed']}",
            f"- Insertions: +{d['branch']['insertions']}",
            f"- Deletions: -{d['branch']['deletions']}",
        ])

        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "report.md"
        path.write_text("\n".join(lines))
        return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_notebook_generator.py -v --timeout=15 -x`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/battle_test/notebook_generator.py tests/test_ouroboros_governance/test_battle_test_notebook_generator.py
git commit -m "feat(battle-test): add NotebookGenerator with Jupyter and Markdown fallback"
```

---

## Task 6: BattleTestHarness

**Files:**
- Create: `backend/core/ouroboros/battle_test/harness.py`
- Test: `tests/test_ouroboros_governance/test_battle_test_harness.py`

This is the main orchestrator that wires everything together. The test verifies the boot/shutdown lifecycle with mocked components.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for BattleTestHarness — main orchestrator lifecycle."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


@pytest.mark.asyncio
async def test_harness_lifecycle(tmp_path):
    """Verify harness boots, enters event loop, and shuts down cleanly."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness, HarnessConfig

    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.50,
        idle_timeout_s=2.0,
        branch_prefix="ouroboros/test",
        session_dir=tmp_path / "session",
    )
    harness = BattleTestHarness(config=config)

    # Mock all heavy components
    harness._boot_oracle = AsyncMock()
    harness._boot_governance_stack = AsyncMock()
    harness._boot_governed_loop_service = AsyncMock()
    harness._boot_jarvis_tiers = AsyncMock()
    harness._boot_intake = AsyncMock()
    harness._boot_graduation = AsyncMock()
    harness._create_branch = MagicMock(return_value="ouroboros/test-2026-04-06")
    harness._shutdown_components = AsyncMock()
    harness._generate_report = MagicMock()

    # Run with immediate shutdown
    harness._shutdown_event = asyncio.Event()
    harness._shutdown_event.set()  # Trigger immediate shutdown

    await harness.run()

    harness._boot_oracle.assert_awaited_once()
    harness._boot_governance_stack.assert_awaited_once()
    harness._shutdown_components.assert_awaited_once()
    harness._generate_report.assert_called_once()


def test_harness_config_defaults():
    from backend.core.ouroboros.battle_test.harness import HarnessConfig
    config = HarnessConfig(repo_path=Path("."))
    assert config.cost_cap_usd == 0.50
    assert config.idle_timeout_s == 600.0
    assert config.branch_prefix == "ouroboros/battle-test"


def test_harness_config_from_env(monkeypatch):
    from backend.core.ouroboros.battle_test.harness import HarnessConfig
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "1.00")
    monkeypatch.setenv("OUROBOROS_BATTLE_IDLE_TIMEOUT", "300")
    config = HarnessConfig.from_env()
    assert config.cost_cap_usd == 1.00
    assert config.idle_timeout_s == 300.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_harness.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""BattleTestHarness — standalone headless Ouroboros governance runner.

Boots the full Ouroboros brain (17/18 LIVE components minus vision),
creates an accumulation branch, lets sensors find work, auto-applies
SAFE_AUTO operations, and produces convergence analytics on shutdown.

Usage: See scripts/ouroboros_battle_test.py for CLI entry point.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class HarnessConfig:
    """Configuration for the battle test harness."""
    repo_path: Path = field(default_factory=lambda: Path("."))
    cost_cap_usd: float = 0.50
    idle_timeout_s: float = 600.0
    branch_prefix: str = "ouroboros/battle-test"
    session_dir: Optional[Path] = None
    notebook_output_dir: Optional[Path] = None

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        return cls(
            repo_path=Path(os.environ.get("JARVIS_REPO_PATH", ".")),
            cost_cap_usd=float(os.environ.get("OUROBOROS_BATTLE_COST_CAP", "0.50")),
            idle_timeout_s=float(os.environ.get("OUROBOROS_BATTLE_IDLE_TIMEOUT", "600")),
            branch_prefix=os.environ.get("OUROBOROS_BATTLE_BRANCH_PREFIX", "ouroboros/battle-test"),
        )


class BattleTestHarness:
    """Standalone headless Ouroboros governance runner.

    Boots GovernanceStack, GovernedLoopService, IntakeLayerService,
    TheOracle, and all JARVIS-level tiers. Creates an accumulation branch
    and lets the organism autonomously improve the JARVIS codebase.
    """

    def __init__(self, config: HarnessConfig) -> None:
        self._config = config
        self._session_id = f"bt-{time.strftime('%Y-%m-%d-%H%M%S')}"
        self._session_dir = config.session_dir or (
            Path.home() / ".jarvis" / "ouroboros" / "battle-test" / self._session_id
        )
        self._notebook_dir = config.notebook_output_dir or Path("notebooks")

        # Components (set during boot)
        self._oracle: Any = None
        self._stack: Any = None
        self._gls: Any = None
        self._intake: Any = None
        self._graduation: Any = None
        self._predictive: Any = None

        # Battle test infrastructure
        from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
        from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
        from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

        self._cost_tracker = CostTracker(
            budget_usd=config.cost_cap_usd,
            persist_path=self._session_dir / "cost_state.json",
        )
        self._idle_watchdog = IdleWatchdog(timeout_s=config.idle_timeout_s)
        self._recorder = SessionRecorder(session_id=self._session_id)

        # Shutdown signals
        self._shutdown_event = asyncio.Event()
        self._start_time = 0.0
        self._stop_reason = "unknown"
        self._branch_name: Optional[str] = None

    async def run(self) -> None:
        """Boot, run, and shutdown the battle test."""
        self._start_time = time.time()
        logger.info("[BattleTest] Starting session %s", self._session_id)

        try:
            # Boot sequence
            await self._boot_oracle()
            await self._boot_governance_stack()
            await self._boot_governed_loop_service()
            await self._boot_jarvis_tiers()
            self._branch_name = self._create_branch()
            await self._boot_intake()
            await self._boot_graduation()

            logger.info(
                "[BattleTest] Ouroboros is alive. Session: %s. Budget: $%.2f",
                self._session_id, self._config.cost_cap_usd,
            )
            print(f"\nOuroboros is alive. Session: {self._session_id}")
            print(f"Branch: {self._branch_name}")
            print(f"Budget: ${self._config.cost_cap_usd:.2f}")
            print("Press Ctrl+C to stop.\n")

            # Start idle watchdog
            await self._idle_watchdog.start()

            # Wait for any stop condition
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self._shutdown_event.wait()),
                    asyncio.create_task(self._cost_tracker.budget_event.wait()),
                    asyncio.create_task(self._idle_watchdog.idle_event.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            # Determine stop reason
            if self._cost_tracker.exhausted:
                self._stop_reason = f"Budget exhausted (${self._cost_tracker.total_spent:.4f})"
            elif self._idle_watchdog.idle_event.is_set():
                self._stop_reason = f"Idle timeout ({self._config.idle_timeout_s:.0f}s)"
            else:
                self._stop_reason = "User shutdown (SIGINT)"

        except Exception as exc:
            self._stop_reason = f"Error: {exc}"
            logger.error("[BattleTest] Fatal error: %s", exc, exc_info=True)
        finally:
            await self._shutdown_components()
            self._generate_report()

    def register_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register SIGINT handler for graceful shutdown."""
        try:
            loop.add_signal_handler(signal.SIGINT, self._shutdown_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # ── Boot methods (overridable for testing) ──────────────────

    async def _boot_oracle(self) -> None:
        logger.info("[BattleTest] Booting TheOracle...")
        try:
            from backend.core.ouroboros.oracle import TheOracle
            self._oracle = TheOracle()
            await self._oracle.initialize()
            logger.info("[BattleTest] TheOracle ready.")
        except Exception as exc:
            logger.warning("[BattleTest] Oracle failed to boot: %s (continuing without)", exc)

    async def _boot_governance_stack(self) -> None:
        logger.info("[BattleTest] Booting GovernanceStack...")
        try:
            import argparse
            from backend.core.ouroboros.governance.integration import (
                GovernanceConfig, create_governance_stack,
            )
            args = argparse.Namespace(skip_governance=False, governance_mode="governed")
            gov_config = GovernanceConfig.from_env_and_args(args)
            self._stack = await create_governance_stack(
                config=gov_config, oracle=self._oracle,
            )
            await self._stack.start()
            logger.info("[BattleTest] GovernanceStack ready.")
        except Exception as exc:
            logger.error("[BattleTest] GovernanceStack failed: %s", exc, exc_info=True)
            raise

    async def _boot_governed_loop_service(self) -> None:
        logger.info("[BattleTest] Booting GovernedLoopService...")
        try:
            from backend.core.ouroboros.governance.governed_loop_service import (
                GovernedLoopService, GovernedLoopConfig,
            )
            gls_config = GovernedLoopConfig.from_env()
            self._gls = GovernedLoopService(
                stack=self._stack,
                config=gls_config,
                say_fn=None,
            )
            await self._gls.start()
            logger.info("[BattleTest] GovernedLoopService ready.")
        except Exception as exc:
            logger.error("[BattleTest] GLS failed: %s", exc, exc_info=True)
            raise

    async def _boot_jarvis_tiers(self) -> None:
        logger.info("[BattleTest] Booting JARVIS-level tiers...")
        # Tier 3: Predictive Regression Engine (background task)
        try:
            from backend.core.ouroboros.governance.predictive_engine import PredictiveRegressionEngine
            self._predictive = PredictiveRegressionEngine(project_root=self._config.repo_path)
            await self._predictive.start()
            logger.info("[BattleTest] Tier 3 (PredictiveRegression) ready.")
        except Exception as exc:
            logger.warning("[BattleTest] Tier 3 failed: %s (continuing)", exc)
        # Tiers 1, 2, 5, 6, 7 are instantiated inside the orchestrator
        # via try/except imports during pre-GENERATE injection. No explicit
        # boot needed — they activate when the first operation runs.
        logger.info("[BattleTest] JARVIS tiers ready (1,2,5,6,7 activate on first operation).")

    def _create_branch(self) -> str:
        from backend.core.ouroboros.battle_test.branch_manager import BranchManager
        mgr = BranchManager(
            repo_path=self._config.repo_path,
            branch_prefix=self._config.branch_prefix,
        )
        return mgr.create_branch()

    async def _boot_intake(self) -> None:
        logger.info("[BattleTest] Booting IntakeLayerService (headless profile)...")
        try:
            from backend.core.ouroboros.governance.intake.intake_layer_service import (
                IntakeLayerService, IntakeLayerConfig,
            )
            intake_config = IntakeLayerConfig.from_env(project_root=self._config.repo_path)
            self._intake = IntakeLayerService(
                gls=self._gls, config=intake_config, say_fn=None,
            )
            await self._intake.start()
            logger.info("[BattleTest] IntakeLayerService ready.")
        except Exception as exc:
            logger.error("[BattleTest] Intake failed: %s", exc, exc_info=True)
            raise

    async def _boot_graduation(self) -> None:
        logger.info("[BattleTest] Booting GraduationOrchestrator...")
        try:
            from backend.core.ouroboros.governance.graduation_orchestrator import GraduationOrchestrator
            self._graduation = GraduationOrchestrator()
            logger.info("[BattleTest] GraduationOrchestrator ready.")
        except Exception as exc:
            logger.warning("[BattleTest] Graduation failed: %s (continuing)", exc)

    # ── Shutdown ────────────────────────────────────────────────

    async def _shutdown_components(self) -> None:
        logger.info("[BattleTest] Shutting down... (%s)", self._stop_reason)
        self._idle_watchdog.stop()
        if self._intake:
            try:
                await self._intake.stop()
            except Exception:
                pass
        if self._predictive:
            try:
                self._predictive.stop()
            except Exception:
                pass
        if self._gls:
            try:
                await self._gls.stop()
            except Exception:
                pass
        if self._stack:
            try:
                await self._stack.stop()
            except Exception:
                pass
        if self._oracle:
            try:
                await self._oracle.shutdown()
            except Exception:
                pass
        self._cost_tracker.save()

    def _generate_report(self) -> None:
        duration = time.time() - self._start_time

        # Get convergence data
        convergence_state = "insufficient_data"
        convergence_slope = 0.0
        convergence_r2 = 0.0
        try:
            from backend.core.ouroboros.governance.composite_score import ScoreHistory
            from backend.core.ouroboros.governance.convergence_tracker import ConvergenceTracker
            history = ScoreHistory()
            composites = history.get_composite_values()
            if len(composites) >= 5:
                report = ConvergenceTracker().analyze(composites)
                convergence_state = report.state.value
                convergence_slope = report.slope
                convergence_r2 = report.r_squared_log
        except Exception:
            pass

        # Get branch stats
        branch_stats = {"commits": 0, "files_changed": 0, "insertions": 0, "deletions": 0}
        try:
            from backend.core.ouroboros.battle_test.branch_manager import BranchManager
            mgr = BranchManager(repo_path=self._config.repo_path)
            mgr._branch = self._branch_name
            mgr._base_branch = "main"
            branch_stats = mgr.get_diff_stats()
        except Exception:
            pass

        # Save summary JSON
        self._recorder.save_summary(
            output_dir=self._session_dir,
            stop_reason=self._stop_reason,
            duration_s=duration,
            cost_total=self._cost_tracker.total_spent,
            cost_breakdown=self._cost_tracker.breakdown,
            branch_stats=branch_stats,
            convergence_state=convergence_state,
            convergence_slope=convergence_slope,
            convergence_r2=convergence_r2,
        )

        # Print terminal summary
        summary_text = self._recorder.format_terminal_summary(
            stop_reason=self._stop_reason,
            duration_s=duration,
            cost_total=self._cost_tracker.total_spent,
            cost_breakdown=self._cost_tracker.breakdown,
            branch_name=self._branch_name or "(no branch)",
            branch_stats=branch_stats,
            convergence_state=convergence_state,
            convergence_slope=convergence_slope,
            convergence_r2=convergence_r2,
        )
        print(summary_text)

        # Generate notebook
        try:
            from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator
            summary_path = self._session_dir / "summary.json"
            if summary_path.exists():
                gen = NotebookGenerator(summary_path=summary_path)
                output = gen.generate(output_dir=self._notebook_dir)
                print(f"\n  Analysis: {output}")
        except Exception as exc:
            logger.debug("Notebook generation failed: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_harness.py -v --timeout=15 -x`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/battle_test/harness.py tests/test_ouroboros_governance/test_battle_test_harness.py
git commit -m "feat(battle-test): add BattleTestHarness with full Ouroboros boot and event-driven shutdown"
```

---

## Task 7: CLI Entry Point

**Files:**
- Create: `scripts/ouroboros_battle_test.py`

- [ ] **Step 1: Write the CLI entry point**

```python
#!/usr/bin/env python3
"""Ouroboros Battle Test Runner — standalone headless governance daemon.

Boots the full Ouroboros brain and lets it autonomously find and apply
improvements to the JARVIS codebase. Proves the organism works.

Usage:
    python3 scripts/ouroboros_battle_test.py
    python3 scripts/ouroboros_battle_test.py --cost-cap 1.00 --idle-timeout 300
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ouroboros Battle Test — standalone headless governance daemon",
    )
    parser.add_argument(
        "--cost-cap", type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_COST_CAP", "0.50")),
        help="Session budget in USD (default: $0.50)",
    )
    parser.add_argument(
        "--idle-timeout", type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_IDLE_TIMEOUT", "600")),
        help="Seconds of no activity before shutdown (default: 600)",
    )
    parser.add_argument(
        "--branch-prefix", type=str,
        default=os.environ.get("OUROBOROS_BATTLE_BRANCH_PREFIX", "ouroboros/battle-test"),
        help="Git branch prefix (default: ouroboros/battle-test)",
    )
    parser.add_argument(
        "--repo-path", type=str,
        default=os.environ.get("JARVIS_REPO_PATH", str(_PROJECT_ROOT)),
        help="Path to JARVIS repo (default: project root)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate environment
    if not os.environ.get("DOUBLEWORD_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set DOUBLEWORD_API_KEY or ANTHROPIC_API_KEY before running.", file=sys.stderr)
        sys.exit(1)

    # Force governed mode
    os.environ.setdefault("JARVIS_GOVERNANCE_MODE", "governed")

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness, HarnessConfig

    config = HarnessConfig(
        repo_path=Path(args.repo_path),
        cost_cap_usd=args.cost_cap,
        idle_timeout_s=args.idle_timeout,
        branch_prefix=args.branch_prefix,
    )

    harness = BattleTestHarness(config=config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    harness.register_signal_handlers(loop)

    try:
        loop.run_until_complete(harness.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/ouroboros_battle_test.py
```

- [ ] **Step 3: Verify it parses args without crashing**

Run: `python3 scripts/ouroboros_battle_test.py --help`
Expected: Shows help text with --cost-cap, --idle-timeout, --branch-prefix, --repo-path, --verbose

- [ ] **Step 4: Commit**

```bash
git add scripts/ouroboros_battle_test.py
git commit -m "feat(battle-test): add CLI entry point for ouroboros_battle_test.py"
```

---

## Task 8: Integration Test

**Files:**
- Create: `tests/test_ouroboros_governance/test_battle_test_integration.py`

- [ ] **Step 1: Write integration test**

```python
"""Integration test — verifies battle test components wire together."""
from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_full_battle_test_lifecycle(tmp_path):
    """Boot harness with mocks, run one cycle, verify report generation."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness, HarnessConfig
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
    from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog
    from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator

    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.50,
        idle_timeout_s=1.0,
        session_dir=tmp_path / "session",
        notebook_output_dir=tmp_path / "notebooks",
    )
    harness = BattleTestHarness(config=config)

    # Mock all heavy boot methods
    harness._boot_oracle = AsyncMock()
    harness._boot_governance_stack = AsyncMock()
    harness._boot_governed_loop_service = AsyncMock()
    harness._boot_jarvis_tiers = AsyncMock()
    harness._boot_intake = AsyncMock()
    harness._boot_graduation = AsyncMock()
    harness._create_branch = MagicMock(return_value="ouroboros/test-branch")

    # Record a fake operation before shutdown
    harness._recorder.record_operation(
        op_id="op-1", status="completed", sensor="TestFailureSensor",
        technique="module_mutation", composite_score=0.35, elapsed_s=2.0,
    )

    # Let idle watchdog fire quickly (1s timeout, no poke)
    await harness.run()

    # Verify session dir created with summary
    summary_path = tmp_path / "session" / "summary.json"
    assert summary_path.exists()
    data = json.loads(summary_path.read_text())
    assert data["session_id"].startswith("bt-")
    assert data["operations"]["completed"] == 1


def test_cost_tracker_gates_budget(tmp_path):
    """Verify cost tracker fires event when budget exhausted."""
    from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
    tracker = CostTracker(budget_usd=0.10, persist_path=tmp_path / "cost.json")
    tracker.record(provider="doubleword_397b", cost_usd=0.05)
    assert not tracker.exhausted
    tracker.record(provider="doubleword_397b", cost_usd=0.06)
    assert tracker.exhausted
    assert tracker.budget_event.is_set()
    tracker.save()
    # Verify persistence
    loaded = CostTracker(budget_usd=0.10, persist_path=tmp_path / "cost.json")
    assert loaded.total_spent == pytest.approx(0.11)
    assert loaded.exhausted


def test_notebook_generator_from_session(tmp_path):
    """Verify notebook generates from real session data."""
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
    from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator

    recorder = SessionRecorder(session_id="bt-integration-001")
    for i in range(5):
        recorder.record_operation(
            op_id=f"op-{i}", status="completed", sensor="OpportunityMinerSensor",
            technique="module_mutation", composite_score=0.8 - i * 0.1, elapsed_s=1.5,
        )
    summary_path = recorder.save_summary(
        output_dir=tmp_path,
        stop_reason="idle",
        duration_s=60.0,
        cost_total=0.12,
        cost_breakdown={"doubleword_397b": 0.12},
        branch_stats={"commits": 5, "files_changed": 8, "insertions": 120, "deletions": 30},
        convergence_state="improving",
        convergence_slope=-0.02,
        convergence_r2=0.75,
    )
    gen = NotebookGenerator(summary_path=summary_path)
    output = gen.generate(output_dir=tmp_path / "notebooks")
    assert output.exists()
```

- [ ] **Step 2: Run integration tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_integration.py -v --timeout=30 -x`
Expected: All 3 tests PASS

- [ ] **Step 3: Run full battle test suite**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_*.py -v --timeout=60`
Expected: All tests PASS across all 7 test files

- [ ] **Step 4: Commit**

```bash
git add tests/test_ouroboros_governance/test_battle_test_integration.py
git commit -m "test(battle-test): add integration tests for full battle test lifecycle"
```

---

## Task 9: Final Verification

- [ ] **Step 1: Run ALL battle test tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_*.py -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 2: Run ALL RSI convergence tests to verify no regressions**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_composite_score.py tests/test_ouroboros_governance/test_convergence_tracker.py tests/test_ouroboros_governance/test_adaptive_graduation.py tests/test_ouroboros_governance/test_transition_tracker.py tests/test_ouroboros_governance/test_oracle_prescorer.py tests/test_ouroboros_governance/test_vindication_reflector.py tests/test_ouroboros_governance/test_rsi_convergence_integration.py -v --timeout=60`
Expected: 134 tests PASS

- [ ] **Step 3: Verify CLI --help works**

Run: `python3 scripts/ouroboros_battle_test.py --help`
Expected: Shows help text

- [ ] **Step 4: Final commit**

```bash
git add -A
git status
git commit -m "feat(battle-test): complete Ouroboros Battle Test Runner

Standalone headless daemon that boots the full Ouroboros governance brain
(17/18 LIVE components) and autonomously improves the JARVIS codebase.

Components:
- BattleTestHarness: boot, event-driven run, graceful shutdown
- CostTracker: per-session budget with asyncio.Event
- BranchManager: accumulation branch with structured commits
- IdleWatchdog: poke-based idle detection
- SessionRecorder: stats collection + terminal summary
- NotebookGenerator: Jupyter notebook with Markdown fallback
- CLI: argparse entry point with env var overrides

Usage: python3 scripts/ouroboros_battle_test.py --cost-cap 0.50"
```
