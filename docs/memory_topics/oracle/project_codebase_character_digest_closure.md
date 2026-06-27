---
title: Project Codebase Character Digest Closure
modules: [backend/core/ouroboros/governance/codebase_character.py]
status: historical
source: project_codebase_character_digest_closure.md
---

May 2: 3-slice arc closed same-day. Empirical motivation: soak v3 (bt-2026-05-03-004700, 40m44s) showed doc_staleness=10× while exploration fired 1× — fixation pathology where O+V repeatedly proposed requirements.txt + README edits instead of exploring the actual codebase domains (voice biometric, ghost hands, vision pipeline). Option A (sensor priority dampening) rejected as symptom-mask. Option B implemented: macro-level ecosystem awareness via Reverse Russian Doll integration of existing SemanticIndex clusters.

**Slice 1 — Pure-stdlib substrate** at `backend/core/ouroboros/governance/codebase_character.py` (~430 LOC):
  - `DigestOutcome` 5-value closed enum (READY / INSUFFICIENT_CLUSTERS / STALE_INDEX / DISABLED / FAILED)
  - Frozen `ClusterCharacter` + `CodebaseCharacterSnapshot` with `is_ready()` / `to_dict()` / `to_prompt_section(max_chars=)`
  - `_ClusterLike` Protocol — substrate accepts ANY structural shape, never imports `semantic_index` directly; immune to ClusterInfo field reordering
  - Total `compute_codebase_character()` — NEVER raises; wraps everything in try/except → `DigestOutcome.FAILED`
  - 5 env knobs all clamped (master, min_clusters [1,16], stale_after_s [60s,7d], max_clusters [1,32], excerpt [40,400])
  - `to_prompt_section()` includes authority disclaimer + char-budget truncation that NEVER splits a cluster body (drops whole clusters from tail until under budget)
  - 70/70 tests + zero LLM/file-I/O/git/clustering of own (pure projection over existing artifact)

**Slice 2 — StrategicDirection wire-up** (surgical edit at `format_for_prompt()` line ~144):
  - New `_render_codebase_character_section()` static method mirrors `_render_posture_section()` discipline EXACTLY (fail-silent, ImportError-safe, advisory-only, no execution authority)
  - Reads `SemanticIndex.clusters` + `.stats()` (existing artifact, NO rebuild trigger from prompt path), projects via Slice 1's substrate, returns `to_prompt_section(max_chars=1500)`
  - Master flag stayed default-False at Slice 2; pre-Slice-2 prompt output byte-stable when off
  - 20/20 Slice 2 tests + 90/90 combined Slice 1+2 + 180/180 adjacent regression sweep

**Slice 3 — Graduation + ProactiveExploration cluster-coverage bias**:
  - Master flag flipped False→True with empty/whitespace-as-unset asymmetric env semantics
  - `register_shipped_invariants()` returns 4 AST pins:
    - `digest_outcome_vocabulary` — 5-value enum frozen
    - `proactive_exploration_cluster_bias_present` — BUG-FIX REGRESSION PIN, validates `scan_once` body contains `_emit_cluster_coverage_signals` invocation (refactor cannot silently delete the wire-up)
    - `compute_codebase_character_total` — no raise statements in body
    - `codebase_character_no_caller_imports` — substrate stays caller-agnostic (allows only flag_registry + meta.shipped_code_invariants registration-contract imports)
  - `register_flags()` returns 5 FlagSpecs (master TRUE, min_clusters 2, stale_after_s 86400.0, max_clusters_in_digest 8, exploration_cluster_emit_per_scan 1) all `since="CodebaseCharacterDigest Slice 3 (2026-05-02)"`
  - **ProactiveExplorationSensor.`_emit_cluster_coverage_signals`**: independent signal source emitting under-touched semantic clusters; per-scan cap default 1 (env-tunable [1,8] via `JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN`); session-dedup on `centroid_hash8`; `target_files=(".",)` project-root sentinel (model uses tool loop to discover representative files in cluster's domain)
  - `EVENT_TYPE_CODEBASE_CHARACTER_INJECTED = "codebase_character_injected"` SSE event added to `_VALID_EVENT_TYPES` frozenset
  - GET route `/observability/codebase-character` registered: bounded JSON projection (config + snapshot.to_dict()), 403 codebase_character_disabled when master off, 403 disabled when umbrella off
  - 46/46 graduation tests + 600/600 combined cross-arc sweep green

**Key zero-duplication contracts** (load-bearing):
  - REUSES `SemanticIndex.clusters` property — never duplicates k-means clustering
  - REUSES `SemanticIndex.get_default_index()` singleton — never instantiates a second index
  - REUSES posture-section discipline pattern in `_render_codebase_character_section` (mirror, not copy-paste)
  - REUSES `make_envelope` (intake intent_envelope) — never duplicates envelope construction
  - REUSES FlagRegistry + ShippedCodeInvariant registration contracts via auto-discovery (no edits to centralized seed file)
  - REUSES IDEObservabilityRouter `_check_rate_limit` + `_json_response` + `_error_response` helpers

**Cost contract preserved by construction**:
  - Zero LLM calls (substrate + sensor read existing artifact only)
  - Zero file I/O on prompt + sensor paths
  - Zero git invocations on consumer paths
  - Zero new K× amplification — sensor cap default 1 emit per 2h scan
  - SemanticIndex async build path owns refresh discipline; consumers NEVER trigger rebuild

**Anti-fixation success metric** (vs Soak v3 baseline at `.ouroboros/sessions/bt-2026-05-03-004700/`):
  - Baseline: doc_staleness=10× / exploration=1× ratio (10:1)
  - Target: ratio inverts to ≤1:3 OR exploration sensor's domain coverage shows ≥4 distinct clusters touched (vs effectively-1 in baseline)

**Branch hygiene** (P0 follow-up logged as Task #47):
  - AutoCommitter bypassed .gitignore on the ouroboros/battle-test/* branch (94-file commit, 93 .pyc files + 1 useful .python-version)
  - Surgical cherry-pick path applied to main: 4 valuable commits (AdmissionGate Slices 1-3 + CodebaseCharacterDigest Slices 1-2) + .python-version added separately
  - Battle-test branch left intact as immutable audit trail
  - AutoCommitter .gitignore bypass logged for structural fix (must honor `git check-ignore` filter, not use `git add -f`)

Pushed clean to origin/main: c6ddf7d699 (current HEAD as of close).

Deferred follow-ups: /codebase-character REPL verb, Slice 3 production wire-up of Slice 2 prompt block under live SemanticIndex, empirical Soak v4 verification of fixation-ratio inversion.
