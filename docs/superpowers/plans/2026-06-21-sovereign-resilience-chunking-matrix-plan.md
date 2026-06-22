# Sovereign Resilience & Chunking Matrix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Turn a *dispatched* strategic GOAL into a *convergeable* one by (A) a self-healing transport circuit breaker that rotates DW traffic off a dead lane, and (B) adaptive recursive chunking that makes a BLOCKed GOAL build its own safety net (AST-symbol-scoped sub-goals + test-first prerequisite) instead of terminating.

**Architecture:** Pure, testable leaf modules wired into existing seams. Matrix A extends the FailbackFSM/topology-sentinel/dynamic-transport machinery; Matrix B extends `goal_decomposition_planner` + `multi_step_orchestrator` + the `orchestrator` BLOCK seam. No parallel systems.

**Tech Stack:** Python 3.9+ (stdlib `ast`, `asyncio.wait_for`), pytest. `from __future__ import annotations` in every new file. ASCII only.

**Spec (binding):** `docs/superpowers/specs/2026-06-21-sovereign-resilience-chunking-matrix.md`. **Diagnosis done** (live soak): batch-lane retrieval TIMEOUT while realtime healthy (1.7s); Advisor correctly BLOCKs whole-file blast radius.

## Global Constraints
- **No hardcoding:** every threshold/cap is a runtime function of live signals; env vars tune the *curve*, never a literal `MAX_*`. Fail-soft everywhere (new code NEVER crashes a dispatch/op).
- **Reuse-first / zero-duplication:** Matrix A MUST reuse `FailbackFSM` failure classification + `_RECOVERY_PARAMS` magnitudes + the Slice183 `batch_lane_healthy` signal — NO parallel health tracker. Matrix B's governor MUST reuse `MemoryPressureGate` + `SensorGovernor` patterns + `loop_sink` latency — NO parallel memory/load monitor. A reviewer MUST reject any task that builds a parallel health/memory tracker.
- **Safety gate inviolable:** chunking SATISFIES the OperationAdvisor, never bypasses it. A sub-goal that still trips the gate is re-evaluated, never force-passed.
- **OFF byte-identical:** masters `JARVIS_TRANSPORT_BREAKER_ENABLED` + `JARVIS_RECURSIVE_CHUNKING_ENABLED`; OFF ⇒ today's behavior exactly.
- **Pure AST only:** the scoper uses `ast.parse`/`ast.get_source_segment` — NEVER `exec`/`eval`/`compile(mode=exec)`.
- **Worktree:** verify writes via `git show`/`grep` (Edit-flush anomaly seen in `.claude/worktrees/*`). Commits need `ledger_sovereignty.mark_owned` (already stamped).
- **Phase order:** ALL of Matrix A (Tasks A1-A3) before ANY Matrix B task.

---

# MATRIX A — Sovereign Transport Circuit Breaker

## Task A1: `TransportCircuitBreaker` core (3-state, adaptive, jittered)
**Files:** Create `backend/core/ouroboros/governance/transport_circuit_breaker.py`; Test `tests/governance/test_transport_circuit_breaker.py`.

**Interfaces:**
- Produces:
  - `class BreakerState(enum.Enum): CLOSED; OPEN; HALF_OPEN`
  - `class TransportCircuitBreaker` with:
    - `record(self, lane: str, *, ok: bool, failure_mode: str | None = None, now: float) -> None` — feed an attempt outcome (`lane` in `{"batch","realtime"}`; `failure_mode` from FailbackFSM e.g. `"TIMEOUT"`/`"SERVER_ERROR"`/`"STREAM_STALL"`). `now` injected (testable clock).
    - `state(self, lane: str) -> BreakerState`
    - `select_lane(self, preferred: str, *, now: float) -> str` — returns the lane to actually use: if `preferred` is OPEN, returns the sibling; HALF_OPEN allows the preferred through (the probe); CLOSED returns `preferred`.
    - `due_for_probe(self, lane: str, *, now: float) -> bool` — True when an OPEN lane's jittered recovery deadline has elapsed (caller drives the async probe, Task A2).
    - `note_probe_result(self, lane: str, *, ok: bool, now: float) -> None` — HALF_OPEN→CLOSED on ok, →OPEN (longer timer) on fail.
  - `def breaker_enabled() -> bool` (reads `JARVIS_TRANSPORT_BREAKER_ENABLED`, default `false` for now).
  - module singleton `get_transport_breaker() -> TransportCircuitBreaker`.

- [ ] **Step 1: Read first** — `candidate_generator.py:1663-1671` (`_RECOVERY_PARAMS` TIMEOUT base=45/max=300, SERVER_ERROR base=60/max=600) to mirror magnitudes via env (do NOT import the dict; reference the same env-tunable values). Note the transient failure modes the FSM emits.
- [ ] **Step 2: Write failing tests** (`tests/governance/test_transport_circuit_breaker.py`):
```python
from __future__ import annotations
import importlib
from backend.core.ouroboros.governance import transport_circuit_breaker as tcb

def _fresh():
    importlib.reload(tcb)
    return tcb.TransportCircuitBreaker()

def test_closed_passes_preferred():
    b = _fresh()
    assert b.select_lane("batch", now=0.0) == "batch"
    assert b.state("batch") is tcb.BreakerState.CLOSED

def test_trips_open_after_adaptive_failures_and_rotates():
    b = _fresh()
    t = 0.0
    for _ in range(20):  # sustained batch failures
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t); t += 1.0
    assert b.state("batch") is tcb.BreakerState.OPEN
    # OPEN batch -> rotate to realtime
    assert b.select_lane("batch", now=t) == "realtime"

def test_success_keeps_closed():
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=True, now=t); t += 1.0
    assert b.state("batch") is tcb.BreakerState.CLOSED

def test_open_becomes_due_for_probe_after_jittered_timer():
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t); t += 1.0
    assert not b.due_for_probe("batch", now=t + 1.0)      # within recovery window
    assert b.due_for_probe("batch", now=t + 10_000.0)     # after any jittered window

def test_probe_success_closes_probe_fail_reopens_longer():
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t); t += 1.0
    t += 10_000.0
    assert b.due_for_probe("batch", now=t)
    b.note_probe_result("batch", ok=False, now=t)         # HALF_OPEN -> OPEN
    assert b.state("batch") is tcb.BreakerState.OPEN
    first_wait = b._recovery_deadline("batch") - t        # internal, longer each reopen
    t2 = b._recovery_deadline("batch") + 1.0
    b.note_probe_result("batch", ok=True, now=t2)         # probe ok -> CLOSED
    assert b.state("batch") is tcb.BreakerState.CLOSED

def test_failsoft_bad_input_never_raises():
    b = _fresh()
    b.record("bogus", ok=False, failure_mode=None, now=0.0)
    assert b.select_lane("bogus", now=0.0) == "bogus"      # unknown lane passes through

def test_off_byte_identical_select_is_identity(monkeypatch):
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "false")
    b = _fresh()
    for _ in range(50):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)
    # disabled: never rotates (caller checks breaker_enabled(); breaker still tracks)
    assert tcb.breaker_enabled() is False
```
- [ ] **Step 3: Run → fail** (`pytest tests/governance/test_transport_circuit_breaker.py -q`; ModuleNotFound).
- [ ] **Step 4: Implement** `transport_circuit_breaker.py`:
  - Per-lane state record: `BreakerState`, rolling outcome deque (bounded), `consecutive_open` count, `recovery_deadline`.
  - **Adaptive trip threshold:** trip when the failure *rate* over the rolling window exceeds an env-tunable ratio (`JARVIS_TRANSPORT_BREAKER_FAIL_RATIO`, default 0.5) AND the window has min samples (`JARVIS_TRANSPORT_BREAKER_MIN_SAMPLES`, default 5) — a function of observed traffic, NOT a fixed N.
  - **Jittered exp recovery:** `base = env(JARVIS_TRANSPORT_BREAKER_BASE_S, 60.0)`, `cap = env(..._MAX_S, 600.0)`; `wait = min(cap, base * 2**consecutive_open)`; jitter `±env(..._JITTER_FRAC, 0.2) * wait` derived deterministically from `(lane, consecutive_open)` hash (NO `random` — keeps tests deterministic and resume-safe). `_recovery_deadline(lane)` exposes it for tests.
  - `select_lane`: CLOSED→preferred; OPEN→`_sibling(preferred)`; HALF_OPEN→preferred (probe passes through).
  - `due_for_probe`: OPEN and `now >= recovery_deadline` → flips internal state to HALF_OPEN and returns True (once).
  - `note_probe_result`: HALF_OPEN + ok → CLOSED (reset counters); else OPEN (`consecutive_open += 1`, new deadline).
  - All methods fail-soft (try/except → no-op/identity). `_sibling`: batch↔realtime; unknown→same.
- [ ] **Step 5: Run → pass.** Commit: `feat(resilience): TransportCircuitBreaker core — 3-state adaptive jittered breaker (CLOSED/OPEN/HALF_OPEN)`.

## Task A2: HALF-OPEN async probe driver
**Files:** Modify `transport_circuit_breaker.py` (add async probe helper); Test add to `tests/governance/test_transport_circuit_breaker.py`.

**Interfaces:**
- Produces: `async def run_probe_if_due(breaker, lane, probe_fn, *, now) -> bool | None` — if `due_for_probe`, `await asyncio.wait_for(probe_fn(lane), timeout=env(JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S, 15.0))`, call `note_probe_result`, return the result; else return None. A probe that raises/times out counts as `ok=False`. `probe_fn` is injected (the dispatch path supplies a tiny realtime/batch ping).

- [ ] **Step 1: Failing test:**
```python
import asyncio
from backend.core.ouroboros.governance import transport_circuit_breaker as tcb
def test_run_probe_closes_on_success():
    b = tcb.TransportCircuitBreaker()
    for _ in range(20): b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)
    async def good(_lane): return True
    res = asyncio.run(tcb.run_probe_if_due(b, "batch", good, now=10_000.0))
    assert res is True and b.state("batch") is tcb.BreakerState.CLOSED
def test_run_probe_timeout_counts_as_fail():
    b = tcb.TransportCircuitBreaker()
    for _ in range(20): b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)
    async def hang(_lane):
        await asyncio.sleep(100)
    import os; os.environ["JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S"]="0.05"
    res = asyncio.run(tcb.run_probe_if_due(b, "batch", hang, now=10_000.0))
    assert res is False and b.state("batch") is tcb.BreakerState.OPEN
def test_run_probe_not_due_returns_none():
    b = tcb.TransportCircuitBreaker()
    assert asyncio.run(tcb.run_probe_if_due(b, "batch", None, now=0.0)) is None
```
- [ ] **Step 2: Run → fail.** **Step 3: Implement** `run_probe_if_due` (fail-soft; wait_for timeout → ok=False). **Step 4: Run → pass.**
- [ ] **Step 5: Commit:** `feat(resilience): HALF-OPEN async probe driver (bounded, self-healing, fail=timeout)`.

## Task A3: Wire breaker into the DW dispatch transport-selection path
**Files:** Modify `backend/core/ouroboros/governance/candidate_generator.py` (transport-selection + outcome-report sites; confirm exact lines via read-first); Test `tests/governance/test_transport_breaker_wiring.py`.

**Interfaces:**
- Consumes: A1 `get_transport_breaker`, `breaker_enabled`, `select_lane`, `record`; A2 `run_probe_if_due`.

- [ ] **Step 1: Read first** — find (a) the Slice183 dispatch-telemetry site that computes `batch_lane_healthy` / `FORCE_BATCH` (grep `batch_lane_healthy`), (b) the transport-selection point where batch vs realtime/SSE is chosen for a generation attempt, (c) the per-attempt failure-classification site (where `failure_source`/`mode=TIMEOUT` is known, ~`candidate_generator.py:3996-4095`). Record exact lines.
- [ ] **Step 2: Failing test** (structural + behavioral with a fake breaker): assert that when `breaker_enabled()` and the batch lane is OPEN, the selected transport is `realtime`; and that each attempt outcome calls `breaker.record(lane, ok=..., failure_mode=...)`. Use dependency injection / monkeypatch of `get_transport_breaker`. Example:
```python
def test_open_batch_forces_realtime(monkeypatch):
    from backend.core.ouroboros.governance import transport_circuit_breaker as tcb
    b = tcb.TransportCircuitBreaker()
    for _ in range(20): b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED","true")
    monkeypatch.setattr(tcb, "get_transport_breaker", lambda: b)
    # the candidate_generator transport selector, given preferred='batch', must return 'realtime'
    from backend.core.ouroboros.governance import candidate_generator as cg
    assert cg._breaker_select_transport("batch") == "realtime"  # thin seam added in Step 3
```
- [ ] **Step 3: Implement** a thin seam `_breaker_select_transport(preferred: str) -> str` in candidate_generator that: if `breaker_enabled()`, returns `get_transport_breaker().select_lane(preferred, now=time.monotonic())`, else returns `preferred` (OFF byte-identical). Call it at the transport-selection point so an OPEN batch lane forces realtime. At the per-attempt outcome site, call `get_transport_breaker().record(lane, ok=success, failure_mode=mode, now=...)` (guarded by `breaker_enabled()`). Schedule `run_probe_if_due` on the existing dispatch idle tick (probe_fn = a tiny realtime/batch generation ping). ALL guarded + fail-soft.
- [ ] **Step 4: Run** `pytest tests/governance/test_transport_breaker_wiring.py -q` + regression `-k "candidate_generator or topology or failback"` (note pre-existing reds). **Step 5: Commit:** `feat(resilience): wire TransportCircuitBreaker into DW dispatch — OPEN batch rotates to realtime, self-heals via probe`.

---

# MATRIX B — Adaptive Recursion Matrix (only after A1-A3)

## Task B1: `AstSymbolScoper` + syntactic-integrity gate (B1a)
**Files:** Create `backend/core/ouroboros/governance/ast_symbol_scoper.py`; Test `tests/governance/test_ast_symbol_scoper.py`.

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ScopedTarget: file_path: str; symbol: str; lineno: int; end_lineno: int` (symbol `""` = whole-file fallback).
  - `def isolate_symbols(file_path: str, description: str, *, hints: tuple[str,...] = ()) -> tuple[ScopedTarget, ...]` — parse the file AST, select top-level `ClassDef`/`FunctionDef` (and one level of methods) whose name appears in `description`/`hints`; for each, run the B1a integrity gate; return only validated `ScopedTarget`s. Empty/parse-failure → `(ScopedTarget(file_path, "", 0, 0),)` (whole-file degrade).
  - `def slice_is_valid(source_segment: str) -> bool` — B1a: `ast.parse(textwrap.dedent(segment))` round-trip; True iff structurally valid. NEVER exec/eval/compile-exec.

- [ ] **Step 1: Failing tests:**
```python
from __future__ import annotations
from backend.core.ouroboros.governance import ast_symbol_scoper as s

SRC = '''
import os
class SemanticIndex:
    def build(self):
        return 1
    def query(self, q):
        return q
def helper():
    return 0
'''
def test_isolates_named_class_method(tmp_path):
    p = tmp_path/"semantic_index.py"; p.write_text(SRC)
    out = s.isolate_symbols(str(p), "route SemanticIndex.build through subprocess")
    names = {t.symbol for t in out}
    assert "SemanticIndex.build" in names or "SemanticIndex" in names
    assert all(t.symbol == "" or t.lineno > 0 for t in out)
def test_slice_integrity_gate_rejects_severed_decorator():
    assert s.slice_is_valid("def f():\n    return 1\n") is True
    assert s.slice_is_valid("@deco\n") is False          # severed decorator, no def
    assert s.slice_is_valid("    return 1") is False       # orphaned body
def test_parse_failure_degrades_to_whole_file(tmp_path):
    p = tmp_path/"broken.py"; p.write_text("def (:\n")    # unparseable
    out = s.isolate_symbols(str(p), "fix thing")
    assert len(out) == 1 and out[0].symbol == ""
def test_no_match_degrades_to_whole_file(tmp_path):
    p = tmp_path/"semantic_index.py"; p.write_text(SRC)
    out = s.isolate_symbols(str(p), "totally unrelated description")
    assert out == (s.ScopedTarget(str(p), "", 0, 0),)
def test_never_execs(monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, "exec", lambda *a, **k: (_ for _ in ()).throw(AssertionError("exec called")))
    s.slice_is_valid("def f(): return 1")
```
- [ ] **Step 2: Run → fail. Step 3: Implement** using `ast.parse` + `ast.get_source_segment` (read file text, walk `tree.body`, match `node.name in description/hints`, extract segment, gate via `slice_is_valid`, build `ScopedTarget` with `node.lineno/end_lineno`). Wrap everything fail-soft → whole-file degrade. **Step 4: Run → pass.**
- [ ] **Step 5: Commit:** `feat(chunking): AstSymbolScoper — pure-AST symbol isolation + ast.parse integrity gate (B1a), whole-file degrade`.

## Task B2: Test-first prerequisite injection (decomposer)
**Files:** Modify `backend/core/ouroboros/governance/goal_decomposition_planner.py` (add symbol-scoped + test-first decompose path); Test `tests/governance/test_decomposition_test_first.py`.

**Interfaces:**
- Consumes: B1 `isolate_symbols`, `ScopedTarget`; existing `SubGoal` (`goal_decomposition_planner.py:330-359`), `SubGoalKind`.
- Produces: `def decompose_for_block(goal, *, zero_coverage: bool, scoper=isolate_symbols) -> tuple[SubGoal, ...]` — returns a topo-ordered tuple: when `zero_coverage`, index 0 is a `kind=SEQUENTIAL` test-gen SubGoal (`title="Generate PyTest suite for <symbols>"`, `target_files` = the symbol-bearing files), and the mutation SubGoal(s) carry `depends_on_sub_ids=(test_subgoal_id,)` and `target_files` narrowed to the scoped symbols' files.

- [ ] **Step 1: Read first** — `SubGoal` fields + how existing `heuristic_decompose` builds ids (`parent_id::step-NN`) + `_make_envelope_for_sub_goal` (`:729-793`).
- [ ] **Step 2: Failing test:**
```python
from backend.core.ouroboros.governance import goal_decomposition_planner as g
class _Goal:  # minimal RoadmapGoal stand-in
    goal_id="GOAL-001"; title="t"
    description="route SemanticIndex.build through subprocess"
    target_files=("backend/core/ouroboros/governance/semantic_index.py",)
def test_test_first_prepended_and_mutation_depends_on_it(tmp_path, monkeypatch):
    subs = g.decompose_for_block(_Goal(), zero_coverage=True)
    assert subs[0].kind is g.SubGoalKind.SEQUENTIAL
    assert "PyTest" in subs[0].title or "test" in subs[0].title.lower()
    mutation = [s for s in subs if s is not subs[0]]
    assert mutation and all(subs[0].sub_goal_id in s.depends_on_sub_ids for s in mutation)
def test_no_zero_coverage_no_test_subgoal():
    subs = g.decompose_for_block(_Goal(), zero_coverage=False)
    assert all("PyTest" not in s.title for s in subs)
```
- [ ] **Step 3: Implement** `decompose_for_block` reusing `isolate_symbols` for target narrowing + `SubGoal` construction (deterministic ids). **Step 4: Run → pass. Step 5: Commit:** `feat(chunking): test-first prerequisite injection — mutation sub-goal blocks on generated PyTest suite`.

## Task B3: `AdaptiveRecursionGovernor` (dynamic depth/fan-out, reuse MemoryPressureGate)
**Files:** Create `backend/core/ouroboros/governance/adaptive_recursion_governor.py`; Test `tests/governance/test_adaptive_recursion_governor.py`.

**Interfaces:**
- Consumes (REUSE — do NOT reimplement): `memory_pressure_gate.MemoryPressureGate` (pressure level), `telemetry/loop_sink` latency signal.
- Produces: `def recursion_budget(*, queue_len: int, loop_blocked_ms: float, pressure_level: int, depth: int) -> Budget` where `@dataclass(frozen=True) class Budget: allowed: bool; max_fanout: int; reason: str`. `allowed=False` when load is high or `depth` exceeds an *adaptive* ceiling = `f(queue_len, loop_blocked_ms, pressure_level)` (shrinks under load, expands when idle). NO literal depth cap — derive from signals; env knobs (`JARVIS_RECURSION_FANOUT_IDLE`, `..._QUEUE_SOFT`, `..._LOOP_MS_SOFT`) tune the curve.

- [ ] **Step 1: Read first** — `memory_pressure_gate.py` API (level enum + how to read it) to consume, not duplicate.
- [ ] **Step 2: Failing tests:**
```python
from backend.core.ouroboros.governance import adaptive_recursion_governor as gov
def test_idle_expands_fanout():
    b = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=0)
    assert b.allowed and b.max_fanout >= 3
def test_heavy_load_shrinks_to_one_or_blocks():
    b = gov.recursion_budget(queue_len=500, loop_blocked_ms=2000.0, pressure_level=3, depth=2)
    assert (not b.allowed) or b.max_fanout == 1
def test_depth_ceiling_is_adaptive_not_literal():
    shallow = gov.recursion_budget(queue_len=0, loop_blocked_ms=0.0, pressure_level=0, depth=4)
    deep_loaded = gov.recursion_budget(queue_len=300, loop_blocked_ms=1500.0, pressure_level=2, depth=4)
    assert shallow.allowed and not deep_loaded.allowed  # same depth, different verdict by load
def test_failsoft_bad_signals_blocks_safely():
    b = gov.recursion_budget(queue_len=-1, loop_blocked_ms=float("nan"), pressure_level=99, depth=0)
    assert isinstance(b.allowed, bool)
```
- [ ] **Step 3: Implement** the monotone budget function (higher load/depth → smaller fanout → block). Fail-soft: any bad input → `Budget(allowed=False, max_fanout=1, reason="failsoft")`. **Step 4: Run → pass. Step 5: Commit:** `feat(chunking): AdaptiveRecursionGovernor — dynamic depth/fan-out from queue+loop-latency+pressure (no literal caps)`.

## Task B4: Semantic DAG de-dup
**Files:** Create `backend/core/ouroboros/governance/recursion_dedup.py`; Test `tests/governance/test_recursion_dedup.py`.

**Interfaces:**
- Consumes (REUSE): an existing sha256 helper (`state_drift`/`oracle` hashing) — do NOT hand-roll a new hash scheme beyond calling it.
- Produces: `def subgoal_hash(scoped_targets: tuple[str,...], description: str) -> str`; `class AttemptLedger` with `seen(self, h: str) -> bool` and `mark(self, h: str) -> None` (bounded FIFO, env `JARVIS_RECURSION_LEDGER_SIZE` default 512). `def is_duplicate(h, ledger, active_plan_hashes: frozenset[str]) -> bool`.

- [ ] **Step 1: Failing tests:**
```python
from backend.core.ouroboros.governance import recursion_dedup as d
def test_hash_stable_and_scope_sensitive():
    h1 = d.subgoal_hash(("a.py::F",), "do x")
    assert h1 == d.subgoal_hash(("a.py::F",), "do x")
    assert h1 != d.subgoal_hash(("a.py::G",), "do x")
def test_ledger_dedup():
    led = d.AttemptLedger()
    h = d.subgoal_hash(("a.py::F",), "x")
    assert not d.is_duplicate(h, led, frozenset())
    led.mark(h)
    assert d.is_duplicate(h, led, frozenset())
def test_active_plan_dup():
    led = d.AttemptLedger(); h = d.subgoal_hash(("a.py::F",), "x")
    assert d.is_duplicate(h, led, frozenset({h}))
```
- [ ] **Step 2: Run → fail. Step 3: Implement** (reuse sha256 helper; bounded `collections.deque`+set). **Step 4: Run → pass. Step 5: Commit:** `feat(chunking): semantic DAG de-dup — bounded attempt ledger + active-plan cross-check (no infinite recursion)`.

## Task B5: BLOCK → decompose → re-inject seam
**Files:** Modify `backend/core/ouroboros/governance/orchestrator.py:2396-2407` (the `AdvisoryDecision.BLOCK` handler); Test `tests/governance/test_block_decompose_reinject.py`.

**Interfaces:**
- Consumes: B2 `decompose_for_block`, B3 `recursion_budget`, B4 `subgoal_hash`/`AttemptLedger`/`is_duplicate`, `multi_step_orchestrator.emit_sub_goal_envelopes` (`:886-952`), `router.ingest`. `def chunking_enabled() -> bool` (`JARVIS_RECURSIVE_CHUNKING_ENABLED`, default false).

- [ ] **Step 1: Read first** — `orchestrator.py:2396-2407` (exact BLOCK block + how it reaches `ctx`/the Advisory's coverage+blast signals + how to reach the router from the orchestrator), `multi_step_orchestrator.emit_sub_goal_envelopes` signature.
- [ ] **Step 2: Failing test** (fakes for advisory + router): when `chunking_enabled()` + governor grants budget + not duplicate, a BLOCK with zero-coverage+high-blast yields parent `terminal_reason_code="decomposed"` and ≥1 `router.ingest` call with a test-first sub-goal ordered before mutation; when disabled → legacy `terminal_reason_code="advisor_blocked"` (byte-identical). Structural assertion that the BLOCK site calls `decompose_for_block` under the gate is acceptable for the deep wiring.
- [ ] **Step 3: Implement** the seam: replace the unconditional `CANCELLED advisor_blocked` with `if chunking_enabled() and recursion_budget(...).allowed and not is_duplicate(...): subs = decompose_for_block(goal, zero_coverage=...); valid = [s for s in subs if <B1a already enforced>]; await emit_sub_goal_envelopes(router, valid); ctx = ctx.advance(CANCELLED, terminal_reason_code="decomposed")` else legacy. Fail-soft: ANY error → legacy `advisor_blocked` path (never lose the op). Mark attempted hashes in the ledger. **Step 4: Run** + regression `-k "orchestrator or advisor or decomposition or multi_step"`. **Step 5: Commit:** `feat(chunking): BLOCK->decompose->re-inject seam — terminate 'decomposed' not 'failed', starvation-safe, fail-soft to legacy`.

## Task B6: Integration + OFF byte-identical
**Files:** Test `tests/governance/test_resilience_chunking_integration.py`.
- [ ] End-to-end with fakes: (A) batch-TIMEOUT storm → breaker OPEN → `select_lane` returns realtime → probe success → CLOSED. (B) BLOCK(zero-cov, high-blast) → governor budget → AST scope (integrity-gated) → test-first sub-goal + symbol-scoped mutation depends_on it → de-dup discards a repeat → `router.ingest` in topo order. Both masters OFF → legacy paths byte-identical. Reused-subsystem regression sweep (candidate_generator, topology_sentinel, orchestrator, goal_decomposition_planner, multi_step_orchestrator, memory_pressure_gate, intake). Commit.

## Done criteria
- Breaker self-heals (OPEN→HALF_OPEN probe→CLOSED, jittered, zero human action); OPEN batch rotates to the healthy realtime lane. Chunking makes a BLOCKed GOAL emit AST-symbol-scoped + test-first sub-goals (integrity-gated, de-duped, adaptively bounded) and re-inject without starvation; safety gate never weakened; no op lost. Both masters OFF byte-identical. Zero real regressions. Final cross-cutting coherence review (mandatory).
- **Live proof (future soak):** GOAL-001 → BLOCK → decompose → test-first + symbol-scoped sub-goals clear the Advisor → generation succeeds on the breaker-rotated realtime lane → orange PR.

## Self-review
- Spec coverage: A1/A2/A3 → §4 Matrix A; B1+B1a → §4 B1; B2 → B2; B3 → B3; B4 → B4; B5 → B5; B6 → §6. Reuse-first + no-hardcoding + safety-inviolable + OFF-byte-identical enforced in Global Constraints.
- New-module interfaces are self-consistent across tasks (`TransportCircuitBreaker`/`run_probe_if_due`; `ScopedTarget`/`isolate_symbols`/`slice_is_valid`; `decompose_for_block`; `recursion_budget`/`Budget`; `subgoal_hash`/`AttemptLedger`/`is_duplicate`).
- Big-file tasks (A3, B5) ship read-first instructions — confirm exact lines against live code.
