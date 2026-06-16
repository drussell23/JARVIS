# Architecture Design Document — Oracle Incremental Streaming Persistence (Phase 2)

**Status:** DESIGN (no implementation). Pre-requisite gate: §9 benchmark must pass before any code.
**Date:** 2026-06-16
**Author:** O+V Sovereign Engineering (Claude, on operator authorization)
**Depends on:** Phase 1 Adaptive Backpressure (#69541, merged) + Oracle teardown fix (#69538, merged).

---

## 0. Problem statement

The Oracle persists its `CodebaseKnowledgeGraph` (a NetworkX `DiGraph` of ~24k files →
~200k+ typed nodes/edges) as a **single ~1.4 GB `codebase_graph.pkl`**, written all-at-once
via `_save_cache` → `to_thread`. Two structural costs:

1. **Monolithic checkpoints.** Even the existing periodic checkpoint
   (`JARVIS_ORACLE_CHECKPOINT_EVERY_N_BATCHES`) rewrites the *entire* 1.4 GB graph each time.
   That is expensive enough that, pre-Phase-1, checkpoints rarely landed before the cold
   index starved/wedged — so every boot restarted cold (the chicken-and-egg).
2. **All-or-nothing durability.** A forced termination mid-write loses the whole checkpoint
   (mitigated by atomic `os.replace`, but the *content* is still whole-graph).

Phase 1 made the index a good citizen (it no longer starves the loop), so checkpoints can
now land. Phase 2 makes each checkpoint **incremental** — commit only what changed — so
durability is cheap, continuous, and resume-from-interruption is exact.

## 1. Goals / non-goals

**Goals**
- Incremental, streaming persistence: a forced kill loses at most the last *batch*, not the build.
- Cheap checkpoints (write only dirty rows), enabling frequent durability without GIL/I/O stalls.
- Exact resume: next boot loads committed state + re-indexes only files whose `source_hash` changed.
- Concurrency-safe: one index writer + many concurrent query readers, no `database is locked`.
- Bounded, non-wedging corruption recovery.

**Non-goals**
- No change to the in-memory graph API (`CodebaseKnowledgeGraph` stays NetworkX-backed at runtime).
- No multi-*process* writers (see §3 — the index is single-writer by construction).
- No query-engine redesign; SQLite is a persistence layer, not the live query substrate.

## 2. Current state (grounded)

| Type | Fields (verbatim from `oracle.py`) |
|---|---|
| `NodeID` (frozen) | `repo, file_path, name, node_type: NodeType, line_number` — str key `repo:file_path:name` |
| `NodeData` | `node_id, docstring, signature, decorators[], base_classes[], complexity, line_count, last_modified, source_hash` |
| `EdgeData` | `edge_type: EdgeType, line_number, context` |
| `_file_hashes` | `Dict[str→str]` (file_path → content hash; the skip-unchanged signal) |

Persistence: `_save_cache`/`_save_cache_blocking` pickle a data dict; `_load_cache` reads it;
path via `_resolved_graph_cache_path()` → `sandbox_fallback(GRAPH_CACHE_FILE)`.

## 3. Concurrency & Locking Armor

**Mandated:** `aiosqlite` + `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`.

**Candor — the real concurrency model (do NOT over-engineer multi-writer locking):**
- The index is **single-writer**. The AST-heavy parse runs in **`ProcessPoolExecutor`
  workers that return data via IPC** — workers never touch SQLite. Only the **main asyncio
  process** mutates the graph and therefore only the main process writes the DB.
- Readers (semantic_search, blast-radius, graph queries) are **N concurrent readers**.
- This is exactly **WAL's sweet spot: 1 writer + N readers, no reader/writer blocking.** A
  multi-process-writer locking scheme would be solving a problem we don't have.
- **`aiosqlite` is not "truly async"** — it runs sync SQLite on a per-connection worker
  thread. That is the right tool here (it keeps the event loop unblocked during the batch
  commit, composing with Phase-1 backpressure), but the doc states it honestly: it is
  *offloaded*, not lock-free magic. One writer connection (serialized commits) + a small
  reader-connection pool.

**Settings rationale:**
- `WAL`: concurrent readers during writes; the writer appends to the `-wal` file.
- `synchronous=NORMAL`: under WAL, durable across application crashes (only a power-loss in
  the wal-checkpoint window risks the last txn) — the right durability/throughput trade for a
  *rebuildable* cache. (`FULL` is overkill for derived data; `OFF` risks corruption.)
- `busy_timeout` (e.g. 5000 ms) so a transient reader/writer overlap retries instead of
  raising `database is locked`.
- `wal_autocheckpoint` tuned so the `-wal` file doesn't grow unbounded during a full cold index.

## 4. Schema Typology & Graph Serialization

Normalized relational schema (`schema_version` in `meta` gates migrations):

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);   -- schema_version, last_full_index_ts, repo set

CREATE TABLE nodes (
  node_key     TEXT PRIMARY KEY,        -- "repo:file_path:name" (NodeID.__str__)
  repo         TEXT NOT NULL,
  file_path    TEXT NOT NULL,
  name         TEXT NOT NULL,
  node_type    TEXT NOT NULL,           -- NodeType.value
  line_number  INTEGER NOT NULL DEFAULT 0,
  docstring    TEXT,
  signature    TEXT,
  decorators   TEXT,                    -- JSON array
  base_classes TEXT,                    -- JSON array
  complexity   INTEGER NOT NULL DEFAULT 0,
  line_count   INTEGER NOT NULL DEFAULT 0,
  last_modified REAL NOT NULL DEFAULT 0,
  source_hash  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_nodes_file ON nodes(file_path);   -- per-file dirty replace
CREATE INDEX idx_nodes_type ON nodes(node_type);

CREATE TABLE edges (
  src_key     TEXT NOT NULL,
  dst_key     TEXT NOT NULL,
  edge_type   TEXT NOT NULL,            -- EdgeType.value
  line_number INTEGER NOT NULL DEFAULT 0,
  context     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (src_key, dst_key, edge_type, line_number)
);
CREATE INDEX idx_edges_src ON edges(src_key);
CREATE INDEX idx_edges_dst ON edges(dst_key);      -- reverse traversal (blast radius)
CREATE INDEX idx_edges_file ON edges(src_key);     -- per-file dirty replace via join on nodes.file_path

CREATE TABLE file_hashes (
  file_path TEXT PRIMARY KEY,
  source_hash TEXT NOT NULL,
  indexed_at REAL NOT NULL
);
```

- **Lists** (`decorators`, `base_classes`) → JSON `TEXT` columns (small, low cardinality;
  not worth their own tables — YAGNI).
- **Per-file as the unit of incremental change.** A file's `source_hash` (already computed)
  is the dirty key. Re-indexing a file = `DELETE FROM nodes/edges WHERE file_path = ?` then
  re-insert that file's rows + upsert `file_hashes` — all in one transaction. Unchanged files
  are skipped entirely (the existing `_file_hashes` logic, now DB-backed).

## 5. Incremental batch-commit heuristic

- **Commit boundary = the existing index batch** (Phase-1 throttled). After each batch's
  files are parsed, write all their node/edge/hash rows in **one transaction**, then `COMMIT`.
- **Streaming guarantee:** because every batch is its own committed transaction, a forced
  kill loses *only the in-flight batch*. There is no separate "checkpoint" — **every commit
  is a checkpoint** (this *replaces* `JARVIS_ORACLE_CHECKPOINT_EVERY_N_BATCHES`, removing the
  monolithic whole-graph rewrite entirely).
- **Tunable cadence:** `JARVIS_ORACLE_SQLITE_COMMIT_EVERY_N_FILES` (default ~500 nodes-worth,
  i.e. coalesce small batches so we don't fsync per file) — bounds commit overhead while
  keeping at-most-one-batch loss. Coalescing composes with Phase-1's dynamic batch size.
- **Resume:** on boot, `file_hashes` tells the index which files are already current; only
  changed/new files are re-parsed. No "load the 1.4 GB blob first" step.

## 6. Seamless Migration Path & corruption fallback

**Migration (one-time, idempotent):**
1. On boot, if `oracle.db` is absent but a legacy `codebase_graph.pkl` exists → load the
   pickle once, stream it into the DB (batched transactions), then mark `meta.migrated_from_pkl`.
   The pickle is retained (read-only) for one release as a rollback escape hatch.
2. If neither exists → cold index (Phase-1-throttled) populates the DB incrementally.

**Corruption fallback ladder (deterministic, bounded, non-wedging):**
1. On open: `PRAGMA quick_check` (cheap) → if not `ok`, escalate.
2. `PRAGMA integrity_check` (bounded via `asyncio.wait_for`); if it exceeds the deadline or
   reports corruption → **quarantine** the db (`rename oracle.db → oracle.db.corrupt.<ts>`),
   log `ORACLE_DB_QUARANTINED`, and fall through to (3).
3. **Rebuild from source** — a fresh DB + cold index, **Phase-1 backpressured** so the
   rebuild never starves the loop, and **teardown-bounded** (#69538) so a mid-rebuild kill
   exits cleanly. The graph is *derived data*; a quarantine→rebuild is a slow cold start,
   never a correctness loss (same invariant the pickle path already relied on).
4. All steps `asyncio.wait_for`-bounded; the event loop is never blocked on a DB op (every
   call is `aiosqlite` thread-offloaded). No fallback path can wedge the FSM.

**Master flag:** `JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED` (default **false** until §9
benchmark passes + a soak proves resume). OFF = byte-identical legacy pickle path.

## 7. Risks & open questions (the gate that must clear first)

1. **LOAD performance is the make-or-break risk.** Pickle loads the whole graph in one
   deserialize; SQLite must **reconstruct the NetworkX DiGraph from ~200k+ rows** at boot.
   If full-graph load from SQLite is *slower* than the pickle load, Phase 2 trades cheap
   checkpoints for a slower boot — possibly a net loss. **This is not assumable; it must be
   measured (§9) before any implementation.**
2. **Memory:** building the in-memory DiGraph from rows has the same peak memory as the
   pickle; SQLite doesn't reduce runtime RAM (it reduces *write* cost + enables lazy/partial
   load later, out of scope here).
3. **`aiosqlite` single-connection serialization** under a write-heavy cold index — commit
   throughput must keep up with the (throttled) parse rate; benchmark write TPS.
4. **Schema evolution:** `meta.schema_version` + a forward-only migration runner.

**Candor — a lower-risk alternative to weigh at the §9 gate:** a **chunked/delta pickle**
(per-file pickle shards + a manifest) would also give incremental commits + interruption-safe
resume, with a *faster* load path (mmap shards, no row→object reconstruction) and far less
blast radius than a relational migration. The benchmark should compare **three** options —
(a) status-quo monolithic pickle, (b) SQLite-normalized, (c) chunked-pickle — on **load
time, checkpoint cost, and resume correctness**, and the winner is chosen by data, not by the
mandate. Committing to SQLite *before* proving its load path would itself be a shortcut-by-
assumption.

## 8. Rollout & test strategy

- Pure-function tests: row↔dataclass (de)serialization round-trips for every NodeType/EdgeType.
- Concurrency test: 1 writer txn loop + N reader connections under WAL → zero `database is locked`.
- Resume test: index N files → kill mid-batch → reboot → assert only the in-flight batch
  re-indexed, graph identical to uninterrupted build.
- Corruption test: truncate/garble the db → boot quarantines + rebuilds, loop never blocks.
- Migration test: legacy pkl → DB → graph byte-equivalent to the pkl's graph.
- OFF-parity: master flag off → legacy pickle path byte-identical.

## 9. DECISION GATE (must pass before implementation)

Build a representative DB from the current graph and **benchmark, on real hardware:**
- **Full-graph load time** (SQLite vs chunked-pickle vs monolithic pickle) — target: ≤ pickle.
- **Checkpoint cost** per batch (must be ≪ the 1.4 GB monolithic rewrite).
- **Write throughput** vs the Phase-1-throttled parse rate (no write backlog).
- **Resume correctness** (graph equality after interrupt+resume).

**Proceed to implementation only if a candidate beats the monolithic pickle on checkpoint
cost without regressing load time.** Otherwise, re-scope (likely chunked-pickle) or keep the
monolithic pickle (now that Phase 1 lets it checkpoint at all).

---

*This ADD is the spec, not the build. Implementation is a separate plan slice, gated on §9.*
