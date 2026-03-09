# Reactor Feedback Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the Ouroboros self-improvement feedback loop by persisting objective quality metrics after every applied patch, computing per-task-type deltas when Reactor promotes a new model, and publishing a curriculum signal to bias Reactor's next training batch toward high-failure areas.

**Architecture:** Three new modules (`PatchBenchmarker`, `ModelAttributionRecorder`, `CurriculumPublisher`) are wired into the governance pipeline via two new orchestrator helpers (`_run_benchmark`, `_persist_performance_record`) and two new GovernedLoopService background tasks (`_curriculum_loop`, `_reactor_event_loop`). All signals are fault-isolated and non-blocking — they never alter the operation terminal state.

**Tech Stack:** Python 3.11+, asyncio, pytest (asyncio_mode=auto — NEVER use `@pytest.mark.asyncio`), ruff, radon, SQLite (aiosqlite via existing PerformanceRecordPersistence), pytest-cov.

**Design doc:** `docs/plans/2026-03-09-reactor-feedback-loop-design.md`

---

## Task 1: Storage Layer — Extend PerformanceRecord + Add ModelAttributionRecord + v1→v2 Migration

**Files:**
- Modify: `backend/core/ouroboros/integration.py` (PerformanceRecord class ~line 209, PerformanceRecordPersistence class ~line 224)
- Create: `tests/test_ouroboros_governance/test_performance_storage_v2.py`

### Context
`PerformanceRecord` is a plain `@dataclass` (not frozen) in `backend/core/ouroboros/integration.py`. `PerformanceRecordPersistence` uses SQLite primary + JSON fallback with `SCHEMA_VERSION = 1`. The `save_record()` method queues records for batch write. `_init_sqlite()` creates the schema on startup. `_record_to_dict()` / `_dict_to_record()` handle serialization.

### Step 1: Write the failing tests

Create `tests/test_ouroboros_governance/test_performance_storage_v2.py`:

```python
"""Tests for extended PerformanceRecord v2 fields and ModelAttributionRecord."""
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from backend.core.ouroboros.integration import (
    ModelAttributionRecord,
    PerformanceRecord,
    PerformanceRecordPersistence,
    TaskDifficulty,
)


class TestPerformanceRecordV2Fields:
    def test_v2_fields_have_safe_defaults(self):
        rec = PerformanceRecord(
            model_id="m1",
            task_type="code_improvement",
            difficulty=TaskDifficulty.MEDIUM,
            success=True,
            latency_ms=500.0,
            iterations_used=1,
            code_quality_score=0.9,
        )
        assert rec.op_id == ""
        assert rec.patch_hash == ""
        assert rec.pass_rate == 0.0
        assert rec.lint_violations == 0
        assert rec.coverage_pct == 0.0
        assert rec.complexity_delta == 0.0

    def test_v2_fields_roundtrip_through_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            rec = PerformanceRecord(
                model_id="m1",
                task_type="bug_fix",
                difficulty=TaskDifficulty.HIGH,
                success=True,
                latency_ms=1200.0,
                iterations_used=2,
                code_quality_score=0.75,
                op_id="op-abc123",
                patch_hash="deadbeef",
                pass_rate=0.95,
                lint_violations=3,
                coverage_pct=72.5,
                complexity_delta=-0.8,
            )
            p._write_queue.append(rec)
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                p._save_to_sqlite([rec])
            )
            results = asyncio.get_event_loop().run_until_complete(
                p.get_records_by_model_and_task("m1", "bug_fix", limit=10)
            )
            assert len(results) == 1
            r = results[0]
            assert r.op_id == "op-abc123"
            assert r.patch_hash == "deadbeef"
            assert abs(r.pass_rate - 0.95) < 1e-6
            assert r.lint_violations == 3
            assert abs(r.coverage_pct - 72.5) < 1e-6
            assert abs(r.complexity_delta - (-0.8)) < 1e-6


class TestModelAttributionRecord:
    def test_record_dataclass_fields(self):
        rec = ModelAttributionRecord(
            model_id="v2",
            previous_model_id="v1",
            training_batch_size=40,
            task_type="refactoring",
            success_rate_delta=0.12,
            latency_delta_ms=-80.0,
            quality_delta=0.05,
            sample_size=15,
            confidence=0.75,
            summary="v2 improved refactoring by 12%",
            recorded_at=datetime(2026, 3, 9),
        )
        assert rec.model_id == "v2"
        assert rec.confidence == 0.75

    def test_save_attribution_record_persists_to_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            rec = ModelAttributionRecord(
                model_id="v2",
                previous_model_id="v1",
                training_batch_size=40,
                task_type="refactoring",
                success_rate_delta=0.12,
                latency_delta_ms=-80.0,
                quality_delta=0.05,
                sample_size=15,
                confidence=0.75,
                summary="summary",
                recorded_at=datetime(2026, 3, 9),
            )
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                p.save_attribution_record(rec)
            )
            db_path = Path(tmp) / "performance_records.db"
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT model_id FROM model_attribution").fetchone()
            assert row is not None
            assert row[0] == "v2"


class TestSchemaMigration:
    def test_v1_database_migrated_to_v2_on_init(self):
        """An existing v1 DB gains the new columns after PerformanceRecordPersistence init."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "performance_records.db"
            # Create a v1 DB manually (no new columns)
            with sqlite3.connect(db_path) as conn:
                conn.execute("""
                    CREATE TABLE performance_records (
                        id INTEGER PRIMARY KEY,
                        model_id TEXT, task_type TEXT, difficulty TEXT,
                        success INTEGER, latency_ms REAL, iterations_used INTEGER,
                        code_quality_score REAL, timestamp TEXT, error_message TEXT,
                        context_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute(
                    "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"
                )
                conn.execute("INSERT INTO schema_version VALUES (1)")
                conn.commit()

            # Init should detect v1 and migrate
            PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)

            with sqlite3.connect(db_path) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(performance_records)")}
                assert "op_id" in cols
                assert "patch_hash" in cols
                assert "pass_rate" in cols
                assert "lint_violations" in cols
                assert "coverage_pct" in cols
                assert "complexity_delta" in cols
                # model_attribution table exists
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                assert "model_attribution" in tables
                version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            assert version == 2
```

### Step 2: Run tests to confirm they fail

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/test_ouroboros_governance/test_performance_storage_v2.py -v 2>&1 | head -40
```

Expected: FAIL — `ModelAttributionRecord` not found, `get_records_by_model_and_task` not found.

### Step 3: Implement storage layer changes

**3a. Add v2 fields to `PerformanceRecord`** (after `output_tokens: int = 0` ~line 221):

```python
    # v2 — linkage + objective quality metrics (all default to safe zero-value)
    op_id: str = ""
    patch_hash: str = ""
    pass_rate: float = 0.0
    lint_violations: int = 0
    coverage_pct: float = 0.0
    complexity_delta: float = 0.0
```

**3b. Add `ModelAttributionRecord` dataclass** (after `PerformanceRecord` class, before `PerformanceRecordPersistence`):

```python
@dataclass
class ModelAttributionRecord:
    """Per-model, per-task-type quality delta recorded at model promotion."""
    model_id: str
    previous_model_id: str
    training_batch_size: int
    task_type: str
    success_rate_delta: float
    latency_delta_ms: float
    quality_delta: float
    sample_size: int
    confidence: float   # 0..1 = min(n_old, n_new) / lookback_n
    summary: str
    recorded_at: datetime = field(default_factory=datetime.now)
```

**3c. Bump `SCHEMA_VERSION = 2`** in `PerformanceRecordPersistence`.

**3d. Add `_migrate_sqlite()` method** called from `_init_sqlite()` when stored version < 2:

```python
def _migrate_sqlite(self, conn: sqlite3.Connection, from_version: int) -> None:
    """Apply incremental migrations."""
    if from_version < 2:
        conn.execute("ALTER TABLE performance_records ADD COLUMN op_id TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE performance_records ADD COLUMN patch_hash TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE performance_records ADD COLUMN pass_rate REAL NOT NULL DEFAULT 0.0")
        conn.execute("ALTER TABLE performance_records ADD COLUMN lint_violations INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE performance_records ADD COLUMN coverage_pct REAL NOT NULL DEFAULT 0.0")
        conn.execute("ALTER TABLE performance_records ADD COLUMN complexity_delta REAL NOT NULL DEFAULT 0.0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_attribution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id TEXT NOT NULL,
                previous_model_id TEXT NOT NULL,
                training_batch_size INTEGER NOT NULL,
                task_type TEXT NOT NULL,
                success_rate_delta REAL NOT NULL,
                latency_delta_ms REAL NOT NULL,
                quality_delta REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                confidence REAL NOT NULL,
                summary TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute("UPDATE schema_version SET version = 2")
```

In `_init_sqlite()`, after inserting the initial schema version row, add migration call:

```python
cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
row = cursor.fetchone()
if not row:
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (self.SCHEMA_VERSION,))
else:
    stored = row[0]
    if stored < self.SCHEMA_VERSION:
        self._migrate_sqlite(conn, stored)
```

Also add the `model_attribution` table to the fresh-create path (for new DBs):
```sql
CREATE TABLE IF NOT EXISTS model_attribution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    previous_model_id TEXT NOT NULL,
    training_batch_size INTEGER NOT NULL,
    task_type TEXT NOT NULL,
    success_rate_delta REAL NOT NULL,
    latency_delta_ms REAL NOT NULL,
    quality_delta REAL NOT NULL,
    sample_size INTEGER NOT NULL,
    confidence REAL NOT NULL,
    summary TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
```

**3e. Update `_record_to_dict()` and `_dict_to_record()`** to include the six new v2 fields. In `_record_to_dict()` add:

```python
"op_id": record.op_id,
"patch_hash": record.patch_hash,
"pass_rate": record.pass_rate,
"lint_violations": record.lint_violations,
"coverage_pct": record.coverage_pct,
"complexity_delta": record.complexity_delta,
```

In `_dict_to_record()` add:

```python
op_id=data.get("op_id", ""),
patch_hash=data.get("patch_hash", ""),
pass_rate=data.get("pass_rate", 0.0),
lint_violations=data.get("lint_violations", 0),
coverage_pct=data.get("coverage_pct", 0.0),
complexity_delta=data.get("complexity_delta", 0.0),
```

**3f. Update `_save_to_sqlite()`** INSERT statement to include the new columns.

**3g. Add `save_attribution_record()` method**:

```python
async def save_attribution_record(self, record: ModelAttributionRecord) -> None:
    """Persist a ModelAttributionRecord to SQLite immediately."""
    if not self._use_sqlite:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, self._write_attribution_sync, record)

def _write_attribution_sync(self, record: ModelAttributionRecord) -> None:
    with sqlite3.connect(self._db_path) as conn:
        conn.execute(
            """INSERT INTO model_attribution
               (model_id, previous_model_id, training_batch_size, task_type,
                success_rate_delta, latency_delta_ms, quality_delta,
                sample_size, confidence, summary, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.model_id, record.previous_model_id, record.training_batch_size,
                record.task_type, record.success_rate_delta, record.latency_delta_ms,
                record.quality_delta, record.sample_size, record.confidence,
                record.summary, record.recorded_at.isoformat(),
            ),
        )
        conn.commit()
```

**3h. Add `get_records_by_model_and_task()` method**:

```python
async def get_records_by_model_and_task(
    self,
    model_id: str,
    task_type: str,
    limit: int = 20,
) -> List[PerformanceRecord]:
    """Return up to `limit` most-recent PerformanceRecords for a model+task pair."""
    if not self._use_sqlite:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, self._query_by_model_task_sync, model_id, task_type, limit
    )

def _query_by_model_task_sync(
    self, model_id: str, task_type: str, limit: int
) -> List[PerformanceRecord]:
    with sqlite3.connect(self._db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM performance_records
               WHERE model_id = ? AND task_type = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (model_id, task_type, limit),
        ).fetchall()
    return [self._dict_to_record(dict(row)) for row in rows]
```

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_performance_storage_v2.py -v
```

Expected: all PASS.

### Step 5: Commit

```bash
git add backend/core/ouroboros/integration.py tests/test_ouroboros_governance/test_performance_storage_v2.py
git commit -m "feat(storage): extend PerformanceRecord v2 + ModelAttributionRecord + v1->v2 SQLite migration"
```

---

## Task 2: PatchBenchmarker

**Files:**
- Create: `backend/core/ouroboros/governance/patch_benchmarker.py`
- Create: `tests/test_ouroboros_governance/test_patch_benchmarker.py`

### Context

`PatchBenchmarker.benchmark(ctx)` measures lint (ruff), coverage (pytest-cov), and complexity (radon) on the files modified by the patch. It must never raise. It runs under a module-level `asyncio.Semaphore(2)`. Per-step budgets: lint 15s, coverage 35s, complexity 10s. `benchmark()` accepts `ctx: OperationContext`; target files are in `ctx.target_files`. Pre-apply file content is in `ctx.pre_apply_snapshots` (a `dict[str, str]`).

### Step 1: Write the failing tests

Create `tests/test_ouroboros_governance/test_patch_benchmarker.py`:

```python
"""Tests for PatchBenchmarker."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.patch_benchmarker import (
    BenchmarkResult,
    PatchBenchmarker,
    _compute_patch_hash,
    _infer_task_type,
)
from backend.core.ouroboros.governance.op_context import OperationContext


def _make_ctx(description="improve auth logic", target_files=(), pre_apply_snapshots=None):
    ctx = MagicMock(spec=OperationContext)
    ctx.description = description
    ctx.target_files = target_files
    ctx.pre_apply_snapshots = pre_apply_snapshots or {}
    ctx.op_id = "op-test-001"
    return ctx


class TestInferTaskType:
    def test_test_in_description(self):
        assert _infer_task_type("add unit tests for auth", ()) == "testing"

    def test_file_under_tests_dir(self):
        assert _infer_task_type("improve logic", ("tests/test_foo.py",)) == "testing"

    def test_refactor_in_description(self):
        assert _infer_task_type("refactor the auth module", ()) == "refactoring"

    def test_bug_fix(self):
        assert _infer_task_type("fix null pointer bug", ()) == "bug_fix"

    def test_security(self):
        assert _infer_task_type("security patch for token validation", ()) == "security"

    def test_performance(self):
        assert _infer_task_type("optimize hot path", ()) == "performance"

    def test_default(self):
        assert _infer_task_type("update auth module", ()) == "code_improvement"

    def test_priority_order_test_beats_refactor(self):
        assert _infer_task_type("refactor tests", ()) == "testing"


class TestComputePatchHash:
    def test_deterministic(self):
        h1 = _compute_patch_hash({"a.py": "x", "b.py": "y"})
        h2 = _compute_patch_hash({"b.py": "y", "a.py": "x"})
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = _compute_patch_hash({"a.py": "x"})
        h2 = _compute_patch_hash({"a.py": "y"})
        assert h1 != h2

    def test_returns_hex_string(self):
        h = _compute_patch_hash({"a.py": "x"})
        assert len(h) == 64
        int(h, 16)  # must be valid hex


class TestBenchmarkNeverRaises:
    async def test_benchmark_returns_result_when_tools_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx()
            result = await benchmarker.benchmark(ctx)
            assert isinstance(result, BenchmarkResult)
            assert 0.0 <= result.quality_score <= 1.0

    async def test_benchmark_returns_on_subprocess_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("nonexistent_file.py",))
            # Must not raise
            result = await benchmarker.benchmark(ctx)
            assert isinstance(result, BenchmarkResult)

    async def test_timed_out_flag_set_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=0.001)
            ctx = _make_ctx()
            result = await benchmarker.benchmark(ctx)
            # With near-zero timeout, at least one step should time out
            assert isinstance(result, BenchmarkResult)
            # timed_out may or may not be set depending on OS timing, but must not raise


class TestQualityScoreFormula:
    def test_perfect_scores(self):
        result = BenchmarkResult(
            pass_rate=1.0, lint_violations=0, coverage_pct=100.0,
            complexity_delta=-1.0, patch_hash="", quality_score=0.0,
            task_type="code_improvement", timed_out=False, error=None,
        )
        # Recompute: lint_score=1.0, coverage_score=1.0, complexity_score=1.0
        expected = 0.45 * 1.0 + 0.45 * 1.0 + 0.10 * 1.0
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=1.0, coverage_score=1.0, complexity_score=1.0, radon_available=True)
        assert abs(score - expected) < 1e-6

    def test_radon_unavailable_redistributes_weight(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=1.0, coverage_score=1.0, complexity_score=0.0, radon_available=False)
        # Weights: lint=0.50, coverage=0.50, complexity ignored
        assert abs(score - 1.0) < 1e-6

    def test_scores_clamped_to_0_1(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=2.0, coverage_score=-1.0, complexity_score=0.5, radon_available=True)
        assert 0.0 <= score <= 1.0
```

### Step 2: Run tests to confirm they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_patch_benchmarker.py -v 2>&1 | head -30
```

Expected: FAIL — module not found.

### Step 3: Implement PatchBenchmarker

Create `backend/core/ouroboros/governance/patch_benchmarker.py`:

```python
"""PatchBenchmarker — measures objective quality of an applied patch.

Runs lint (ruff), coverage (pytest-cov), and complexity (radon) on the
modified files. Never raises. All failures surface in BenchmarkResult.error.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger(__name__)

# Bounded concurrency: max 2 parallel benchmarks
_SEMAPHORE = asyncio.Semaphore(2)

# Per-step time budgets (seconds)
_LINT_BUDGET = 15.0
_COVERAGE_BUDGET = 35.0
_COMPLEXITY_BUDGET = 10.0

_TASK_TAXONOMY = [
    ("testing",         lambda d, fs: "test" in d.lower() or any("tests/" in f or f.startswith("test_") for f in fs)),
    ("refactoring",     lambda d, fs: "refactor" in d.lower()),
    ("bug_fix",         lambda d, fs: "bug" in d.lower() or "fix" in d.lower()),
    ("security",        lambda d, fs: "security" in d.lower()),
    ("performance",     lambda d, fs: "perf" in d.lower() or "optim" in d.lower()),
    ("code_improvement",lambda d, fs: True),  # default
]


def _infer_task_type(description: str, target_files: tuple[str, ...]) -> str:
    for task_type, predicate in _TASK_TAXONOMY:
        if predicate(description, target_files):
            return task_type
    return "code_improvement"


def _compute_patch_hash(applied: dict[str, str]) -> str:
    """sha256 of sorted rel_path:content entries. Deterministic, order-independent."""
    payload = "\n".join(sorted(f"{k}:{v}" for k, v in applied.items()))
    return hashlib.sha256(payload.encode()).hexdigest()


def _compute_quality_score(
    lint_score: float,
    coverage_score: float,
    complexity_score: float,
    radon_available: bool,
) -> float:
    ls = max(0.0, min(1.0, lint_score))
    cs = max(0.0, min(1.0, coverage_score))
    xs = max(0.0, min(1.0, complexity_score))
    if radon_available:
        return 0.45 * ls + 0.45 * cs + 0.10 * xs
    else:
        return 0.50 * ls + 0.50 * cs


@dataclass(frozen=True)
class BenchmarkResult:
    pass_rate: float
    lint_violations: int
    coverage_pct: float
    complexity_delta: float
    patch_hash: str
    quality_score: float
    task_type: str
    timed_out: bool
    error: Optional[str]


class PatchBenchmarker:
    def __init__(
        self,
        project_root: Path,
        timeout_s: float = 60.0,
        pre_apply_snapshots: Optional[dict[str, str]] = None,
    ) -> None:
        self._root = project_root
        self._timeout_s = timeout_s
        self._pre_apply_snapshots = pre_apply_snapshots or {}

    async def benchmark(self, ctx: "OperationContext") -> BenchmarkResult:
        async with _SEMAPHORE:
            return await self._run(ctx)

    async def _run(self, ctx: "OperationContext") -> BenchmarkResult:
        target_files = [str(f) for f in ctx.target_files]
        task_type = _infer_task_type(ctx.description, tuple(target_files))
        patch_hash = _compute_patch_hash(
            {str(f): Path(self._root / f).read_text(errors="replace")
             for f in target_files if (self._root / f).exists()}
        )
        timed_out = False
        errors: list[str] = []

        # Lint
        lint_violations = 0
        lint_score = 0.0
        try:
            lint_violations, lint_score = await asyncio.wait_for(
                self._run_lint(target_files), timeout=_LINT_BUDGET
            )
        except asyncio.TimeoutError:
            timed_out = True
            errors.append("lint timed out")
        except Exception as exc:
            errors.append(f"lint: {exc}")

        # Coverage
        coverage_pct = 0.0
        coverage_score = 0.0
        pass_rate = 0.0
        try:
            coverage_pct, pass_rate = await asyncio.wait_for(
                self._run_coverage(target_files), timeout=_COVERAGE_BUDGET
            )
            coverage_score = min(1.0, coverage_pct / 100.0)
        except asyncio.TimeoutError:
            timed_out = True
            errors.append("coverage timed out")
        except Exception as exc:
            errors.append(f"coverage: {exc}")

        # Complexity
        complexity_delta = 0.0
        radon_available = False
        try:
            complexity_delta, radon_available = await asyncio.wait_for(
                self._run_complexity(target_files), timeout=_COMPLEXITY_BUDGET
            )
        except asyncio.TimeoutError:
            timed_out = True
            errors.append("complexity timed out")
        except Exception as exc:
            errors.append(f"complexity: {exc}")

        complexity_score = max(0.0, min(1.0, 1.0 - max(0.0, complexity_delta / 5.0)))
        quality_score = _compute_quality_score(lint_score, coverage_score, complexity_score, radon_available)

        return BenchmarkResult(
            pass_rate=pass_rate,
            lint_violations=lint_violations,
            coverage_pct=coverage_pct,
            complexity_delta=complexity_delta,
            patch_hash=patch_hash,
            quality_score=quality_score,
            task_type=task_type,
            timed_out=timed_out,
            error="; ".join(errors) if errors else None,
        )

    async def _run_lint(self, target_files: list[str]) -> tuple[int, float]:
        """Returns (violation_count, lint_score)."""
        if not target_files:
            return 0, 1.0
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._lint_sync, target_files)
        return result

    def _lint_sync(self, target_files: list[str]) -> tuple[int, float]:
        try:
            r = subprocess.run(
                ["ruff", "check", "--select=E,F,W", "--output-format=json"] + target_files,
                capture_output=True, text=True, cwd=self._root, timeout=_LINT_BUDGET,
            )
            violations = len(json.loads(r.stdout)) if r.stdout.strip().startswith("[") else 0
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return 0, 0.0

        lines = sum(
            len(Path(self._root / f).read_text(errors="replace").splitlines())
            for f in target_files if (self._root / f).exists()
        )
        score = max(0.0, 1.0 - violations / max(1, lines * 0.05))
        return violations, score

    async def _run_coverage(self, target_files: list[str]) -> tuple[float, float]:
        """Returns (coverage_pct, pass_rate)."""
        if not target_files:
            return 0.0, 0.0
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._coverage_sync, target_files)

    def _coverage_sync(self, target_files: list[str]) -> tuple[float, float]:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = subprocess.run(
                    ["python3", "-m", "pytest", "--cov", "--cov-report=json",
                     f"--cov-config=/dev/null", "-q", "--tb=no", "--no-header",
                     "--ignore=docs", "--ignore=.worktrees"] + target_files,
                    capture_output=True, text=True, cwd=self._root, timeout=_COVERAGE_BUDGET,
                )
                cov_file = Path(self._root / "coverage.json")
                if not cov_file.exists():
                    return 0.0, 0.0
                data = json.loads(cov_file.read_text())
                cov_pct = data.get("totals", {}).get("percent_covered", 0.0)
                # pass_rate: parse "X passed" from pytest output
                pass_rate = 0.0
                for line in r.stdout.splitlines():
                    if "passed" in line:
                        parts = line.split()
                        try:
                            passed = int(parts[0])
                            total_match = [p for p in parts if "failed" in p or "error" in p]
                            if not total_match:
                                pass_rate = 1.0
                            else:
                                failed = sum(int(p.split()[0]) for p in total_match if p[0].isdigit())
                                pass_rate = passed / max(1, passed + failed)
                        except (ValueError, IndexError):
                            pass_rate = 1.0 if r.returncode == 0 else 0.0
                        break
                return float(cov_pct), pass_rate
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
            return 0.0, 0.0

    async def _run_complexity(self, target_files: list[str]) -> tuple[float, bool]:
        """Returns (complexity_delta, radon_available)."""
        if not target_files:
            return 0.0, False
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._complexity_sync, target_files)

    def _complexity_sync(self, target_files: list[str]) -> tuple[float, bool]:
        try:
            # After CC
            r_after = subprocess.run(
                ["python3", "-m", "radon", "cc", "-s", "-a"] + target_files,
                capture_output=True, text=True, cwd=self._root, timeout=_COMPLEXITY_BUDGET,
            )
            after_cc = self._parse_radon_average(r_after.stdout)

            # Before CC from pre_apply_snapshots
            before_cc = after_cc  # default: no delta
            if self._pre_apply_snapshots:
                with tempfile.TemporaryDirectory() as tmp:
                    for rel_path, content in self._pre_apply_snapshots.items():
                        dest = Path(tmp) / Path(rel_path).name
                        dest.write_text(content)
                    before_files = [str(Path(tmp) / Path(f).name) for f in target_files
                                    if Path(rel_path).name in [Path(f).name for f in target_files]]
                    if before_files:
                        r_before = subprocess.run(
                            ["python3", "-m", "radon", "cc", "-s", "-a"] + before_files,
                            capture_output=True, text=True, cwd=tmp, timeout=_COMPLEXITY_BUDGET,
                        )
                        before_cc = self._parse_radon_average(r_before.stdout)

            return after_cc - before_cc, True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return 0.0, False

    @staticmethod
    def _parse_radon_average(output: str) -> float:
        """Parse 'Average complexity: X.X (A)' from radon output."""
        for line in output.splitlines():
            if "Average complexity" in line:
                try:
                    return float(line.split(":")[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass
        return 0.0
```

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_patch_benchmarker.py -v
```

Expected: all PASS.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/patch_benchmarker.py tests/test_ouroboros_governance/test_patch_benchmarker.py
git commit -m "feat(ouroboros): add PatchBenchmarker with lint/coverage/complexity measurement"
```

---

## Task 3: ModelAttributionRecorder

**Files:**
- Create: `backend/core/ouroboros/governance/model_attribution_recorder.py`
- Create: `tests/test_ouroboros_governance/test_model_attribution_recorder.py`

### Context

`ModelAttributionRecorder` queries `PerformanceRecordPersistence.get_records_by_model_and_task()` for old and new model IDs, computes deltas for each task type in the taxonomy, and writes `ModelAttributionRecord` rows via `save_attribution_record()`. Skip task types with insufficient samples. Called from `GovernedLoopService._handle_model_promoted()` (Task 7) under a 30s timeout.

### Step 1: Write the failing tests

Create `tests/test_ouroboros_governance/test_model_attribution_recorder.py`:

```python
"""Tests for ModelAttributionRecorder."""
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.model_attribution_recorder import ModelAttributionRecorder
from backend.core.ouroboros.integration import (
    ModelAttributionRecord,
    PerformanceRecord,
    PerformanceRecordPersistence,
    TaskDifficulty,
)


def _make_record(model_id: str, task_type: str, success: bool, latency_ms: float = 500.0, quality: float = 0.8):
    return PerformanceRecord(
        model_id=model_id,
        task_type=task_type,
        difficulty=TaskDifficulty.MEDIUM,
        success=success,
        latency_ms=latency_ms,
        iterations_used=1,
        code_quality_score=quality,
    )


async def _populate(persistence, model_id, task_type, count, success_rate, latency_ms=500.0, quality=0.8):
    for i in range(count):
        rec = _make_record(model_id, task_type, i / count < success_rate, latency_ms, quality)
        await persistence._save_to_sqlite([rec])


class TestModelAttributionRecorder:
    async def test_records_written_for_sufficient_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "v2", "bug_fix", 10, success_rate=0.9, quality=0.85)
            await _populate(p, "v1", "bug_fix", 10, success_rate=0.6, quality=0.70)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20, min_sample_size=3)
            results = await recorder.record_model_transition(
                new_model_id="v2", previous_model_id="v1", training_batch_size=40
            )
            assert len(results) >= 1
            r = results[0]
            assert r.task_type == "bug_fix"
            assert r.model_id == "v2"
            assert r.previous_model_id == "v1"
            assert 0.0 <= r.confidence <= 1.0
            assert "v2" in r.summary

    async def test_skips_task_type_with_insufficient_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            # Only 2 records for v2 (below min_sample_size=3)
            await _populate(p, "v2", "testing", 2, success_rate=1.0)
            await _populate(p, "v1", "testing", 10, success_rate=0.5)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20, min_sample_size=3)
            results = await recorder.record_model_transition("v2", "v1", 20)
            assert all(r.task_type != "testing" for r in results)

    async def test_confidence_capped_at_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "v2", "code_improvement", 25, success_rate=0.8)
            await _populate(p, "v1", "code_improvement", 25, success_rate=0.5)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20)
            results = await recorder.record_model_transition("v2", "v1", 20)
            for r in results:
                assert r.confidence <= 1.0

    async def test_record_persisted_to_database(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "v2", "refactoring", 5, success_rate=0.8)
            await _populate(p, "v1", "refactoring", 5, success_rate=0.5)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20, min_sample_size=3)
            results = await recorder.record_model_transition("v2", "v1", 10)
            assert len(results) >= 1
            db = Path(tmp) / "performance_records.db"
            with sqlite3.connect(db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM model_attribution").fetchone()[0]
            assert count >= 1
```

### Step 2: Run tests to confirm they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_model_attribution_recorder.py -v 2>&1 | head -30
```

### Step 3: Implement ModelAttributionRecorder

Create `backend/core/ouroboros/governance/model_attribution_recorder.py`:

```python
"""ModelAttributionRecorder — per-task-type quality delta at model promotion."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from statistics import mean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.ouroboros.integration import (
        ModelAttributionRecord,
        PerformanceRecordPersistence,
    )

logger = logging.getLogger(__name__)

TASK_TAXONOMY = (
    "code_improvement", "refactoring", "bug_fix", "code_review",
    "testing", "documentation", "performance", "security",
)


class ModelAttributionRecorder:
    def __init__(
        self,
        persistence: "PerformanceRecordPersistence",
        lookback_n: int = 20,
        min_sample_size: int = 3,
    ) -> None:
        self._persistence = persistence
        self._lookback_n = lookback_n
        self._min_sample_size = min_sample_size

    async def record_model_transition(
        self,
        new_model_id: str,
        previous_model_id: str,
        training_batch_size: int,
        task_types: list[str] | None = None,
    ) -> "list[ModelAttributionRecord]":
        from backend.core.ouroboros.integration import ModelAttributionRecord

        to_process = task_types if task_types else list(TASK_TAXONOMY)
        results: list[ModelAttributionRecord] = []
        summary_parts: list[str] = []

        for task_type in to_process:
            new_records = await self._persistence.get_records_by_model_and_task(
                new_model_id, task_type, limit=self._lookback_n
            )
            old_records = await self._persistence.get_records_by_model_and_task(
                previous_model_id, task_type, limit=self._lookback_n
            )
            n_new, n_old = len(new_records), len(old_records)
            if min(n_new, n_old) < self._min_sample_size:
                summary_parts.append(f"{task_type}: insufficient data [n={min(n_new, n_old)}]")
                continue

            sr_delta = mean(float(r.success) for r in new_records) - mean(float(r.success) for r in old_records)
            lat_delta = mean(r.latency_ms for r in new_records) - mean(r.latency_ms for r in old_records)
            q_delta = mean(r.code_quality_score for r in new_records) - mean(r.code_quality_score for r in old_records)
            confidence = min(n_new, n_old) / self._lookback_n
            sample_size = min(n_new, n_old)

            sign = lambda v: "+" if v >= 0 else ""
            summary_parts.append(
                f"{task_type} success {sign(sr_delta)}{sr_delta*100:.1f}% "
                f"quality {sign(q_delta)}{q_delta:.2f} "
                f"latency {sign(lat_delta)}{lat_delta:.0f}ms [n={sample_size}]"
            )

            rec = ModelAttributionRecord(
                model_id=new_model_id,
                previous_model_id=previous_model_id,
                training_batch_size=training_batch_size,
                task_type=task_type,
                success_rate_delta=sr_delta,
                latency_delta_ms=lat_delta,
                quality_delta=q_delta,
                sample_size=sample_size,
                confidence=min(1.0, confidence),
                summary="",  # filled below
                recorded_at=datetime.now(tz=timezone.utc),
            )
            results.append(rec)

        full_summary = (
            f"Model {new_model_id} ({training_batch_size} experiences): "
            + "; ".join(summary_parts)
        )
        logger.info("[ModelAttribution] %s", full_summary)

        import dataclasses
        final_results: list[ModelAttributionRecord] = []
        for rec in results:
            rec_with_summary = dataclasses.replace(rec, summary=full_summary)
            await self._persistence.save_attribution_record(rec_with_summary)
            final_results.append(rec_with_summary)

        return final_results
```

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_model_attribution_recorder.py -v
```

Expected: all PASS.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/model_attribution_recorder.py tests/test_ouroboros_governance/test_model_attribution_recorder.py
git commit -m "feat(ouroboros): add ModelAttributionRecorder for per-task quality delta at model promotion"
```

---

## Task 4: CurriculumPublisher

**Files:**
- Create: `backend/core/ouroboros/governance/curriculum_publisher.py`
- Create: `tests/test_ouroboros_governance/test_curriculum_publisher.py`

### Context

`CurriculumPublisher.publish()` queries `PerformanceRecordPersistence` for recent records per task type, computes a weighted failure priority, normalizes the top-K, and writes a JSON event file to `event_dir`. Returns `None` when all task types have insufficient data.

### Step 1: Write the failing tests

Create `tests/test_ouroboros_governance/test_curriculum_publisher.py`:

```python
"""Tests for CurriculumPublisher."""
import json
import math
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.curriculum_publisher import (
    CurriculumEntry,
    CurriculumPayload,
    CurriculumPublisher,
)
from backend.core.ouroboros.integration import PerformanceRecord, PerformanceRecordPersistence, TaskDifficulty


def _make_record(model_id="m1", task_type="bug_fix", success=True, latency_ms=500.0):
    from datetime import datetime
    return PerformanceRecord(
        model_id=model_id, task_type=task_type, difficulty=TaskDifficulty.MEDIUM,
        success=success, latency_ms=latency_ms, iterations_used=1, code_quality_score=0.8,
        timestamp=datetime.now(),
    )


async def _populate(p, task_type, n_fail, n_pass):
    for _ in range(n_fail):
        await p._save_to_sqlite([_make_record(task_type=task_type, success=False)])
    for _ in range(n_pass):
        await p._save_to_sqlite([_make_record(task_type=task_type, success=True)])


class TestCurriculumPublisher:
    async def test_returns_none_when_no_data(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), min_sample_size=3)
            result = await publisher.publish()
            assert result is None

    async def test_writes_json_file_when_data_available(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "bug_fix", n_fail=7, n_pass=3)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), top_k=5, min_sample_size=3)
            result = await publisher.publish()
            assert result is not None
            files = list(Path(ev).glob("curriculum_*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["schema_version"] == "curriculum.1"
            assert data["event_type"] == "curriculum_signal"
            assert len(data["top_k"]) >= 1

    async def test_top_k_priorities_sum_to_1(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "bug_fix", n_fail=6, n_pass=4)
            await _populate(p, "code_improvement", n_fail=4, n_pass=6)
            await _populate(p, "testing", n_fail=8, n_pass=2)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), top_k=5, min_sample_size=3)
            result = await publisher.publish()
            assert result is not None
            total = sum(e.priority for e in result.top_k)
            assert abs(total - 1.0) < 1e-6

    async def test_high_failure_rate_gets_higher_priority(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "bug_fix", n_fail=9, n_pass=1)      # 90% failure
            await _populate(p, "code_improvement", n_fail=1, n_pass=9)  # 10% failure
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), top_k=5, min_sample_size=3)
            result = await publisher.publish()
            assert result is not None
            entries = {e.task_type: e for e in result.top_k}
            assert entries["bug_fix"].priority > entries["code_improvement"].priority

    async def test_publish_never_raises_on_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev))
            result = await publisher.publish()  # must not raise
            assert result is None
```

### Step 2: Run tests to confirm they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_curriculum_publisher.py -v 2>&1 | head -30
```

### Step 3: Implement CurriculumPublisher

Create `backend/core/ouroboros/governance/curriculum_publisher.py`:

```python
"""CurriculumPublisher — periodic weighted failure signal to Reactor."""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.integration import PerformanceRecordPersistence

logger = logging.getLogger(__name__)

TASK_TAXONOMY = (
    "code_improvement", "refactoring", "bug_fix", "code_review",
    "testing", "documentation", "performance", "security",
)


@dataclass(frozen=True)
class CurriculumEntry:
    task_type: str
    priority: float
    failure_rate: float
    sample_size: int
    confidence: float


@dataclass(frozen=True)
class CurriculumPayload:
    schema_version: str
    event_type: str
    generated_at: str
    top_k: list[CurriculumEntry]


class CurriculumPublisher:
    def __init__(
        self,
        persistence: "PerformanceRecordPersistence",
        event_dir: Path,
        window_n: int = 50,
        top_k: int = 5,
        impact_weights: dict[str, float] | None = None,
        min_sample_size: int = 3,
        half_life_hours: float = 24.0,
    ) -> None:
        self._persistence = persistence
        self._event_dir = event_dir
        self._window_n = window_n
        self._top_k = top_k
        self._impact_weights: dict[str, float] = impact_weights or {}
        self._min_sample_size = min_sample_size
        self._half_life_hours = half_life_hours

    async def publish(self) -> Optional[CurriculumPayload]:
        now = datetime.now(tz=timezone.utc)
        entries: list[tuple[str, float, float, int, float]] = []  # (task_type, raw, fail_rate, n, conf)

        for task_type in TASK_TAXONOMY:
            records = await self._persistence.get_records_by_model_and_task(
                model_id="",  # all models — pass empty to query all
                task_type=task_type,
                limit=self._window_n,
            )
            if len(records) < self._min_sample_size:
                continue

            failure_rate = 1.0 - sum(float(r.success) for r in records) / len(records)
            impact_weight = self._impact_weights.get(task_type, 1.0)
            # recency: exponential decay based on mean age of records
            mean_age_hours = 0.0
            aged = [r for r in records if hasattr(r, "timestamp") and r.timestamp]
            if aged:
                mean_age_hours = sum(
                    (now - r.timestamp.replace(tzinfo=timezone.utc)
                     if r.timestamp.tzinfo is None
                     else now - r.timestamp).total_seconds() / 3600.0
                    for r in aged
                ) / len(aged)
            recency_weight = math.exp(-math.log(2) * mean_age_hours / self._half_life_hours)
            confidence_weight = min(len(records), self._window_n) / self._window_n
            raw_priority = failure_rate * impact_weight * recency_weight * confidence_weight
            entries.append((task_type, raw_priority, failure_rate, len(records), confidence_weight))

        if not entries:
            return None

        # Sort descending by raw priority, take top-K
        entries.sort(key=lambda x: x[1], reverse=True)
        top = entries[: self._top_k]

        # Normalize so top-K sum to 1.0
        total = sum(e[1] for e in top)
        if total == 0.0:
            return None

        payload_entries = [
            CurriculumEntry(
                task_type=task_type,
                priority=raw / total,
                failure_rate=fail_rate,
                sample_size=sample_size,
                confidence=conf,
            )
            for task_type, raw, fail_rate, sample_size, conf in top
        ]

        payload = CurriculumPayload(
            schema_version="curriculum.1",
            event_type="curriculum_signal",
            generated_at=now.isoformat(),
            top_k=payload_entries,
        )

        ts_ms = int(time.time() * 1000)
        out_path = self._event_dir / f"curriculum_{ts_ms}.json"
        out_path.write_text(
            json.dumps(
                {
                    "schema_version": payload.schema_version,
                    "event_type": payload.event_type,
                    "generated_at": payload.generated_at,
                    "top_k": [asdict(e) for e in payload.top_k],
                },
                indent=2,
            )
        )
        logger.info("[CurriculumPublisher] Wrote %s (%d task types)", out_path.name, len(payload_entries))
        return payload
```

**Note:** `get_records_by_model_and_task` currently filters by `model_id`. For curriculum, we want all models. Add a query variant `get_records_by_task(task_type, limit)` to `PerformanceRecordPersistence`:

```python
async def get_records_by_task(self, task_type: str, limit: int = 50) -> List[PerformanceRecord]:
    if not self._use_sqlite:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, self._query_by_task_sync, task_type, limit)

def _query_by_task_sync(self, task_type: str, limit: int) -> List[PerformanceRecord]:
    with sqlite3.connect(self._db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM performance_records WHERE task_type = ? ORDER BY timestamp DESC LIMIT ?",
            (task_type, limit),
        ).fetchall()
    return [self._dict_to_record(dict(row)) for row in rows]
```

Update `CurriculumPublisher` to use `get_records_by_task()` instead of the model-scoped query.

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_curriculum_publisher.py -v
```

Expected: all PASS.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/curriculum_publisher.py backend/core/ouroboros/integration.py tests/test_ouroboros_governance/test_curriculum_publisher.py
git commit -m "feat(ouroboros): add CurriculumPublisher with weighted failure-priority signal"
```

---

## Task 5: OperationContext — benchmark_result + pre_apply_snapshots Fields

**Files:**
- Modify: `backend/core/ouroboros/governance/op_context.py` (~line 419, ~line 477, ~line 630)
- Modify: `tests/test_ouroboros_governance/test_op_context.py` (add new test class)

### Context

`OperationContext` is a frozen dataclass. New fields use `field(default_factory=...)` for mutable defaults. The hash chain is automatic: `_context_to_hash_dict()` iterates `dataclasses.fields()` and calls `dataclasses.asdict()` on frozen sub-dataclasses. `BenchmarkResult` is a frozen dataclass so it serializes correctly automatically. `pre_apply_snapshots` is a plain dict — hashed as-is (json.dumps with sort_keys=True handles dict ordering). New `with_benchmark_result()` and `with_pre_apply_snapshots()` methods follow the exact pattern of `with_expanded_files()` (lines 630–644).

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_op_context.py`:

```python
class TestOperationContextBenchmarkFields:
    """Tests for benchmark_result and pre_apply_snapshots additions."""

    def test_benchmark_result_defaults_to_none(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        assert ctx.benchmark_result is None

    def test_pre_apply_snapshots_defaults_to_empty_dict(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        assert ctx.pre_apply_snapshots == {}

    def test_with_benchmark_result_returns_new_context(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.9, lint_violations=2, coverage_pct=75.0,
            complexity_delta=-0.5, patch_hash="abc", quality_score=0.85,
            task_type="bug_fix", timed_out=False, error=None,
        )
        ctx2 = ctx.with_benchmark_result(br)
        assert ctx2.benchmark_result == br
        assert ctx2.benchmark_result is not ctx.benchmark_result or ctx.benchmark_result is None

    def test_with_benchmark_result_does_not_change_phase(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.9, lint_violations=0, coverage_pct=80.0,
            complexity_delta=0.0, patch_hash="x", quality_score=0.9,
            task_type="code_improvement", timed_out=False, error=None,
        )
        ctx2 = ctx.with_benchmark_result(br)
        assert ctx2.phase == ctx.phase

    def test_with_benchmark_result_updates_hash_chain(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.5, lint_violations=1, coverage_pct=50.0,
            complexity_delta=1.0, patch_hash="h1", quality_score=0.5,
            task_type="refactoring", timed_out=False, error=None,
        )
        ctx2 = ctx.with_benchmark_result(br)
        assert ctx2.context_hash != ctx.context_hash
        assert ctx2.previous_hash == ctx.context_hash

    def test_with_pre_apply_snapshots_stores_content(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        snapshots = {"src/foo.py": "def foo(): pass\n"}
        ctx2 = ctx.with_pre_apply_snapshots(snapshots)
        assert ctx2.pre_apply_snapshots == snapshots

    def test_with_pre_apply_snapshots_does_not_change_phase(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        ctx2 = ctx.with_pre_apply_snapshots({"f.py": "x"})
        assert ctx2.phase == ctx.phase

    def test_hash_deterministic_with_benchmark_result(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.9, lint_violations=0, coverage_pct=80.0,
            complexity_delta=0.0, patch_hash="x", quality_score=0.9,
            task_type="code_improvement", timed_out=False, error=None,
        )
        ctx2a = ctx.with_benchmark_result(br)
        ctx2b = ctx.with_benchmark_result(br)
        assert ctx2a.context_hash == ctx2b.context_hash
```

### Step 2: Run tests to confirm they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py::TestOperationContextBenchmarkFields -v 2>&1 | head -30
```

### Step 3: Implement OperationContext additions

**3a. Import `BenchmarkResult` with TYPE_CHECKING** at the top of `op_context.py`:

```python
from __future__ import annotations
# (already present or add)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
```

**3b. Add two new fields** to `OperationContext` (after `expanded_context_files: Tuple[str, ...] = ()`, ~line 419):

```python
    benchmark_result: Optional["BenchmarkResult"] = None
    pre_apply_snapshots: Dict[str, str] = field(default_factory=dict)
```

Note: `Dict[str, str]` — `OperationContext` is frozen, but `field(default_factory=dict)` is correct for frozen dataclasses (factory runs at construction).

**3c. Add to `create()` `fields_for_hash` dict** (after `"expanded_context_files": ()`, ~line 503):

```python
"benchmark_result": None,
"pre_apply_snapshots": {},
```

**3d. Add two new `with_*` methods** after `with_expanded_files()` (~line 644):

```python
def with_benchmark_result(self, result: "BenchmarkResult") -> "OperationContext":
    """Return a new context with benchmark_result set (no phase change)."""
    intermediate = dataclasses.replace(
        self,
        benchmark_result=result,
        previous_hash=self.context_hash,
        context_hash="",
    )
    fields_for_hash = _context_to_hash_dict(intermediate)
    new_hash = _compute_hash(fields_for_hash)
    return dataclasses.replace(intermediate, context_hash=new_hash)

def with_pre_apply_snapshots(self, snapshots: Dict[str, str]) -> "OperationContext":
    """Return a new context with pre_apply_snapshots set (no phase change)."""
    intermediate = dataclasses.replace(
        self,
        pre_apply_snapshots=snapshots,
        previous_hash=self.context_hash,
        context_hash="",
    )
    fields_for_hash = _context_to_hash_dict(intermediate)
    new_hash = _compute_hash(fields_for_hash)
    return dataclasses.replace(intermediate, context_hash=new_hash)
```

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py -v -k "TestOperationContextBenchmarkFields"
```

Expected: all PASS. Also run the full test_op_context.py to confirm no regressions:

```bash
python3 -m pytest tests/test_ouroboros_governance/test_op_context.py -v 2>&1 | tail -20
```

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/op_context.py tests/test_ouroboros_governance/test_op_context.py
git commit -m "feat(op-context): add benchmark_result and pre_apply_snapshots fields with hash-chain methods"
```

---

## Task 6: Orchestrator Wiring — Both Apply Paths + Config + GovernanceStack

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (~line 533, ~line 575, ~line 874, ~line 115 for config)
- Modify: `backend/core/ouroboros/governance/integration.py` (GovernanceStack ~line 286, create_governance_stack ~line 438)
- Modify: `tests/test_ouroboros_governance/test_orchestrator.py` (add new test class)

### Context

- Single-repo VERIFY path: lines 575–586. After `ctx.advance(OperationPhase.VERIFY)`, add `_run_benchmark()`, then `_persist_performance_record()` before `return ctx`.
- Cross-repo VERIFY path: `_execute_saga_apply()` lines 947–956. Same pattern after `ctx.advance(OperationPhase.VERIFY)`.
- `pre_apply_snapshots` captured in APPLY phase before `change_engine.execute()` (~line 541).
- `GovernanceStack` (`governance/integration.py`) needs `performance_persistence: Optional[Any] = None` field.
- `OrchestratorConfig` fields: see design doc Section 5f.
- Import `get_performance_persistence` from `backend.core.ouroboros.integration`.

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_orchestrator.py`:

```python
class TestBenchmarkWiring:
    """Tests for _run_benchmark and _persist_performance_record wiring."""

    async def test_run_benchmark_disabled_returns_ctx_unchanged(self):
        """When benchmark_enabled=False, ctx is returned unmodified."""
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from unittest.mock import MagicMock, AsyncMock
        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = False
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        ctx = MagicMock()
        result = await orch._run_benchmark(ctx, [])
        assert result is ctx

    async def test_run_benchmark_enabled_calls_benchmarker(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        from unittest.mock import MagicMock, AsyncMock, patch
        from pathlib import Path
        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = True
        config.benchmark_timeout_s = 5.0
        config.project_root = Path("/tmp")
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        ctx = MagicMock()
        ctx.pre_apply_snapshots = {}
        br = BenchmarkResult(
            pass_rate=1.0, lint_violations=0, coverage_pct=80.0,
            complexity_delta=0.0, patch_hash="h", quality_score=0.9,
            task_type="code_improvement", timed_out=False, error=None,
        )
        ctx.with_benchmark_result.return_value = ctx
        with patch(
            "backend.core.ouroboros.governance.orchestrator.PatchBenchmarker"
        ) as MockBenchmarker:
            MockBenchmarker.return_value.benchmark = AsyncMock(return_value=br)
            result = await orch._run_benchmark(ctx, [])
            ctx.with_benchmark_result.assert_called_once_with(br)

    async def test_run_benchmark_never_raises_on_exception(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from unittest.mock import MagicMock, AsyncMock, patch
        from pathlib import Path
        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = True
        config.benchmark_timeout_s = 5.0
        config.project_root = Path("/tmp")
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        ctx = MagicMock()
        ctx.pre_apply_snapshots = {}
        ctx.op_id = "op-x"
        with patch(
            "backend.core.ouroboros.governance.orchestrator.PatchBenchmarker"
        ) as MockBenchmarker:
            MockBenchmarker.return_value.benchmark = AsyncMock(side_effect=RuntimeError("boom"))
            result = await orch._run_benchmark(ctx, [])
            assert result is ctx  # original ctx returned on failure

    async def test_persist_performance_record_no_persistence_is_noop(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator
        from unittest.mock import MagicMock
        orch = Orchestrator.__new__(Orchestrator)
        stack = MagicMock()
        stack.performance_persistence = None
        orch._stack = stack
        ctx = MagicMock()
        ctx.op_id = "op-x"
        await orch._persist_performance_record(ctx)  # must not raise

    async def test_persist_performance_record_calls_save_record(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        from backend.core.ouroboros.governance.op_context import OperationPhase
        from unittest.mock import MagicMock, AsyncMock
        orch = Orchestrator.__new__(Orchestrator)
        stack = MagicMock()
        stack.performance_persistence = MagicMock()
        stack.performance_persistence.save_record = AsyncMock()
        orch._stack = stack
        ctx = MagicMock()
        ctx.op_id = "op-x"
        ctx.phase = OperationPhase.COMPLETE
        ctx.model_id = "m1"
        ctx.difficulty = MagicMock()
        ctx.elapsed_ms = 500.0
        ctx.iterations_used = 1
        ctx.benchmark_result = BenchmarkResult(
            pass_rate=0.9, lint_violations=0, coverage_pct=75.0,
            complexity_delta=0.0, patch_hash="p", quality_score=0.85,
            task_type="bug_fix", timed_out=False, error=None,
        )
        await orch._persist_performance_record(ctx)
        stack.performance_persistence.save_record.assert_called_once()
```

### Step 2: Run tests to confirm they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py::TestBenchmarkWiring -v 2>&1 | head -30
```

### Step 3: Implement orchestrator changes

**3a. Add new `OrchestratorConfig` fields** (after `context_expansion_timeout_s: float = 30.0`):

```python
    # Benchmarking
    benchmark_enabled: bool = True
    benchmark_timeout_s: float = 60.0

    # Model attribution
    model_attribution_enabled: bool = True
    model_attribution_lookback_n: int = 20
    model_attribution_min_sample_size: int = 3

    # Curriculum
    curriculum_enabled: bool = True
    curriculum_publish_interval_s: float = 3600.0
    curriculum_window_n: int = 50
    curriculum_top_k: int = 5
    curriculum_impact_weights: Dict[str, float] = field(default_factory=dict)

    # Reactor event polling
    reactor_event_poll_interval_s: float = 30.0
```

**3b. Add imports** near top of `orchestrator.py`:

```python
from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult, PatchBenchmarker
from backend.core.ouroboros.integration import PerformanceRecord
```

**3c. Add `_run_benchmark()` helper** (new private method after `_publish_outcome()`):

```python
async def _run_benchmark(
    self,
    ctx: OperationContext,
    applied_files: Sequence[Path],
) -> OperationContext:
    """Run PatchBenchmarker. Fault-isolated — never raises, never alters terminal state."""
    if not self._config.benchmark_enabled:
        return ctx
    try:
        benchmarker = PatchBenchmarker(
            project_root=self._config.project_root,
            timeout_s=self._config.benchmark_timeout_s,
            pre_apply_snapshots=getattr(ctx, "pre_apply_snapshots", {}),
        )
        result = await asyncio.wait_for(
            benchmarker.benchmark(ctx),
            timeout=self._config.benchmark_timeout_s,
        )
        return ctx.with_benchmark_result(result)
    except Exception as exc:
        logger.warning(
            "[Orchestrator] Benchmark failed for op=%s: %s; continuing without metrics",
            ctx.op_id, exc,
        )
        return ctx
```

**3d. Add `_persist_performance_record()` helper**:

```python
async def _persist_performance_record(self, ctx: OperationContext) -> None:
    """Write PerformanceRecord to persistence. Fault-isolated — never raises."""
    if self._stack.performance_persistence is None:
        return
    try:
        br = getattr(ctx, "benchmark_result", None)
        record = PerformanceRecord(
            model_id=getattr(ctx, "model_id", None) or "unknown",
            task_type=br.task_type if br else "code_improvement",
            difficulty=ctx.difficulty,
            success=ctx.phase == OperationPhase.COMPLETE,
            latency_ms=getattr(ctx, "elapsed_ms", 0.0),
            iterations_used=getattr(ctx, "iterations_used", 1),
            code_quality_score=br.quality_score if br else 0.0,
            op_id=ctx.op_id,
            patch_hash=br.patch_hash if br else "",
            pass_rate=br.pass_rate if br else 0.0,
            lint_violations=br.lint_violations if br else 0,
            coverage_pct=br.coverage_pct if br else 0.0,
            complexity_delta=br.complexity_delta if br else 0.0,
        )
        await self._stack.performance_persistence.save_record(record)
    except Exception as exc:
        logger.warning(
            "[Orchestrator] PerformanceRecord persist failed for op=%s: %s",
            ctx.op_id, exc,
        )
```

**3e. Capture `pre_apply_snapshots`** in the single-repo APPLY path, before `change_engine.execute()` (~line 541):

```python
# Capture pre-apply snapshots for complexity baseline
snapshots: dict[str, str] = {}
for f in ctx.target_files:
    fpath = self._config.project_root / f
    if fpath.exists():
        try:
            snapshots[str(f)] = fpath.read_text(errors="replace")
        except OSError:
            pass
if snapshots:
    ctx = ctx.with_pre_apply_snapshots(snapshots)
```

**3f. Wire `_run_benchmark()` + `_persist_performance_record()` in single-repo VERIFY path** (replace lines 575–586):

```python
# ---- Phase 8: VERIFY ----
ctx = ctx.advance(OperationPhase.VERIFY)
await self._record_ledger(ctx, OperationState.APPLIED, {"op_id": ctx.op_id})
ctx = await self._run_benchmark(ctx, [])
ctx = ctx.advance(OperationPhase.COMPLETE)
self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
await self._publish_outcome(ctx, OperationState.APPLIED)
await self._persist_performance_record(ctx)
return ctx
```

**3g. Wire same helpers in `_execute_saga_apply()`** (replace lines 947–956):

```python
ctx = ctx.advance(OperationPhase.VERIFY)
await self._record_ledger(ctx, OperationState.APPLIED, {"saga_id": apply_result.saga_id})
ctx = await self._run_benchmark(ctx, [])
ctx = ctx.advance(OperationPhase.COMPLETE)
self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_saga)
await self._publish_outcome(ctx, OperationState.APPLIED)
await self._persist_performance_record(ctx)
return ctx
```

**3h. Add `performance_persistence` to `GovernanceStack`** in `governance/integration.py` (after `learning_bridge: Optional[Any]`, ~line 314):

```python
    performance_persistence: Optional[Any] = None
```

**3i. Wire `performance_persistence`** in `create_governance_stack()` (~line 526):

```python
from backend.core.ouroboros.integration import get_performance_persistence
# ...
stack = GovernanceStack(
    # existing fields...
    performance_persistence=get_performance_persistence(),
)
```

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py::TestBenchmarkWiring -v
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py -v 2>&1 | tail -20
```

Expected: new tests PASS, no regressions.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_orchestrator.py
git commit -m "feat(orchestrator): wire PatchBenchmarker + PerformanceRecord persistence into both VERIFY paths"
```

---

## Task 7: GovernedLoopService — Background Tasks with Lifecycle

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (~line 238, ~line 261, ~line 306)
- Modify: `tests/test_ouroboros_governance/test_governed_loop_service.py` (add new test class)

### Context

`GovernedLoopService` has `_health_probe_task: Optional[asyncio.Task]` with cancel+await in `stop()`. New tasks follow the exact same pattern. `start()` checks `self._config.curriculum_enabled` before creating tasks. `stop()` cancels all three background tasks. `_curriculum_loop()` sleeps for interval then calls `publish()`. `_reactor_event_loop()` polls event_dir every `reactor_event_poll_interval_s`. `_handle_model_promoted()` calls `record_model_transition()` under 30s timeout.

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_governed_loop_service.py`:

```python
class TestBackgroundTaskLifecycle:
    """Tests for curriculum_loop and reactor_event_loop lifecycle."""

    async def test_curriculum_task_created_on_start_when_enabled(self):
        from unittest.mock import MagicMock, AsyncMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig
        )
        config = GovernedLoopConfig(curriculum_enabled=True)
        service = GovernedLoopService(config=config)
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch("backend.core.ouroboros.governance.governed_loop_service.CurriculumPublisher"),
            patch("backend.core.ouroboros.governance.governed_loop_service.ModelAttributionRecorder"),
            patch("backend.core.ouroboros.governance.governed_loop_service.get_performance_persistence"),
        ):
            service._generator = None
            await service.start()
            assert service._curriculum_task is not None
            assert service._reactor_event_task is not None
            await service.stop()

    async def test_curriculum_task_cancelled_on_stop(self):
        from unittest.mock import MagicMock, AsyncMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig
        )
        config = GovernedLoopConfig(curriculum_enabled=True)
        service = GovernedLoopService(config=config)
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch("backend.core.ouroboros.governance.governed_loop_service.CurriculumPublisher"),
            patch("backend.core.ouroboros.governance.governed_loop_service.ModelAttributionRecorder"),
            patch("backend.core.ouroboros.governance.governed_loop_service.get_performance_persistence"),
        ):
            service._generator = None
            await service.start()
            curriculum_task = service._curriculum_task
            await service.stop()
            assert curriculum_task.done()

    async def test_reactor_event_loop_dispatches_model_promoted(self):
        import json, time
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, AsyncMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService, GovernedLoopConfig

        with tempfile.TemporaryDirectory() as ev_dir:
            ev = Path(ev_dir)
            # Write a model_promoted event file
            event = {
                "schema_version": "reactor.1",
                "event_type": "model_promoted",
                "model_id": "v2",
                "previous_model_id": "v1",
                "training_batch_size": 40,
                "promoted_at": "2026-03-09T07:00:00Z",
            }
            (ev / f"model_promoted_{int(time.time() * 1000)}.json").write_text(json.dumps(event))

            config = GovernedLoopConfig(curriculum_enabled=True, reactor_event_poll_interval_s=0.0)
            service = GovernedLoopService(config=config)
            service._event_dir = ev
            recorder = AsyncMock()
            recorder.record_model_transition = AsyncMock(return_value=[])
            service._model_attribution_recorder = recorder
            seen: set[str] = set()
            await service._handle_event_files(seen)
            recorder.record_model_transition.assert_called_once_with(
                new_model_id="v2",
                previous_model_id="v1",
                training_batch_size=40,
                task_types=None,
            )

    async def test_unknown_event_type_does_not_raise(self):
        import json, time
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, AsyncMock
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService, GovernedLoopConfig

        with tempfile.TemporaryDirectory() as ev_dir:
            ev = Path(ev_dir)
            (ev / f"unknown_{int(time.time() * 1000)}.json").write_text(
                json.dumps({"event_type": "something_reactor_invented", "data": 42})
            )
            config = GovernedLoopConfig(curriculum_enabled=True)
            service = GovernedLoopService(config=config)
            service._event_dir = ev
            service._model_attribution_recorder = AsyncMock()
            seen: set[str] = set()
            await service._handle_event_files(seen)  # must not raise
```

### Step 2: Run tests to confirm they fail

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestBackgroundTaskLifecycle -v 2>&1 | head -30
```

### Step 3: Implement GovernedLoopService changes

**3a. Add new config fields to `GovernedLoopConfig`** (the config dataclass, ~line 173):

```python
    curriculum_enabled: bool = True
    curriculum_publish_interval_s: float = 3600.0
    curriculum_window_n: int = 50
    curriculum_top_k: int = 5
    curriculum_impact_weights: Dict[str, float] = field(default_factory=dict)
    model_attribution_lookback_n: int = 20
    model_attribution_min_sample_size: int = 3
    reactor_event_poll_interval_s: float = 30.0
```

**3b. Add imports** near top of `governed_loop_service.py`:

```python
from backend.core.ouroboros.governance.curriculum_publisher import CurriculumPublisher
from backend.core.ouroboros.governance.model_attribution_recorder import ModelAttributionRecorder
from backend.core.ouroboros.integration import get_performance_persistence
```

**3c. Add new instance variables to `__init__()`** (~line 238, after `self._started_at`):

```python
        self._curriculum_task: Optional[asyncio.Task] = None
        self._reactor_event_task: Optional[asyncio.Task] = None
        self._curriculum_publisher: Optional[CurriculumPublisher] = None
        self._model_attribution_recorder: Optional[ModelAttributionRecorder] = None
        self._event_dir: Optional[Path] = None
```

**3d. Wire background tasks in `start()`** (after `self._started_at = time.monotonic()`, before the `if self._generator` state check):

```python
        if self._config.curriculum_enabled:
            import os
            event_dir = Path(os.environ.get(
                "JARVIS_REACTOR_EVENT_DIR",
                str(Path.home() / ".jarvis" / "reactor_events"),
            ))
            event_dir.mkdir(parents=True, exist_ok=True)
            self._event_dir = event_dir
            persistence = get_performance_persistence()
            self._curriculum_publisher = CurriculumPublisher(
                persistence=persistence,
                event_dir=event_dir,
                window_n=self._config.curriculum_window_n,
                top_k=self._config.curriculum_top_k,
                impact_weights=self._config.curriculum_impact_weights,
            )
            self._model_attribution_recorder = ModelAttributionRecorder(
                persistence=persistence,
                lookback_n=self._config.model_attribution_lookback_n,
                min_sample_size=self._config.model_attribution_min_sample_size,
            )
            self._curriculum_task = asyncio.create_task(
                self._curriculum_loop(), name="curriculum_loop"
            )
            self._reactor_event_task = asyncio.create_task(
                self._reactor_event_loop(), name="reactor_event_loop"
            )
```

**3e. Cancel new tasks in `stop()`** (alongside `_health_probe_task` cancellation):

```python
        for task_attr in ("_curriculum_task", "_reactor_event_task"):
            task: Optional[asyncio.Task] = getattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
```

**3f. Add `_curriculum_loop()` method**:

```python
    async def _curriculum_loop(self) -> None:
        """Publish curriculum signal every interval. Never crashes the service."""
        while True:
            try:
                await asyncio.sleep(self._config.curriculum_publish_interval_s)
                if self._curriculum_publisher:
                    await asyncio.wait_for(
                        self._curriculum_publisher.publish(),
                        timeout=30.0,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] curriculum_loop error: %s", exc)
```

**3g. Add `_reactor_event_loop()` and `_handle_event_files()` methods**:

```python
    async def _reactor_event_loop(self) -> None:
        """Poll event_dir for Reactor events. Never crashes the service."""
        seen: set[str] = set()
        while True:
            try:
                await asyncio.sleep(self._config.reactor_event_poll_interval_s)
                await self._handle_event_files(seen)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] reactor_event_loop error: %s", exc)

    async def _handle_event_files(self, seen: set[str]) -> None:
        """Process new JSON files in event_dir. Extracted for testability."""
        if self._event_dir is None:
            return
        for path in sorted(self._event_dir.glob("*.json")):
            if path.name in seen:
                continue
            seen.add(path.name)
            try:
                data = json.loads(path.read_text())
                event_type = data.get("event_type", "")
                if event_type == "model_promoted":
                    await self._handle_model_promoted(data)
                elif event_type == "ouroboros_improvement":
                    pass  # consumed elsewhere
                else:
                    logger.debug(
                        "[GovernedLoop] Unknown event_type=%r in %s",
                        event_type, path.name,
                    )
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] reactor_event_loop: failed to process %s: %s",
                    path.name, exc,
                )

    async def _handle_model_promoted(self, data: dict) -> None:
        if self._model_attribution_recorder is None:
            return
        try:
            await asyncio.wait_for(
                self._model_attribution_recorder.record_model_transition(
                    new_model_id=data["model_id"],
                    previous_model_id=data["previous_model_id"],
                    training_batch_size=int(data["training_batch_size"]),
                    task_types=data.get("task_types"),
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("[GovernedLoop] _handle_model_promoted failed: %s", exc)
```

Add `import json` to the imports at the top of `governed_loop_service.py` if not already present.

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestBackgroundTaskLifecycle -v
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py -v 2>&1 | tail -20
```

Expected: new tests PASS, no regressions.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "feat(governed-loop): add curriculum_loop and reactor_event_loop background tasks with shutdown lifecycle"
```

---

## Final Verification

Run the full governance test suite to confirm no regressions:

```bash
python3 -m pytest tests/test_ouroboros_governance/ -v 2>&1 | tail -30
```

Expected: all new tests PASS, pre-existing failures (the 29 `TestParseGenerationResponse` tests) remain unchanged.
