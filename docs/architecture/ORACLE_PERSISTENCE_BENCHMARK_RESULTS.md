# Oracle Persistence — Synthetic Quad-Modal Benchmark Results

> **Gate:** `ORACLE_PERSISTENCE_ADD.md` §9 (Benchmark Gate). Phase-2 implementation is
> blocked until empirical data picks the persistence backend. This document is that data.
> **Harness:** `scripts/benchmarks/oracle_persistence_benchmark.py` (self-contained, repeatable).

## TL;DR — the data validates SQLite, but *not* for the reason the ADD assumed

The race was framed as tri-modal (monolithic pickle vs chunked pickle vs aiosqlite). During
execution the per-file chunked mode exposed a filesystem-overhead problem on load, so I added a
**fourth** mode — *coarse* chunking (shard by package, ~50 shards) — to test the obvious fix.

That fourth mode is what makes the verdict trustworthy: **coarse chunking fixes the load
regression but destroys the checkpoint advantage**, because a realistic scatter of edits touches
nearly every coarse shard. SQLite is the *only* design that is good on both axes at once — its
update granularity (row) is decoupled from its read granularity (bulk SELECT), so it sidesteps
the chunk-size tension entirely.

## Synthetic payload

Exact production dataclasses (`NodeID`, `NodeData`, `EdgeData`, `NodeType`, `EdgeType` imported
from `oracle.py` — no mocks):

- **24,000 files × 4 entities = 120,000 nodes**, 512-byte docstrings each
- **192,000 edges** (CALLS / IMPORTS / INHERITS mix)
- Incremental-checkpoint probe: **500 new nodes** scattered across packages

This is ~300 MB of live `DiGraph` in RAM — a faithful 1:N scale model of the real
`codebase_graph.pkl` (the real graph is ~1.4 GB; load/checkpoint costs scale ~linearly, the
*relative* ranking is what transfers).

## Telemetry (two consecutive runs — ranking is stable; absolute ms vary with machine load)

```
MODE                         save_s  disk_MB   TTFR_s  loop_lag_ms  load_RAM_MB  ckpt500_ms
-------------------------------------------------------------------------------------------- run 1
A monolithic-pickle            0.64       29     5.74        361.2          345       476.8
B chunked-pickle (per-file)    3.67       49     9.12        357.7          354        14.7
D chunked-coarse (~50 shards)  0.80       30     6.02        298.3          300       712.8
C aiosqlite-normalized         2.52      184     7.93        457.9          411        47.2
-------------------------------------------------------------------------------------------- run 2
A monolithic-pickle            0.73       29     5.61        314.3          345       482.9
B chunked-pickle (per-file)    3.69       49     9.04        358.7          354        16.8
D chunked-coarse (~50 shards)  0.78       30     5.98        293.0          300       648.5
C aiosqlite-normalized         2.51      184     7.90        458.0          411        44.8
```

**Metric definitions:**
- `TTFR_s` — time-to-first-ready: full deserialize **+** `DiGraph` reconstruction, run via
  `asyncio.to_thread` for all modes (production loads off-loop). This is boot latency.
- `loop_lag_ms` — peak event-loop stall during load, measured by a concurrent sleep-overshoot
  heartbeat. **Fairness fix:** an earlier draft reconstructed mode C on-loop and A/B off-loop,
  producing a bogus 12 s lag for C; all four now reconstruct in `to_thread`, so this column
  isolates GIL contention only.
- `ckpt500_ms` — cost to persist 500 new scattered nodes incrementally (the hot path during
  live indexing — the workload that caused the cold-index `ControlPlaneStarvation` wedge).

## Findings

### 1. Load (TTFR) — the chunk-granularity tension
`A (5.7s) < D (6.0s) < C (7.9s) < B (9.1s)`

- Monolithic pickle is fastest — one `pickle.load`, no per-shard overhead.
- **Per-file chunking (B) is +59% slower** — opening/reading 24,000 shard files dominates;
  the format itself is fine, the *filesystem* is the tax.
- **Coarse chunking (D) erases that tax** (+5% over monolithic) — confirming the FS-overhead
  diagnosis. ~50 shards = ~50 opens.
- SQLite (C) is +38% — one file, but row→object marshalling costs more than a flat unpickle.

### 2. Incremental checkpoint — where chunking betrays itself
`B (15ms) < C (46ms) ≪ A (480ms) < D (680ms)`

- Monolithic (A) must rewrite the **entire** file on every checkpoint (480ms here; seconds at
  1.4 GB). **This is the root cause of the cold-index starvation** — the index never got to
  checkpoint cheaply, so write pressure competed with the FSM control plane.
- Per-file chunking (B) wins (15ms) — but only because each shard is tiny.
- **Coarse chunking (D) is the *worst* (680ms)** — the surprise. 500 scattered nodes touch
  nearly all ~50 coarse shards, forcing a read-modify-write of almost the whole graph. Coarse
  shards buy load speed by sacrificing the entire reason to chunk.
- SQLite (C) stays cheap (46ms) under the same scatter — row-level `UPSERT` is indexed by key,
  immune to the file-grouping scatter problem.

### 3. Disk & RAM
- Disk: `A (29MB) ≈ D (30MB) < B (49MB) < C (184MB, 6.3×)`. SQLite pays for indices + row
  overhead. Acceptable on any modern disk; worth noting.
- RAM: all ~300–411 MB; the live `DiGraph` dominates regardless of on-disk format.

### 4. The decisive trade matrix

| Mode | Load | Checkpoint | Disk | Fatal flaw |
|------|------|-----------|------|------------|
| A monolithic | **best** | terrible | best | full-rewrite checkpoint → the starvation we observed |
| B chunk/file | worst | **best** | ok | 24k-file load tax |
| D chunk/coarse | good | worst | best | scatter touches every shard → checkpoint collapses |
| **C aiosqlite** | ok (+38%) | **near-best** | worst (6×) | none fatal |

## Verdict — proceed with aiosqlite (ADD §9 satisfied, with a caveat)

**SQLite is the only mode without a fatal axis.** Every chunking strategy faces an irreducible
tension: shard size cheap to *load* (coarse) is expensive to *incrementally write* (scatter),
and vice versa. SQLite dissolves the tension because read granularity (bulk SELECT) and write
granularity (indexed row) are independent.

The §9 gate asked for a candidate that **beats monolithic on checkpoint without regressing load.**
Strictly, *no* mode is free — SQLite beats monolithic on checkpoint by **~10×** but regresses
load by **~38%**. That trade is correct for this system because:

1. The observed failure was **write-path starvation during indexing**, not load latency.
   SQLite's 10× cheaper checkpoint targets the actual wound.
2. Load happens **once per boot** and is already mitigated by Phase-1 Adaptive Backpressure;
   checkpoint happens **continuously** as files change.
3. The +38% (~2.2 s here; tens of seconds at 1.4 GB cold) is a bounded, one-time, warm-cacheable
   cost — and the warm `.db` means cold loads become rare.

**Caveat carried into Phase 2:** the +38% load and 6× disk are real. The implementation should
(a) keep the warm-cache path dominant so cold loads are exceptional, and (b) re-run this harness
against the *real* 1.4 GB graph before graduation to confirm the linear-scale assumption holds.

Rejected, with data:
- **Monolithic pickle** — its full-rewrite checkpoint is the documented cause of the cold-index
  wedge. Phase-1 backpressure treats the symptom; it does not make a 480ms→multi-second
  checkpoint cheap.
- **Chunked pickle (any granularity)** — fine vs coarse is a lose-one-axis dial. No setting wins
  both load and scatter-checkpoint, and the format offers no transactional integrity.

## Reproduce

```bash
cd <repo-root>
PYTHONPATH=$(pwd) python3 scripts/benchmarks/oracle_persistence_benchmark.py
```
