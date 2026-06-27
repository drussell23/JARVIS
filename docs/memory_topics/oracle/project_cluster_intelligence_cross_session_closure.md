---
title: ClusterIntelligence-CrossSession — CLOSED 2026-05-03
modules: [scripts/cluster_coverage_e2e_probe.py, scripts/empirical_closure_verdict.py]
status: merged
source: project_cluster_intelligence_cross_session_closure.md
---

# ClusterIntelligence-CrossSession — CLOSED 2026-05-03

5-slice arc closing the "sovereign architect" gap identified after soak v3. Before: O+V's cluster_coverage envelopes asked the model to grep blind (`target_files=(".",)` project-root sentinel) and rebuilt domain knowledge from scratch every session. After: envelopes carry concrete file paths derived from cluster member commits, post-verify hook persists touched files into a cross-session DomainMap keyed on stable `centroid_hash8`, and the next session's envelopes thread prior-exploration context (theme + role + files + count) into the description so the model picks up where it left off.

## Slices shipped

- **Slice 1** — `representative_paths` on ClusterInfo (build-time enrichment via `git log --name-only` second pass; top-K most-touched paths per cluster). 48 tests. Master flag `JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED`.
- **Slice 2** — `compute_codebase_character` projects `representative_paths` onto `ClusterCharacter` + `to_prompt_section` renders new "Files: ..." line + `ProactiveExplorationSensor` envelope `target_files=` becomes real paths instead of `(".",)` sentinel (gated by `JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS`). 30 tests.
- **Slice 3** — `domain_map_memory.py` cross-session typed memory. Per-entry JSON file at `.jarvis/domain_map/<centroid_hash8>.json` + atomic write via tempfile+rename + cross-process flock per entry via the existing `cross_process_jsonl.flock_critical_section` primitive. Frozen `DomainMapEntry` dataclass + idempotent merge with monotonic-max confidence + caller-wins-if-non-empty for theme/role/op_id/cluster_id + dedup-preserving-order union for discovered_files. 91 tests including 20-thread concurrent stress proving lossless merge.
- **Slice 4** — `cluster_exploration_cascade_observer.py`. `OperationContext.intake_evidence_json` additive frozen field carries envelope.evidence as JSON snapshot. Post-verify orchestrator hook (one additive call site) extracts `category=="cluster_coverage"` tag + persists via DomainMapStore. Read-side `render_prior_context_block` consumed by ProactiveExploration to prefix envelope description. Architectural-role inference STUBBED behind `JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED` (placeholder marker only — actual Venom round deferred to cost-authorized arc). 46 tests.
- **Slice 5** — graduation. 4 master flag flips false→true (auto_role stays false as cost escape hatch). 12 FlagRegistry seeds across 4 modules (3+2+4+3). 3 AST pins. New `EVENT_TYPE_DOMAIN_MAP_UPDATED` SSE event + publish helper, fired by cascade observer on every persist. End-to-end test proves the full loop fires at default env. 34 tests.

## Architectural decisions worth remembering

- **Did NOT use OpsDigestObserver Protocol for the cascade hook.** That surface is a process-global singleton (one observer wins) which would conflict with the SessionRecorder. Used a direct function call from the orchestrator's existing post-verify hook instead — one additive line, no architectural contention.
- **Per-entry JSON files vs JSONL.** Cluster memory is keyed by `centroid_hash8`, not append-only log. Per-entry files give O(1) lookup, atomic write via tempfile+rename, and easy per-entry cleanup. Mirrors `UserPreferenceMemory`'s per-entry markdown pattern.
- **JSON-string for ctx.intake_evidence_json, not Mapping.** OperationContext is frozen and uses `dataclasses.replace`; Mapping fields are tricky in frozen dataclasses (dict isn't hashable). String snapshot sidesteps the issue + is hash-friendly.
- **Architectural-role inference deliberately stubbed.** `JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED` flag exists and stamps a `role_inference_pending` placeholder marker, but no actual Venom call wired. Substrate ships first; cost commitment deferred to a post-graduation arc.

## Test counts + AST pins

- **519/519 combined sweep** (134 SemanticIndex + 136 CodebaseCharacter + 48 Slice 1 + 30 Slice 2 + 91 Slice 3 + 46 Slice 4 + 34 Slice 5)
- **12 FlagRegistry seeds**: 3 (semantic_index Slice 1) + 2 (proactive_exploration_sensor Slice 2) + 4 (domain_map_memory Slice 3) + 3 (cluster_exploration_cascade_observer Slice 4)
- **3 AST pins**: semantic_index_slice1_helpers (helpers present + no exec/eval/compile); domain_map_memory_authority (only cross_process_jsonl + registration contract; DomainMapEntry stays frozen); cluster_cascade_observer_authority (only domain_map_memory + observability + registration contract; no orchestrator/iron_gate/etc)
- **1 new SSE event**: `EVENT_TYPE_DOMAIN_MAP_UPDATED` + best-effort publisher; cascade observer fires on every persist; publish failure isolated from cascade

## Reuse contract honored (no duplication)

- Existing `git log` subprocess pattern in `_assemble_corpus` (line 982) — Slice 1 extends format `%ct|%s` → `%ct|%H|%s` only when master on; Slice 1 helpers add a second `--name-only` pass with same timeout discipline
- `ClusterInfo` frozen dataclass — additive field, defaults preserve byte-identical pre-Slice-1 behavior
- `_ClusterLike` Protocol in codebase_character — additive field documented; defensive `getattr` projection handles older instances without the field
- `cross_process_jsonl.flock_critical_section` — same flock primitive InvariantDriftStore + ApprovalStore + AdaptationLedger use
- `UserPreferenceMemory` per-entry-file pattern (atomic write + defensive parse) mirrored for DomainMap
- `OperationContext` additive field via existing `create()` kwargs
- `UnifiedIntakeRouter` ctx construction — one additive `intake_evidence_json=` kwarg

## Reverse Russian Doll posture

Outer doll (O+V) gains full cross-session memory active by default. The model now sees real file paths in cluster_coverage envelopes, builds on prior exploration history threaded into envelope descriptions, and contributes its own touched_files back into the persistent domain map after every successful exploration.

Antivenom (the constraint) scaled proportionally: 12 FlagRegistry seeds catalogue every cost surface; 3 AST pins lock the structural authority boundaries (cascade observer MUST NOT import orchestrator/iron_gate/etc; domain_map MUST stay pure-stdlib + frozen); all reach + flow defaults inherit NEVER-raise discipline; role inference STUBBED so no covert cost commitment; master-off escape hatches preserved per surface.

Closes the "sovereign architect" gap empirically. Soak v4 will tell us whether the cluster→file enrichment + DomainMap memory inverts the doc_staleness:exploration ratio that v3 was bumping torch versions against.

## Empirical-closure addendum — 2026-05-03 (same day)

Soak v4 surfaced a deeper root cause: the ClusterIntelligence substrate was structurally graduated default-true but **operationally inert** because `fastembed` cannot initialize in this environment (HF Xet downloader panic + sandbox file-permission errors during model download). With `embed_failures=1`, `corpus_n` stayed at 0, `cluster_mode` silently downgraded `kmeans→centroid`, and `cluster_count=0` — making cluster_coverage envelopes never fire in any of the four most recent soaks (0/4 firings vs 32-34 doc_staleness emissions per soak — a 24:1 fixation ratio, **worse** than the v3 10:1 baseline). The arc was dead code in production despite shipping clean.

**Fix shipped same day** as a structural addendum (not a new arc — a bug fix on a graduated arc): adaptive embedder substrate composing fastembed (primary) with a pure-stdlib hashing TF-IDF embedder (fallback). Three classes added to `semantic_index.py`:

- `_StdlibHashingEmbedder` — sibling of `_Embedder`. Pure-stdlib (`hashlib` + `re` + `math`), zero network, zero disk I/O. md5 token hash → fixed-D bucket → sublinear TF (`1+log(count)`) → L2 normalize → `List[List[float]]` matching the sibling contract exactly. Default `dim=128`, env-tunable.
- `_AdaptiveEmbedder` — wraps `_Embedder` + `_StdlibHashingEmbedder`. First embed call probes fastembed; on `None` return, permanently swaps to stdlib + publishes `EVENT_TYPE_SEMANTIC_EMBEDDER_FALLBACK` SSE event once. Thread-safe via internal lock.
- `_embedder_factory()` — env-driven selector. `JARVIS_SEMANTIC_EMBEDDER=stdlib` → stdlib only; `=fastembed` + `JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED=true` (both default) → adaptive; fallback disabled → bare `_Embedder` (legacy fail-closed). `SemanticIndex.__init__` constructs via factory (single source of truth, AST-pinned).

**Empirical proof**:
- Pre-fix soak (`bt-2026-05-03-021235`): C1 FAIL `corpus_n=0 cluster_count=0`, C2 FAIL `path=NONE`, C3 FAIL `0 emits`, C4 FAIL `0 entries`, C5 ratio 24:1.
- Post-fix soak (`bt-2026-05-03-042201`, 30s after boot): C1 PASS `corpus_n=34 cluster_count=3 mode=kmeans converged=True`, C2 PASS `path=stdlib (fallback)`, C5 ratio 2:1 (5× improvement). C3 false-fail (ProactiveExplorationSensor's 2h cadence never reached second tick in a 30-min idle soak).
- Surgical e2e probe (`scripts/cluster_coverage_e2e_probe.py`): 3 cluster_coverage envelopes → 3 cascade observer invocations → **3 DomainMap entries persisted** (`8a076629.json`, `86404310.json`, `2cf4c24d.json`) with theme labels + 5-8 discovered files each. Cluster→cascade→DomainMap chain end-to-end functional.

**Test counts + AST pins added**:
- 52 new regression tests in `test_semantic_adaptive_embedder.py` (stdlib primitive + adaptive wrapper + factory + integration + invariant + flag-registry coverage)
- 583/583 combined sweep across the full ClusterIntelligence-CrossSession + adaptive-embedder family (zero regressions)
- 2 new FlagRegistry seeds: `JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED` (BOOL default-true) + `JARVIS_SEMANTIC_STDLIB_EMBEDDER_DIM` (INT default-128)
- 1 new AST pin: `semantic_index_adaptive_embedder` — pins (a) all four substrate symbols present, (b) `_StdlibHashingEmbedder` body MUST NOT import fastembed (structural independence), (c) `SemanticIndex.__init__` MUST construct via `_embedder_factory()`

**New verdict + probe scripts shipped**:
- `scripts/empirical_closure_verdict.py` — read-only verdict over 4 primary contracts + 1 advisory (doc_staleness:exploration ratio); reusable against any session for future regression detection
- `scripts/cluster_coverage_e2e_probe.py` — surgical in-process exercise of the full cluster_coverage→cascade→DomainMap chain; deterministic, no soak required, useful for CI

**Reverse Russian Doll posture preserved**: Outer doll (the substrate) gained adaptive resilience to embedder unavailability — works offline, in CI, in sandboxed environments. Antivenom scaled proportionally: AST pin locks fastembed-independence at the stdlib body; SSE event makes the degradation operator-visible; explicit env knobs let operators pin behavior; no hardcoding.

The "sovereign architect" gap is now structurally closed AND empirically proven — not just by ratio improvement but by deterministic e2e exercise of the cascade chain.
