# Domain 1 — Staging 2: The Graph Fold (Brain-side CausalGraph + Ingestor)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Brain-side in-memory `CausalGraph` folded from `StructuralDelta` events (O(1) per delta), an event-sourced `CausalGraphIngestor` (intake-WAL durable, deterministically re-foldable after a crash), and a `BlastRadiusOracle` whose intra-repo answer strictly delegates to `TheOracle`.

**Architecture:** `CausalGraph` is a native in-memory node registry keyed by `str(NodeID)` — each `CausalNode` carries `signature_hash`, `kind`, its declared import dotted-names, and its `last_emit_seq`/`last_head_sha` lineage. The fold is **emit_seq-monotonic per symbol** (last-writer-wins by the Staging-0 Lamport seq), which makes it **commutative and order-independent** — the mathematical foundation of the crash-recovery determinism proof (live-fold ≡ any-order WAL-replay). Intra-repo dependency edges/blast-radius are NEVER stored or re-derived — they delegate to `get_oracle()`. The `CausalGraphIngestor` consumes the Staging-1 `CausalDeltaSubscriber.on_delta` callback, folds into the graph, and durably appends each delta envelope to an intake `WAL` (off-loop via `offload`). Cross-repo edges + dynamic weighting are Staging 3; the merge-base reconciler is Staging 4.

**Tech Stack:** Staging-0 `StructuralDelta`; Staging-1 `CausalDeltaSubscriber`; `oracle.py` `get_oracle`/`get_blast_radius`/`get_dependents`/`NodeID`/`BlastRadius`; intake `WAL`/`WALEntry`; `cooperative_fs_io.offload`.

## Global Constraints (Staging-2 mandates, binding)

- **MANDATE 1 (O(1) event-driven fold):** ingesting a `StructuralDelta` mutates ONLY the directly-affected nodes/edges — O(1) in the delta's symbol count. NO periodic full-graph recalculation, NO sweep-and-prune GC. Nodes are removed ONLY by an explicit `symbols_removed` entry; the graph never garbage-collects heuristically.
- **MANDATE 2 (async non-blocking, no timeout fallbacks):** the graph is native in-memory adjacency (dicts/sets) that the future `BlastRadiusOracle` traverses without starving the loop; every FS touch (WAL append, snapshot) is `await offload(...)` — never a blocking write on the loop. NO hardcoded timeout-fallback constants anywhere.
- **MANDATE 3 (DRY intra-repo delegation):** all intra-repo dependency edges + blast radius come from `get_oracle().get_blast_radius(target)` / `.get_dependents(target)`. ZERO re-implemented Python import resolution in the causal graph. (The graph stores each node's DECLARED import dotted-names — a fact folded from the delta, used by Staging 3 for cross-repo edges — but never derives intra-repo dependency traversal itself.)
- **MANDATE 4 (deterministic event-sourced fold):** the tear-down/spin-up proof — an arbitrary live delta sequence yields a graph mathematically identical (node-set + every node field, snapshot-equal) to one rebuilt purely from the intake WAL after a simulated crash. The emit_seq-monotonic fold is what guarantees this regardless of replay order.
- Standing: dark behind existing masters (ingestor wired only inside the OrganismBusHost start-guard); single-writer (the Brain writes the graph; the Mac only publishes — unchanged); `from __future__ import annotations`; py3.9 (`asyncio.wait_for`, never `asyncio.timeout`); async-first; ASCII; fail-soft (a malformed delta is logged-and-dropped, never crashes the fold or the bus). TDD; named-files commits.

## Key facts (scouted, verified)

- Intake WAL: `WAL(path: Path, max_age_days=7)`; `append(WALEntry(lease_id, envelope_dict, status, ts_monotonic, ts_utc))`; `pending_entries() -> List[WALEntry]` (never call `update_status` on causal deltas → they stay `"pending"` → `pending_entries()` returns the FULL ordered event log). Flock/fsync durable. `intake/wal.py:29/37/53/81`.
- Oracle: `get_oracle() -> TheOracle` (`oracle.py:4574`); `get_blast_radius(target: str) -> BlastRadius` (`:4004`); `get_dependents(target: str) -> List[NodeID]` (`:4029`). `BlastRadius` fields: `source_node`, `directly_affected: Set[NodeID]`, `transitively_affected: Set[NodeID]`, `broken_imports`, `broken_calls`, `risk_level` + `.to_dict()` (`:420`).
- Offload: `async def offload(fn, ...)` (`cooperative_fs_io.py:619`) — the off-loop FS substrate (reused by DurableOutbound/ResourceManifest/EmitSequence).
- Staging-1: `CausalDeltaSubscriber(trinity_bus, *, on_delta: Callable[[dict], None])`; the `on_delta` receives the envelope `{"delta": {...}, "lineage": {"repo","head_sha","parent_sha","merge_base","emit_seq"}}`. `StructuralDelta.from_dict(d)` reconstructs the Staging-0 delta.

---

### Task 1: `CausalGraph` + `CausalNode` + O(1) emit_seq-monotonic fold + snapshot

**Files:**
- Create: `backend/core/ouroboros/governance/causal/causal_graph.py`
- Test: `tests/governance/causal/test_causal_graph.py`

**Interfaces (Produces):**
```python
@dataclass
class CausalNode:
    symbol_id: str            # "repo:file:name"
    repo: str                 # RepoType.value
    file_path: str
    kind: str                 # class|function|method
    signature_hash: str
    imports: FrozenSet[str]   # declared import dotted-names (for Staging-3 cross-repo)
    last_emit_seq: int
    last_head_sha: str

class CausalGraph:
    def apply_delta(self, envelope: dict) -> int:
        """Fold one stamped delta envelope. O(1) in the delta's symbol count.
        emit_seq-MONOTONIC per symbol: a symbols_added/resignatured/import change
        applies ONLY if delta.emit_seq > node.last_emit_seq (or the node is new);
        symbols_removed removes ONLY if delta.emit_seq > last_emit_seq. A stale
        (lower/equal emit_seq) delta is a no-op -> commutative + order-independent.
        file_level_churn: mark all known nodes of that file with the new lineage
        (coarse -- the structural detail was elided at the bound) WITHOUT touching
        other files' nodes. Returns count of nodes mutated. NEVER raises (malformed
        envelope -> 0, logged)."""
    def node(self, symbol_id: str) -> Optional[CausalNode]
    def nodes_in_repo(self, repo: str) -> List[CausalNode]
    def node_count(self) -> int
    def snapshot(self) -> dict:        # deterministic, fully-ordered serialization of every node+field
    @classmethod
    def from_snapshot(cls, snap: dict) -> "CausalGraph"
    def state_fingerprint(self) -> str:  # sha256 of the canonical snapshot -- the equality primitive for Mandate 4
```
- Adjacency is native dicts: `self._nodes: Dict[str, CausalNode]`, `self._by_repo: Dict[str, Set[str]]`, `self._by_file: Dict[Tuple[str,str], Set[str]]` — all O(1) mutation, no traversal on write. NO intra-repo edge storage (Oracle owns that).

- [ ] **Step 1: Write failing tests** — (a) apply a delta with `symbols_added` → nodes present with correct fields; (b) resignature (higher emit_seq) → signature_hash updated; (c) STALE delta (lower/equal emit_seq than the node's last) → NO-OP (node unchanged) — the commutativity pin; (d) symbols_removed (higher emit_seq) → node gone; a stale remove → node stays; (e) file_level_churn → that file's nodes get the new lineage, OTHER files untouched (O(1)-scope pin: assert an unrelated repo/file node is byte-identical before/after); (f) `apply_delta` on two orderings of the same delta set → identical `state_fingerprint()` (order-independence — the Mandate-4 foundation); (g) snapshot → from_snapshot → identical `state_fingerprint()`; (h) malformed envelope → returns 0, no raise.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN.** **Step 5: Commit** — `feat(domain1): CausalGraph -- O(1) emit_seq-monotonic fold + deterministic snapshot (Staging 2 Task 1)`.

---

### Task 2: `BlastRadiusOracle` intra-repo path (strict Oracle delegation)

**Files:**
- Create: `backend/core/ouroboros/governance/causal/blast_radius_oracle.py`
- Test: `tests/governance/causal/test_blast_radius_oracle.py`

**Interfaces (Produces):**
```python
@dataclass(frozen=True)
class IntraRepoImpact:
    source_symbol: str
    directly_affected: Tuple[str, ...]
    transitively_affected: Tuple[str, ...]
    risk_level: str

class BlastRadiusOracle:
    def __init__(self, graph: CausalGraph, *, oracle_fn: Optional[Callable[[], Any]] = None) -> None:
        # oracle_fn defaults to get_oracle (injectable for tests)
    async def intra_repo(self, symbol_id: str) -> IntraRepoImpact:
        """Delegate ENTIRELY to get_oracle().get_blast_radius(symbol_id) -> map the
        BlastRadius (directly/transitively_affected NodeIDs -> str, risk_level) into
        IntraRepoImpact. ZERO re-derived import resolution. Oracle call offloaded if
        it does sync FS/graph work (await offload). Fail-soft: Oracle miss/exception
        -> empty impact (source only), never raises. NON-BLOCKING (no timeout const)."""
```
- The cross-repo traversal (widest-path max-product, cycle-detected) is Staging 3 — NOT here. Staging 2 is the intra-repo delegation only.

- [ ] **Step 1: Write failing tests** — inject a fake `oracle_fn` returning a `BlastRadius` with known directly/transitively/risk → `intra_repo` maps them exactly (NodeID→str); Oracle raising → empty impact, no raise; assert the impl calls `get_blast_radius` (delegation pin — a recording fake) and does NOT itself walk imports (grep/AST: no `ast.parse` / import-graph building in the module).
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN.** **Step 5: Commit** — `feat(domain1): BlastRadiusOracle intra-repo -- strict TheOracle delegation (Staging 2 Task 2)`.

---

### Task 3: `CausalGraphIngestor` — fold + event-sourced WAL + snapshot compaction + wiring

**Files:**
- Create: `backend/core/ouroboros/governance/causal/causal_graph_ingestor.py`
- Modify: `backend/core/ouroboros/governance/transport/organism_bus_host.py` (wire the ingestor to the subscriber)
- Test: `tests/governance/causal/test_causal_graph_ingestor.py`

**Interfaces (Produces):**
```python
class CausalGraphIngestor:
    def __init__(self, graph: CausalGraph, *, wal_path: Optional[str] = None,
                 snapshot_path: Optional[str] = None) -> None:
        # env: JARVIS_CAUSAL_WAL_PATH (default <repo>/.jarvis/causal_graph_wal.jsonl),
        #      JARVIS_CAUSAL_SNAPSHOT_PATH (default <repo>/.jarvis/causal_graph_snapshot.json),
        #      JARVIS_CAUSAL_SNAPSHOT_EVERY_N (default 500), JARVIS_CAUSAL_SNAPSHOT_IDLE_S (default 3600).
    def ingest(self, envelope: dict) -> None:
        """The CausalDeltaSubscriber.on_delta target. Fold into the graph (O(1)), then
        schedule an offloaded WAL append of the envelope (fire-and-forget, fail-soft).
        NON-BLOCKING: the fold is an in-memory dict op; the durable write is offloaded.
        Every JARVIS_CAUSAL_SNAPSHOT_EVERY_N ingests OR after IDLE_S of no ingest ->
        fold-to-snapshot compaction (write graph.snapshot() offloaded, truncate the WAL
        to empty). This is DETERMINISTIC fold-to-snapshot (spec Q4), NOT heuristic GC."""
    async def replay_from_wal(self) -> None:
        """Crash recovery: load snapshot (if present) into the graph via from_snapshot,
        then fold every WAL entry (offloaded read) through graph.apply_delta. The
        emit_seq-monotonic fold makes this order-independent + idempotent. After replay,
        graph.state_fingerprint() == the live graph's."""
    async def start(self) -> None   # replay_from_wal + arm
    async def stop(self) -> None
```
- Wire in `organism_bus_host`: construct the graph + ingestor, `await ingestor.start()` (replay), then construct `CausalDeltaSubscriber(trinity_bus, on_delta=ingestor.ingest)` (REPLACING the Staging-1 bare subscriber — the ingestor is now the on_delta sink). Lazy import inside the start-guard; master-off byte-identical. Stop in reverse.

- [ ] **Step 1: Write failing tests** — (a) `ingest(envelope)` → graph folded + a WAL entry appended (read the WAL back); (b) non-blocking: `ingest` returns immediately, the offloaded WAL write lands (condition-poll); (c) compaction: after `SNAPSHOT_EVERY_N` ingests a snapshot file exists and the WAL is truncated; (d) snapshot+tail == full-replay: ingest N, force a snapshot at N/2, ingest N/2 more, then `replay_from_wal` on a fresh graph → `state_fingerprint()` equals the live graph (compaction is loss-free); (e) organism_bus_host wires ingestor+subscriber when armed (recording fake), byte-identical off; (f) malformed envelope → dropped, graph + WAL unaffected, no raise.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN.** **Step 5: Commit** — `feat(domain1): CausalGraphIngestor -- event-sourced fold + WAL + snapshot compaction + wiring (Staging 2 Task 3)`.

---

### Task 4: The tear-down/spin-up determinism proof (Mandate 4 — load-bearing)

**Files:**
- Test: `tests/governance/causal/test_causal_fold_determinism.py`

- [ ] **Step 1: The crash-recovery determinism test:**
  - Build a graph + ingestor on a tmp WAL. Ingest an ARBITRARY sequence of deltas (mix of add/resignature/remove/file_churn across all three repos, INCLUDING out-of-order emit_seq and duplicate replays and stale updates). Capture `live_fingerprint = graph.state_fingerprint()`.
  - **Simulate a Brain VM crash:** destroy the graph + ingestor objects entirely (no graceful stop — the durable truth is the WAL + snapshot on disk).
  - Reconstruct: fresh `CausalGraph` + `CausalGraphIngestor` on the SAME wal_path/snapshot_path; `await replay_from_wal()`. Capture `recovered_fingerprint`.
  - **Assert MATHEMATICALLY: `recovered_fingerprint == live_fingerprint`** — node-set equal, every node field equal (the snapshot canonical form is the equality witness). Also assert `graph.snapshot() == recovered.snapshot()` for a second independent equality.
  - **Order-independence corollary:** replay the SAME WAL entries in a SHUFFLED order → identical fingerprint (the emit_seq-monotonic commutativity, end-to-end).
  - Run 3× (with different arbitrary sequences) for flake-immunity; stream the fingerprints.
- [ ] **Step 2: Run** `python3 -m pytest tests/governance/causal/test_causal_fold_determinism.py -v -s` (unsandboxed if WAL writes ~/.jarvis; use a tmp_path WAL to avoid it). **Step 3: Commit** — `test(domain1): tear-down/spin-up fold determinism -- live graph == WAL-reconstructed after crash (Staging 2 Task 4)`.

---

## Self-Review
1. **Spec coverage (Staging 2 = spec Staging 2 "graph + intra-repo blast radius"):** CausalGraph fold (T1), CausalGraphIngestor event-sourced (T3), BlastRadiusOracle intra-repo Oracle-delegated (T2), determinism/crash proof (T4). Cross-repo edges/weighting → Staging 3; reconciler → Staging 4. Correctly out.
2. **Placeholder scan:** exact signatures/envs; the `state_fingerprint` is the concrete equality witness for Mandate 4; snapshot compaction is spec-Q4's deterministic fold-to-snapshot, explicitly NOT the heuristic GC Mandate 1 bans.
3. **Type consistency:** `apply_delta(envelope: dict)` consumes the Staging-1 `on_delta` envelope verbatim; `state_fingerprint()` used identically in T1/T3/T4; `IntraRepoImpact` maps `BlastRadius` fields (directly/transitively_affected, risk_level) exactly.
