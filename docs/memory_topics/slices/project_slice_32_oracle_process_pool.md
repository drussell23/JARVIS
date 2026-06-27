---
title: Root cause
modules: []
status: historical
source: project_slice_32_oracle_process_pool.md
---

PR #59222 squash-merged 2026-05-27 at `6274f76e37`. Branch `ouroboros/slice-32-process-pool-isolation`. Closes v25 (`bt-2026-05-27-194342`) control-plane wedge — 25-min asyncio loop freeze (13:34→14:00); LoopDeadman fired `os._exit(75)` after 1531.6s without heartbeat.

# Root cause

`Oracle._index_file` dispatched parse + `CodeStructureVisitor.visit` via default `ThreadPoolExecutor`. Worker threads hold GIL during pure-Python AST traversal. With N workers + 29k-file repo, asyncio event loop starves → cascade to wedge.

# Slice 32 fix — composition, not duplication

Per operator binding ("build cleanly on what already exists"): routed through EXISTING `ast_compile_helper.py` module-singleton `ProcessPoolExecutor` (spawn ctx). Oracle becomes 2nd consumer alongside OpportunityMiner. NO parallel pool.

# New surface in `ast_compile_helper.py`

- `OracleAnalysisResult` frozen dataclass (reuses `AnalyzeOutcome`+`ExecutionMode` — no new enums)
- `_worker_analyze_for_oracle_in_process` module-level worker (lazy-imports `CodeStructureVisitor` inside body to avoid main-process cycle; spawn-resolvable qualname)
- `analyze_python_source_for_oracle` public async coro (mirrors `analyze_python_source_for_opportunity_miner` shape)
- `JARVIS_ORACLE_SLOW_CALL_ALERT_MS` default 30s — structured `oracle_slow_call` WARN; loop keeps ticking (observability, not abort)

# IPC payload discipline

Worker returns `list[NodeData] + list[Tuple[NodeID, NodeID, EdgeData]] + content_hash + worker_elapsed_ms`. NodeID frozen dataclass; NodeData/EdgeData plain dataclass; NodeType/EdgeType enums — all transitively IPC-safe. **NO `ast.AST` ever crosses IPC** (operator binding, AST-pin enforced).

# oracle.py wiring

`TheOracle._index_file`:
1. Read content via `asyncio.to_thread` (I/O releases GIL)
2. Hash + skip-unchanged on main thread (cheap)
3. If unchanged → return (no IPC roundtrip)
4. Else dispatch to `analyze_python_source_for_oracle`
5. Apply nodes/edges to graph (same as legacy)

Legacy `_read_parse_visit_blocking` preserved verbatim. Escape hatch `JARVIS_ORACLE_LEGACY_THREAD_MODE=1` restores pre-Slice-32 path byte-identically. Default **off**.

# Test surface

4 AST pins + 7 spine = 11 tests; all green. Slice 11 pins updated to admit Slice 32's legitimate additions (`_worker_analyze_for_oracle_in_process` in parse-cage; `analyze_python_source_for_oracle` + `OracleAnalysisResult` in `__all__`). Both carry Slice 32 attribution.

Key spine test: `test_spine_main_loop_stays_responsive_during_process_dispatch` — heartbeat sibling coro ticks ≥2 over parse window. Structural inverse of v25 wedge.

# Verification

- Slice 32: 11/11
- Slice 11 + Slice 20+ + Aegis bridge: 264/264
- Smoke (10 KB synthetic): outcome=ok, execution_mode=process, 181 NodeData + 420 edges round-tripped, worker 58ms

# v26 expectation

Bearer gate (Slice 31) holds + asyncio loop responsive (Slice 32). If failure persists, surface has moved one layer further — `tool_loop_starved` fallback OR true DW capability question. No methodology-bar euphoria; v26 RESOLVED is the capability bar.

Related: [[project_slice_31_aegis_session_bearer]] (bearer gate), [[project_slice_30_explicit_parameter_threading]] (adaptive timeout reaching DW), [[feedback_no_preresult_euphoria]].
