# Oracle Live Indexer — Design Document

**Date:** 2026-03-09
**Goal:** Wire the existing `TheOracle` GraphRAG system into the `GovernedLoopService` production path so all three repos (JARVIS, jarvis-prime, reactor-core) are indexed on boot, kept live after every applied patch, and surfaced to `ContextExpander` so J-Prime receives a real file manifest instead of guessing paths blind.

---

## Problem

`TheOracle` (`backend/core/ouroboros/oracle.py`) is a fully-built 1877-line GraphRAG system with multi-repo support, 12 edge types, blast-radius computation, dead-code detection, and a `get_relevant_files_for_query()` method — but it is never initialized in production. `GovernedLoopService` (Zone 6.8) is the real production entry point; `engine.py` is dead code. The Oracle's graph cache at `~/.jarvis/oracle/codebase_graph.pkl` is 316 bytes — essentially empty. `ContextExpander` guesses file paths blind because it has no file manifest to offer J-Prime.

## What Is Not Changing

- `PatchBenchmarker`, `CurriculumPublisher`, `ModelAttributionRecorder` and all Phase 2 wiring are untouched.
- The benchmark/curriculum/performance record paths in `GovernedOrchestrator` are untouched.
- `ContextExpander`'s existing expansion loop (MAX_ROUNDS=2, MAX_FILES=5) is untouched — Oracle enrichment is purely additive.
- `GovernanceStack` existing fields are unchanged; one new optional field is added.

---

## Architecture

### New Component: `_oracle_index_loop` (GovernedLoopService background task)

Fits the existing background-task pattern (health probe, curriculum, reactor events). Fires from `start()` as a non-blocking `asyncio.create_task`. The service never waits for indexing to complete before becoming ACTIVE.

```
GovernedLoopService.start()
  └─ asyncio.create_task(_oracle_index_loop())   <- non-blocking, 5th background task

_oracle_index_loop():
  1. oracle = TheOracle()
  2. await oracle.initialize()        <- loads serialized cache OR runs full_index() if stale
  3. self._oracle = oracle
  4. self._stack.oracle = oracle      <- now available to orchestrator + context expander
  5. loop: await asyncio.sleep(incremental_poll_interval_s)
           await oracle.incremental_update([])   <- scans all 3 repos for changed files
  6. on CancelledError: await oracle.shutdown(); return
  7. on any Exception: log structured warning, self._oracle = None, return
```

### New Field: `GovernanceStack.oracle`

```python
oracle: Optional["TheOracle"] = None
```

Set by `GovernedLoopService` after successful initialization. Read by `GovernedOrchestrator` and `ContextExpander` via `self._stack.oracle`. `None` until indexed — all callers check before use.

### New Fields: `GovernedLoopConfig`

```python
oracle_enabled: bool = True
oracle_incremental_poll_interval_s: float = 300.0   # 5-minute change scan
```

### Modified: `ContextExpander`

Constructor receives `oracle: Optional[TheOracle] = None`. Before building the planning prompt, if oracle is ready (`oracle.get_status().get("running", False)`), queries:

```python
candidate_files = await oracle.get_relevant_files_for_query(ctx.description, limit=20)
```

Injects the result as an "Available files" section into the planning prompt:

```
Available files related to this task (real paths, choose from these):
  - backend/core/ouroboros/governance/orchestrator.py
  - backend/core/ouroboros/governance/op_context.py
  ...

Which of these (if any) would help you generate a correct patch?
```

If oracle is `None`, not ready, or raises — silently falls back to current baseline behavior (J-Prime guesses). No exception escapes `expand()`.

### Modified: `GovernedOrchestrator` — COMPLETE-phase incremental update

Two call sites, both fault-isolated. Neither can change `OperationPhase`.

**Single-repo path** (after VERIFY, before COMPLETE):
```python
applied_files = [Path(p).resolve() for p in ctx.target_files]
if getattr(self._stack, "oracle", None) is not None:
    await self._stack.oracle.incremental_update(applied_files)
```

**Cross-repo saga path** (after verify pass, before COMPLETE):
```python
applied_files = [
    (self._config.repo_registry.get(repo).local_path / rel_path).resolve()
    for repo, patch in patch_map.items()
    for rel_path, _ in patch.new_content
]
if getattr(self._stack, "oracle", None) is not None and applied_files:
    await self._stack.oracle.incremental_update(applied_files)
```

Both wrapped in `try/except Exception` with `logger.warning`. `asyncio.CancelledError` propagates naturally.

---

## Data Flow

```
Boot:
  GovernedLoopService.start()
    └─ _oracle_index_loop (background)
         └─ TheOracle.initialize()
              ├─ _load_cache()    -> fast path if graph cache is fresh
              └─ full_index()     -> walks JARVIS + jarvis-prime + reactor-core
                   └─ CodeStructureVisitor (AST) x N files
    └─ self._stack.oracle = oracle   (now available)

Per operation:
  ContextExpander.expand(ctx)
    └─ oracle.get_relevant_files_for_query(description, 20)
         └─ NetworkX BFS from keyword-matched nodes
    └─ inject manifest into J-Prime planning prompt

  GovernedOrchestrator VERIFY -> COMPLETE:
    └─ oracle.incremental_update(applied_files)
         └─ re-index changed files
         └─ update graph edges (imports, calls, inheritance)

Incremental (every 5 min):
  _oracle_index_loop polls:
    └─ oracle.incremental_update([])
         └─ _scan_for_changes() per repo
         └─ re-indexes only changed files (MD5 hash comparison)
```

---

## Fault Isolation

| Failure | Effect | Behavior |
|---------|--------|----------|
| Oracle `initialize()` raises | `_oracle` stays `None`, task exits | Service starts ACTIVE/DEGRADED normally |
| Oracle not ready at expand time | `stack.oracle` is `None` | ContextExpander uses baseline J-Prime guessing |
| `get_relevant_files_for_query` raises | Caught in expand() | Falls back to baseline silently |
| `incremental_update` raises | Caught in try/except Exception | Warning log, operation reaches COMPLETE normally |
| `asyncio.CancelledError` in index loop | Propagates to task, Oracle shuts down | Service stop proceeds cleanly |

---

## Tests (4 new)

1. **oracle indexer failure does not fail service start** — patch `TheOracle.initialize` to raise RuntimeError; assert service reaches ACTIVE/DEGRADED
2. **context expander fallback when oracle not ready** — oracle.get_status() returns `{"running": False}`; assert expand() completes without raising
3. **COMPLETE-phase incremental_update invoked with expected paths** — mock `stack.oracle`; run single-repo VERIFY path; assert `incremental_update` called with resolved paths
4. **oracle update exception does not change terminal state** — `incremental_update` raises RuntimeError; assert ctx.phase == COMPLETE

---

## Env Var Note

`OracleConfig` reads `JARVIS_PATH` / `JARVIS_PRIME_PATH` / `REACTOR_CORE_PATH`. These default correctly to `~/Documents/repos/JARVIS-AI-Agent`, `~/Documents/repos/jarvis-prime`, `~/Documents/repos/reactor-core` — matching actual disk locations on this machine. No `.env` changes required.

---

## Out of Scope

- ChromaDB semantic embedding (Step 3 in the broader roadmap)
- Dead code / semantic OpportunityMiner signals (Step 6)
- J-Prime tool-use loop (Step 4)
- Self-model document (Step 5)
