# Oracle SQLite Hot-Path — Cold-Index Soak Results

Empirical validation of the Phase-2 **adaptive transactional batching** wired into
`_index_repository`. Harness: `scripts/soaks/oracle_sqlite_soak.py`.

## What was run

A real cold index of `backend/core` (1,645 source files) with
`JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED=1`, while a concurrent 50 ms heartbeat measured the
worst event-loop stall (a control-plane responsiveness proxy — the anti-starvation proof). Then a
fresh `TheOracle` warm-booted from the resulting SQLite db.

## Results

```
PHASE A — cold index (incremental SQLite)
  files indexed (graph nodes)   :    122,972
  edges                         :    225,698
  cold index wall time          :      29.67 s
  index rate                    :       4145 nodes/s
  db size on disk               :      145.1 MB
  peak RSS after index          :        409 MB
  *** max control-plane stall    :     309.7 ms (559 pings)
PHASE B — warm reboot (load from SQLite)
  warm load ok                  :       True
  warm load nodes               :    122,972
  warm load wall time           :      2.549 s
  speedup (cold/warm)           :         12x
VERDICT: PASS
```

## What this proves

1. **Cold-boot starvation eradicated.** The control plane's worst stall during the entire 30 s
   cold index was **309.7 ms** (single spike; 559 continuous heartbeats). The failure this work
   targets was `ControlPlaneStarvation` with `lag_ms=3209` — a **~10× improvement**. The old
   monolithic per-batch pickle rewrite held the GIL for multi-second stretches; the incremental
   `upsert_files` commits are small and `aiosqlite`-offloaded, so the loop keeps ticking.

2. **Incremental, not monolithic.** The db grew continuously during the index (observed live at
   139 MB mid-run → 145 MB final) — every adaptive batch committed its own files. There is no
   end-of-index whole-graph rewrite (final step flushes only the metrics meta row).

3. **Memory bounded.** Peak RSS held at **409 MB** for a 123k-node / 226k-edge graph — no
   accretion toward the historical 52 GB OOM loop that the monolithic pickle path produced.

4. **Warm boot is fast.** A second process restored all 122,972 nodes from SQLite in **2.55 s**
   (12× faster than the cold rebuild) — the cold-boot bottleneck is gone for warm starts.

## Adaptive transactional batching (how the commit window is set — no hardcoding)

The commit boundary **is** the Phase-1 AIMD batch boundary. After each batch, the incremental
commit's wall-time is folded back into the throttle:
`eff_lag = max(loop_lag_ms, commit_ms)` → `throttle.update(eff_lag)`. So when disk I/O throttles
(slow commit) **or** the event loop lags, the next batch contracts; when both are fast, it expands
toward the ceiling. The batch window tracks both control-plane responsiveness and disk write
latency, with zero hardcoded interval.

## ACID atomicity (Phase 2)

Every batch commits inside `AioSqliteProvider._write_txn` — an async context manager that opens
`BEGIN IMMEDIATE` (grabs the write lock upfront so `busy_timeout` applies) and guarantees a
deterministic async `ROLLBACK` if the body raises. A file-parse exception or mid-batch interrupt
rolls back to the last clean committed batch — no partial / orphaned nodes infiltrate the schema.
(Regression: `test_write_txn_rolls_back_on_error`.)

## Scope honesty — the production-scale gate is NOT yet cleared

This soak ran at **~123k nodes**. The real accreted production graph
(`~/.jarvis/oracle/codebase_graph.pkl`) is **2.67 GB** (~20× larger). Migrating it requires
loading that pickle into a live `DiGraph` first — ~10 GB+ RAM, the exact memory monster this work
replaces — which is unsafe on a laptop and belongs on the Linux production host.

**Therefore `JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED` stays default-OFF.** The default flip to ON
is gated on a clean production-host run of:

```bash
# On the production Linux host, with the real 2.67 GB pkl present:
PYTHONPATH=$(pwd) JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED=1 \
  python3 -u -c "import asyncio; from backend.core.ouroboros.oracle import get_oracle; \
  asyncio.run(get_oracle()._load_cache())"   # triggers one-time migrate_pickle_to_sqlite + warm load
```

That run confirms the migration ingests the full graph, archives the `.pkl`, and warm-boots from
SQLite within memory budget. Only then should the default be flipped.
