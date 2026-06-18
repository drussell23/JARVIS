# SQLite-Backed Lazy Traversal — Architecture Design Document

> **Status:** DRAFT for operator review (brainstorm output; not yet a plan).
> **Author:** O+V architecture pass, 2026-06-18.
> **Goal:** Eliminate the **query-time resident-RAM wall**. Today every blast-radius / call-chain /
> dependency traversal runs over the whole in-memory NetworkX `DiGraph` (~5 GB for the 29k-file
> brain). Re-back traversals with on-demand SQLite reads + an adaptive cache so the full graph is
> *queryable* on a 16 GB host without ever loading it all — securing absolute local dominance.

---

## 0. Sequencing correction (load-bearing — read first)

The operator plan lists "graduate the Scoper to default-ON" as Phase 1. **It must come LAST, not
first.** The Scoper's between-subtree eviction leaves the in-memory `DiGraph` *partial* after a
constrained-host build. Every current query traverses that in-memory graph. So flipping the Scoper
default-ON **before** lazy traversal exists would silently return **incomplete query results** on
exactly the constrained hosts the Scoper targets. Once traversals read SQLite (this ADD), the
partial in-memory graph is irrelevant and the graduation becomes safe. **Order: Lazy Traversal
(this ADD) → then graduate the Scoper.** The Scoper graduation is folded in as the final slice (§9).

## 1. Context — what actually has to change

The traversal surface is smaller than it looks: **12 public query methods on `TheOracle` reduce to
~7 primitive graph operations** (the rest are filters/compositions). The full inventory lives in the
exploration map; the primitives are:

| Primitive (today, in-memory) | SQLite replacement (indexed) |
|---|---|
| `graph.nodes[k]` (node attrs) | `SELECT … FROM nodes WHERE node_key=?` (PK) |
| `graph.successors(k)` | `SELECT dst_key,edge_type,line_number,context FROM edges WHERE src_key=?` (**idx_edges_src**) |
| `graph.predecessors(k)` | `SELECT src_key,… FROM edges WHERE dst_key=?` (**idx_edges_dst**) |
| `graph.edges[u,v]` | `SELECT … FROM edges WHERE src_key=? AND dst_key=?` |
| `find_nodes_in_file` | `SELECT node_key FROM nodes WHERE file_path=?` (**idx_nodes_file**) |
| `find_nodes_by_type` | `SELECT node_key FROM nodes WHERE node_type=?` (**idx_nodes_type**) |
| `find_nodes_by_name` | `SELECT node_key FROM nodes WHERE name=?` (+ a name index, §4) |

**The Phase-2 schema already ships `idx_edges_src`, `idx_edges_dst`, `idx_nodes_file`,
`idx_nodes_type`** — forward and reverse traversal are already O(log n) indexed lookups. The higher-
level algorithms (`compute_blast_radius` BFS, `find_call_chain` shortest-path, `get_subgraph` BFS,
`get_dependencies/dependents`, `find_dead_code`) are **already expressed in terms of these
primitives** — so re-backing the primitives re-backs the algorithms for free.

## 2. Hard constraint: the query API is SYNCHRONOUS

`find`, `get_blast_radius`, `get_call_chain`, `get_circular_dependencies`, … are all `def` (sync),
called from many sync sites. Making them `async` would ripple across the whole codebase (out of
scope, high risk). **Therefore the lazy backend uses a synchronous `sqlite3` read-only connection**,
not `aiosqlite`. WAL mode (already enabled by the writer) guarantees a sync reader can run
concurrently with the `aiosqlite` writer with zero `database is locked` — the same 1-writer/N-reader
property the Phase-2 concurrency soak proved. The "async LRU" from the plan is reconciled in §5: the
cache itself is a plain in-process bounded map (sync access on the hot path); its *contraction* under
memory pressure is the adaptive part.

## 3. Architecture — a `GraphBackend` seam (interface segregation, mirrors PersistenceProvider)

Introduce a backend protocol that exposes ONLY the ~7 primitives. The graph algorithms are rewritten
to call the protocol, never NetworkX directly. Two implementations:

```
GraphBackend (Protocol)
  ├─ get_node(key) -> dict | None
  ├─ successors(key) -> Iterable[(dst, edge_attrs)]
  ├─ predecessors(key) -> Iterable[(src, edge_attrs)]
  ├─ get_edge(u, v) -> dict | None
  ├─ nodes_in_file(fp) / nodes_by_type(t) / nodes_by_name(n)
  └─ contains(key) -> bool
  • InMemoryBackend  — wraps the existing nx.DiGraph + indices (DEFAULT; byte-identical path)
  • SqliteBackend    — lazy sync sqlite3 reads + adaptive LRU (§4, §5)
```

`CodebaseKnowledgeGraph` keeps its public method names; internally `compute_blast_radius`,
`find_call_chain`, `get_subgraph`, `get_edges_from/to`, `get_node`, etc. are refactored to call
`self._backend.<primitive>` instead of `self._graph.<nx op>`. Selection is gated (§8): off →
`InMemoryBackend` (today's behavior, byte-identical); on → `SqliteBackend`.

**Why a seam, not a rewrite-in-place:** same discipline as `PersistenceProvider` — the algorithms
become storage-agnostic, the in-memory path stays a verbatim fallback/rollback, and a future
backend (remote graph store) is a third implementation.

## 4. Phase A — lazy SQLite primitives

`SqliteBackend` opens a dedicated **read-only** `sqlite3` connection (`PRAGMA query_only=ON`,
`busy_timeout`, WAL inherited). Each primitive is one indexed statement:

- `successors(k)` → `SELECT dst_key,edge_type,line_number,context FROM edges WHERE src_key=?`
- `predecessors(k)` → `SELECT src_key,edge_type,line_number,context FROM edges WHERE dst_key=?`
- `get_node(k)` → `SELECT <cols> FROM nodes WHERE node_key=?` → rebuilt into the exact attr dict
  shape (`node_id` sub-dict + fields), byte-identical to the in-memory node attrs.
- `nodes_by_name` needs a new `CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name)` (additive,
  created on open; the only schema addition).

**Algorithms over primitives (no full-graph load):**
- **blast-radius / dependents / dependencies / call-chain**: these are *local* cones — BFS/shortest-
  path that visit only the reachable neighborhood, each hop an indexed `successors`/`predecessors`
  query. Bounded by the neighborhood size, not the global graph. (Existing depth caps retained.)
- **shortest-path (`find_call_chain`)**: replace `nx.shortest_path` with a bounded BFS over lazy
  `successors` (same result; visits only until target found or frontier exhausts).
- **subgraph**: BFS over lazy neighbors collecting keys, then materialize just those.

## 5. Phase B — adaptive working-set node cache (the Armor for queries)

Recursive traversals re-touch hot nodes; to avoid DB thrash, `SqliteBackend` wraps `get_node` (and
small adjacency results) in a **bounded LRU** (`OrderedDict`, move-to-end on hit, popitem(last=False)
on overflow). The cache is **adaptive, not static** — its `maxsize` is recomputed from
`MemoryPressureGate.probe()`:

- **OK/WARN** → full configured `maxsize` (`JARVIS_ORACLE_TRAVERSAL_CACHE_MAX`, default e.g. 50k nodes).
- **HIGH** → contract to a fraction (e.g. ×0.5), evicting LRU entries immediately.
- **CRITICAL** → contract hard (e.g. ×0.1) + `gc.collect()` to flush freed dicts back to the OS.

The probe is cheap and consulted on a throttled cadence (every N lookups / T seconds), so the hot
path stays sync and fast. This is the query-side mirror of the index-side Memory Armor: under host
pressure the resident working set shrinks instead of growing unbounded. Reuses the **same
`MemoryPressureGate`** — no new pressure logic.

> "Async" reconciliation: the hot read path is sync (matching the sync query API). The *contraction*
> can additionally be driven opportunistically (a lightweight trim on the existing index-loop /
> governor tick) so eviction doesn't only happen on the next query — but the authoritative, simplest
> mechanism is probe-on-access. We start there; a background trimmer is an optional add-on slice.

## 6. Local vs global queries — honest scope

- **Local traversals** (blast-radius, call-chain, dependencies/dependents, neighborhood, callers/
  callees) — the "killer features" — are bounded neighborhoods → **fully lazy, bounded RAM.** ✓
- **Global analyses** are inherently whole-graph:
  - `find_circular_dependencies` (`nx.simple_cycles`) — enumerating *all* cycles is exponential and
    needs global structure regardless of backend. Lazy plan: compute over the **edges table** via an
    iterative SCC/DFS that streams `(src,dst)` rows (loads edge keys, not node attrs → far cheaper
    than the full `DiGraph`), with a result/▒depth cap. Still heavier than local queries — flagged.
  - `find_dead_code` / `get_all_nodes` — full scans; become streamed `SELECT`s over `nodes`/`edges`
    (row cursors, not a resident graph), bounded by the cache.
  These remain "expensive but no longer require a 5 GB resident graph" — they stream the DB.

## 7. Correctness, performance, failure modes
- **Correctness:** results must equal the in-memory backend's. Regression strategy: a parity test
  harness builds a small graph, runs every query method through BOTH backends, asserts identical
  results (the canonical proof).
- **Performance:** local queries are a handful of indexed lookups + cache hits — sub-ms to ms. The
  trade vs in-memory NetworkX is DB round-trips on cache misses; acceptable and bounded.
- **Failure modes:** db missing/corrupt → `SqliteBackend` falls back to `InMemoryBackend` (fail-soft,
  never a crashed query). Read connection is `query_only` (a query can never mutate the brain).
  Cache contraction never errors (best-effort).

## 8. Gating
`JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED` (default **OFF** → `InMemoryBackend`, byte-identical to
today). On → `SqliteBackend`. Knobs: `JARVIS_ORACLE_TRAVERSAL_CACHE_MAX`, the pressure-contraction
fractions, the probe cadence. This is the flag that, once soaked, makes the Scoper's partial
in-memory graph safe — so it graduates alongside / just before the Scoper flip.

## 9. Decomposition (each = its own spec → plan → build)
1. **`GraphBackend` seam + `InMemoryBackend`** — extract the ~7 primitives, refactor the algorithms
   to call them, prove byte-identical via the dual-backend parity harness. (No behavior change; pure
   refactor + safety net. Highest-value first step — de-risks everything after.)
2. **`SqliteBackend` lazy primitives** — sync read-only conn + `idx_nodes_name` + the 7 primitives +
   parity tests (both backends identical on a real small graph).
3. **Adaptive LRU cache** wired to `MemoryPressureGate` (contract under pressure) + a soak proving
   bounded resident RAM during a deep recursive blast-radius on a large graph.
4. **Global-query streaming** (cycles / dead-code over DB cursors) — optional/lower priority.
5. **Graduate the Scoper default-ON** — now safe, because queries no longer depend on a complete
   in-memory graph. (The operator's "Phase 1," correctly sequenced last.)

> Slices 1→3 deliver the core win (local traversals bounded on 16 GB). 4 is a follow-on. 5 is the
> capstone that makes constrained-host builds fully operational.

## 10. Open questions for the operator
1. **Default cache size** (`JARVIS_ORACLE_TRAVERSAL_CACHE_MAX`) — 50k nodes is a starting guess
   (~150 MB at the measured per-node cost); tune after the Phase-3 soak.
2. **Global queries** — are `circular_dependencies` / `dead_code` actually used in the hot loop, or
   rare/offline? If rare, Slice 4 can stay deferred (YAGNI) and they can keep loading-on-demand.
3. **Scope of Slice 1 refactor** — refactor *only* the traversal primitives now, or also tidy the 12
   public methods' shared filtering helpers while we're in there? (Lean = primitives only.)
