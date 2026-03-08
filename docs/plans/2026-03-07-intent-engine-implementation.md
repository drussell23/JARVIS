# Intent Engine (Layer 1) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the Intent Engine — a long-running async service that detects test failures and stack traces, deduplicates and rate-limits them, checks autonomy gates, and routes stable signals to the governed pipeline or narrates them via voice.

**Architecture:** Six modules under `backend/core/ouroboros/governance/intent/`. Signals flow: TestWatcher/ErrorInterceptor → IntentSignal → dedup → rate limiter → autonomy gate → GovernedLoopService.submit() or voice narration. All async, all configurable via env vars, all fail-open on optional dependencies.

**Tech Stack:** Python 3.9+, asyncio, pytest subprocess, logging.Handler, existing GovernedLoopService, CommProtocol, safe_say(), dataclasses, UUIDv7 (uuid6).

**Design doc:** `docs/plans/2026-03-07-autonomous-layers-design.md` §2 (Layer 1)

---

## Task 1: IntentSignal Dataclass + Dedup Logic (`signals.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/intent/signals.py`
- Create: `backend/core/ouroboros/governance/intent/__init__.py`
- Create: `tests/governance/intent/__init__.py`
- Test: `tests/governance/intent/test_signals.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/intent/test_signals.py"""
import pytest
from datetime import datetime, timezone


def test_intent_signal_creation():
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    sig = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_utils.py",),
        repo="jarvis",
        description="test_edge_case failed 2x",
        evidence={"traceback": "AssertionError"},
        confidence=0.95,
        stable=True,
    )
    assert sig.source == "intent:test_failure"
    assert sig.signal_id  # auto-generated
    assert isinstance(sig.timestamp, datetime)
    assert sig.stable is True


def test_intent_signal_frozen():
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    sig = IntentSignal(
        source="intent:test_failure",
        target_files=("f.py",),
        repo="jarvis",
        description="fail",
        evidence={},
        confidence=0.9,
        stable=True,
    )
    with pytest.raises(AttributeError):
        sig.source = "other"


def test_dedup_key_same_for_identical_signals():
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    sig1 = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_utils.py",),
        repo="jarvis",
        description="test_edge_case failed",
        evidence={"signature": "AssertionError: expected 3"},
        confidence=0.9,
        stable=True,
    )
    sig2 = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_utils.py",),
        repo="jarvis",
        description="test_edge_case failed again",
        evidence={"signature": "AssertionError: expected 3"},
        confidence=0.95,
        stable=True,
    )
    assert sig1.dedup_key == sig2.dedup_key


def test_dedup_key_differs_for_different_files():
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    sig1 = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_a.py",),
        repo="jarvis",
        description="fail",
        evidence={"signature": "err"},
        confidence=0.9,
        stable=True,
    )
    sig2 = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_b.py",),
        repo="jarvis",
        description="fail",
        evidence={"signature": "err"},
        confidence=0.9,
        stable=True,
    )
    assert sig1.dedup_key != sig2.dedup_key


def test_dedup_tracker_blocks_duplicate_within_cooldown():
    from backend.core.ouroboros.governance.intent.signals import (
        IntentSignal,
        DedupTracker,
    )

    tracker = DedupTracker(cooldown_s=300.0)
    sig = IntentSignal(
        source="intent:test_failure",
        target_files=("f.py",),
        repo="jarvis",
        description="fail",
        evidence={"signature": "err"},
        confidence=0.9,
        stable=True,
    )
    assert tracker.is_new(sig) is True
    assert tracker.is_new(sig) is False  # duplicate


def test_dedup_tracker_allows_after_cooldown():
    import time
    from backend.core.ouroboros.governance.intent.signals import (
        IntentSignal,
        DedupTracker,
    )

    tracker = DedupTracker(cooldown_s=0.0)  # zero cooldown
    sig = IntentSignal(
        source="intent:test_failure",
        target_files=("f.py",),
        repo="jarvis",
        description="fail",
        evidence={"signature": "err"},
        confidence=0.9,
        stable=True,
    )
    assert tracker.is_new(sig) is True
    time.sleep(0.01)
    assert tracker.is_new(sig) is True  # cooldown expired


def test_cross_signal_dedup_test_failure_wins_over_stack_trace():
    """Same file from both test failure and stack trace = one op, test failure wins."""
    from backend.core.ouroboros.governance.intent.signals import (
        IntentSignal,
        DedupTracker,
    )

    tracker = DedupTracker(cooldown_s=300.0)
    test_sig = IntentSignal(
        source="intent:test_failure",
        target_files=("src/utils.py",),
        repo="jarvis",
        description="test failed",
        evidence={"signature": "err"},
        confidence=0.9,
        stable=True,
    )
    trace_sig = IntentSignal(
        source="intent:stack_trace",
        target_files=("src/utils.py",),
        repo="jarvis",
        description="stack trace",
        evidence={"signature": "err"},
        confidence=0.8,
        stable=False,
    )
    assert tracker.is_new(test_sig) is True
    # Same file + same error signature → blocked regardless of source
    assert tracker.is_new(trace_sig) is False
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intent/test_signals.py -v`
Expected: FAIL (module not found)

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/intent/signals.py

IntentSignal dataclass and deduplication logic.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §2
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id


@dataclass(frozen=True)
class IntentSignal:
    """Immutable signal emitted by a trigger (test watcher, error interceptor)."""

    source: str  # "intent:test_failure" | "intent:stack_trace" | "intent:git_analysis"
    target_files: Tuple[str, ...]
    repo: str  # "jarvis" | "prime" | "reactor-core"
    description: str
    evidence: Dict[str, Any]
    confidence: float  # 0.0-1.0
    stable: bool  # True = met stability criteria
    signal_id: str = field(default_factory=lambda: generate_operation_id("sig"))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def dedup_key(self) -> str:
        """Hash of (repo + sorted files + error signature) for cross-signal dedup."""
        parts = [
            self.repo,
            "|".join(sorted(self.target_files)),
            str(self.evidence.get("signature", "")),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class DedupTracker:
    """Tracks seen signals and rejects duplicates within cooldown window."""

    def __init__(self, cooldown_s: float = 300.0) -> None:
        self._cooldown_s = cooldown_s
        self._seen: Dict[str, float] = {}  # dedup_key -> last_seen_monotonic

    def is_new(self, signal: IntentSignal) -> bool:
        """Return True if signal is new (not a duplicate within cooldown)."""
        now = time.monotonic()
        key = signal.dedup_key
        last = self._seen.get(key)
        if last is not None and (now - last) < self._cooldown_s:
            return False
        self._seen[key] = now
        return True

    def clear(self) -> None:
        self._seen.clear()
```

Also create empty `__init__.py` files for packages.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intent/test_signals.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/__init__.py \
       backend/core/ouroboros/governance/intent/signals.py \
       tests/governance/intent/__init__.py \
       tests/governance/intent/test_signals.py
git commit -m "feat(intent): add IntentSignal dataclass and DedupTracker"
```

---

## Task 2: Rate Limiter (`rate_limiter.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/intent/rate_limiter.py`
- Test: `tests/governance/intent/test_rate_limiter.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/intent/test_rate_limiter.py"""
import pytest
import time


def test_rate_limiter_allows_within_limits():
    from backend.core.ouroboros.governance.intent.rate_limiter import (
        RateLimiter,
        RateLimiterConfig,
    )

    limiter = RateLimiter(RateLimiterConfig(max_ops_per_hour=5, max_ops_per_day=20))
    allowed, reason = limiter.check("tests/test_a.py")
    assert allowed is True
    assert reason == ""


def test_rate_limiter_per_file_cooldown():
    from backend.core.ouroboros.governance.intent.rate_limiter import (
        RateLimiter,
        RateLimiterConfig,
    )

    limiter = RateLimiter(
        RateLimiterConfig(per_file_cooldown_s=600.0)
    )
    allowed1, _ = limiter.check("tests/test_a.py")
    assert allowed1 is True
    limiter.record("tests/test_a.py")

    allowed2, reason = limiter.check("tests/test_a.py")
    assert allowed2 is False
    assert "file_cooldown" in reason


def test_rate_limiter_different_file_not_blocked():
    from backend.core.ouroboros.governance.intent.rate_limiter import (
        RateLimiter,
        RateLimiterConfig,
    )

    limiter = RateLimiter(
        RateLimiterConfig(per_file_cooldown_s=600.0)
    )
    limiter.check("tests/test_a.py")
    limiter.record("tests/test_a.py")

    allowed, _ = limiter.check("tests/test_b.py")
    assert allowed is True


def test_rate_limiter_hourly_cap():
    from backend.core.ouroboros.governance.intent.rate_limiter import (
        RateLimiter,
        RateLimiterConfig,
    )

    limiter = RateLimiter(
        RateLimiterConfig(
            max_ops_per_hour=2,
            max_ops_per_day=100,
            per_file_cooldown_s=0.0,
        )
    )
    for i in range(2):
        limiter.check(f"file_{i}.py")
        limiter.record(f"file_{i}.py")

    allowed, reason = limiter.check("file_new.py")
    assert allowed is False
    assert "hourly_cap" in reason


def test_rate_limiter_daily_cap():
    from backend.core.ouroboros.governance.intent.rate_limiter import (
        RateLimiter,
        RateLimiterConfig,
    )

    limiter = RateLimiter(
        RateLimiterConfig(
            max_ops_per_hour=100,
            max_ops_per_day=3,
            per_file_cooldown_s=0.0,
        )
    )
    for i in range(3):
        limiter.check(f"file_{i}.py")
        limiter.record(f"file_{i}.py")

    allowed, reason = limiter.check("file_new.py")
    assert allowed is False
    assert "daily_cap" in reason


def test_rate_limiter_per_signal_cooldown():
    from backend.core.ouroboros.governance.intent.rate_limiter import (
        RateLimiter,
        RateLimiterConfig,
    )

    limiter = RateLimiter(
        RateLimiterConfig(per_signal_cooldown_s=300.0, per_file_cooldown_s=0.0)
    )
    allowed1, _ = limiter.check("f.py", signal_key="sig_abc")
    assert allowed1 is True
    limiter.record("f.py", signal_key="sig_abc")

    allowed2, reason = limiter.check("f.py", signal_key="sig_abc")
    assert allowed2 is False
    assert "signal_cooldown" in reason


def test_rate_limiter_config_from_env(monkeypatch):
    monkeypatch.setenv("JARVIS_INTENT_MAX_OPS_HOUR", "10")
    monkeypatch.setenv("JARVIS_INTENT_MAX_OPS_DAY", "50")
    monkeypatch.setenv("JARVIS_INTENT_FILE_COOLDOWN_S", "120.0")
    monkeypatch.setenv("JARVIS_INTENT_SIGNAL_COOLDOWN_S", "60.0")

    from backend.core.ouroboros.governance.intent.rate_limiter import (
        RateLimiterConfig,
    )

    cfg = RateLimiterConfig.from_env()
    assert cfg.max_ops_per_hour == 10
    assert cfg.max_ops_per_day == 50
    assert cfg.per_file_cooldown_s == 120.0
    assert cfg.per_signal_cooldown_s == 60.0
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intent/test_rate_limiter.py -v`
Expected: FAIL (module not found)

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/intent/rate_limiter.py

Per-file cooldown, per-signal cooldown, hourly cap, daily cap.
All limits configurable via env vars.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §2 Rate Limiter
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class RateLimiterConfig:
    max_ops_per_hour: int = 5
    max_ops_per_day: int = 20
    per_file_cooldown_s: float = 600.0  # 10 min between ops on same file
    per_signal_cooldown_s: float = 300.0  # 5 min between same signal

    @classmethod
    def from_env(cls) -> RateLimiterConfig:
        return cls(
            max_ops_per_hour=int(os.environ.get("JARVIS_INTENT_MAX_OPS_HOUR", "5")),
            max_ops_per_day=int(os.environ.get("JARVIS_INTENT_MAX_OPS_DAY", "20")),
            per_file_cooldown_s=float(
                os.environ.get("JARVIS_INTENT_FILE_COOLDOWN_S", "600.0")
            ),
            per_signal_cooldown_s=float(
                os.environ.get("JARVIS_INTENT_SIGNAL_COOLDOWN_S", "300.0")
            ),
        )


class RateLimiter:
    """Enforces per-file cooldown, per-signal cooldown, hourly and daily caps."""

    def __init__(self, config: Optional[RateLimiterConfig] = None) -> None:
        self._config = config or RateLimiterConfig()
        self._file_last_op: Dict[str, float] = {}  # file -> monotonic ts
        self._signal_last_op: Dict[str, float] = {}  # signal_key -> monotonic ts
        self._op_timestamps: List[float] = []  # monotonic ts of all recorded ops

    def check(
        self, file_path: str, signal_key: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Check if an operation is allowed. Returns (allowed, reason_code)."""
        now = time.monotonic()

        # Per-file cooldown
        last_file = self._file_last_op.get(file_path)
        if last_file is not None:
            elapsed = now - last_file
            if elapsed < self._config.per_file_cooldown_s:
                return False, "rate_limit:file_cooldown"

        # Per-signal cooldown
        if signal_key is not None:
            last_sig = self._signal_last_op.get(signal_key)
            if last_sig is not None:
                elapsed = now - last_sig
                if elapsed < self._config.per_signal_cooldown_s:
                    return False, "rate_limit:signal_cooldown"

        # Hourly cap
        one_hour_ago = now - 3600.0
        hourly_count = sum(1 for t in self._op_timestamps if t > one_hour_ago)
        if hourly_count >= self._config.max_ops_per_hour:
            return False, "rate_limit:hourly_cap"

        # Daily cap
        one_day_ago = now - 86400.0
        daily_count = sum(1 for t in self._op_timestamps if t > one_day_ago)
        if daily_count >= self._config.max_ops_per_day:
            return False, "rate_limit:daily_cap"

        return True, ""

    def record(self, file_path: str, signal_key: Optional[str] = None) -> None:
        """Record that an operation was executed."""
        now = time.monotonic()
        self._file_last_op[file_path] = now
        if signal_key is not None:
            self._signal_last_op[signal_key] = now
        self._op_timestamps.append(now)
        # Prune old timestamps (older than 24h)
        cutoff = now - 86400.0
        self._op_timestamps = [t for t in self._op_timestamps if t > cutoff]
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intent/test_rate_limiter.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/rate_limiter.py \
       tests/governance/intent/test_rate_limiter.py
git commit -m "feat(intent): add RateLimiter with per-file, per-signal, hourly, and daily caps"
```

---

## Task 3: Test Watcher (`test_watcher.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/intent/test_watcher.py`
- Test: `tests/governance/intent/test_test_watcher.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/intent/test_test_watcher.py"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_parse_pytest_output_detects_failures():
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher

    watcher = TestWatcher(repo="jarvis", test_dir="tests/")
    output = (
        "FAILED tests/test_utils.py::test_edge_case - AssertionError: expected 3, got 2\n"
        "FAILED tests/test_utils.py::test_other - ValueError: bad\n"
        "2 failed, 10 passed in 3.21s\n"
    )
    failures = watcher.parse_pytest_output(output, exit_code=1)
    assert len(failures) == 2
    assert failures[0].test_id == "tests/test_utils.py::test_edge_case"
    assert "AssertionError" in failures[0].error_text
    assert failures[1].test_id == "tests/test_utils.py::test_other"


@pytest.mark.asyncio
async def test_parse_pytest_output_no_failures():
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher

    watcher = TestWatcher(repo="jarvis", test_dir="tests/")
    output = "10 passed in 2.01s\n"
    failures = watcher.parse_pytest_output(output, exit_code=0)
    assert len(failures) == 0


@pytest.mark.asyncio
async def test_stability_requires_two_consecutive_failures():
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher, TestFailure

    watcher = TestWatcher(repo="jarvis", test_dir="tests/")
    failure = TestFailure(
        test_id="tests/test_a.py::test_x",
        file_path="tests/test_a.py",
        error_text="AssertionError",
    )

    # First failure — not stable
    signals = watcher.process_failures([failure])
    assert len(signals) == 0

    # Second consecutive failure — stable
    signals = watcher.process_failures([failure])
    assert len(signals) == 1
    assert signals[0].stable is True
    assert signals[0].source == "intent:test_failure"


@pytest.mark.asyncio
async def test_stability_resets_on_pass():
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher, TestFailure

    watcher = TestWatcher(repo="jarvis", test_dir="tests/")
    failure = TestFailure(
        test_id="tests/test_a.py::test_x",
        file_path="tests/test_a.py",
        error_text="AssertionError",
    )

    # First failure
    watcher.process_failures([failure])

    # Pass (no failures) — resets stability
    watcher.process_failures([])

    # Another failure — not stable (counter reset)
    signals = watcher.process_failures([failure])
    assert len(signals) == 0


@pytest.mark.asyncio
async def test_run_pytest_subprocess(tmp_path):
    """Test that run_pytest calls subprocess and returns output."""
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher

    watcher = TestWatcher(
        repo="jarvis",
        test_dir=str(tmp_path),
        repo_path=str(tmp_path),
    )
    # Create a trivial passing test
    test_file = tmp_path / "test_trivial.py"
    test_file.write_text("def test_pass(): assert True\n")

    output, exit_code = await watcher.run_pytest()
    assert isinstance(output, str)
    assert exit_code == 0


@pytest.mark.asyncio
async def test_extracts_file_path_from_test_id():
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher

    watcher = TestWatcher(repo="jarvis", test_dir="tests/")
    assert watcher.extract_file("tests/test_a.py::test_x") == "tests/test_a.py"
    assert watcher.extract_file("tests/sub/test_b.py::TestClass::test_y") == "tests/sub/test_b.py"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intent/test_test_watcher.py -v`
Expected: FAIL (module not found)

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/intent/test_watcher.py

Pytest result watcher. Polls by running pytest, parses output for failures,
tracks consecutive failure history, and emits stable IntentSignals.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §2 Test Watcher
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .signals import IntentSignal

logger = logging.getLogger(__name__)

_FAIL_PATTERN = re.compile(
    r"^FAILED\s+(\S+)\s+-\s+(.+)$", re.MULTILINE
)


@dataclass
class TestFailure:
    test_id: str  # e.g. "tests/test_utils.py::test_edge_case"
    file_path: str  # e.g. "tests/test_utils.py"
    error_text: str


class TestWatcher:
    """Polls pytest, detects stable failures, emits IntentSignals."""

    def __init__(
        self,
        repo: str,
        test_dir: str = "tests/",
        repo_path: Optional[str] = None,
        poll_interval_s: Optional[float] = None,
        pytest_timeout_s: float = 120.0,
    ) -> None:
        self._repo = repo
        self._test_dir = test_dir
        self._repo_path = repo_path or os.environ.get(
            "JARVIS_REPO_PATH", "."
        )
        self._poll_interval_s = poll_interval_s or float(
            os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300")
        )
        self._pytest_timeout_s = pytest_timeout_s
        # test_id -> consecutive failure count
        self._failure_streak: Dict[str, int] = {}
        self._running = False

    async def run_pytest(self) -> Tuple[str, int]:
        """Run pytest subprocess and return (stdout, exit_code)."""
        cmd = [
            "python3", "-m", "pytest",
            self._test_dir,
            "--tb=short", "-q",
            "--no-header",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._repo_path,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._pytest_timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("pytest timed out after %.0fs", self._pytest_timeout_s)
            return "", -1
        return stdout_bytes.decode(errors="replace"), proc.returncode or 0

    def parse_pytest_output(
        self, output: str, exit_code: int
    ) -> List[TestFailure]:
        """Parse pytest output for FAILED lines."""
        if exit_code == 0:
            return []
        failures: List[TestFailure] = []
        for match in _FAIL_PATTERN.finditer(output):
            test_id = match.group(1)
            error_text = match.group(2).strip()
            file_path = self.extract_file(test_id)
            failures.append(
                TestFailure(
                    test_id=test_id,
                    file_path=file_path,
                    error_text=error_text,
                )
            )
        return failures

    def process_failures(
        self, failures: List[TestFailure]
    ) -> List[IntentSignal]:
        """Update streak counters and emit signals for stable failures.

        A stable failure = same test fails in 2 consecutive runs.
        """
        current_ids = {f.test_id for f in failures}

        # Reset streak for tests that passed this run
        for test_id in list(self._failure_streak):
            if test_id not in current_ids:
                del self._failure_streak[test_id]

        signals: List[IntentSignal] = []
        for f in failures:
            self._failure_streak[f.test_id] = (
                self._failure_streak.get(f.test_id, 0) + 1
            )
            if self._failure_streak[f.test_id] >= 2:
                signals.append(
                    IntentSignal(
                        source="intent:test_failure",
                        target_files=(f.file_path,),
                        repo=self._repo,
                        description=f"{f.test_id} failed {self._failure_streak[f.test_id]}x consecutively",
                        evidence={
                            "test_id": f.test_id,
                            "error_text": f.error_text,
                            "signature": f.error_text,
                            "consecutive_failures": self._failure_streak[f.test_id],
                        },
                        confidence=min(
                            0.95,
                            0.7 + 0.1 * self._failure_streak[f.test_id],
                        ),
                        stable=True,
                    )
                )
        return signals

    @staticmethod
    def extract_file(test_id: str) -> str:
        """Extract file path from pytest node id."""
        return test_id.split("::")[0]

    async def poll_once(self) -> List[IntentSignal]:
        """Run one poll cycle: pytest -> parse -> process -> signals."""
        output, exit_code = await self.run_pytest()
        if exit_code == -1:
            return []  # timeout, skip cycle
        failures = self.parse_pytest_output(output, exit_code)
        return self.process_failures(failures)

    async def start(self) -> None:
        """Long-running poll loop. Call from asyncio.create_task()."""
        self._running = True
        logger.info(
            "TestWatcher started for repo=%s test_dir=%s interval=%.0fs",
            self._repo, self._test_dir, self._poll_interval_s,
        )
        while self._running:
            try:
                signals = await self.poll_once()
                for sig in signals:
                    logger.info("Stable test failure: %s", sig.description)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("TestWatcher poll error")
            await asyncio.sleep(self._poll_interval_s)

    def stop(self) -> None:
        self._running = False
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intent/test_test_watcher.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/test_watcher.py \
       tests/governance/intent/test_test_watcher.py
git commit -m "feat(intent): add TestWatcher with pytest polling and stable failure detection"
```

---

## Task 4: Error Interceptor (`error_interceptor.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/intent/error_interceptor.py`
- Test: `tests/governance/intent/test_error_interceptor.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/intent/test_error_interceptor.py"""
import logging
import pytest


def test_interceptor_captures_error_log():
    from backend.core.ouroboros.governance.intent.error_interceptor import (
        ErrorInterceptor,
    )

    interceptor = ErrorInterceptor(repo="jarvis")
    collected = []
    interceptor.on_signal = collected.append

    test_logger = logging.getLogger("test.interceptor.capture")
    interceptor.install(test_logger)

    test_logger.error("Connection timeout in prime_client.py line 342")

    assert len(collected) == 1
    sig = collected[0]
    assert sig.source == "intent:stack_trace"
    assert sig.stable is False
    assert "Connection timeout" in sig.description

    interceptor.uninstall(test_logger)


def test_interceptor_ignores_warning_and_info():
    from backend.core.ouroboros.governance.intent.error_interceptor import (
        ErrorInterceptor,
    )

    interceptor = ErrorInterceptor(repo="jarvis")
    collected = []
    interceptor.on_signal = collected.append

    test_logger = logging.getLogger("test.interceptor.ignore")
    interceptor.install(test_logger)

    test_logger.warning("This is a warning")
    test_logger.info("This is info")

    assert len(collected) == 0
    interceptor.uninstall(test_logger)


def test_interceptor_captures_critical():
    from backend.core.ouroboros.governance.intent.error_interceptor import (
        ErrorInterceptor,
    )

    interceptor = ErrorInterceptor(repo="jarvis")
    collected = []
    interceptor.on_signal = collected.append

    test_logger = logging.getLogger("test.interceptor.critical")
    interceptor.install(test_logger)

    test_logger.critical("Fatal: database connection lost")

    assert len(collected) == 1
    assert collected[0].confidence > 0.8  # critical = higher confidence
    interceptor.uninstall(test_logger)


def test_interceptor_extracts_file_from_record():
    from backend.core.ouroboros.governance.intent.error_interceptor import (
        ErrorInterceptor,
    )

    interceptor = ErrorInterceptor(repo="jarvis")
    collected = []
    interceptor.on_signal = collected.append

    test_logger = logging.getLogger("test.interceptor.file")
    interceptor.install(test_logger)

    test_logger.error("Something broke")

    assert len(collected) == 1
    # target_files should contain the source file from the log record
    assert len(collected[0].target_files) >= 1
    interceptor.uninstall(test_logger)


def test_interceptor_extracts_traceback():
    from backend.core.ouroboros.governance.intent.error_interceptor import (
        ErrorInterceptor,
    )

    interceptor = ErrorInterceptor(repo="jarvis")
    collected = []
    interceptor.on_signal = collected.append

    test_logger = logging.getLogger("test.interceptor.tb")
    interceptor.install(test_logger)

    try:
        raise ValueError("test error for interceptor")
    except ValueError:
        test_logger.exception("Caught an error")

    assert len(collected) == 1
    assert "traceback" in collected[0].evidence
    interceptor.uninstall(test_logger)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intent/test_error_interceptor.py -v`
Expected: FAIL (module not found)

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/intent/error_interceptor.py

Logging handler that intercepts ERROR/CRITICAL records and emits
observe-only IntentSignals (Phase 1.5 -- no auto-submit).

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §2 Error Interceptor
"""
from __future__ import annotations

import logging
import traceback as tb_module
from typing import Callable, Optional

from .signals import IntentSignal


class _InterceptHandler(logging.Handler):
    """Logging handler that forwards ERROR+ records to ErrorInterceptor."""

    def __init__(self, interceptor: ErrorInterceptor) -> None:
        super().__init__(level=logging.ERROR)
        self._interceptor = interceptor

    def emit(self, record: logging.LogRecord) -> None:
        self._interceptor._handle_record(record)


class ErrorInterceptor:
    """Captures ERROR/CRITICAL log records and emits observe-only IntentSignals."""

    def __init__(self, repo: str = "jarvis") -> None:
        self._repo = repo
        self._handler: Optional[_InterceptHandler] = None
        self.on_signal: Optional[Callable[[IntentSignal], None]] = None

    def install(self, logger: logging.Logger) -> None:
        """Attach the intercept handler to a logger."""
        self._handler = _InterceptHandler(self)
        logger.addHandler(self._handler)

    def uninstall(self, logger: logging.Logger) -> None:
        """Remove the intercept handler from a logger."""
        if self._handler is not None:
            logger.removeHandler(self._handler)
            self._handler = None

    def _handle_record(self, record: logging.LogRecord) -> None:
        """Process a single ERROR/CRITICAL log record."""
        if self.on_signal is None:
            return

        # Extract file info from the log record
        source_file = record.pathname or "unknown"
        line_no = record.lineno

        # Build evidence
        evidence: dict = {
            "logger_name": record.name,
            "level": record.levelname,
            "source_file": source_file,
            "line_no": line_no,
            "signature": record.getMessage()[:200],
        }

        # Capture traceback if present
        if record.exc_info and record.exc_info[1] is not None:
            tb_text = "".join(
                tb_module.format_exception(*record.exc_info)
            )
            evidence["traceback"] = tb_text

        # Higher confidence for CRITICAL
        confidence = 0.7 if record.levelno == logging.ERROR else 0.85

        target_files = (source_file,)

        signal = IntentSignal(
            source="intent:stack_trace",
            target_files=target_files,
            repo=self._repo,
            description=record.getMessage()[:500],
            evidence=evidence,
            confidence=confidence,
            stable=False,  # observe-only, not auto-submitted
        )
        self.on_signal(signal)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intent/test_error_interceptor.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/error_interceptor.py \
       tests/governance/intent/test_error_interceptor.py
git commit -m "feat(intent): add ErrorInterceptor for observe-only stack trace signals"
```

---

## Task 5: IntentEngine (`engine.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/intent/engine.py`
- Test: `tests/governance/intent/test_engine.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/intent/test_engine.py"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


@pytest.fixture
def mock_governed_loop_service():
    svc = AsyncMock()
    svc.submit = AsyncMock(return_value=MagicMock(
        op_id="op-001",
        terminal_phase="COMPLETE",
    ))
    return svc


@pytest.fixture
def intent_engine_config():
    from backend.core.ouroboros.governance.intent.engine import IntentEngineConfig
    return IntentEngineConfig(
        repos={"jarvis": "."},
        test_dirs={"jarvis": "tests/"},
        poll_interval_s=0.1,  # fast for tests
    )


@pytest.mark.asyncio
async def test_engine_lifecycle(intent_engine_config, mock_governed_loop_service):
    from backend.core.ouroboros.governance.intent.engine import IntentEngine

    engine = IntentEngine(
        config=intent_engine_config,
        governed_loop_service=mock_governed_loop_service,
    )
    assert engine.state == "inactive"

    await engine.start()
    assert engine.state == "watching"

    engine.stop()
    await asyncio.sleep(0.05)
    assert engine.state == "inactive"


@pytest.mark.asyncio
async def test_engine_routes_stable_test_failure_to_submit(
    intent_engine_config, mock_governed_loop_service
):
    from backend.core.ouroboros.governance.intent.engine import IntentEngine
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    engine = IntentEngine(
        config=intent_engine_config,
        governed_loop_service=mock_governed_loop_service,
    )

    signal = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_a.py",),
        repo="jarvis",
        description="test failed 2x",
        evidence={"signature": "AssertionError"},
        confidence=0.9,
        stable=True,
    )

    result = await engine.handle_signal(signal)
    assert result == "submitted"
    mock_governed_loop_service.submit.assert_called_once()


@pytest.mark.asyncio
async def test_engine_routes_observe_only_to_narrate(
    intent_engine_config, mock_governed_loop_service
):
    from backend.core.ouroboros.governance.intent.engine import IntentEngine
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    engine = IntentEngine(
        config=intent_engine_config,
        governed_loop_service=mock_governed_loop_service,
    )

    signal = IntentSignal(
        source="intent:stack_trace",
        target_files=("src/prime_client.py",),
        repo="jarvis",
        description="Connection timeout",
        evidence={"signature": "TimeoutError"},
        confidence=0.7,
        stable=False,  # observe-only
    )

    result = await engine.handle_signal(signal)
    assert result == "observed"
    mock_governed_loop_service.submit.assert_not_called()


@pytest.mark.asyncio
async def test_engine_rejects_duplicate(
    intent_engine_config, mock_governed_loop_service
):
    from backend.core.ouroboros.governance.intent.engine import IntentEngine
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    engine = IntentEngine(
        config=intent_engine_config,
        governed_loop_service=mock_governed_loop_service,
    )

    signal = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_a.py",),
        repo="jarvis",
        description="test failed",
        evidence={"signature": "err"},
        confidence=0.9,
        stable=True,
    )

    result1 = await engine.handle_signal(signal)
    assert result1 == "submitted"

    result2 = await engine.handle_signal(signal)
    assert result2 == "deduplicated"


@pytest.mark.asyncio
async def test_engine_rejects_rate_limited(
    mock_governed_loop_service,
):
    from backend.core.ouroboros.governance.intent.engine import (
        IntentEngine,
        IntentEngineConfig,
    )
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    config = IntentEngineConfig(
        repos={"jarvis": "."},
        test_dirs={"jarvis": "tests/"},
        max_ops_per_hour=1,
        max_ops_per_day=1,
        file_cooldown_s=0.0,
        signal_cooldown_s=0.0,
        dedup_cooldown_s=0.0,
    )
    engine = IntentEngine(
        config=config,
        governed_loop_service=mock_governed_loop_service,
    )

    sig1 = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_a.py",),
        repo="jarvis",
        description="fail 1",
        evidence={"signature": "err1"},
        confidence=0.9,
        stable=True,
    )
    sig2 = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_b.py",),
        repo="jarvis",
        description="fail 2",
        evidence={"signature": "err2"},
        confidence=0.9,
        stable=True,
    )

    result1 = await engine.handle_signal(sig1)
    assert result1 == "submitted"

    result2 = await engine.handle_signal(sig2)
    assert result2 == "rate_limited"


@pytest.mark.asyncio
async def test_engine_builds_operation_context_correctly(
    intent_engine_config, mock_governed_loop_service
):
    from backend.core.ouroboros.governance.intent.engine import IntentEngine
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    engine = IntentEngine(
        config=intent_engine_config,
        governed_loop_service=mock_governed_loop_service,
    )

    signal = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_a.py",),
        repo="jarvis",
        description="test_edge_case failed 2x",
        evidence={"signature": "AssertionError", "test_id": "tests/test_a.py::test_edge_case"},
        confidence=0.9,
        stable=True,
    )

    await engine.handle_signal(signal)

    call_args = mock_governed_loop_service.submit.call_args
    ctx = call_args[0][0] if call_args[0] else call_args[1].get("ctx")
    assert ctx is not None
    assert "tests/test_a.py" in ctx.target_files
    # trigger_source kwarg
    trigger_source = call_args[1].get("trigger_source", call_args[0][1] if len(call_args[0]) > 1 else None)
    assert trigger_source == "intent:test_failure"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intent/test_engine.py -v`
Expected: FAIL (module not found)

**Step 3: Write minimal implementation**

```python
"""backend/core/ouroboros/governance/intent/engine.py

IntentEngine -- central orchestrator for the intent detection layer.
Routes signals through dedup -> rate limit -> autonomy gate ->
GovernedLoopService.submit() or voice narration.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §2
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .signals import IntentSignal, DedupTracker
from .rate_limiter import RateLimiter, RateLimiterConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentEngineConfig:
    repos: Dict[str, str]  # repo_name -> repo_path
    test_dirs: Dict[str, str]  # repo_name -> test directory
    poll_interval_s: float = 300.0
    dedup_cooldown_s: float = 300.0
    max_ops_per_hour: int = 5
    max_ops_per_day: int = 20
    file_cooldown_s: float = 600.0
    signal_cooldown_s: float = 300.0

    @classmethod
    def from_env(cls) -> IntentEngineConfig:
        repos: Dict[str, str] = {}
        test_dirs: Dict[str, str] = {}

        jarvis_path = os.environ.get("JARVIS_REPO_PATH", ".")
        repos["jarvis"] = jarvis_path
        test_dirs["jarvis"] = "tests/"

        prime_path = os.environ.get("JARVIS_PRIME_REPO_PATH")
        if prime_path:
            repos["prime"] = prime_path
            test_dirs["prime"] = "tests/"

        reactor_path = os.environ.get("JARVIS_REACTOR_REPO_PATH")
        if reactor_path:
            repos["reactor-core"] = reactor_path
            test_dirs["reactor-core"] = "tests/"

        return cls(
            repos=repos,
            test_dirs=test_dirs,
            poll_interval_s=float(
                os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300")
            ),
            dedup_cooldown_s=float(
                os.environ.get("JARVIS_INTENT_DEDUP_COOLDOWN_S", "300")
            ),
            max_ops_per_hour=int(
                os.environ.get("JARVIS_INTENT_MAX_OPS_HOUR", "5")
            ),
            max_ops_per_day=int(
                os.environ.get("JARVIS_INTENT_MAX_OPS_DAY", "20")
            ),
            file_cooldown_s=float(
                os.environ.get("JARVIS_INTENT_FILE_COOLDOWN_S", "600")
            ),
            signal_cooldown_s=float(
                os.environ.get("JARVIS_INTENT_SIGNAL_COOLDOWN_S", "300")
            ),
        )


def _build_operation_context(signal: IntentSignal) -> Any:
    """Build an OperationContext from an IntentSignal.

    Imports lazily to avoid circular dependency at module load.
    """
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.operation_id import generate_operation_id

    return OperationContext(
        op_id=generate_operation_id("intent"),
        created_at=datetime.now(timezone.utc),
        phase="classify",
        phase_entered_at=datetime.now(timezone.utc),
        context_hash="",
        previous_hash=None,
        target_files=signal.target_files,
        description=signal.description,
    )


class IntentEngine:
    """Long-running async service that orchestrates intent detection.

    States: inactive -> watching -> active (processing signal) -> watching
    """

    def __init__(
        self,
        config: IntentEngineConfig,
        governed_loop_service: Any,
        narrate_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config = config
        self._gls = governed_loop_service
        self._narrate = narrate_fn
        self._state = "inactive"
        self._dedup = DedupTracker(cooldown_s=config.dedup_cooldown_s)
        self._rate_limiter = RateLimiter(
            RateLimiterConfig(
                max_ops_per_hour=config.max_ops_per_hour,
                max_ops_per_day=config.max_ops_per_day,
                per_file_cooldown_s=config.file_cooldown_s,
                per_signal_cooldown_s=config.signal_cooldown_s,
            )
        )
        self._watchers: Dict[str, Any] = {}
        self._tasks: List[asyncio.Task] = []

    @property
    def state(self) -> str:
        return self._state

    async def start(self) -> None:
        """Start the engine and all watchers."""
        if self._state != "inactive":
            return
        self._state = "watching"

        from .test_watcher import TestWatcher

        for repo, repo_path in self._config.repos.items():
            test_dir = self._config.test_dirs.get(repo, "tests/")
            watcher = TestWatcher(
                repo=repo,
                test_dir=test_dir,
                repo_path=repo_path,
                poll_interval_s=self._config.poll_interval_s,
            )
            self._watchers[repo] = watcher

        logger.info("IntentEngine started with repos=%s", list(self._config.repos))

    def stop(self) -> None:
        """Stop the engine and all watchers."""
        for watcher in self._watchers.values():
            watcher.stop()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._watchers.clear()
        self._state = "inactive"
        logger.info("IntentEngine stopped")

    async def handle_signal(self, signal: IntentSignal) -> str:
        """Process a single signal through the pipeline.

        Returns one of:
            "submitted"    -- sent to GovernedLoopService
            "observed"     -- narrated only (observe-only mode)
            "deduplicated" -- blocked by dedup
            "rate_limited" -- blocked by rate limiter
        """
        # 1. Dedup check
        if not self._dedup.is_new(signal):
            logger.debug("Signal deduplicated: %s", signal.signal_id)
            return "deduplicated"

        # 2. Rate limit check
        primary_file = signal.target_files[0] if signal.target_files else ""
        allowed, reason = self._rate_limiter.check(
            primary_file, signal_key=signal.dedup_key
        )
        if not allowed:
            logger.info("Signal rate-limited: %s reason=%s", signal.signal_id, reason)
            return "rate_limited"

        # 3. Mode check: auto-submit vs observe-only
        if signal.source == "intent:test_failure" and signal.stable:
            # Auto-submit to governed pipeline
            prev_state = self._state
            self._state = "active"
            try:
                ctx = _build_operation_context(signal)
                await self._gls.submit(ctx, trigger_source=signal.source)
                self._rate_limiter.record(primary_file, signal_key=signal.dedup_key)
                logger.info(
                    "Signal submitted: %s -> op %s",
                    signal.signal_id, ctx.op_id,
                )
                return "submitted"
            except Exception:
                logger.exception("Failed to submit signal %s", signal.signal_id)
                return "observed"
            finally:
                self._state = prev_state
        else:
            # Observe-only: narrate via voice if available
            if self._narrate is not None:
                try:
                    msg = (
                        f"I'm seeing errors in {signal.target_files[0]} "
                        f"-- {signal.description[:100]}. Want me to investigate?"
                    )
                    await self._narrate(msg, source="intent_engine")
                except Exception:
                    logger.debug("Narration failed for signal %s", signal.signal_id)
            logger.info("Signal observed: %s", signal.signal_id)
            return "observed"

    async def poll_all(self) -> List[str]:
        """Run one poll cycle across all watchers. Returns list of outcomes."""
        outcomes: List[str] = []
        for repo, watcher in self._watchers.items():
            try:
                signals = await watcher.poll_once()
                for sig in signals:
                    outcome = await self.handle_signal(sig)
                    outcomes.append(outcome)
            except Exception:
                logger.exception("Poll error for repo %s", repo)
        return outcomes
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intent/test_engine.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/engine.py \
       tests/governance/intent/test_engine.py
git commit -m "feat(intent): add IntentEngine orchestrator with dedup, rate limiting, and signal routing"
```

---

## Task 6: Package Exports (`__init__.py`)

**Files:**
- Modify: `backend/core/ouroboros/governance/intent/__init__.py`
- Modify: `backend/core/ouroboros/governance/__init__.py` (add intent exports)
- Test: `tests/governance/intent/test_exports.py`

**Step 1: Write the failing test**

```python
"""tests/governance/intent/test_exports.py"""

def test_intent_public_api():
    from backend.core.ouroboros.governance.intent import (
        IntentSignal,
        DedupTracker,
        RateLimiter,
        RateLimiterConfig,
        TestWatcher,
        ErrorInterceptor,
        IntentEngine,
        IntentEngineConfig,
    )
    assert IntentSignal is not None
    assert IntentEngine is not None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/intent/test_exports.py -v`
Expected: FAIL (cannot import)

**Step 3: Write implementation**

Update `backend/core/ouroboros/governance/intent/__init__.py`:

```python
"""backend/core/ouroboros/governance/intent/__init__.py

Public API for the intent detection layer.
"""
from .signals import IntentSignal, DedupTracker
from .rate_limiter import RateLimiter, RateLimiterConfig
from .test_watcher import TestWatcher, TestFailure
from .error_interceptor import ErrorInterceptor
from .engine import IntentEngine, IntentEngineConfig

__all__ = [
    "IntentSignal",
    "DedupTracker",
    "RateLimiter",
    "RateLimiterConfig",
    "TestWatcher",
    "TestFailure",
    "ErrorInterceptor",
    "IntentEngine",
    "IntentEngineConfig",
]
```

Then append intent exports to `backend/core/ouroboros/governance/__init__.py` near the end:

```python
# --- Intent Engine (Layer 1) ---
from .intent import (
    IntentSignal,
    DedupTracker,
    RateLimiter,
    RateLimiterConfig,
    TestWatcher,
    TestFailure,
    ErrorInterceptor,
    IntentEngine,
    IntentEngineConfig,
)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intent/test_exports.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/__init__.py \
       backend/core/ouroboros/governance/__init__.py \
       tests/governance/intent/test_exports.py
git commit -m "feat(intent): export public API from intent package"
```

---

## Task 7: Integration Test -- End-to-End Signal Flow

**Files:**
- Create: `tests/governance/intent/test_e2e_intent.py`

**Step 1: Write the integration test**

```python
"""tests/governance/intent/test_e2e_intent.py

End-to-end: TestWatcher detects stable failure -> IntentEngine routes to
GovernedLoopService.submit() with correct OperationContext.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_e2e_stable_failure_submits_to_governed_pipeline():
    from backend.core.ouroboros.governance.intent.engine import (
        IntentEngine,
        IntentEngineConfig,
    )
    from backend.core.ouroboros.governance.intent.test_watcher import (
        TestWatcher,
        TestFailure,
    )

    mock_gls = AsyncMock()
    mock_gls.submit = AsyncMock(return_value=MagicMock(
        op_id="op-e2e-001",
        terminal_phase="COMPLETE",
    ))

    config = IntentEngineConfig(
        repos={"jarvis": "."},
        test_dirs={"jarvis": "tests/"},
        dedup_cooldown_s=0.0,
        file_cooldown_s=0.0,
        signal_cooldown_s=0.0,
    )
    engine = IntentEngine(config=config, governed_loop_service=mock_gls)
    await engine.start()

    # Simulate what TestWatcher.process_failures does
    watcher = engine._watchers["jarvis"]
    failure = TestFailure(
        test_id="tests/test_utils.py::test_edge_case",
        file_path="tests/test_utils.py",
        error_text="AssertionError: expected 3, got 2",
    )

    # First failure -- not stable yet
    signals_1 = watcher.process_failures([failure])
    assert len(signals_1) == 0

    # Second failure -- stable, emits signal
    signals_2 = watcher.process_failures([failure])
    assert len(signals_2) == 1
    assert signals_2[0].stable is True

    # Route through engine
    result = await engine.handle_signal(signals_2[0])
    assert result == "submitted"

    # Verify GLS was called correctly
    mock_gls.submit.assert_called_once()
    call_args = mock_gls.submit.call_args
    ctx = call_args[0][0]
    assert "tests/test_utils.py" in ctx.target_files
    assert call_args[1]["trigger_source"] == "intent:test_failure"

    engine.stop()


@pytest.mark.asyncio
async def test_e2e_observe_only_stack_trace():
    from backend.core.ouroboros.governance.intent.engine import (
        IntentEngine,
        IntentEngineConfig,
    )
    from backend.core.ouroboros.governance.intent.error_interceptor import (
        ErrorInterceptor,
    )
    import logging

    mock_gls = AsyncMock()
    narrated: list = []

    async def mock_narrate(text, source=""):
        narrated.append(text)

    config = IntentEngineConfig(
        repos={"jarvis": "."},
        test_dirs={"jarvis": "tests/"},
        dedup_cooldown_s=0.0,
    )
    engine = IntentEngine(
        config=config,
        governed_loop_service=mock_gls,
        narrate_fn=mock_narrate,
    )

    interceptor = ErrorInterceptor(repo="jarvis")
    collected_signals = []
    interceptor.on_signal = collected_signals.append

    test_logger = logging.getLogger("test.e2e.stack_trace")
    interceptor.install(test_logger)

    test_logger.error("Connection timeout in prime_client.py line 342")

    assert len(collected_signals) == 1
    sig = collected_signals[0]

    result = await engine.handle_signal(sig)
    assert result == "observed"
    assert len(narrated) == 1
    assert "errors" in narrated[0].lower() or "seeing" in narrated[0].lower()
    mock_gls.submit.assert_not_called()

    interceptor.uninstall(test_logger)


@pytest.mark.asyncio
async def test_e2e_dedup_blocks_repeated_signals():
    from backend.core.ouroboros.governance.intent.engine import (
        IntentEngine,
        IntentEngineConfig,
    )
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    mock_gls = AsyncMock()
    mock_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-001"))

    config = IntentEngineConfig(
        repos={"jarvis": "."},
        test_dirs={"jarvis": "tests/"},
        dedup_cooldown_s=300.0,  # long cooldown
        file_cooldown_s=0.0,
        signal_cooldown_s=0.0,
    )
    engine = IntentEngine(config=config, governed_loop_service=mock_gls)

    sig = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_a.py",),
        repo="jarvis",
        description="fail",
        evidence={"signature": "err"},
        confidence=0.9,
        stable=True,
    )

    r1 = await engine.handle_signal(sig)
    r2 = await engine.handle_signal(sig)

    assert r1 == "submitted"
    assert r2 == "deduplicated"
    assert mock_gls.submit.call_count == 1
```

**Step 2: No new implementation -- integration test for existing code**

**Step 3: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intent/test_e2e_intent.py -v`
Expected: ALL PASS (after Tasks 1-6 are complete)

**Step 4: Commit**

```bash
git add tests/governance/intent/test_e2e_intent.py
git commit -m "test(intent): add end-to-end integration tests for intent engine pipeline"
```

---

## Task 8: Run Full Test Suite and Verify

**Step 1: Run all intent engine tests**

Run: `python3 -m pytest tests/governance/intent/ -v --tb=short`
Expected: ALL PASS

**Step 2: Run existing governance tests to verify no regressions**

Run: `python3 -m pytest tests/governance/ -v --tb=short`
Expected: ALL PASS (no regressions)

**Step 3: Verify imports work cleanly**

Run: `python3 -c "from backend.core.ouroboros.governance.intent import IntentEngine, IntentSignal, TestWatcher, ErrorInterceptor, RateLimiter; print('All imports OK')"`
Expected: "All imports OK"

---

## Summary

| Task | Module | Tests | Purpose |
|------|--------|-------|---------|
| 1 | `signals.py` | 7 | IntentSignal dataclass + DedupTracker |
| 2 | `rate_limiter.py` | 7 | Per-file, per-signal, hourly, daily limits |
| 3 | `test_watcher.py` | 6 | Pytest polling + stable failure detection |
| 4 | `error_interceptor.py` | 5 | Logger handler for observe-only signals |
| 5 | `engine.py` | 6 | Central orchestrator with routing logic |
| 6 | `__init__.py` | 1 | Package exports |
| 7 | E2E tests | 3 | End-to-end signal flow verification |
| 8 | Suite run | -- | Regression check |

**Total: 35 tests across 8 tasks, 6 new source files.**
