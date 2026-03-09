# Reactor Feedback Loop Design

**Date:** 2026-03-09
**Status:** Approved — ready for implementation plan

---

## Goal

Close the Reactor self-improvement feedback loop with three complementary signals:

1. **Post-patch benchmarking** — measure objective quality of every applied patch and persist it.
2. **Model quality attribution** — when Reactor promotes a new model, compute per-task-type deltas against the previous model.
3. **Curriculum signal** — periodically publish a weighted failure-priority payload so Reactor biases its next training batch toward high-failure areas.

None of these signals block the governance hot path. All are fault-isolated.

---

## Section 1: Storage Layer

### 1a. Extended `PerformanceRecord`

Located in `backend/core/ouroboros/integration.py`. Additive fields — all have safe defaults so existing callers are unaffected.

```python
@dataclass
class PerformanceRecord:
    # existing fields unchanged
    model_id: str
    task_type: str
    difficulty: TaskDifficulty
    success: bool
    latency_ms: float
    iterations_used: int
    code_quality_score: float
    timestamp: datetime = field(default_factory=datetime.now)
    error_message: Optional[str] = None
    context_tokens: int = 0
    output_tokens: int = 0

    # NEW — v2 fields (all default to safe zero-value)
    op_id: str = ""
    patch_hash: str = ""          # sha256(sorted applied file bytes)
    pass_rate: float = 0.0        # fraction of pytest tests passing (0.0..1.0)
    lint_violations: int = 0
    coverage_pct: float = 0.0     # branch coverage on target files
    complexity_delta: float = 0.0 # radon CC delta: after - before (negative = simpler)
```

### 1b. New `ModelAttributionRecord`

New dataclass in `backend/core/ouroboros/integration.py`, new SQLite table `model_attribution`.

```python
@dataclass
class ModelAttributionRecord:
    model_id: str
    previous_model_id: str
    training_batch_size: int
    task_type: str
    success_rate_delta: float   # e.g. +0.23
    latency_delta_ms: float
    quality_delta: float
    sample_size: int
    confidence: float           # 0..1 = min(n_old, n_new) / lookback_n
    summary: str                # human-readable one-liner
    recorded_at: datetime
```

### 1c. SQLite migration v1 → v2

`PerformanceRecordPersistence` bumps `SCHEMA_VERSION` to `2`. On startup, if the DB exists at v1, run:

```sql
ALTER TABLE performance_records ADD COLUMN op_id TEXT NOT NULL DEFAULT '';
ALTER TABLE performance_records ADD COLUMN patch_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE performance_records ADD COLUMN pass_rate REAL NOT NULL DEFAULT 0.0;
ALTER TABLE performance_records ADD COLUMN lint_violations INTEGER NOT NULL DEFAULT 0;
ALTER TABLE performance_records ADD COLUMN coverage_pct REAL NOT NULL DEFAULT 0.0;
ALTER TABLE performance_records ADD COLUMN complexity_delta REAL NOT NULL DEFAULT 0.0;

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
);
```

`PerformanceRecordPersistence` gains a `save_attribution_record(record: ModelAttributionRecord) -> None` method and a `get_records_by_model_and_task(model_id, task_type, limit) -> list[PerformanceRecord]` query method used by `ModelAttributionRecorder`.

---

## Section 2: PatchBenchmarker

**New file:** `backend/core/ouroboros/governance/patch_benchmarker.py`

### Interface

```python
@dataclass(frozen=True)
class BenchmarkResult:
    pass_rate: float           # 0.0..1.0
    lint_violations: int
    coverage_pct: float        # 0.0..100.0
    complexity_delta: float    # radon CC delta; negative = simpler
    patch_hash: str            # sha256 of sorted applied file bytes + relative paths
    quality_score: float       # 0.45*lint_score + 0.45*coverage_score + 0.10*complexity_score
    task_type: str
    timed_out: bool
    error: Optional[str]

class PatchBenchmarker:
    def __init__(self, project_root: Path, timeout_s: float = 60.0): ...
    async def benchmark(self, ctx: OperationContext) -> BenchmarkResult: ...
```

### Measurement steps (each time-budgeted within global `timeout_s`)

**Lint score** — `ruff check --select=E,F,W --output-format=json <target_files>`:
```
lint_score = max(0.0, 1.0 - violations / max(1, lines_of_code * 0.05))
```
Normalized so a 5% violation density → score 0. If ruff unavailable → `lint_score = 0.0`, `lint_violations = 0`, non-fatal.

**Coverage score** — test file discovery via `TestRunner.resolve_affected_tests()` (existing service), fallback to package-level discovery. Runs `pytest --cov=<target_module> --cov-report=json -q`. Parses `coverage.json`. If no tests found → `coverage_pct = 0`, `coverage_score = 0`.

```
coverage_score = coverage_pct / 100.0   (clamp to 0..1)
```

**Complexity delta** — radon `cc -s -a` on target files before (git HEAD~1 or sandbox snapshot) vs after. `complexity_delta = after_avg_cc - before_avg_cc`. If radon unavailable → `complexity_delta = 0.0`. "Before" baseline is the **pre-apply file content** (read from git before APPLY or from rollback snapshot), not blindly from HEAD.

**Quality score formula:**
```
lint_score_c    = clamp(lint_score, 0.0, 1.0)
coverage_score_c = clamp(coverage_score, 0.0, 1.0)
complexity_score_c = clamp(1.0 - max(0.0, complexity_delta / 5.0), 0.0, 1.0)

quality_score = 0.45 * lint_score_c + 0.45 * coverage_score_c + 0.10 * complexity_score_c
```

If radon is unavailable, redistribute its 10% weight equally to lint and coverage (0.50 / 0.50).

**Patch hash:**
```python
patch_hash = sha256(
    "\n".join(sorted(f"{rel_path}:{content}" for rel_path, content in applied.items()))
    .encode()
).hexdigest()
```
Where `applied` maps relative path → final file bytes. Deterministic, order-independent.

**Task type inference** (priority order, no LLM):
1. `ctx.description` contains "test" → `"testing"`
2. Any target file path is under `tests/` → `"testing"`
3. `ctx.description` contains "refactor" → `"refactoring"`
4. `ctx.description` contains "bug" or "fix" → `"bug_fix"`
5. `ctx.description` contains "security" → `"security"`
6. `ctx.description` contains "perf" or "optim" → `"performance"`
7. Else → `"code_improvement"`

### Safeguards (acceptance criteria)

- Per-step time budgets inside global `timeout_s` (lint budget: 15s, coverage: 35s, complexity: 10s) so one slow tool cannot starve all metrics.
- On tool timeout: set affected metric to 0, `timed_out = True`; return partial result with remaining metrics.
- `benchmark()` never raises; all exceptions are caught and surfaced in `BenchmarkResult.error`.
- Run under a module-level `asyncio.Semaphore(2)` to prevent parallel benchmarks from self-inducing pressure.
- "Before" complexity baseline: read from pre-apply snapshot passed through `ctx` (see Section 5a), never blindly from `git HEAD`.

---

## Section 3: ModelAttributionRecorder

**New file:** `backend/core/ouroboros/governance/model_attribution_recorder.py`

### Interface

```python
class ModelAttributionRecorder:
    def __init__(
        self,
        persistence: PerformanceRecordPersistence,
        lookback_n: int = 20,
        min_sample_size: int = 3,
    ): ...

    async def record_model_transition(
        self,
        new_model_id: str,
        previous_model_id: str,
        training_batch_size: int,
    ) -> list[ModelAttributionRecord]: ...
```

### Delta computation (per task type)

For each task type in the taxonomy (`"code_improvement"`, `"refactoring"`, `"bug_fix"`, `"code_review"`, `"testing"`, `"documentation"`, `"performance"`, `"security"`):

1. Query `get_records_by_model_and_task(new_model_id, task_type, limit=lookback_n)`
2. Query `get_records_by_model_and_task(previous_model_id, task_type, limit=lookback_n)`
3. If `min(len(new_records), len(old_records)) < min_sample_size` → skip (no record written)

```
success_rate_delta = mean(new.success)      - mean(old.success)
latency_delta_ms   = mean(new.latency_ms)   - mean(old.latency_ms)
quality_delta      = mean(new.quality_score)- mean(old.quality_score)
confidence         = min(n_new, n_old) / lookback_n   (0..1)
```

### Summary format (deterministic, no LLM)

```
"Model {new_model_id} ({training_batch_size} experiences): "
"{task_type} success {+/-X.X%} quality {+/-0.XX} latency {+/-Xms} [n={sample_size}]; ..."
"<task_type>: insufficient data [n={n}]"
```

Written to `logger.info` and stored verbatim in `ModelAttributionRecord.summary`. Persisted via `persistence.save_attribution_record()`.

---

## Section 4: CurriculumPublisher

**New file:** `backend/core/ouroboros/governance/curriculum_publisher.py`

### Interface

```python
@dataclass(frozen=True)
class CurriculumPayload:
    schema_version: str      # "curriculum.1"
    event_type: str          # "curriculum_signal"
    generated_at: str        # ISO-8601
    top_k: list[CurriculumEntry]

@dataclass(frozen=True)
class CurriculumEntry:
    task_type: str
    priority: float          # normalized 0..1
    failure_rate: float
    sample_size: int
    confidence: float

class CurriculumPublisher:
    def __init__(
        self,
        persistence: PerformanceRecordPersistence,
        event_dir: Path,
        window_n: int = 50,
        top_k: int = 5,
        impact_weights: dict[str, float] | None = None,  # None = uniform 1.0
        min_sample_size: int = 3,
    ): ...

    async def publish(self) -> CurriculumPayload | None: ...
```

### Priority formula (per task type)

```
failure_rate      = 1.0 - mean(success) over last window_n records
impact_weight     = impact_weights.get(task_type, 1.0)
recency_weight    = exp(-ln(2) * mean_age_hours / 24.0)  # half-life = 24h
confidence_weight = min(sample_count, window_n) / window_n

raw_priority = failure_rate * impact_weight * recency_weight * confidence_weight
```

All factors in [0,1]. After computing raw_priority for all eligible task types (those with `sample_count >= min_sample_size`), normalize the top-K so their priorities sum to 1.0 before writing. If all task types have insufficient samples → return `None`, no file written.

### Event file format

Written to `event_dir/curriculum_{timestamp_ms}.json`:

```json
{
  "schema_version": "curriculum.1",
  "event_type": "curriculum_signal",
  "generated_at": "2026-03-09T07:15:00Z",
  "top_k": [
    {
      "task_type": "bug_fix",
      "priority": 0.38,
      "failure_rate": 0.61,
      "sample_size": 34,
      "confidence": 0.68
    }
  ]
}
```

Idempotent: millisecond timestamp filename prevents collisions. Reactor tracks its own read cursor.

---

## Section 5: Orchestrator & GovernedLoopService Wiring

### 5a. Benchmark hook — both apply paths

**Single-repo path** (lines 575–586 in `orchestrator.py`):

```python
# ---- Phase 8: VERIFY ----
ctx = ctx.advance(OperationPhase.VERIFY)
await self._record_ledger(ctx, OperationState.APPLIED, {"op_id": ctx.op_id})

# Benchmark — non-blocking, fault-isolated
ctx = await self._run_benchmark(ctx, change_result.applied_files)

ctx = ctx.advance(OperationPhase.COMPLETE)
self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
await self._publish_outcome(ctx, OperationState.APPLIED)
await self._persist_performance_record(ctx)   # NEW — dedicated writer
return ctx
```

**Cross-repo path** (`_execute_saga_apply()`, after SAGA_APPLY_COMPLETED + CrossRepoVerifier passes):

```python
# SAGA_SUCCEEDED
ctx = ctx.advance(OperationPhase.VERIFY)
await self._record_ledger(ctx, OperationState.APPLIED, {"saga_id": apply_result.saga_id})

# Benchmark — same helper, saga-aware applied_files from apply_result
ctx = await self._run_benchmark(ctx, apply_result.applied_files)

ctx = ctx.advance(OperationPhase.COMPLETE)
self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_saga)
await self._publish_outcome(ctx, OperationState.APPLIED)
await self._persist_performance_record(ctx)   # NEW — dedicated writer
return ctx
```

**`_run_benchmark()` helper** (private, fault-isolated — never raises, never alters terminal state):

```python
async def _run_benchmark(
    self,
    ctx: OperationContext,
    applied_files: Sequence[Path],
) -> OperationContext:
    if not self._config.benchmark_enabled:
        return ctx
    try:
        benchmarker = PatchBenchmarker(
            project_root=self._config.project_root,
            timeout_s=self._config.benchmark_timeout_s,
            pre_apply_snapshots=ctx.pre_apply_snapshots,  # for complexity baseline
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

### 5b. PerformanceRecord persistence — dedicated writer

`LearningBridge` remains focused on qualitative outcome memory (`OperationOutcome` → `LearningMemory`). It does not own `PerformanceRecord`.

New private helper on `Orchestrator`:

```python
async def _persist_performance_record(self, ctx: OperationContext) -> None:
    """Write PerformanceRecord to persistence. Fault-isolated — never raises."""
    if self._stack.performance_persistence is None:
        return
    try:
        br = ctx.benchmark_result
        record = PerformanceRecord(
            model_id=ctx.model_id or "unknown",
            task_type=br.task_type if br else "code_improvement",
            difficulty=ctx.difficulty,
            success=ctx.phase == OperationPhase.COMPLETE,
            latency_ms=ctx.elapsed_ms,
            iterations_used=ctx.iterations_used,
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

`self._stack.performance_persistence` is a new optional slot on `GovernanceStack`; wired at construction time from `get_performance_persistence()` (existing singleton in `integration.py`).

### 5c. OperationContext additions

Two new optional fields on `OperationContext` (same frozen-dataclass + hash-chain pattern):

```python
# In OperationContext
benchmark_result: Optional[BenchmarkResult] = None
pre_apply_snapshots: dict[str, str] = field(default_factory=dict)  # rel_path → content
```

Added to `_context_to_hash_dict()` using a **canonical ordered dict** of benchmark_result fields (not `str()`):

```python
"benchmark_result": {
    "pass_rate": ctx.benchmark_result.pass_rate,
    "lint_violations": ctx.benchmark_result.lint_violations,
    "coverage_pct": ctx.benchmark_result.coverage_pct,
    "complexity_delta": ctx.benchmark_result.complexity_delta,
    "patch_hash": ctx.benchmark_result.patch_hash,
    "quality_score": ctx.benchmark_result.quality_score,
    "task_type": ctx.benchmark_result.task_type,
} if ctx.benchmark_result else {}
```

`pre_apply_snapshots` hashed as `dict(sorted(ctx.pre_apply_snapshots.items()))`.

New methods:

```python
def with_benchmark_result(self, result: BenchmarkResult) -> "OperationContext": ...
def with_pre_apply_snapshots(self, snapshots: dict[str, str]) -> "OperationContext": ...
```

Both follow the `with_expanded_files()` pattern: `dataclasses.replace()` + recompute hash without phase change.

`pre_apply_snapshots` is populated in the APPLY phase before `change_engine.execute()` is called — snapshot target file bytes at that moment.

### 5d. GovernedLoopService — background tasks with lifecycle

In `__init__()`, add held references:

```python
self._curriculum_task: Optional[asyncio.Task] = None
self._reactor_event_task: Optional[asyncio.Task] = None
self._curriculum_publisher: Optional[CurriculumPublisher] = None
self._model_attribution_recorder: Optional[ModelAttributionRecorder] = None
```

In `start()`, after existing initialization:

```python
if self._config.curriculum_enabled:
    persistence = get_performance_persistence()
    event_dir = Path(os.environ.get("JARVIS_REACTOR_EVENT_DIR",
                                    Path.home() / ".jarvis" / "reactor_events"))
    event_dir.mkdir(parents=True, exist_ok=True)
    self._curriculum_publisher = CurriculumPublisher(
        persistence=persistence,
        event_dir=event_dir,
        window_n=self._config.curriculum_window_n,
        top_k=self._config.curriculum_top_k,
        impact_weights=self._config.curriculum_impact_weights or {},
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

In `stop()`, alongside existing `_health_probe_task` cancellation:

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

**`_curriculum_loop()`**:

```python
async def _curriculum_loop(self) -> None:
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
            # continue — curriculum failure must never crash the loop
```

**`_reactor_event_loop()`** — polls `event_dir` for new JSON files, dispatches by `event_type`. Unknown event types are logged at DEBUG and skipped; loop never crashes on unknown events:

```python
async def _reactor_event_loop(self) -> None:
    seen: set[str] = set()
    while True:
        try:
            await asyncio.sleep(self._config.reactor_event_poll_interval_s)
            for path in sorted(event_dir.glob("*.json")):
                if path.name in seen:
                    continue
                seen.add(path.name)
                try:
                    data = json.loads(path.read_text())
                    event_type = data.get("event_type", "")
                    if event_type == "model_promoted":
                        await self._handle_model_promoted(data)
                    elif event_type == "ouroboros_improvement":
                        pass  # existing handling (no-op here — consumed elsewhere)
                    else:
                        logger.debug("[GovernedLoop] Unknown event_type=%r in %s", event_type, path.name)
                except Exception as exc:
                    logger.warning("[GovernedLoop] reactor_event_loop: failed to process %s: %s", path.name, exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[GovernedLoop] reactor_event_loop error: %s", exc)
```

### 5e. `model_promoted` event schema

Written by Reactor to `event_dir/model_promoted_{timestamp_ms}.json`:

```json
{
  "schema_version": "reactor.1",
  "event_type": "model_promoted",
  "model_id": "jarvis-v3.2",
  "previous_model_id": "jarvis-v3.1",
  "training_batch_id": "batch-2026-03-09-001",
  "training_batch_size": 47,
  "promoted_at": "2026-03-09T07:00:00Z",
  "task_types": ["code_improvement", "bug_fix"]
}
```

Fields consumed by `_handle_model_promoted()`:
- Required: `model_id`, `previous_model_id`, `training_batch_size`
- Optional: `task_types` (if present, attribution recorder only processes listed task types)
- Unknown fields are silently ignored.

`_handle_model_promoted()` calls `self._model_attribution_recorder.record_model_transition()` under a 30s `asyncio.wait_for`. Failure is logged and swallowed — never propagates to the event loop.

### 5f. New `OrchestratorConfig` fields

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
curriculum_impact_weights: dict[str, float] = field(default_factory=dict)

# Reactor event polling
reactor_event_poll_interval_s: float = 30.0
```

All configurable via environment variables (same pattern as existing `JARVIS_COLD_START_GRACE_S`).

---

## Phase / Component Matrix

| What | Module | Trigger |
|---|---|---|
| Extended `PerformanceRecord` | `backend/core/ouroboros/integration.py` | schema migration |
| `ModelAttributionRecord` + table | `backend/core/ouroboros/integration.py` | schema migration |
| `PatchBenchmarker` | `governance/patch_benchmarker.py` | VERIFY phase (both paths) |
| `ModelAttributionRecorder` | `governance/model_attribution_recorder.py` | `model_promoted` event |
| `CurriculumPublisher` | `governance/curriculum_publisher.py` | 1-hour background loop |
| `OperationContext` new fields | `governance/op_context.py` | APPLY (snapshot) + VERIFY (benchmark) |
| `_persist_performance_record()` | `governance/orchestrator.py` | COMPLETE (both paths) |
| `_run_benchmark()` | `governance/orchestrator.py` | VERIFY (both paths) |
| Background task lifecycle | `governance/governed_loop_service.py` | start() / stop() |

---

## Invariants

1. Benchmark, attribution, and curriculum failures **never alter operation terminal state** (COMPLETE stays COMPLETE, POSTMORTEM stays POSTMORTEM).
2. `LearningBridge` owns qualitative outcome memory only; `PerformanceRecord` is written by a dedicated `_persist_performance_record()` helper.
3. All new background tasks have stored handles and are cancelled + awaited in `stop()`.
4. `OperationContext` hash is deterministic for `benchmark_result` (canonical ordered dict, not `str()`).
5. Unknown Reactor event types are logged at DEBUG and ignored; the event loop never crashes on unknown events.
6. "Before" complexity baseline comes from `pre_apply_snapshots` captured before APPLY, not from `git HEAD`.
