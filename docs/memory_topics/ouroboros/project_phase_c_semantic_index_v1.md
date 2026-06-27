---
title: Phase C Slice 3a — Semantic Index v1.0 cluster math + telemetry (2026-04-20)
modules: []
status: merged
source: project_phase_c_semantic_index_v1.md
---

# Phase C Slice 3a — Semantic Index v1.0 cluster math + telemetry (2026-04-20)

First slice of Epoch 3 (Synthetic Soul deepening). Expands v0.1's
single-centroid cosine alignment to a k-means-over-corpus cluster
model so the organism can recognize multiple simultaneous themes
instead of averaging them into a mediocre mean vector.

## What shipped

### Math (hand-rolled NumPy, no sklearn)

- `_kmeans_numpy(vectors, k, seed, max_iter, tol)` — deterministic
  seeded Lloyd iteration with shuffled-indices init + empty-cluster
  repair (reseeds from point farthest from own centroid). Returns
  `(labels, centroids, iter_count, converged, inertia)`. K=1 is
  special-cased to return all-zeros labels + mean centroid +
  converged=True.
- `_silhouette_cosine(vectors, labels)` — mean silhouette in
  cosine-distance space. Single-cluster labeling returns 0.0
  (undefined convention, lets K=1 be the tie-break floor).
- `_auto_k_kmeans(vectors, k_min, k_max, seed, max_iter, tol)` —
  sweep K ∈ [k_min, k_max ∩ N], pick max-silhouette K. K=1 always
  scores 0.0; any K≥2 that scores ≤0 loses to K=1 (graceful
  degradation to v0.1 when data is coherent).
- `_cosine_distance_matrix(vectors)` — dense N×N cosine-distance
  via normalized-vector dot products; zero-norm rows get cos-dist=1.

### Data types

- `ClusterInfo` frozen dataclass: `cluster_id`, `size`, `kind`,
  `centroid` (tuple), `centroid_hash8`, `nearest_item_text`,
  `nearest_item_source`, `source_composition`. Immutable snapshot.
- `IndexStats` extended with: `cluster_mode`, `cluster_count`,
  `clusters` (content-light dict summaries), `cluster_churn`,
  `kmeans_silhouette`, `kmeans_inertia`, `kmeans_converged`,
  `kmeans_iter_count`, `alignment_histogram_by_kind`,
  `failure_gravity_alerts`, `failure_gravity_window_rate`.

### Cluster-kind classifier

Source-composition → one of four kinds at ≥60% dominance (env-tunable):
- `goal` (git_commit + goal combined ≥ threshold)
- `conversation`
- `postmortem`
- `mixed` (no single source-family crosses threshold)

### SemanticIndex wire-up

- `build()` runs auto-K clustering only when `cluster_mode=kmeans`;
  populates `_clusters`, `_cluster_labels`, `_prev_cluster_hashes`;
  computes `cluster_churn` vs previous build; resets failure-gravity
  rolling window on each rebuild.
- `score()` — **unchanged return value in Slice 3a**. Still returns
  v0.1 centroid cosine. Cluster alignment is shadow-observed via
  `_observe_cluster_alignment()` which updates the histogram + the
  failure-gravity window without altering scoring.
- `score_with_cluster(text)` — new debug/evidence API. Returns
  `{score, cluster_id, cluster_kind, cluster_cosine, cluster_size}`
  for the intake router to stash in `envelope.evidence`.
- `clusters` property — immutable snapshot of the current
  `Tuple[ClusterInfo, ...]`.

### Failure-gravity tripwire

Rolling window of cluster-kinds for scored signals (default size 50,
threshold 0.3). Once the window is full, computes the
postmortem-cluster alignment fraction; emits
`[SemanticIndex] failure_gravity_detected rate=X threshold=T window=N`
WARN + bumps `failure_gravity_alerts` counter when the rate crosses
the threshold. Shadow-mode only in Slice 3a — no policy effect.
Slice 3b introduces zero-boost policy for postmortem-aligned signals.

### Logging

- INFO per rebuild under kmeans mode:
  `[SemanticIndex] kmeans k=N silhouette=S inertia=I converged=X iter=K churn=C`
- INFO per cluster:
  `[SemanticIndex] cluster id=N size=M kind=K hash8=H nearest_src=S nearest=<preview>`
- WARN on failure-gravity trip (see above)
- Legacy INFO line at rebuild extended with `cluster_mode=X cluster_count=N`

## Env knobs (all additive — default behavior unchanged)

- `JARVIS_SEMANTIC_INDEX_CLUSTER_MODE` — default **`centroid`** (v0.1).
  `kmeans` opts into cluster computation. Unrecognized → centroid.
  Case-insensitive.
- `JARVIS_SEMANTIC_CLUSTER_K_MIN` — default 1 (clamped ≥1).
- `JARVIS_SEMANTIC_CLUSTER_K_MAX` — default 5 (clamped ≥1; effective
  max = min(K_MAX, N)).
- `JARVIS_SEMANTIC_CLUSTER_KMEANS_SEED` — default 42.
- `JARVIS_SEMANTIC_CLUSTER_KMEANS_MAX_ITER` — default 30.
- `JARVIS_SEMANTIC_CLUSTER_KMEANS_TOL` — default 1e-4 (centroid
  movement).
- `JARVIS_SEMANTIC_CLUSTER_POSTMORTEM_DOMINANCE` — default 0.6
  (clamped [0,1]).
- `JARVIS_SEMANTIC_CLUSTER_FAILURE_GRAVITY_THRESHOLD` — default 0.3
  (clamped [0,1]).
- `JARVIS_SEMANTIC_CLUSTER_FAILURE_GRAVITY_WINDOW` — default 50.

## Authority invariant

Unchanged. Clustering adds no new consumer surface — the 2 existing
consumers (intake priority + CONTEXT_EXPANSION prompt) read the v0.1
centroid-cosine score today. Enforced via import-surface test:
`test_authority_invariant_clustering_does_not_import_gate_modules`
greps semantic_index.py for forbidden imports of iron_gate,
urgency_router, risk_tier_floor, semantic_guardian, policy_engine.

## Regression spine: 43 new tests (70/70 green total)

- 6 k-means math tests (determinism, K=1 trivial, 2-cluster separation,
  inertia monotonicity in K, empty-cluster repair, K≥N clamping)
- 4 silhouette math tests (single-cluster, perfect separation, random
  labeling, empty input)
- 7 auto-K tests (K=2 discovery, K=1 graceful, K_MAX respected, K_MIN
  respected, K_MAX clamped to N, empty corpus, silhouette_by_k log)
- 7 cluster-kind classifier tests (goal from commits, goal from
  commits+goals combined, postmortem, conversation, mixed, empty,
  threshold adjustability)
- 3 centroid hash tests (determinism, distinctness, empty → empty)
- 4 SemanticIndex integration (centroid mode keeps clusters empty,
  kmeans mode populates, snapshot immutability, cluster churn stability)
- 5 shadow-mode observation (score() return unchanged under kmeans,
  alignment histogram increments, score_with_cluster detail,
  score_with_cluster None when disabled, score_with_cluster None
  cluster fields when clustering off)
- 2 failure-gravity (no alert when window not full, counter present)
- 4 env hardening (malformed mode → centroid, case-insensitive,
  K bounds clamp, dominance threshold clamp)
- 1 authority invariant (import-surface enforcement)

## Slice 3c — Multi-theme CONTEXT_EXPANSION prompt rendering (2026-04-20, CLOSED)

Shipped in the same session as 3a. User-visible slice — operators see
themes before any policy change.

### What shipped

- `_theme_label_from_text(text, *, max_tokens=3)` — deterministic
  tokenizer that drops a small English stopword set, strips trailing
  punctuation, lowercases, and returns the first 2-3 non-stopword
  tokens. Empty-input or all-stopword input returns `""` → caller
  falls back to `theme-<cluster_id>`. Pure function, no LLM.
- `SemanticIndex._render_theme_sections(...)` — static helper that
  produces the `### Theme: <label> (N items, <kind>)` block list.
  Orders themes by size descending (ties broken by cluster_id asc).
  Ranks items within each theme by cosine to the cluster's centroid.
  Caps themes at `PROMPT_TOP_K` and items-per-theme at `PROMPT_TOP_K`.
- `format_prompt_sections()` rewritten to branch:
  - `cluster_mode == "kmeans"` AND `len(clusters) >= 2` AND corpus
    non-empty → themed path
  - Otherwise (centroid mode, K=1, or empty clusters) → v0.1 "Focus
    items" fallback
  - Postmortem "Recent friction / closures" subsection unchanged
    (raw recency list, orthogonal to themes)
- `_THEME_LABEL_STOPWORDS` — small English stopword set (articles,
  auxiliaries, prepositions, conjunctions, pronouns, punctuation).

### Behavioral invariants

- Postmortem-kind clusters DO appear as Themes (with `, postmortem)`
  kind tag). Operators see failure themes as structural elements
  before Slice 3b introduces the policy effect.
- Authority disclaimer preamble (`**no authority** over Iron Gate,
  routing, risk tier, policy, or FORBIDDEN_PATH matching`) preserved
  verbatim across both rendering paths.
- Deterministic output — same corpus + same seed produces
  byte-identical prompt text. Critical for prompt-cache stability.
- K=1 degrades gracefully: no themed section, v0.1 fallback.
- Master-off / prompt-injection-gate-off returns None identically
  to v0.1.

### Regression spine — 21 new tests (91/91 total green)

- 6 theme-label tokenizer (stopword filter, empty fallback,
  determinism, max_tokens, lowercasing)
- 15 renderer (themed output under kmeans, K=1 fallback, centroid
  mode fallback, kind tag present, theme cap, size-descending
  ordering, postmortem subsection preserved, authority disclaimer
  preserved, deterministic output, item cap per theme, empty-corpus
  None, postmortem cluster as theme, master-off None,
  prompt-injection-gate-off None, all-stopword-text fallback)

## Slice 3b — policy routing + zero-boost-with-evidence (2026-04-20, CLOSED)

Third slice of Epoch 3. Policy change — clusters now CAN alter routing
behavior, but only under explicit opt-in with the zero-boost-with-evidence
safety discipline.

### What shipped

- `_cluster_scoring_policy()` env helper — `JARVIS_SEMANTIC_CLUSTER_SCORING_POLICY`
  default `"centroid"`, honors `"max_cluster"`, case-insensitive,
  malformed → `"centroid"` fallback.
- `SemanticIndex._score_and_align(vec)` → `(score, winner, policy_used)`
  as the single source of truth. Routes by policy, finds winner
  regardless of policy (for kind-aware suppression + evidence).
  Defensive fallback: `max_cluster` with empty clusters degrades to
  `centroid` — stats reflect the EFFECTIVE policy, not the configured
  intent.
- `score()` routed through `_score_and_align`. Under `max_cluster`,
  returns max cosine across cluster centroids. Shadow observation
  (histogram + failure-gravity) fires regardless of policy.
- `boost_for()` routed through `_score_and_align`. Zero-boost-with-
  evidence when `policy_used == "max_cluster" AND winner.kind ==
  CLUSTER_KIND_POSTMORTEM`. Bumps `postmortem_boost_suppressions`
  counter + emits INFO log:
  `[SemanticIndex] postmortem_suppress cluster_id=N hash8=X cosine=Y size=M
   (boost zeroed; alignment still observed — Slice 3b zero-boost-with-evidence)`
- `score_with_cluster(text)` extended with `policy_used` + `boost_applied`
  fields. Evidence-stash now carries the full routing decision for
  intake-router logging.
- `IndexStats` extended with `scoring_policy`,
  `postmortem_boost_suppressions`, `scored_by_policy` (per-policy
  signal counts — lets operators see mid-session policy flips).

### Architectural invariants (enforced by tests)

- **Observation ≠ suppression**: `test_alignment_histogram_records_suppressed_postmortem`
  + `test_failure_gravity_window_records_suppressed_postmortem` pin
  that suppression is ONLY a boost-layer decision. The observation
  layer (histogram, failure-gravity window, evidence stash) continues
  to fire so operators see the postmortem theme activating.
- **Score ≠ boost**: `test_score_still_returns_real_cosine_under_suppression`
  pins that `score()` returns the real cosine even when `boost_for()`
  suppresses to 0. Downstream consumers see alignment magnitude.
- **Backward compat**: `test_score_centroid_policy_matches_v01_behavior`
  pins that `policy=centroid` (default) is BYTE-IDENTICAL to v0.1
  + Slice 3a. No regression on flag-off.
- **Defensive fallback**: `test_score_max_cluster_fallback_when_clusters_empty`
  pins that misconfiguration (`max_cluster` without clusters) degrades
  cleanly to centroid — no crash, no NaN, no silent zero.
- **Authority invariant preserved**: `test_slice_3b_suppression_affects_boost_only_not_score`
  re-asserts the import-surface invariant. 3b adds NO new consumer
  surface.

### Live demo (fake embedder + forced postmortem cluster, 2026-04-20)

    text: 'auth oauth'
    score (cosine):    0.7715    ← real cosine (observable)
    cluster_id:        0
    cluster_kind:      postmortem ← identified
    cluster_cosine:    0.7715    ← visible in evidence
    policy_used:       max_cluster
    boost_applied:     0          ← SUPPRESSED (zero-boost-with-evidence)
    postmortem_boost_suppressions: 1  (counter bumped)
    alignment_histogram_by_kind:   {'postmortem': 1}  (observation preserved)

### Regression spine — 27 new tests (118/118 total green)

- 5 env / config (default, honored, case-insensitive, malformed,
  empty-string fallback)
- 4 score routing (centroid = v0.1 backward-compat, max_cluster =
  max-cluster-cosine, fallback when clusters empty, effective policy
  in stats)
- 6 suppression (postmortem-kind suppressed, not-suppressed under
  centroid policy, not-suppressed for non-postmortem kinds,
  counter increments, INFO log fires, nonzero under centroid +
  postmortem alignment)
- 3 observation preservation (histogram records suppressed, failure-
  gravity window records suppressed, score returns real cosine under
  suppression)
- 4 score_with_cluster detail (policy_used, boost_applied,
  boost_applied=0 when suppressed, cluster_cosine populated even
  under centroid)
- 3 stats (scoring_policy present, scored_by_policy tallies per-policy,
  postmortem_boost_suppressions counter present)
- 2 authority / stability (import-surface, policy stable across
  repeated calls)

## Slice 3d — two-flag graduation (2026-04-20, CLOSED)

Final slice of Epoch 3. Flipped both defaults from opt-in shadow values
to active production defaults. Operators who want v0.1 behavior now
opt out explicitly; the organism defaults to thinking in themes and
acting on them with zero-boost-with-evidence discipline.

### What shipped

- `_cluster_mode()` default: `"centroid"` → **`"kmeans"`**
- `_cluster_scoring_policy()` default: `"centroid"` → **`"max_cluster"`**
- Both docstrings updated to document the graduation date + opt-out path
- Unrecognized env values fall back to the graduated defaults (not the
  pre-graduation values) — matches "least surprise" under the new regime
- 8 existing tests updated to either use explicit opt-out env vars or
  assert the new graduated defaults

### Full v0.1 revert requires BOTH opt-outs

This is the operator contract documented by `test_3d_full_v01_revert_requires_both_opt_outs`:

    JARVIS_SEMANTIC_INDEX_CLUSTER_MODE=centroid       # disables clustering
    JARVIS_SEMANTIC_CLUSTER_SCORING_POLICY=centroid   # disables max_cluster routing

Either one alone leaves partial 3a/3b behavior in place. Matters for
operators reverting due to unexpected theme behavior — they need to
know flipping one isn't enough.

### Regression spine additions — 8 new Slice 3d tests (126/126 total green)

- Graduation pins (2): cluster_mode default, scoring_policy default
- Opt-out pins (2): explicit `=centroid` reverts each flag
- Architecture pins (2): full-revert requires both + zero-env end-to-end
  wiring (clusters built AND max_cluster routing active)
- Authority + contract pins (2): import-surface invariant held + docstring
  graduation language present (bit-rot guard)

### Epoch 3 scorecard

| Slice | Scope | Status |
|---|---|---|
| 3a | Hand-rolled NumPy k-means + auto-K silhouette + cluster-kind classifier + shadow-mode alignment + failure-gravity tripwire | ✅ |
| 3c | Themed CONTEXT_EXPANSION prompt rendering + deterministic theme labels | ✅ |
| 3b | Policy routing + zero-boost-with-evidence for postmortem kinds | ✅ |
| 3d | Two-flag graduation — both defaults flipped to active | ✅ |

**126/126 tests green across all four slices.** Authority invariant
preserved throughout — clustering + max_cluster policy remain advisory,
consumed only by intake priority + CONTEXT_EXPANSION prompt.

### Live-fire zero-env proof (2026-04-20)

    # No cluster-related env vars set (fresh operator install)
    > idx = SemanticIndex(repo)
    > idx.build(force=True)
    stats.cluster_mode     = 'kmeans'       ← graduated default
    stats.cluster_count    = 3              ← themes recognized
    > idx.score('auth new work')
    stats.scoring_policy   = 'max_cluster'  ← graduated routing active
    > idx.format_prompt_sections()
    ### Theme: perf pool (4 items, conversation)  ← themed rendering live
