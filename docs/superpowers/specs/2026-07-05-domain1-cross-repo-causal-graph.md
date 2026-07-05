# Domain 1 — The Unified Cross-Repo Causal Graph (Semantic Bus)

**Date:** 2026-07-05
**Status:** DRAFT — awaiting operator sign-off (spec gate; no implementation until approved)
**Author:** O+V campaign (Claude, for Derek J. Russell)
**Depends on:** Stages 0-4 (all live-proven 2026-07-04): the distributed `TrinityEventBus` transport, the Brain-VM runtime, the Body↔Brain single-writer split, the generation-fenced sovereign Brain.
**North Star:** the sensory organ of Intent-Driven Development — the graph that lets O+V *see* the downstream blast radius of a structural change across JARVIS / J-Prime / Reactor-Core, so intent can be inferred rather than instructed. Domains 2-4 (bounded speculation, intent-invalidation, adversarial falsifier) consume this graph; it ships first.

## Problem

O+V today reasons about one repo at a time. Three walls block cross-repo intent inference:

1. **No cross-repo structural sight.** `TheOracle`'s dependency graph (`oracle.py:856` `CodebaseKnowledgeGraph`) and the Iron-Triad blast-radius (`reverse_dep_resolver.py:243` `_transitive_reverse_closure`, `oracle.py:1181` `compute_blast_radius`) are **intra-repo only** — rooted at a single `repo_root`, Python-import edges within one tree. A routing-boundary change in Reactor-Core casts no shadow onto J-Prime or JARVIS. The one existing cross-repo blast primitive (`multi_repo/blast_radius.py`) resolves references by **regex text-grep**, not AST edges — imprecise and noisy.
2. **No structural delta on the wire.** Cross-repo awareness today is either full-content hashing (`cross_repo_drift_sensor.py:35` `_CONTRACT_FILES` SHA-256 of watched files — detects *that* something changed, never *what structurally*) or commit-subject mining (`cross_repo_causal_mirror.py:340` `_walk_mirror_commits`). Shipping raw diffs or file contents across the Body→Brain wire is bandwidth-heavy, leaks source, and — the deeper failure — carries no *structure* the graph can reason over.
3. **No timeline-rewrite survival.** No signal or causal path in the codebase handles force-push / rebase / reflog / merge-base (confirmed: these appear only in rollback tooling, never in any sensor or graph). A rebased local history would feed the Brain a contradictory trajectory and silently corrupt the global graph.

## Goal

A single **causal graph living exclusively on the GCP Brain** whose nodes are `(repo, file, symbol)` and whose edges are **dynamically weighted** structural + causal relationships across the three repos. The Mac Body is a pure **capture-and-emit publisher**: it computes bounded **AST structural deltas** for its local changes and publishes them onto `TrinityEventBus` (single-writer preserved — the Body never writes the graph). The Brain ingests deltas idempotently, updates edge weights via **half-life decay + cosine similarity** (zero hardcoded thresholds), and answers one question in real time: *given this structural change in repo A, what is the mathematically-computed blast radius in repos B and C?* The ingestion layer is **event-sourced and reconciliation-deterministic** — a force-push or rebase retracts the orphaned timeline's contributions and replays the new one without corrupting the global trajectory.

## Non-Goals (YAGNI)

- Not the intent *inference* itself (Domain 2) — Domain 1 supplies the blast-radius signal; the hypothesis engine consumes it.
- Not autonomous cross-repo PR generation (Domain 2/the sovereign mutator) — this graph *informs* it, does not perform it.
- Not a new transport — `TrinityEventBus` + the Stage-2/3/4 `DistributedEventBus`/`TrinityBusBridge` carry every byte (Mandate 3).
- Not moving the Oracle's intra-repo graph — Domain 1 *composes* it (per-repo Oracle stays the intra-repo edge source); the causal graph adds the cross-repo edges and the dynamic weighting layer on top.
- Not touching the FSM ledger or Immutable-Orange — the graph is advisory sensory data, never an authority surface.

## Architecture

```
┌─ Mac Body (capture + emit; single-writer preserved) ────┐         ┌─ GCP Brain (the causal graph lives here ONLY) ─────────┐
│  StructuralDeltaSensor (net-new)                        │ Trinity │  CausalGraphIngestor (net-new, Brain-side subscriber)   │
│    fs.changed / commit hook -> AST symbol+edge extract  │ EventBus│    idempotent fold: apply StructuralDelta -> graph      │
│    -> StructuralDelta (bounded, signed by SHA lineage)  │  (Stage │  CausalGraph (net-new): nodes (repo,file,symbol)       │
│    -> publish_raw(topic=causal.delta.<repo>, source=Rt) │  2/3/4  │    edges: intra-repo (Oracle) + cross-repo (contract    │
│  RepoRegistry (reuse) reads prime/reactor checkouts     │ bridge) │      surface cosine + mined co-change, decayed)         │
│  NO graph write. NO raw diff/content on the wire.       │◄═══════►│  BlastRadiusOracle (net-new): weighted traversal ->    │
│                                                          │  carries│    per-foreign-node impact score (the sensory answer)  │
│                                                          │  RepoType│  TimelineReconciler (net-new): merge-base retraction   │
└──────────────────────────────────────────────────────────┘  + WAL  └─────────────────────────────────────────────────────────┘
```

### Component 1 — `StructuralDeltaSensor` (Body-side, net-new)

Triggered by the existing fs.changed / commit signals (the `TrinityEventBus` `fs.changed.*` bus the Gap-4 campaign already wired). For each changed `.py` it computes a **bounded AST structural delta** — never a textual diff, never file content (Mandate 1):

- Extract the symbol+edge SET of the file at the new revision using the **canonical AST extractor** `oracle.py:507` `CodeStructureVisitor` (signatures, decorators, base classes, complexity, `IMPORTS`/`IMPORTS_FROM`/`CALLS`/`INHERITS` edges) — reused verbatim, not re-implemented. Extract the same at the parent revision (`git show <parent>:<file>` piped to the visitor).
- Diff the two symbol sets → `StructuralDelta`: `symbols_added`, `symbols_removed`, `symbols_resignatured` (name stable, signature/decorator/base-class changed), `import_edges_added`, `import_edges_removed`, each as a small typed record `(symbol_id, kind, signature_hash)`. **Bounded:** the delta caps at `JARVIS_CAUSAL_DELTA_MAX_SYMBOLS` (env, default 64); overflow collapses to a `file_level_churn` marker with counts — a large refactor becomes ~2KB of structure, not 4,000 lines of diff.
- Stamp lineage (Mandate 4 payload): `repo` (RepoType.value), `head_sha`, `parent_sha`, `merge_base` (`git merge-base HEAD <tracking-branch>`), and a per-source monotonic `emit_seq` (a Lamport-style counter persisted in `.jarvis/causal_emit_seq`).
- Publish via `trinity_event_bus.publish_raw(topic=f"causal.delta.{repo}", data=<StructuralDelta.to_dict()>, source=RepoType, correlation_id=head_sha, causation_id=parent_sha)` (`trinity_event_bus.py:1006`). The Stage-2 `TrinityBusBridge` outbound allowlist gains `causal.delta.*`; the delta rides the proven Stage-2/3/4 wire to the Brain. **The Body's job ends here** (capture-and-emit; no graph write — single-writer holds by construction).

### Component 2 — `CausalGraph` + `CausalGraphIngestor` (Brain-side, net-new, event-sourced)

The graph lives only on the Brain. It is **event-sourced**: the durable log of `StructuralDelta` events IS the source of truth (reusing the Stage-3 intake-WAL flock primitives — `intake/wal.py` — so the graph survives Brain restart / resurrection and is deterministically re-foldable, the same discipline the hypothesis FSM in Domain 3 will use). `CausalGraphIngestor` subscribes `causal.delta.*` on the Brain's `TrinityEventBus` and folds each delta:

- **Nodes:** `(repo, file, symbol)` reusing the Oracle `NodeID` model (`oracle.py:346`) so an intra-repo Oracle node and a causal-graph node are the same identity.
- **Intra-repo edges:** delegated to the per-repo `TheOracle` (`get_dependents` `oracle.py:4029`, `compute_blast_radius` `oracle.py:1181`) — Domain 1 does NOT re-derive intra-repo import edges; it *queries* the Oracle. (RepoRegistry gives the Brain local-FS access to all three checkouts — `multi_repo/registry.py:50` `RepoRegistry.from_env`.)
- **Cross-repo edges (net-new, the heart of Domain 1) — two independently-weighted kinds:**
  1. **Contract edges** — between a changed symbol and the *declared boundary surfaces* it crosses (the mTLS/API client seams the drift sensor already knows: `cross_repo_drift_sensor.py:35` `_CONTRACT_FILES`, plus the `*_client.py` families). Weight = **cosine similarity** (`semantic_index.py:803` `_cosine`) between the changed symbol's embedding and the foreign contract surface's embedding, both from the shared bge-small embedder (`semantic_index.py:527`). A routing-boundary change whose signature is semantically close to a J-Prime client method scores a strong contract edge; an unrelated internal helper scores near-zero. **No threshold** — the cosine *is* the weight.
  2. **Co-change edges** — mined from cross-repo commit history that historically moves together (reuse `cross_repo_causal_mirror.py:485` `scan_mirror_correlations` / `_walk_mirror_commits`, generalized from one mirror to the trinity via RepoRegistry). Weight = a **recency-decayed** co-occurrence: each historical co-change contributes `_recency_weight(age_s, halflife_days)` (`semantic_index.py:839`, `0.5^(age/halflife)`) with `JARVIS_CAUSAL_COCHANGE_HALFLIFE_DAYS` (env, default 14 — the SemanticIndex commit half-life). Old co-changes fade; recent ones dominate. **No threshold** — the decayed sum *is* the weight.

  The composite cross-repo edge weight = a config-free blend `w = 1 - (1-w_contract)·(1-w_cochange)` (probabilistic OR — two independent evidence sources; naturally in [0,1], no tuning constant).

### Component 3 — `BlastRadiusOracle` (Brain-side, net-new — the mathematical blast radius)

Answers the sensory question. Given a `StructuralDelta` on `(repo A, symbol S)`:

1. Intra-repo blast radius from `TheOracle.compute_blast_radius(S)` (`oracle.py:1181`) — the existing cycle-armored BFS over reverse edges with its risk tiers.
2. Cross-repo propagation: a **weighted async traversal** (cycle-armored via the `reverse_dep_resolver.py:243` `_transitive_reverse_closure` deque-worklist pattern — reused, generalized to weighted edges) from S across the composite cross-repo edges. Each reached foreign node accrues an **impact score** = product of edge weights along the strongest path (a max-product / widest-path traversal — deterministic, bounded by a decay floor so weak chains terminate, not by a magic constant: the path prunes when the running product drops below `1/JARVIS_CAUSAL_MAX_NODES`, i.e. the traversal is self-limiting by the same node budget that bounds its size).
3. Output: a `CrossRepoBlastRadius` — a ranked list of `(foreign_repo, foreign_symbol, impact_score, evidence=[contract|cochange])`. This is the exact signal Domain 2's intent-inference and confidence calculus consume. Fully async (the whole traversal is `await`-driven off the graph's in-memory adjacency; the graph never blocks the Brain's control loop — the Stage-3 offload discipline applies to any FS/embed touch).

### Component 4 — `TimelineReconciler` (Brain-side, net-new — Mandate 4)

The graph is event-sourced, so timeline manipulation is handled by **retract-and-replay**, deterministically:

- **Out-of-order events:** each delta carries `(head_sha, parent_sha, emit_seq)`. The ingestor maintains, per source repo, the observed commit DAG (child→parent). A delta whose `parent_sha` is not yet seen is **parked** (bounded pending buffer) until its parent arrives — the fold applies in causal order, not arrival order. `TrinityEvent.fingerprint` dedup (`trinity_event_bus.py:291`, 60s window) + the `emit_seq` monotonic guard drop duplicates/replays idempotently (reuse, no new dedup — Mandate 3 and the single-dedup-authority principle from Stage 3).
- **Force-push / rebase (the timeline rewrite):** the reconciler detects divergence structurally — a newly-observed `head_sha` for a repo whose `parent_sha` does **not** descend from the last-known head (verified via `git merge-base --is-ancestor` against the Brain's own checkout of that repo, and the delta's stamped `merge_base`). On divergence: (a) compute the merge-base; (b) identify the **orphaned commit range** (last-known-head back to the merge-base, now unreachable); (c) **retract** every graph contribution those orphaned deltas made — because the graph is a fold over the event log, retraction = re-fold excluding the orphaned events (the co-change edge decays and contract edges recompute cleanly; no torn state); (d) **replay** the new timeline's deltas from the merge-base forward. The global trajectory on the Brain reconciles to exactly the state it would have had if the rewritten history had been the only history — deterministic, corruption-free. This mirrors the Stage-3 WAL retract-and-replay proven live, applied to git timelines instead of network partitions.

## Mandate Compliance (explicit, per the operator's four constraints)

1. **Root-Cause Only — AST deltas + mathematical blast radius.** No raw diff or file content ever crosses the wire; `StructuralDeltaSensor` ships only the bounded symbol/edge set-diff. Blast radius is computed (`BlastRadiusOracle` widest-path max-product traversal over weighted edges), not heuristic-tagged.
2. **Architectural Purity — async + dynamically weighted, zero hardcoded thresholds.** Contract weight = cosine; co-change weight = half-life-decayed sum; composite = probabilistic OR. The only env knobs are *bounds* (max symbols, max nodes, half-life days), never *relationship-strength thresholds*. The mapping is explicitly between the three named subsystems — Reactor-Core engine ↔ J-Prime semantic classifier ↔ JARVIS orchestrator — via their contract surfaces' embeddings. Every graph read is `await`-driven; every FS/embed touch offloads (Stage-3 discipline).
3. **DRY — `TrinityEventBus` only, Body single-writer, graph Brain-only.** Transport is `publish_raw` + the Stage-2/3/4 bridge (RepoType rides in `source` + payload; `causal.delta.*` added to the outbound allowlist). The Body captures-and-emits; the graph write happens exclusively in the Brain's `CausalGraphIngestor`. Reuses: `CodeStructureVisitor`, `NodeID`, `compute_blast_radius`, `_transitive_reverse_closure`, `_cosine`, `_recency_weight`, the bge-small embedder, `scan_mirror_correlations`, `RepoRegistry`, `intake/wal.py`, `TrinityEvent` fingerprint dedup.
4. **Bulletproof — out-of-order + timeline-rewrite deterministic.** `TimelineReconciler` parks-until-causal-order for out-of-order deltas and retract-and-replays on merge-base divergence (force-push/rebase), event-sourced so the fold is always deterministic. The FSM ledger and Immutable-Orange are untouched — the graph is advisory only.

## Leverage-Existing Map (DRY)

| Need | Reuse | file:line |
|---|---|---|
| AST symbol+edge extraction | `CodeStructureVisitor` | `oracle.py:507` |
| Node identity (multi-repo) | `NodeID(repo,file,name,type,line)` | `oracle.py:346` |
| Intra-repo blast radius | `compute_blast_radius` / `get_dependents` | `oracle.py:1181` / `:4029` |
| Cycle-armored weighted traversal | `_transitive_reverse_closure` (deque worklist) | `reverse_dep_resolver.py:243` |
| Cosine edge weight | `_cosine` | `semantic_index.py:803` |
| Half-life decay edge weight | `_recency_weight` (`0.5^(age/halflife)`) | `semantic_index.py:839` |
| Shared embedder (bge-small) | `_Embedder` | `semantic_index.py:527` |
| Cross-repo co-change mining | `scan_mirror_correlations` / `_walk_mirror_commits` | `cross_repo_causal_mirror.py:485` / `:340` |
| Local multi-repo FS access | `RepoRegistry.from_env` | `multi_repo/registry.py:50` |
| Contract-surface awareness | `_CONTRACT_FILES` + `*_client.py` | `cross_repo_drift_sensor.py:35` |
| Transport (no new layer) | `publish_raw` + `TrinityBusBridge` outbound allowlist | `trinity_event_bus.py:1006` / `trinity_bus_bridge.py:36` |
| Idempotent dedup | `TrinityEvent.fingerprint` (60s window) | `trinity_event_bus.py:291` |
| Durable event log (event-sourcing) | intake `WAL` flock primitives | `intake/wal.py` |
| Commit-history reader | `compute_recent_momentum` / git-log parse | `git_momentum.py:245` |

**Confirmed net-new (flagged honestly — no false-reuse):** the AST *structural set-diff across two revisions* (building blocks exist, the diff does not); cross-repo *AST-edge* resolution (today only regex text-grep in `multi_repo/blast_radius.py`); the dynamic cross-repo *weighting* layer; the *timeline reconciler* (no force-push/rebase/merge-base handling exists in any signal path). These four are the real Domain-1 engineering; everything else composes.

## Staging (each independently shippable + testable; ships dark)

0. **StructuralDelta + the AST set-diff** (Body-local, no wire): the bounded delta computer over `CodeStructureVisitor`, symbol/edge set-diff, overflow collapse, SHA/emit_seq lineage stamping. TDD: known before/after file pairs → exact expected delta; overflow → `file_level_churn`; never emits content/diff text (grep-enforced invariant).
1. **The transport hop**: `causal.delta.*` publish on the Body + the Brain-side subscriber echo, over the real Stage-2/3/4 bridge (RepoType round-trips). Two-process loopback proving a Body delta lands on the Brain idempotently (dup-fingerprint dropped).
2. **The graph + intra-repo blast radius**: `CausalGraph` fold + `CausalGraphIngestor` + `BlastRadiusOracle` intra-repo path (delegating to Oracle). Live: a JARVIS symbol change → correct intra-repo blast radius on the Brain.
3. **Cross-repo edges + weighting**: contract cosine edges + co-change decayed edges + the composite blend + the widest-path traversal. The proof: a Reactor-Core routing-boundary change → a ranked, weighted J-Prime + JARVIS blast radius, with the weights explaining *why* (evidence tags). Zero hardcoded thresholds (grep/AST-enforced: the only numeric envs are bounds).
4. **Timeline reconciliation** (the bulletproof stage): the deliberate-rewrite test — feed the graph a commit chain, then a force-push/rebase that reparents it; assert the orphaned contributions retract and the new timeline replays to a graph byte-identical to a clean fold of the rewritten history. Out-of-order deltas park-and-apply in causal order. This is Domain-1's deliberate-partition analogue.

## Testing Strategy

- **Determinism of the fold** (load-bearing): the same event log, in any arrival order, folds to the identical graph (set-equality on nodes + edge weights within float epsilon) — the event-sourcing guarantee.
- **Timeline-rewrite** (bulletproof): force-push/rebase retract-and-replay yields the exact clean-fold state; a mid-rewrite crash (Brain restart) re-folds deterministically from the WAL.
- **No-content invariant** (Mandate 1): AST/grep-enforced — no `StructuralDelta` field can carry source text or a textual diff; only symbol ids, signature *hashes*, edge tuples, and counts.
- **Weight purity** (Mandate 2): grep/AST-enforced — no numeric literal in the weighting path except the bounded-decay floor derived from the node budget; every relationship strength traces to `_cosine` or `_recency_weight`.
- **Single-writer** (Mandate 3): AST-enforced — the Body-side module has zero graph-write calls; the graph type is constructed/mutated only under `governance/` Brain-side code.
- **Cross-repo accuracy**: a curated fixture trinity (three tiny sibling repos with a known contract seam + a known co-change history) → the expected ranked blast radius; a semantically-unrelated change → near-zero cross-repo edges (no false propagation).
- Live soak on the resurrected Brain once Stages 2-4 wiring is armed: real `causal.delta.*` from the Mac Body → graph updates observed on the Brain, `ControlPlaneStarvation` census flat (the ingest is off-loop).

## Risks / Open Questions (for operator review)

1. **Embedding cost of contract cosine.** Every cross-repo edge candidate embeds the changed symbol + the foreign contract surface. Cache foreign-surface embeddings (they change rarely) in the SemanticIndex `.npz` pattern? **Recommend:** yes — embed foreign contract surfaces once per their own SHA, cache, re-embed only on their change.
2. **Merge-base authority.** The reconciler verifies divergence against the Brain's own checkout of each repo (`git merge-base --is-ancestor`). That checkout must be kept current (a lightweight `git fetch` per repo on delta arrival, no working-tree change). Confirm the Brain may hold read-only fetched mirrors of all three repos.
3. **Co-change mining scope.** `_walk_mirror_commits` caps history depth; a deep co-change signal (quarterly-moving files) may be under-weighted by the 14-day half-life. **Recommend:** the half-life is the right default (recent structure dominates intent); a longer `JARVIS_CAUSAL_COCHANGE_HALFLIFE_DAYS` is one env away if you want deeper memory.
4. **Graph size on a 24/7 Brain.** Event-sourced with unbounded log growth. Compaction: periodic fold-to-snapshot (the SemanticIndex `.npz` cache discipline) + WAL compaction (the intake WAL's own `compact()`), retaining the log tail since the last snapshot. Confirm snapshot cadence policy.
5. **RepoType on the wire.** The Stage-2/3/4 bridge carries a string `origin`, not a typed `RepoType`. Domain 1 rides RepoType inside the payload (`repo` field) + the `causal.delta.<repo>` topic — no transport change. Confirm this is the intended reuse (vs. promoting the bridge to typed RepoType, which is larger net-new work explicitly out of scope here).

## Decision needed from operator

Approve this Domain-1 architecture (the graph, the AST-delta capture, the dynamic weighting, the timeline reconciler), or adjust the open questions above — especially #2 (Brain-held read-only repo mirrors for merge-base authority), #4 (snapshot/compaction cadence), and #5 (RepoType-in-payload vs. typed-transport) — before I invoke `writing-plans` for Staging 0 (the AST structural delta — Body-local, shippable with no wire and no relocation).
