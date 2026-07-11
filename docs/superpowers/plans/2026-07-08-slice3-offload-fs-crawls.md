# Slice 3: Off-Loop FS Crawls (F8 Substrate Expansion) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the three loop-starvation sources that caused A1 Run #13's LoopDeadman hard-kill (rc=75, session `bt-iso-1783574982`) by extending the F8 `cooperative_fs_io.offload` substrate — no watchdog widening, no metric suppression.

**Architecture:** Three independent cures, each matched to its *diagnosed* mechanism: (1) `evidence_capture`'s recursive `tests/**/*.py` glob (the fatal >30s wedge at `plan_runner.py:228`) gets async stamp variants that offload the crawl and delegate stamping to the existing sync functions via a new precomputed-inventory parameter (single stamping logic, DRY); (2) the hot on-loop `flock_append_line` chain (decision-trace ledger, 81–300ms sleeps in a lock poll loop) gets a canonical `async_flock_append_line(s)` inside `cross_process_jsonl` routed through `offload`, consumed by new `record_async`/`record_decision_async` at the two confirmed on-loop call sites; (3) `file_has_test_coverage` is ALREADY index-backed off-loop — its residual is up to 4 redundant `Path.resolve()` syscall chains per call in tight per-file loops, plus LoopSink wall-clock over-attribution under load — cured by resolve caching and a `cpu_ms` attribution field in LoopSink emissions (attribution added, nothing suppressed).

**Tech Stack:** Python 3.9+ asyncio, `cooperative_fs_io.offload` (existing F8 substrate — shared advisor-blast ThreadPoolExecutor), `fcntl.flock`, pytest + pytest-asyncio.

## Global Constraints

- **Mandate 1 (Root-Cause Only):** Do NOT change `JARVIS_LOOP_DEADMAN_TIMEOUT_S` (30s), any LoopSink threshold, or `ControlPlaneStarvation` thresholds. No metric suppression — Task 3 ADDS a cpu_ms field, changes no threshold logic.
- **Mandate 2 (Architectural Purity):** All offload boundaries live in `async def` code and route through `loop.run_in_executor` via `cooperative_fs_io.offload(fn, *args, cpu_bound=False)`. No new thread pools, no `time.sleep` on the loop.
- **Mandate 3 (DRY):** `cooperative_fs_io.offload` is the ONLY offload mechanism. No new `asyncio.to_thread`/`run_in_executor` call sites except the established import-fault fallback idiom copied verbatim from `posture_observer._offload_signal` (posture_observer.py:204-229). No duplicated stamping/serialization logic — sync functions gain precomputed-value parameters; async variants delegate to them.
- **Mandate 4 (Bulletproof):** `flock_append_line` offload must preserve: fresh-fd-per-call (already true — `_acquire_cross_process_lock` opens/closes its own lock fd, `flock_append_lines` opens its own target fd), per-path `threading.Lock` serialization across pool threads (already true — `_in_process_locks` is guard-locked), all-or-nothing multi-line appends within one flock scope (already true), and append ORDERING at converted call sites (achieved by AWAITING each append in-sequence — never fire-and-forget on an audit ledger). Non-reentrancy hazard (nesting an append inside `flock_critical_section` on the same path) must be documented on the new async helpers and covered by a bounded-timeout test (returns False, never deadlocks).
- All new code: `from __future__ import annotations`, env-var config with sensible defaults, no hardcoded model names, `asyncio.wait_for` never `asyncio.timeout`.
- Every fail-soft path preserves existing semantics: offload failure → the same neutral value the sync path returns on internal failure (`()` / `0` / `False`), NEVER an exception into the caller's coroutine.
- `JARVIS_COOPERATIVE_FS_IO_ENABLED=false` (master-off) must degrade every new path to synchronous-inline with byte-identical results (the substrate already guarantees this — tests must prove it end-to-end).
- Commit style: conventional commits ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. `git add` ONLY named files — never `-A`/`.`.
- Ignore any space-numbered debris files (`* 2.py` etc.) — never import from or edit them.

## Evidence Base (from session bt-iso-1783574982, Run #13)

- MAIN-thread tombstone: `plan_runner.py:228 stamp_test_files_pre → evidence_capture.py:154 base.glob("tests/**/*.py")` → pathlib scandir recursion → LoopDeadman `os._exit(75)`.
- `[LoopSink] callsite=cross_process_jsonl.flock_append_line kind=sync` ×5 (81.41–299.68ms); path named at debug.log:5112: `.jarvis/decision_trace.jsonl`. Chain: `route_runner._classify_route (async, :187) → phase8_producers.record_decision (:84) → DecisionTraceLedger.record (:305) → flock_append_line → _acquire_cross_process_lock` — `threading.Lock.acquire(timeout)` + `LOCK_EX|LOCK_NB` poll with `time.sleep` backoff 5ms→250ms, default deadline 5s (`JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S`).
- `[LoopSink] callsite=target_stratification.file_has_test_coverage kind=sync` ×32 (57–165ms), ALL after the coverage index went warm (built 22:32:33, `test_files=2909 ast_modules=12239`); 27/32 in a burst 1:1-interleaved with OpportunityMiner process-pool offloads (`parent_await_ms` up to 616ms). Warm path does NO walk/parse; residual = up to 4 `Path.resolve()` chains/call (`execution_context.py:149`, `target_stratification.py:565`, `target_stratification.py:459` ×2) amplified by wall-clock measurement under GIL/scheduler saturation.

---

### Task 1: evidence_capture async stamps (the fatal wedge)

**Files:**
- Modify: `backend/core/ouroboros/governance/verification/evidence_capture.py`
- Modify: `backend/core/ouroboros/governance/phase_runners/plan_runner.py:225-236`
- Modify: `backend/core/ouroboros/governance/phase_runners/slice4b_runner.py:752-761` and `:823-841`
- Modify: `backend/core/ouroboros/governance/verification/__init__.py:96-105` (re-exports)
- Test: `tests/governance/test_evidence_capture_offload.py` (new)

**Interfaces:**
- Consumes: `cooperative_fs_io.offload(fn, /, *args, cpu_bound=False, **kwargs) -> Any` and `is_offload_error(result) -> bool` (cooperative_fs_io.py:619, :450).
- Produces (later tasks do not depend on these, but reviewers do):
  - `async def stamp_test_files_pre_async(ctx: Any, *, target_dir: Optional[str] = None) -> int`
  - `async def stamp_target_files_pre_async(ctx: Any) -> int`
  - `async def stamp_apply_evidence_post_async(ctx: Any, *, target_dir: Optional[str] = None) -> Dict[str, int]`
  - Sync functions gain keyword-only precomputed params: `stamp_test_files_pre(..., inventory: Optional[Tuple[str, ...]] = None)`, `stamp_test_files_post(..., inventory=None)`, `stamp_target_files_pre(ctx, snapshot: Optional[Tuple[Dict[str, Any], ...]] = None)`, `stamp_target_files_post(ctx, snapshot=None)`, `stamp_apply_evidence_post(..., test_inventory=None, target_snapshot=None)`. Default `None` = crawl inline (byte-identical legacy behavior).

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_evidence_capture_offload.py`:

```python
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.verification import evidence_capture as ec


def _make_tree(tmp_path: Path, n: int = 5) -> Path:
    tdir = tmp_path / "tests"
    tdir.mkdir()
    for i in range(n):
        (tdir / f"test_mod_{i}.py").write_text("def test_x():\n    pass\n")
    return tmp_path


class _Ctx(SimpleNamespace):
    """Plain mutable ctx — object.__setattr__ works."""


@pytest.mark.asyncio
async def test_pre_async_stamps_inventory(tmp_path):
    _make_tree(tmp_path)
    ctx = _Ctx()
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    assert n == 5
    assert len(ctx.test_files_pre) == 5
    assert all(p.startswith("tests/") for p in ctx.test_files_pre)


@pytest.mark.asyncio
async def test_pre_async_crawl_runs_off_loop_thread(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = ec.capture_test_files_inventory

    def spy(*a, **kw):
        seen.append(threading.get_ident())
        return real(*a, **kw)

    monkeypatch.setattr(ec, "capture_test_files_inventory", spy)
    await ec.stamp_test_files_pre_async(_Ctx(), target_dir=str(tmp_path))
    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_pre_async_loop_stays_responsive(tmp_path, monkeypatch):
    import time as _time
    _make_tree(tmp_path)

    def slow_crawl(*a, **kw):
        _time.sleep(0.4)
        return ("tests/test_mod_0.py",)

    monkeypatch.setattr(ec, "capture_test_files_inventory", slow_crawl)
    ticks: list[float] = []

    async def heartbeat():
        t0 = asyncio.get_event_loop().time()
        while len(ticks) < 8:
            await asyncio.sleep(0.05)
            ticks.append(asyncio.get_event_loop().time() - t0)

    hb = asyncio.ensure_future(heartbeat())
    await ec.stamp_test_files_pre_async(_Ctx(), target_dir=str(tmp_path))
    await asyncio.wait_for(hb, timeout=2.0)
    # heartbeat must have ticked DURING the 0.4s crawl, not been frozen
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.3, f"loop starved: gaps={gaps}"


@pytest.mark.asyncio
async def test_pre_async_idempotent_no_crawl_when_stamped(tmp_path, monkeypatch):
    ctx = _Ctx(test_files_pre=("tests/test_already.py",))

    def boom(*a, **kw):
        raise AssertionError("crawl must not run when pre already stamped")

    monkeypatch.setattr(ec, "capture_test_files_inventory", boom)
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    assert n == 1
    assert ctx.test_files_pre == ("tests/test_already.py",)


@pytest.mark.asyncio
async def test_pre_async_offload_error_neutral(tmp_path, monkeypatch):
    """OffloadError → neutral () stamp, never an exception."""
    from backend.core.ouroboros.governance import cooperative_fs_io as cfio

    async def fake_offload(fn, /, *args, cpu_bound=False, **kwargs):
        return cfio.OffloadError(
            fn_name="capture_test_files_inventory",
            exc_type="OSError", message="synthetic", cpu_bound=False,
        )

    monkeypatch.setattr(cfio, "offload", fake_offload)
    ctx = _Ctx()
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    assert n == 0
    assert ctx.test_files_pre == ()


@pytest.mark.asyncio
async def test_pre_async_master_off_inline_identical(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")
    ctx = _Ctx()
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    sync_inv = ec.capture_test_files_inventory(str(tmp_path))
    assert n == 5 and ctx.test_files_pre == sync_inv


def test_sync_pre_accepts_precomputed_inventory(tmp_path):
    ctx = _Ctx()
    n = ec.stamp_test_files_pre(
        ctx, target_dir=str(tmp_path), inventory=("tests/test_a.py",),
    )
    assert n == 1 and ctx.test_files_pre == ("tests/test_a.py",)


def test_sync_pre_none_inventory_crawls_inline(tmp_path):
    _make_tree(tmp_path)
    ctx = _Ctx()
    n = ec.stamp_test_files_pre(ctx, target_dir=str(tmp_path))
    assert n == 5  # legacy behavior byte-identical


@pytest.mark.asyncio
async def test_target_pre_async_snapshots_off_loop(tmp_path, monkeypatch):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    ctx = _Ctx(target_files=[str(f)])
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = ec.snapshot_target_files

    def spy(*a, **kw):
        seen.append(threading.get_ident())
        return real(*a, **kw)

    monkeypatch.setattr(ec, "snapshot_target_files", spy)
    n = await ec.stamp_target_files_pre_async(ctx)
    assert n == 1 and seen and seen[0] != loop_thread
    assert ctx.target_files_pre[0]["content"] == "x = 1\n"


@pytest.mark.asyncio
async def test_apply_evidence_post_async_composite(tmp_path):
    _make_tree(tmp_path)
    f = tmp_path / "mod.py"
    f.write_text("x = 2\n")
    ctx = _Ctx(
        target_files=[str(f)],
        target_files_pre=({"path": str(f), "content": "x = 1\n", "exists": True},),
    )
    diag = await ec.stamp_apply_evidence_post_async(
        ctx, target_dir=str(tmp_path),
    )
    assert diag["enabled"] == 1
    assert diag["test_files_post"] == 5
    assert diag["target_files_post"] == 1
    assert diag["diff_text_bytes"] > 0  # x=1 → x=2 produced a diff
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_evidence_capture_offload.py -v 2>&1 | tail -20`
Expected: FAIL/ERROR — `AttributeError: ... has no attribute 'stamp_test_files_pre_async'` (and `TypeError: ... unexpected keyword argument 'inventory'` for the sync-param tests).

- [ ] **Step 3: Implement in evidence_capture.py**

Add near the top (after existing imports, line ~61): `import asyncio` and the fail-soft offload helper (verbatim idiom from posture_observer.py:204-229, adapted names):

```python
async def _offload_fs(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Route a synchronous fs-crawl through the F8 offload substrate.

    Fail-soft contract (mirrors posture_observer._offload_signal):
    substrate import fault → asyncio.to_thread; OffloadError →
    None (caller substitutes the neutral value). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            offload,
            is_offload_error,
        )
    except Exception:  # noqa: BLE001 — substrate import fault
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception:  # noqa: BLE001
            return None
    result = await offload(fn, *args, cpu_bound=False, **kwargs)
    if is_offload_error(result):
        logger.debug(
            "[EvidenceCapture] offloaded crawl fail-soft: %r", result,
        )
        return None
    return result
```

Modify `stamp_test_files_pre` (evidence_capture.py:174) — add the keyword param and use it:

```python
def stamp_test_files_pre(
    ctx: Any, *, target_dir: Optional[str] = None,
    inventory: Optional[Tuple[str, ...]] = None,
) -> int:
```
and replace the single line `inventory = capture_test_files_inventory(target_dir)` inside its body with:
```python
        if inventory is None:
            inventory = capture_test_files_inventory(target_dir)
```
(keep every other line — enabled gate, idempotency check, `object.__setattr__`, exception envelope — byte-identical).

Apply the same one-line pattern to `stamp_test_files_post` (param `inventory`), `stamp_target_files_pre` (param `snapshot: Optional[Tuple[Dict[str, Any], ...]] = None`, replacing `snapshot = snapshot_target_files(targets)` with the None-guard), and `stamp_target_files_post` (same `snapshot` param).

Modify `stamp_apply_evidence_post` (evidence_capture.py:474) to forward precomputed values:

```python
def stamp_apply_evidence_post(
    ctx: Any, *, target_dir: Optional[str] = None,
    test_inventory: Optional[Tuple[str, ...]] = None,
    target_snapshot: Optional[Tuple[Dict[str, Any], ...]] = None,
) -> Dict[str, int]:
    if not evidence_capture_enabled():
        return {"enabled": 0}
    return {
        "enabled": 1,
        "target_files_post": stamp_target_files_post(
            ctx, snapshot=target_snapshot,
        ),
        "test_files_post": stamp_test_files_post(
            ctx, target_dir=target_dir, inventory=test_inventory,
        ),
        "diff_text_bytes": stamp_diff_text(ctx),
    }
```

Add the async variants (before `__all__`):

```python
async def stamp_test_files_pre_async(
    ctx: Any, *, target_dir: Optional[str] = None,
) -> int:
    """Async variant of stamp_test_files_pre — the recursive
    tests/**/*.py glob runs OFF the asyncio loop via the F8
    cooperative_fs_io substrate (Slice 3; cures the LoopDeadman
    wedge at plan_runner.py:228, session bt-iso-1783574982).
    Same semantics, same neutral fallbacks. NEVER raises."""
    if not evidence_capture_enabled() or ctx is None:
        return 0
    try:
        existing = getattr(ctx, "test_files_pre", None)
        if existing is not None:  # idempotent — no wasted crawl
            return len(existing) if hasattr(existing, "__len__") else 0
        inv = await _offload_fs(capture_test_files_inventory, target_dir)
        if inv is None:
            inv = ()  # same neutral as the sync internal-failure path
        return stamp_test_files_pre(
            ctx, target_dir=target_dir, inventory=tuple(inv),
        )
    except Exception:  # noqa: BLE001 — defensive envelope preserved
        return 0


async def stamp_target_files_pre_async(ctx: Any) -> int:
    """Async variant of stamp_target_files_pre — per-file
    read_bytes snapshot runs off-loop. NEVER raises."""
    if not evidence_capture_enabled() or ctx is None:
        return 0
    try:
        targets = getattr(ctx, "target_files", None)
        if not targets:
            return 0
        snap = await _offload_fs(snapshot_target_files, tuple(targets))
        if snap is None:
            snap = ()
        return stamp_target_files_pre(ctx, snapshot=tuple(snap))
    except Exception:  # noqa: BLE001
        return 0


async def stamp_apply_evidence_post_async(
    ctx: Any, *, target_dir: Optional[str] = None,
) -> Dict[str, int]:
    """Async composite for the APPLY-success path — both crawls
    (target snapshot + test inventory) run off-loop; diff
    computation stays inline (inputs already capped at
    JARVIS_EVIDENCE_MAX_FILE_BYTES, bounded CPU). NEVER raises."""
    if not evidence_capture_enabled() or ctx is None:
        return {"enabled": 0}
    try:
        targets = tuple(getattr(ctx, "target_files", None) or ())
        snap = await _offload_fs(snapshot_target_files, targets) if targets else ()
        inv = await _offload_fs(capture_test_files_inventory, target_dir)
        return stamp_apply_evidence_post(
            ctx,
            target_dir=target_dir,
            test_inventory=tuple(inv) if inv is not None else (),
            target_snapshot=tuple(snap) if snap is not None else (),
        )
    except Exception:  # noqa: BLE001
        return {"enabled": 1}
```

Append the three async names to `__all__` and to the re-export list in `backend/core/ouroboros/governance/verification/__init__.py`.

- [ ] **Step 4: Run the new tests**

Run: `python3 -m pytest tests/governance/test_evidence_capture_offload.py -v 2>&1 | tail -15`
Expected: ALL PASS.

- [ ] **Step 5: Convert the three call sites**

`plan_runner.py:225-230` — replace the import + call:

```python
            from backend.core.ouroboros.governance.verification.evidence_capture import (
                stamp_test_files_pre_async,
            )
            await stamp_test_files_pre_async(
                ctx, target_dir=str(orch._config.project_root),
            )
```

`slice4b_runner.py:753-756`:

```python
                from backend.core.ouroboros.governance.verification.evidence_capture import (
                    stamp_target_files_pre_async,
                )
                await stamp_target_files_pre_async(ctx)
```

`slice4b_runner.py:824-828`:

```python
            from backend.core.ouroboros.governance.verification.evidence_capture import (
                stamp_apply_evidence_post_async,
            )
            _stamp_diag = await stamp_apply_evidence_post_async(
                ctx, target_dir=str(orch._config.project_root),
            )
```

(keep the surrounding `try/except` envelopes and the `logger.info` diagnostics byte-identical).

- [ ] **Step 6: Run existing regression suites for the touched surfaces**

Run: `python3 -m pytest tests/governance/test_evidence_capture_and_consumers.py tests/governance/test_evidence_capture_offload.py -q 2>&1 | tail -5`
Expected: ALL PASS (the legacy suite proves sync behavior unchanged).

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/verification/evidence_capture.py \
        backend/core/ouroboros/governance/verification/__init__.py \
        backend/core/ouroboros/governance/phase_runners/plan_runner.py \
        backend/core/ouroboros/governance/phase_runners/slice4b_runner.py \
        tests/governance/test_evidence_capture_offload.py
git commit -m "perf(evidence): offload tests/**/*.py glob + target snapshots off-loop (Slice 3 T1)

The recursive base.glob at evidence_capture.py:154 wedged the MAIN
asyncio thread >30s in session bt-iso-1783574982 (LoopDeadman rc=75,
plan_runner.py:228 tombstone). Async stamp variants route both crawl
functions through the F8 cooperative_fs_io.offload substrate; sync
stamps gain precomputed-inventory params so stamping logic stays
single-sourced. All three on-loop call sites converted.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: canonical async flock append + decision-trace hot path

**Files:**
- Modify: `backend/core/ouroboros/governance/cross_process_jsonl.py` (add module-level `_append_lines_with_mkdir`, `async_flock_append_line`, `async_flock_append_lines` — place after `async_flock_critical_section`, lines ~515-591)
- Modify: `backend/core/ouroboros/governance/observability/decision_trace_ledger.py` (factor `record()`; add `record_async()`)
- Modify: `backend/core/ouroboros/governance/observability/phase8_producers.py` (add `record_decision_async`)
- Modify: `backend/core/ouroboros/governance/phase_runners/route_runner.py:226-239`
- Modify: `backend/core/ouroboros/governance/orchestrator.py:2976-2985`
- Test: `tests/governance/test_async_flock_append.py` (new)

**Interfaces:**
- Consumes: `cooperative_fs_io.offload`/`is_offload_error`; existing `flock_append_lines(path, lines, *, timeout_s=None) -> bool` (cross_process_jsonl.py:433).
- Produces:
  - `async def async_flock_append_line(path: Path, line: str, *, timeout_s: Optional[float] = None) -> bool`
  - `async def async_flock_append_lines(path: Path, lines: Iterable[str], *, timeout_s: Optional[float] = None) -> bool`
  - `DecisionTraceLedger.record_async(*, op_id, phase, decision, factors, weights, rationale) -> Tuple[bool, str]` (same signature/return as `record`)
  - `phase8_producers.record_decision_async(*, op_id, phase, decision, factors=None, weights=None, rationale="") -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_async_flock_append.py`:

```python
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import cross_process_jsonl as cpj


@pytest.mark.asyncio
async def test_async_append_line_basic(tmp_path):
    target = tmp_path / "ledger.jsonl"
    ok = await cpj.async_flock_append_line(target, '{"a":1}')
    assert ok is True
    assert target.read_text() == '{"a":1}\n'


@pytest.mark.asyncio
async def test_async_append_creates_parent_dir(tmp_path):
    target = tmp_path / "deep" / "nested" / "ledger.jsonl"
    ok = await cpj.async_flock_append_line(target, '{"b":2}')
    assert ok is True and target.exists()


@pytest.mark.asyncio
async def test_async_append_runs_off_loop_thread(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = cpj.flock_append_lines

    def spy(path, lines, *, timeout_s=None):
        seen.append(threading.get_ident())
        return real(path, lines, timeout_s=timeout_s)

    monkeypatch.setattr(cpj, "flock_append_lines", spy)
    await cpj.async_flock_append_line(tmp_path / "x.jsonl", "{}")
    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_concurrent_appends_no_interleave_no_loss(tmp_path):
    """Mandate 4: 32 concurrent async appends → exactly 32 intact
    JSON lines, no partial/interleaved writes, no losses."""
    target = tmp_path / "concurrent.jsonl"
    payloads = [json.dumps({"i": i, "pad": "x" * 512}) for i in range(32)]
    results = await asyncio.gather(*[
        cpj.async_flock_append_line(target, p) for p in payloads
    ])
    assert all(results)
    lines = target.read_text().splitlines()
    assert len(lines) == 32
    parsed = sorted(json.loads(ln)["i"] for ln in lines)  # every line intact JSON
    assert parsed == list(range(32))


@pytest.mark.asyncio
async def test_sequential_awaited_appends_preserve_order(tmp_path):
    target = tmp_path / "ordered.jsonl"
    for i in range(10):
        assert await cpj.async_flock_append_line(target, json.dumps({"seq": i}))
    seqs = [json.loads(ln)["seq"] for ln in target.read_text().splitlines()]
    assert seqs == list(range(10))


@pytest.mark.asyncio
async def test_contended_lock_does_not_block_loop(tmp_path):
    """Mandate 4 + 2: another thread holds the flock; the async append
    waits in the POOL, the loop keeps ticking, append succeeds after
    release."""
    target = tmp_path / "contended.jsonl"
    lock_path = target.with_suffix(target.suffix + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    release = threading.Event()

    def holder():
        with cpj.flock_critical_section(lock_path.with_suffix("")) as ok:
            # hold the same sibling .lock the append will contend on
            release.wait(timeout=5.0)

    # hold the actual lock file the append uses
    import fcntl, os
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)

    ticks: list[float] = []

    async def heartbeat():
        t0 = asyncio.get_event_loop().time()
        for _ in range(10):
            await asyncio.sleep(0.03)
            ticks.append(asyncio.get_event_loop().time() - t0)

    async def delayed_release():
        await asyncio.sleep(0.35)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    hb = asyncio.ensure_future(heartbeat())
    rel = asyncio.ensure_future(delayed_release())
    ok = await cpj.async_flock_append_line(target, '{"c":3}', timeout_s=3.0)
    await asyncio.gather(hb, rel)
    assert ok is True
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.25, f"loop starved during lock wait: {gaps}"


@pytest.mark.asyncio
async def test_lock_timeout_returns_false_never_deadlocks(tmp_path):
    """Non-reentrancy / stuck-holder hazard is BOUNDED: timeout → False."""
    import fcntl, os
    target = tmp_path / "stuck.jsonl"
    lock_path = target.with_suffix(target.suffix + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        ok = await cpj.async_flock_append_line(target, "{}", timeout_s=0.3)
        elapsed = time.monotonic() - t0
        assert ok is False
        assert elapsed < 2.0  # bounded, no deadlock
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.asyncio
async def test_master_off_inline_still_appends(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")
    target = tmp_path / "off.jsonl"
    assert await cpj.async_flock_append_line(target, '{"d":4}') is True
    assert target.read_text() == '{"d":4}\n'


# --- decision-trace async path -------------------------------------------

@pytest.mark.asyncio
async def test_ledger_record_async(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.observability.decision_trace_ledger import (
        DecisionTraceLedger,
    )
    ledger = DecisionTraceLedger(path=tmp_path / "decision_trace.jsonl")
    ok, detail = await ledger.record_async(
        op_id="op-test1", phase="ROUTE", decision="standard",
        factors={"k": "v"}, weights={}, rationale="unit",
    )
    assert ok is True and detail == "ok"
    row = json.loads((tmp_path / "decision_trace.jsonl").read_text().splitlines()[0])
    assert row["op_id"] == "op-test1" and row["decision"] == "standard"


@pytest.mark.asyncio
async def test_record_decision_async_never_raises(monkeypatch):
    from backend.core.ouroboros.governance.observability import phase8_producers as p8

    class _Boom:
        async def record_async(self, **kw):
            raise RuntimeError("synthetic")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.observability."
        "decision_trace_ledger.get_default_ledger",
        lambda: _Boom(),
    )
    ok = await p8.record_decision_async(
        op_id="op-x", phase="ROUTE", decision="standard",
    )
    assert ok is False  # swallowed, logged, never raised
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_async_flock_append.py -v 2>&1 | tail -15`
Expected: FAIL — `AttributeError: module ... has no attribute 'async_flock_append_line'`.

- [ ] **Step 3: Implement in cross_process_jsonl.py**

Add after `async_flock_critical_section` (~line 591). `asyncio` is already imported for the critical-section variant (verify; add if absent):

```python
def _append_lines_with_mkdir(
    path: Path,
    lines: Tuple[str, ...],
    timeout_s: Optional[float],
) -> bool:
    """Offload body for the async append helpers — module-level so it
    is a plain picklable function (thread path doesn't require it,
    but keeps the substrate contract uniform). Ensures the parent
    dir exists (mirrors flock_critical_section's mkdir contract at
    :414-421), then delegates to the canonical sync append.
    NEVER raises."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug(
            "[CrossProcessJSONL] async-append parent mkdir failed: %s", exc,
        )
        return False
    return flock_append_lines(path, lines, timeout_s=timeout_s)


async def async_flock_append_lines(
    path: Path,
    lines: Iterable[str],
    *,
    timeout_s: Optional[float] = None,
) -> bool:
    """Async variant of flock_append_lines — the lock poll loop
    (LOCK_EX|LOCK_NB + time.sleep backoff, worst-case ~2×
    JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S) runs on the F8
    cooperative_fs_io pool, never on the asyncio loop (Slice 3;
    cures the on-loop decision-trace appends observed in session
    bt-iso-1783574982, 81-300ms LoopSink hits).

    ORDERING: awaited appends from one coroutine land in call
    order. Concurrent tasks appending the same path serialize on
    the per-path in-process lock + flock (all-or-nothing per call)
    but their relative order is scheduler-defined — same as today's
    cross-thread behavior.

    NON-REENTRANCY (inherited from the sync substrate): do NOT
    await this while holding flock_critical_section /
    async_flock_critical_section on the SAME path — the wait is
    BOUNDED (returns False at timeout_s) but always fails.
    NEVER raises."""
    materialized = tuple(lines)  # never iterate a caller generator off-thread
    try:
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            offload,
            is_offload_error,
        )
    except Exception:  # noqa: BLE001 — substrate import fault
        try:
            return await asyncio.to_thread(
                _append_lines_with_mkdir, path, materialized, timeout_s,
            )
        except Exception:  # noqa: BLE001
            return False
    result = await offload(
        _append_lines_with_mkdir, path, materialized, timeout_s,
        cpu_bound=False,
    )
    if is_offload_error(result):
        logger.debug(
            "[CrossProcessJSONL] async append fail-soft: %r", result,
        )
        return False
    return bool(result)


async def async_flock_append_line(
    path: Path,
    line: str,
    *,
    timeout_s: Optional[float] = None,
) -> bool:
    """Async variant of flock_append_line. See
    async_flock_append_lines for the ordering / non-reentrancy
    contract. NEVER raises."""
    from backend.core.ouroboros.governance.workspace_resolver import (
        resolve_durable_path,
    )
    try:
        path = resolve_durable_path(path)
    except Exception:  # noqa: BLE001 — mirror sync helper's fail-soft
        pass
    return await async_flock_append_lines(path, (line,), timeout_s=timeout_s)
```

(If the module has an `__all__`, append both names.)

- [ ] **Step 4: Factor DecisionTraceLedger.record + add record_async**

In `decision_trace_ledger.py`, extract everything in `record()` BEFORE the substrate import (row build → `json.dumps` → size check; NOT the mkdir — that moves into the offloaded body via the new helper) into:

```python
    def _prepare_append(
        self, *, op_id: str, phase: str, decision: str,
        factors: Dict[str, Any], weights: Dict[str, float],
        rationale: str,
    ) -> Union[Tuple[str, str, int], Tuple[None, Tuple[bool, str], int]]:
        """Row build + serialize + size-gate + per-op count check —
        the pure-CPU prefix shared by record() and record_async().
        Returns (line, op, current_count) on success, or
        (None, (False, reason), 0) on rejection."""
```
— move the existing lines verbatim; `record()` becomes: prepare → (unchanged mkdir + `flock_append_line` + count bump). Then add:

```python
    async def record_async(
        self, *, op_id: str, phase: str, decision: str,
        factors: Optional[Dict[str, Any]] = None,
        weights: Optional[Dict[str, float]] = None,
        rationale: str = "",
    ) -> Tuple[bool, str]:
        """Async record — identical semantics to record(); the flock
        append (incl. mkdir) runs off-loop via
        cross_process_jsonl.async_flock_append_line. NEVER raises."""
        prepared = self._prepare_append(
            op_id=op_id, phase=phase, decision=decision,
            factors=dict(factors or {}), weights=dict(weights or {}),
            rationale=rationale,
        )
        line, err, current_count = prepared
        if line is None:
            return err
        op = op_id
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (
                async_flock_append_line,
            )
        except ImportError:
            return self._append_legacy_fileno_flock(line, op, current_count)
        ok = await async_flock_append_line(self.path, line)
        if not ok:
            return (False, "flock_append_failed")
        self._per_op_count[op] = current_count + 1
        return (True, "ok")
```

(Adapt the exact `_prepare_append` return-unpacking to the real refactor — the implementer must keep `record()`'s observable behavior byte-identical; the existing suite `tests/governance/test_phase_7_8_cross_process_flock.py` plus decision-trace tests are the guard.)

- [ ] **Step 5: Add record_decision_async in phase8_producers.py**

Duplicate `record_decision`'s envelope exactly (same defensive contract, same SSE publish tail) with `ok, _detail = await ledger.record_async(...)`. Name: `record_decision_async`. Same params, returns `bool`, NEVER raises.

- [ ] **Step 6: Convert the two confirmed on-loop call sites**

`route_runner.py:226-239` (inside `async def _classify_route`):

```python
                from backend.core.ouroboros.governance.observability.phase8_producers import (  # noqa: E501
                    record_decision_async as _phase8_record_decision_async,
                )
                await _phase8_record_decision_async(
                    op_id=ctx.op_id,
                    phase="ROUTE",
                    decision=_provider_route.value,
                    factors={
                        "signal_urgency": str(getattr(ctx, "signal_urgency", "") or ""),
                        "signal_source": str(getattr(ctx, "signal_source", "") or ""),
                        "task_complexity": str(getattr(ctx, "task_complexity", "") or ""),
                    },
                    rationale=_route_reason or "",
                )
```

`orchestrator.py:2976` (inside async `run`, the OP_TERMINAL producer): convert the same way — `await _phase8_record_decision_async(...)` with identical kwargs (first VERIFY the enclosing function is `async def` and the call is not inside a sync nested function; if it turns out sync-nested, leave it and note in the ledger — root-cause scope is the confirmed ROUTE-phase hot path).

- [ ] **Step 7: Run new + guard suites**

Run: `python3 -m pytest tests/governance/test_async_flock_append.py tests/governance/test_cross_process_jsonl.py tests/governance/test_cross_process_jsonl_durable.py tests/governance/test_phase_7_8_cross_process_flock.py -q 2>&1 | tail -5`
Expected: ALL PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/cross_process_jsonl.py \
        backend/core/ouroboros/governance/observability/decision_trace_ledger.py \
        backend/core/ouroboros/governance/observability/phase8_producers.py \
        backend/core/ouroboros/governance/phase_runners/route_runner.py \
        backend/core/ouroboros/governance/orchestrator.py \
        tests/governance/test_async_flock_append.py
git commit -m "perf(jsonl): canonical async flock append off-loop + decision-trace hot path (Slice 3 T2)

_acquire_cross_process_lock's LOCK_NB poll (time.sleep backoff to
250ms, 5s deadline) ran ON the asyncio loop via route_runner
_classify_route -> record_decision -> DecisionTraceLedger.record ->
flock_append_line (.jarvis/decision_trace.jsonl; 81-300ms LoopSink
hits in bt-iso-1783574982). async_flock_append_line(s) route the
whole lock-wait+write through cooperative_fs_io.offload; ledger
gains record_async; both confirmed on-loop producers converted and
AWAITED in-sequence (audit ordering preserved, no fire-and-forget).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: coverage warm-path resolve caching + LoopSink cpu_ms attribution

**Files:**
- Modify: `backend/core/ouroboros/governance/target_stratification.py` (warm path, lines ~545-600)
- Modify: `backend/core/ouroboros/governance/execution_context.py:146` (docstring honesty only)
- Modify: `backend/core/ouroboros/telemetry/loop_sink.py` (`sink_sync` + `_emit_blocked`)
- Test: `tests/governance/test_stratification_warm_path_syscalls.py` (new), extend `tests/governance/test_slice33_arc0_loop_sink.py`

**Interfaces:**
- Produces: `target_stratification._cached_scan_root(repo_root_str: str) -> Path` (module-level, `functools.lru_cache(maxsize=32)`); `loop_sink._emit_blocked(callsite, elapsed_ms, threshold_ms, kind, cpu_ms=None)` — emission line gains `cpu_ms=%.2f` when provided. No public signature changes.

**Diagnosis being fixed (NOT re-offloading):** the function is already index-backed off-loop; the warm path's residual cost is up to 4 `Path.resolve()` syscall chains per call (`execution_context.py:149` via `_auth_root`, `target_stratification.py:565`, and `:459` ×2 in `_strat_path_to_module`), invoked in tight per-file loops (`operation_advisor.py:2155` `sum(...)`, `opportunity_miner_sensor.py:686`). The 100-165ms LoopSink readings are wall-clock inflated by GIL/scheduler saturation — cpu_ms attribution makes that distinction visible forever (observability-over-silent-reroute).

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_stratification_warm_path_syscalls.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import target_stratification as ts


@pytest.fixture
def warm_index(tmp_path, monkeypatch):
    """Build a tiny real coverage index so file_has_test_coverage
    takes the WARM path."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mymod.py").write_text(
        "def test_a():\n    pass\n",
    )
    (tmp_path / "mymod.py").write_text("x = 1\n")
    ts._cached_scan_root.cache_clear()
    # Force a synchronous index build for the test root (worker fn
    # is exposed for the off-loop builder; call it directly here).
    ts._install_coverage_index_for_tests(tmp_path)  # see Step 3
    return tmp_path


def test_warm_path_single_resolve(warm_index, monkeypatch):
    """WARM lookup must issue at most ONE Path.resolve chain
    (the candidate file), not four."""
    calls = {"n": 0}
    real_resolve = Path.resolve

    def counting_resolve(self, *a, **kw):
        calls["n"] += 1
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    assert ts.file_has_test_coverage("mymod.py", warm_index) is True
    assert calls["n"] <= 1, f"warm path did {calls['n']} resolve() chains"


def test_cached_scan_root_caches(warm_index, monkeypatch):
    ts._cached_scan_root.cache_clear()
    r1 = ts._cached_scan_root(str(warm_index))
    r2 = ts._cached_scan_root(str(warm_index))
    assert r1 is r2  # same object — cache hit
    info = ts._cached_scan_root.cache_info()
    assert info.hits >= 1 and info.misses == 1


def test_warm_semantics_unchanged(warm_index):
    assert ts.file_has_test_coverage("mymod.py", warm_index) is True
    assert ts.file_has_test_coverage("nocov.py", warm_index) is False
    # non-.py and test_ inputs still treated as covered
    assert ts.file_has_test_coverage("README.md", warm_index) is True
    assert ts.file_has_test_coverage("test_mymod.py", warm_index) is True
```

Extend `tests/governance/test_slice33_arc0_loop_sink.py` with:

```python
def test_sink_sync_emits_cpu_ms(caplog):
    import logging
    import time
    from backend.core.ouroboros.telemetry import loop_sink as ls

    caplog.set_level(logging.WARNING, logger=ls.logger.name)
    with ls.sink_sync("unit.test.cpu_attr", threshold_ms=1.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.02:
            pass  # genuine CPU burn
    line = next(
        r.getMessage() for r in caplog.records if "unit.test.cpu_attr" in r.getMessage()
    )
    assert "cpu_ms=" in line


def test_sink_sync_cpu_ms_low_for_sleep(caplog):
    import logging
    import re
    import time
    from backend.core.ouroboros.telemetry import loop_sink as ls

    caplog.set_level(logging.WARNING, logger=ls.logger.name)
    with ls.sink_sync("unit.test.sleep_attr", threshold_ms=1.0):
        time.sleep(0.05)  # wall time, ~zero CPU
    line = next(
        r.getMessage() for r in caplog.records if "unit.test.sleep_attr" in r.getMessage()
    )
    m = re.search(r"cpu_ms=([\d.]+)", line)
    assert m and float(m.group(1)) < 20.0  # wall was >=50ms, cpu near zero
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_stratification_warm_path_syscalls.py tests/governance/test_slice33_arc0_loop_sink.py -v 2>&1 | tail -12`
Expected: FAIL — no `_cached_scan_root`, no `_install_coverage_index_for_tests`, no `cpu_ms=` in emission.

- [ ] **Step 3: Implement target_stratification changes**

Module level (near `_COVERAGE_IDX_LOCK`, ~line 232):

```python
@functools.lru_cache(maxsize=32)
def _cached_scan_root(repo_root_str: str) -> Path:
    """Resolve-once cache for the authoritative scan root.

    file_has_test_coverage is invoked in tight per-file loops
    (operation_advisor.py:2155 sum(); opportunity_miner scan_once);
    each call previously issued up to 4 Path.resolve() syscall
    chains (lstat/readlink per component). Repo roots are stable
    for a process lifetime — cache the (auth-root ∘ resolve)
    composition. Bounded at 32 distinct roots (main repo + L3
    worktrees). Fail-soft mirrors the inline path."""
    try:
        from backend.core.ouroboros.governance.execution_context import (
            authoritative_repo_root as _auth_root,
        )
        root = _auth_root(Path(repo_root_str))
    except Exception:  # noqa: BLE001
        root = Path(repo_root_str)
    try:
        return root.resolve()
    except OSError:
        return root
```

(`import functools` at top if absent.) Inside `file_has_test_coverage`, replace the `_auth_root` block (lines ~549-557) AND the `_idx_key = _scan_root.resolve()` block (lines ~564-567) with:

```python
        _scan_root = _cached_scan_root(str(repo_root))
        ...
        _idx_key = _scan_root  # already resolved by the cache
```

In the Strategy-2 branch (~line 594), avoid `_strat_path_to_module`'s double resolve by inlining against the already-resolved root:

```python
        if _strat_ast_import_enabled():
            try:
                _fp = Path(file_path)
                if not _fp.is_absolute():
                    _fp = _scan_root / _fp
                try:
                    _rel = _fp.resolve().relative_to(_idx_key)
                except ValueError:
                    _rel = None
                if _rel is not None:
                    _parts = list(_rel.parts)
                    if _parts and _parts[-1].endswith(".py"):
                        _parts[-1] = _parts[-1][:-3]
                    if _parts and _parts[-1] == "__init__":
                        _parts = _parts[:-1]
                    _module_path = ".".join(_parts) if _parts else None
                    if _module_path and _idx.ast_map.get(_module_path):
                        return True
            except Exception:  # noqa: BLE001 — fail-soft, mirror Strategy 2
                pass
```

(This transcribes `_strat_path_to_module`'s exact part-munging — keep `_strat_path_to_module` itself untouched for its other callers.) Also add the test hook (module level, clearly marked):

```python
def _install_coverage_index_for_tests(scan_root: Path) -> None:
    """TEST-ONLY: synchronously build + register the coverage index
    for scan_root so unit tests can exercise the WARM path without
    the off-loop builder. Mirrors the tail of the real builder's
    registration (same lock, same dict)."""
    idx = _build_coverage_index_sync(scan_root.resolve())
    with _COVERAGE_IDX_LOCK:
        _COVERAGE_INDEXES[scan_root.resolve()] = idx
```

(The implementer must match the REAL names of the builder fn and registry dict — read `target_stratification.py:200-260`; if a same-purpose test hook already exists, use it instead and delete this one from the plan.)

Fix the docstring lie in `execution_context.py:146-147`: replace `Authority posture: pure path math, zero I/O, zero git calls.` with `Authority posture: no git calls, no directory walks. NOTE: Path.resolve() DOES issue lstat/readlink syscalls per component — callers in tight loops should cache (see target_stratification._cached_scan_root).`

- [ ] **Step 4: Implement loop_sink cpu_ms attribution**

In `loop_sink.py` — `_emit_blocked` (line ~270) gains an optional kwarg and emits it:

```python
def _emit_blocked(
    callsite: str,
    elapsed_ms: float,
    threshold_ms: float,
    kind: str,
    cpu_ms: Optional[float] = None,
) -> None:
    if cpu_ms is None:
        logger.warning(
            "[LoopSink] callsite=%s kind=%s blocked_ms=%.2f "
            "threshold_ms=%.1f — on-loop call exceeded threshold",
            callsite, kind, elapsed_ms, threshold_ms,
        )
        return
    logger.warning(
        "[LoopSink] callsite=%s kind=%s blocked_ms=%.2f cpu_ms=%.2f "
        "threshold_ms=%.1f — on-loop call exceeded threshold "
        "(wall>>cpu = scheduler/GIL/syscall-wait inflation, not "
        "callsite CPU)",
        callsite, kind, elapsed_ms, cpu_ms, threshold_ms,
    )
```

In `sink_sync`: capture `c0 = time.thread_time()` next to `t0 = time.monotonic()` (wrap in `try/except` — `thread_time` exists on 3.7+, but stay defensive: on failure set `c0 = None`); in the `finally`, compute `cpu_ms = (time.thread_time() - c0) * 1000.0 if c0 is not None else None` and pass `cpu_ms=cpu_ms` to `_emit_blocked`. `sink_async` unchanged (its wall-vs-await semantics are documented; cpu attribution of an async region spanning many loop turns would be misleading). `stats.record(...)` call unchanged — no schema change to the stats/leaderboard surface.

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/governance/test_stratification_warm_path_syscalls.py tests/governance/test_slice33_arc0_loop_sink.py -q 2>&1 | tail -5`
Expected: ALL PASS.

- [ ] **Step 6: Run guard suites for touched modules**

Run: `python3 -m pytest tests/governance/ -q -k "stratification or loop_sink or coverage_index" 2>&1 | tail -5`
Expected: ALL PASS (no existing warm/cold semantics regressed).

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/target_stratification.py \
        backend/core/ouroboros/governance/execution_context.py \
        backend/core/ouroboros/telemetry/loop_sink.py \
        tests/governance/test_stratification_warm_path_syscalls.py \
        tests/governance/test_slice33_arc0_loop_sink.py
git commit -m "perf(strat): warm-path resolve caching + LoopSink cpu_ms attribution (Slice 3 T3)

file_has_test_coverage is already index-backed off-loop; Run #13's
32 LoopSink hits were (a) up to 4 redundant Path.resolve() syscall
chains per call in tight per-file loops and (b) wall-clock
over-attribution under GIL/scheduler saturation. Cache the
(auth-root . resolve) composition per repo root (lru 32), drop the
warm path to <=1 resolve, and emit cpu_ms alongside blocked_ms in
sink_sync so saturation stalls stop masquerading as callsite cost.
No thresholds changed, nothing suppressed.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: DRY consolidation — migrate the four scattered append wrappers

**Files:**
- Modify: `backend/core/ouroboros/aegis/spend_wal.py:195-199`
- Modify: `backend/core/ouroboros/governance/dw_capacity_ledger.py:222-228`
- Modify: `backend/core/ouroboros/governance/self_immunization.py:1188-1193,1252-1257,1572-1577`
- Modify: `backend/core/ouroboros/governance/swe_bench_pro/evaluator_trace_observer.py:816-824`
- Test: existing suites only (no new file)

**Interfaces:**
- Consumes: `async_flock_append_line` from Task 2 (exact signature above).

**Rationale:** Mandate 3 — these four modules each hand-rolled `asyncio.to_thread`/`run_in_executor(None, ...)` around the same primitive. One canonical substrate; the substrate's master-off inline degrade also unifies rollback behavior.

- [ ] **Step 1: Convert each site**

`dw_capacity_ledger.py:224-226` — replace:
```python
            await asyncio.to_thread(flock_append_line, self._path, line)
```
with:
```python
            await async_flock_append_line(self._path, line)
```
(adjust the import at the top of the file: `from backend.core.ouroboros.governance.cross_process_jsonl import async_flock_append_line` — keep the sync import if other call sites use it). Apply the same mechanical replacement at `self_immunization.py:1190-1191, 1254-1255, 1574-1575` (`await loop.run_in_executor(None, flock_append_line, self._path, line)` → `await async_flock_append_line(self._path, line)`) and `evaluator_trace_observer.py:820-822` (KEEP its existing no-running-loop sync fallback branch byte-identical — only the executor branch converts).

For `spend_wal.py:197`: its wrapper offloads `append_entry_sync` (which does more than the append — read it first). Convert ONLY if `append_entry_sync` is a thin `flock_append_line` delegation; if it composes other work, leave it and note "left: composes beyond append" in the ledger. Honest scope beats forced uniformity.

- [ ] **Step 2: Run the four modules' suites**

Run: `python3 -m pytest tests/governance/ tests/aegis/ -q -k "spend_wal or capacity or immunization or evaluator_trace" 2>&1 | tail -5`
Expected: ALL PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/dw_capacity_ledger.py \
        backend/core/ouroboros/governance/self_immunization.py \
        backend/core/ouroboros/governance/swe_bench_pro/evaluator_trace_observer.py
# plus spend_wal.py IF converted
git commit -m "refactor(jsonl): consolidate scattered append offload wrappers onto canonical async helper (Slice 3 T4)

Four modules each hand-rolled to_thread/run_in_executor around
flock_append_line; all now route through async_flock_append_line
(single substrate, unified master-off degrade).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Full regression + live loop-quietness validation (Run #14 gate)

**Files:** none (verification only)

- [ ] **Step 1: Full sprint regression**

Run: `python3 -m pytest tests/governance/test_evidence_capture_offload.py tests/governance/test_evidence_capture_and_consumers.py tests/governance/test_async_flock_append.py tests/governance/test_cross_process_jsonl.py tests/governance/test_cross_process_jsonl_durable.py tests/governance/test_phase_7_8_cross_process_flock.py tests/governance/test_stratification_warm_path_syscalls.py tests/governance/test_slice33_arc0_loop_sink.py tests/governance/test_posture_observer.py -q 2>&1 | tail -5`
Expected: ALL PASS, 0 failures.

- [ ] **Step 2: Broad governance smoke (catch import/API breaks)**

Run: `python3 -m pytest tests/governance/ -q -x --timeout=300 2>&1 | tail -8`
Expected: PASS (pre-existing failures, if any, must be shown UNCHANGED vs a `git stash` baseline run before claiming green).

- [ ] **Step 3: Whole-branch review, then Run #14**

Dispatch the final whole-branch reviewer (same template as Slice 2). After MERGE-READY: merge to main in an OCA-idle window (ff-only), then re-ignite the A1 validation exactly as Run #13 (Docker up; NO manual chaos pre-arm — the isomorphic driver injects post-boot; `.env`-loaded env; `scripts/ignite_a1_soak.py --max-wall-seconds 5000`). Success signals, in order: `ControlPlaneStarvation` count ≈ 0 for the whole session (Run #13: 81), zero LoopDeadman tombstones, `TOOL OUTPUT BEGIN` ≥ 1, `files_changed>0`, AutoCommit, `a1_verdict.json: proven=true`.

---

## Self-Review (completed at write time)

1. **Spec coverage:** Mandate 1 → no watchdog/threshold edits anywhere (Task 3 explicitly adds attribution without touching threshold logic); Mandate 2 → all three paths cross into the pool via `offload` from `async def` sites; Mandate 3 → single `_offload_fs` helper per module boundary using the substrate, sync logic single-sourced via precomputed params, Task 4 removes existing duplication; Mandate 4 → concurrency/ordering/timeout/non-reentrancy tests in Task 2 Step 1, ordering preserved by awaited in-sequence appends. All three named targets have a task; the diagnosed real mechanisms (not the naive re-offload for target #2) are what get fixed.
2. **Placeholder scan:** Two intentional verify-first instructions remain (orchestrator.py:2976 async-context check; `_install_coverage_index_for_tests` real-name check; spend_wal thin-wrapper check) — these are explicit implementer verifications with fallback behavior specified, not TBDs.
3. **Type consistency:** `async_flock_append_line(path, line, *, timeout_s=None) -> bool` used identically in Tasks 2 and 4; `record_async` returns `Tuple[bool, str]` matching `record`; `stamp_*_async` return types match their sync counterparts (`int`, `Dict[str, int]`).
