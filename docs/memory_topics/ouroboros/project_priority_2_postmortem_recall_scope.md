---
title: Project Priority 2 Postmortem Recall Scope
modules: []
status: merged
source: project_priority_2_postmortem_recall_scope.md
---

**Scope status: DRAFT** (post-Priority-#1-closure 2026-05-01,
authorized for execution per architectural directive). After
commits `3cd01154ac` + `27e656b0e6` + `a4cc239ad3` +
`729b6a39e1` + `35ecae0806` (Priority #1 full arc), the
empirical floor still bottlenecks at **B+** despite the
structural ceiling rising to **A−**. Priority #2 is the
mechanism that converts Priority #1's RECURRENCE_DRIFT
detection signal into actual recurrence prevention — closing
the loop and producing the empirical evidence Move 6 master
flag graduation requires.

## Why Priority #2 (architectural justification)

§28.7 brutal review identified that **detection without
prevention is half a loop**:

  * Move 4 detects structural drift (boot snapshot vs current).
  * Priority #1 detects behavioral drift (window-over-window).
  * Priority #1 Slice 4 reserved an advisory action
    `INJECT_POSTMORTEM_RECALL_HINT` for `RECURRENCE_DRIFT`
    findings — but the action is **wired-but-dormant**
    pending Priority #2's consumer surface.

Without PostmortemRecall:

| Problem | Today |
|---|---|
| Same `failure_class` postmortem appears in 5+ sessions | Detected by Priority #1's `RECURRENCE_DRIFT` finding; advisory action `INJECT_POSTMORTEM_RECALL_HINT` written to `coherence_advisory.jsonl`; no actual prompt-level intervention happens |
| New op against a previously-failing file | EpisodicFailureMemory provides within-op retry context but ZERO cross-session memory. Same mistake repeats fresh every session. |
| Move 6 master graduation evidence path | Operator can't justify K× cost amplification because there's no measured baseline of recurrence reduction |

**Why this is the highest priority** (per §28.7's revised
critical path):

  * Priority #1 produces the upstream signal; Priority #2 is
    the only consumer architecturally compatible with the
    `INJECT_POSTMORTEM_RECALL_HINT` reserved surface
  * Closes the recurrence-prevention loop (detect → recall →
    inject → prevent → measure reduction → graduate Move 6)
  * Compounds with Priority #3 (Counterfactual Replay): once
    PostmortemRecall is live, replay-with-policy-swap can
    answer "would PostmortemRecall have prevented THIS
    recurrence?"
  * Empirical recurrence reduction is the load-bearing metric
    that retroactively justifies every adjacent Move's cost
    profile

## Existing infrastructure to leverage (NO duplication)

The substrate is mostly already shipped — Priority #2 EXTENDS
or COMPOSES, never duplicates:

| Existing | Reuse via | Slice |
|---|---|---|
| `EpisodicFailureMemory.FailureEpisode` (`episodic_memory.py`) | `PostmortemRecord` extends FailureEpisode shape (adds session_id + symbol_name + ast_signature for cross-session lookup) | Slice 1 |
| `EpisodicFailureMemory.format_for_prompt()` | Rendering pattern blueprint (Slice 3 produces cross-session analog with char-budget extension) | Slice 3 |
| `last_session_summary._parse_summary()` (`last_session_summary.py`) | JSON-load + sanitization helper — Slice 2 walks `.ouroboros/sessions/*/summary.json` reusing the shared parser | Slice 2 |
| `last_session_summary._sanitize_field()` | Tier -1 field sanitization — Slice 3's prompt injection reuses for safety | Slice 3 |
| `summary.json` (produced by `governed_loop_service.py`) | Source-of-truth for postmortem records — already contains `failure_class` field (`governed_loop_service.py:2266+`); Slice 2 reads directly | Slice 2 |
| `compute_ast_signature` (Move 6 Slice 2) | Symbol identity matching — function/class signature fingerprint enables cross-session recall by structural similarity | Slice 1 (literal-reuse via test parity, OR import — design-decided at Slice 1 implementation) |
| `cross_process_jsonl.flock_append_line` (Tier 1 #3) | Index file append safety + cross-process correctness | Slice 2 |
| `cross_process_jsonl.flock_critical_section` (Tier 1 #3) | Index rebuild atomic-write coordination | Slice 2 |
| **CONTEXT_EXPANSION injection precedent** (StrategicDirection / SemanticIndex / ConversationBridge / LastSessionSummary all inject here) | Slice 3 follows the established prompt-section composition pattern; render before GENERATE with deterministic ordering | Slice 3 |
| **Priority #1 Slice 4's `INJECT_POSTMORTEM_RECALL_HINT` advisory action** | Slice 4 of Priority #2 is the consumer that reads `coherence_advisory.jsonl` for these advisory records and adjusts recall budget for the next N ops on the matched failure_class | Slice 4 |
| `auto_action_router` (Move 3) | Read-only advisory-record reader pattern; Slice 4 may use auto_action_router observer hook for triggering | Slice 4 |
| `AdaptationLedger.MonotonicTighteningVerdict` | If Slice 4 ever proposes adjusting `JARVIS_POSTMORTEM_RECALL_TOP_K` upward, the proposal goes through Phase C cage rule (load-bearing structural pin) | Slice 4 |
| `FlagRegistry` + `_HARDEN_AND_CONSOLIDATE` posture relevance | 6+ FlagSpec seeds with HARDEN+CONSOLIDATE relevance markers | Slice 5 |
| `shipped_code_invariants` registry | 4 new AST pins (28+4=32 already; +4 = 36 post-Priority-2) | Slice 5 |
| `EventChannelServer` SSE broker | `EVENT_TYPE_POSTMORTEM_RECALL_INJECTED` lazy-publish (mirrors Move 4/5/6/Priority#1 discipline) | Slice 5 |

What Priority #2 BUILDS:

  * **PostmortemRecord** — frozen cross-session extension of
    `FailureEpisode` with session_id + symbol_name + ast_
    signature + age_days fields
  * **PostmortemIndex** — append-only JSONL store of all
    parsed postmortems across `.ouroboros/sessions/*/`,
    cross-process-safe via Tier 1 #3 flock
  * **Recall ranker** — pure decision: given (target_files,
    target_symbols, target_failure_class), return top-K
    relevant postmortems by recency × structural similarity ×
    failure_class match
  * **Prompt section renderer** — `## Recent Failures
    (advisory)` injected at CONTEXT_EXPANSION with char-budget
    truncation
  * **Recurrence consumer** — reads
    `INJECT_POSTMORTEM_RECALL_HINT` advisories from Priority
    #1 Slice 4's chain; boosts recall budget for matched
    failure_class on next N ops; budget decays with TTL

## The 5-slice arc

### Slice 1 — PostmortemRecord primitive (pure data + compute)

**New module**: `verification/postmortem_recall.py`

* Frozen dataclasses (mirror Priority #1 Slice 1's
  J.A.R.M.A.T.R.I.X. discipline):
  - `PostmortemRecord(session_id, op_id, file_path,
    symbol_name, failure_class, failure_reason,
    failure_phase, attempt, ast_signature, recorded_at_ts,
    age_days, schema_version)` — extends
    `episodic_memory.FailureEpisode` shape WITH cross-
    session fields. NEVER imports EpisodicFailureMemory
    directly (avoid coupling); structural parity verified
    by test that asserts every FailureEpisode field is
    present in PostmortemRecord with identical type.
  - `RecallTarget(target_files, target_symbols,
    target_failure_class, max_age_days, schema_version)`
    — bounded query.
  - `RecallVerdict(outcome, records, total_index_size,
    max_relevance, schema_version)` — frozen result.
* 5-value `RecallOutcome` closed enum (J.A.R.M.A.T.R.I.X.):
  - `HIT` — at least one relevant record returned
  - `MISS` — index non-empty but no records met threshold
  - `EMPTY_INDEX` — index has zero records (cold start)
  - `DISABLED` — master flag off
  - `FAILED` — defensive sentinel
* 4-value `RelevanceLevel` closed enum:
  - `NONE` — no field match
  - `LOW` — only failure_class match
  - `MEDIUM` — failure_class + (file OR symbol) match
  - `HIGH` — failure_class + file + symbol match (or AST
    signature match)
* `compute_relevance(record, target) -> RelevanceLevel` —
  pure decision. NEVER raises.
* `recall_postmortems(records, target, *, max_results,
  threshold, halflife_days) -> Tuple[PostmortemRecord, ...]`
  — pure recency-weighted ranking + filtering. Recency
  formula REUSES Priority #1's `_recency_weight` (literal
  byte-parity — same test discipline).
* Schema version `POSTMORTEM_RECALL_SCHEMA_VERSION =
  "postmortem_recall.1"`.
* Master flag `JARVIS_POSTMORTEM_RECALL_ENABLED` default-
  false until Slice 5 graduation.
* **Authority invariants AST-pinned**: stdlib ONLY (mirrors
  Priority #1 Slice 1's strongest-possible invariant). Zero
  governance imports. ast_canonical formula reused inline
  (literal-reuse contract pinned by parity test) so module
  stays pure-stdlib.

**Tests**: ~50 covering frozen-dataclass shape +
serialization (to_dict/from_dict round-trip), master-flag
asymmetric env, RecallOutcome 5-value closed taxonomy pin,
RelevanceLevel 4-value pin, compute_relevance per-level
correctness (parametrized × 4 levels × 6 input shapes),
recall ranking math (recency × relevance × age),
**FailureEpisode field-parity pin** (every FailureEpisode
field present in PostmortemRecord), **ast_canonical
literal-reuse parity** (mirrors Priority #1 Slice 1 pattern),
defensive contract (NEVER raises), authority invariants
AST-pinned.

### Slice 2 — Cross-session index store (flock'd append-only)

**New module**: `verification/postmortem_recall_index.py`

* `.jarvis/postmortem_recall_index.jsonl` — append-only
  JSONL of all parsed PostmortemRecords from
  `.ouroboros/sessions/*/summary.json`. Keyed by
  sha256[:16] over (session_id + op_id + file_path) for
  duplicate suppression on rebuild.
* Cross-process safe via Tier 1 #3:
  - `flock_append_line` for new records
  - `flock_critical_section` for index rebuild atomic-write
* 5-value `IndexOutcome` closed enum:
  - `BUILT` — fresh index built from scratch
  - `UPDATED` — incremental records appended
  - `READ_OK` — read returned populated index
  - `READ_EMPTY` — file missing / no records
  - `FAILED` — defensive sentinel
* Public API:
  - `rebuild_index_from_sessions(*, project_root,
    max_age_days) -> IndexOutcome` — walks
    `.ouroboros/sessions/*/summary.json`. **REUSES
    last_session_summary._parse_summary()** for
    JSON-load + sanitization (zero duplication of the
    parser).
  - `record_postmortem(record) -> IndexOutcome` —
    incremental append.
  - `read_index(*, max_age_days, limit) ->
    Tuple[PostmortemRecord, ...]` — schema-tolerant
    (corrupt lines silently dropped).
* Bounded:
  - `JARVIS_POSTMORTEM_RECALL_MAX_INDEX_SIZE` (default
    5000, floor 100, ceiling 50000). Read-trim-atomic-
    write evicts oldest at cap.
  - `JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS` (default 30,
    floor 1, ceiling 365). Records older than this are
    not included in reads.
* Sub-gate `JARVIS_POSTMORTEM_INDEX_ENABLED` default-false
  until Slice 5.
* **Authority invariants AST-pinned**: stdlib + Slice 1 +
  Tier 1 #3 + LastSessionSummary's parser (specifically
  cited via importfrom). NO orchestrator imports.

**Tests**: ~50 covering rebuild from synthetic
`summary.json` files, incremental append, schema-tolerance
(corrupt lines / wrong schema / partial fields all silently
skipped), age-bounded read filter, ring-buffer rotation at
cap, multi-process flock stress (mirrors Priority #1 Slice
2's discipline), atomic-write integrity, defensive contract
(NEVER raises), 8 authority pins (governance-allowlist
verified, **MUST reference LastSessionSummary parser** as
load-bearing reuse contract).

### Slice 3 — CONTEXT_EXPANSION prompt injector

**New module**: `verification/postmortem_recall_injector.py`

* `render_postmortem_recall_section(*, target_files,
  target_symbols, target_failure_class, max_results,
  max_chars) -> str` — produces a Tier -1 sanitized
  prompt section.
* Format (mirrors LastSessionSummary's section header
  convention):
  ```
  ## Recent Failures (advisory)

  Symbol `helper.py:do_thing` failed N times in last 30d
  with failure_class=test (most recent: 2d ago). Reason:
  "<sanitized>".

  File `auth.py` has 3 prior failures in failure_class=
  build (most recent: 5d ago).

  ...
  ```
* Bounded char limit (env-tunable):
  - `JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS` (default
    2000, floor 500, ceiling 8000)
* `JARVIS_POSTMORTEM_RECALL_TOP_K` (default 3, floor 1,
  ceiling 10) — max records rendered per call
* **Robust degradation** (the load-bearing requirement):
  - Empty index → empty string return (NO injection)
  - Corrupt index → log + empty string (orchestrator
    continues with standard prompt; **GENERATE NEVER
    affected**)
  - All public functions catch every exception → empty
    string fallback
  - AST-pinned: NEVER calls into orchestrator/policy/etc;
    READ-ONLY over the index file.
* Sanitization via reused `last_session_summary._sanitize
  _field()` helper.
* Sub-gate `JARVIS_POSTMORTEM_INJECTION_ENABLED` default-
  false until Slice 5.
* **Orchestrator hook**: Slice 3 exposes a single `compose_
  for_op_context(op_id, *, files, symbols, failure_class)
  -> str` entry point that orchestrator calls during
  CONTEXT_EXPANSION. The hook is wired in Slice 5 (master
  default-true graduation).

**Tests**: ~50 covering render-empty-on-empty-index,
render-empty-on-master-off, render-empty-on-sub-gate-off,
char-budget truncation (parametrized over budget values),
Tier -1 sanitization (control chars stripped, secrets
redacted via `_sanitize_field` reuse), top-K filtering,
relevance threshold filtering, **robust-degradation
matrix** (corrupt JSONL line / malformed record /
non-existent file / disk-error → empty string never raise),
orchestrator hook compose-for-op shape, schema integrity, 6
authority pins (NEVER imports orchestrator/policy/etc).

### Slice 4 — Recurrence consumer (activates Priority #1 Slice 4 advisory)

**New module**: `verification/postmortem_recall_consumer.py`

* Reads `coherence_advisory.jsonl` (written by Priority #1
  Slice 4) for advisory records with `action=
  INJECT_POSTMORTEM_RECALL_HINT`.
* `RecurrenceBoost(failure_class, boost_count, expires_at,
  source_advisory_id, schema_version)` — frozen dataclass.
* `compute_recurrence_boosts(advisories, *, ttl_seconds,
  max_boost_count) -> Mapping[str, RecurrenceBoost]` —
  pure decision: groups by failure_class, takes max boost,
  applies TTL decay.
* `apply_recurrence_boost_to_recall(target,
  boosts) -> RecallTarget` — pure transformation: extends
  RecallTarget's `top_k` upward by boost_count for matched
  failure_class. Bounded by `JARVIS_POSTMORTEM_RECALL_TOP
  _K_CEILING` (default 10) — boost cannot exceed ceiling.
* **Monotonic-tightening parity**: when a boost is applied,
  the proposal IS conceptually a TIGHTENING (more recall =
  more cautious GENERATE). The consumer stamps every
  boost-application with the canonical
  `MonotonicTighteningVerdict.PASSED.value` from
  `adaptation.ledger` — same Phase C cross-stack
  vocabulary as Priority #1 Slice 4.
* TTL-decay so boosts don't accumulate unboundedly:
  - `JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS` (default 6,
    floor 1, ceiling 168)
* **Cost contract preservation**: consumer is read-only on
  `coherence_advisory.jsonl`; only adjusts in-memory
  RecallTarget. NO LLM calls. NO additional generation
  amplification (just adjusts how many existing index
  records are rendered).
* Sub-gate `JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED`
  default-false until Slice 5.
* **Authority invariants AST-pinned**: stdlib + Slice 1 +
  Slice 3 + adaptation.ledger (`MonotonicTighteningVerdict`
  ONLY) + cross_process_jsonl. MUST reference
  MonotonicTighteningVerdict (catches refactor that drops
  Phase C integration).

**Tests**: ~50 covering boost computation per-failure-
class, TTL-decay correctness, ceiling-clamped boost
(bounded by max top-K), monotonic-tightening verdict
stamping (every boost application carries
`MonotonicTighteningVerdict.PASSED.value` string),
backward-compat (when no advisories present, returns
empty boosts → no behavior change), defensive contract
(NEVER raises), 8 authority pins (MUST reference
MonotonicTighteningVerdict + flock_append_line for
optional advisory log).

### Slice 5 — Graduation + operator surfaces

* **Master + 3 sub-gate flags flips** (default false → **true**):
  - `JARVIS_POSTMORTEM_RECALL_ENABLED` (master)
  - `JARVIS_POSTMORTEM_INDEX_ENABLED` (sub-gate)
  - `JARVIS_POSTMORTEM_INJECTION_ENABLED` (sub-gate)
  - `JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED` (sub-gate)

  **Why default-true (matching Priority #1's discipline)**:
  PostmortemRecall is read-only over existing artifacts
  (`.ouroboros/sessions/*/summary.json` + Priority #1's
  advisory chain), runs at CONTEXT_EXPANSION (NOT per-LLM-
  call but per-op pre-generation), produces ONLY a prompt
  section (no auto-flag-flip path). Cost profile is
  fundamentally different from Move 6's K× generation
  amplification.

* **shipped_code_invariants AST pins** (4 new, total 32→36):
  - `postmortem_recall_pure_stdlib` — Slice 1 zero
    governance imports + no exec/eval/compile + no async
    (mirrors Priority #1 Slice 1 + Move 6 Slice 2 critical
    safety pin)
  - `postmortem_recall_index_uses_flock` — Slice 2 MUST
    reference `flock_append_line` + `flock_critical_
    section` AND import `_parse_summary` from
    `last_session_summary` (load-bearing reuse contract)
  - `postmortem_recall_injector_authority_free` — Slice
    3 MUST NOT import orchestrator/policy/iron_gate/etc;
    READ-ONLY over the index; pure prompt-section
    rendering
  - `postmortem_recall_consumer_consumes_adaptation_ledger`
    — Slice 4 MUST import `MonotonicTighteningVerdict`
    from `adaptation.ledger` (Phase C cross-stack
    vocabulary integration)

* **FlagRegistry seeds**: 6 FlagSpec entries:
  - JARVIS_POSTMORTEM_RECALL_ENABLED (SAFETY,
    HARDEN+CONSOLIDATE-relevant, default true)
  - JARVIS_POSTMORTEM_INDEX_ENABLED (SAFETY, default true)
  - JARVIS_POSTMORTEM_INJECTION_ENABLED (SAFETY, default true)
  - JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED (SAFETY,
    default true)
  - JARVIS_POSTMORTEM_RECALL_TOP_K (CAPACITY, default 3,
    floor 1, ceiling 10)
  - JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS (CAPACITY,
    default 30, floor 1, ceiling 365)

* **SSE event** `EVENT_TYPE_POSTMORTEM_RECALL_INJECTED`
  fired on every non-empty injection. Lazy
  ide_observability_stream import (best-effort, never
  raises).

* **Operator surfaces — DEFERRED to Slice 5b** (consistent
  with Priority #1's pattern):
  - `/postmortem` REPL — recent / index / matched / clear
    (mirrors `/coherence` / `/probe` / `/quorum` shape)
  - 4 GET routes:
    `/observability/postmortem{,/index,/matched,/recent}`

* **Comprehensive graduation pin suite** (~50 tests):
  - 4 master/sub-gate flags default-TRUE pins
  - 5 cap-structure clamps (parametrized: default, below-
    floor, above-ceiling)
  - 4 Priority #2 invariant pins registered AND HOLD
    (parametrized × 4)
  - Total invariant count ≥ 36 pin
  - 6 FlagRegistry seeds present + defaults pinned
  - Full-revert matrix (master-off → no injection;
    sub-gate-off matrix)
  - **End-to-end recurrence-prevention proof**: synthetic
    summary.json with N postmortems for `failure_class=X`
    → index built → CONTEXT_EXPANSION render produces
    expected section → recurrence boost activates after
    advisory → next op's recall injects extended top-K
  - Authority invariants final pass (no-orchestrator,
    sync-vs-async per slice, no exec/eval/compile)

### Slice budget

| Slice | New module | Tests | LOC est |
|---|---|---|---|
| 1 — Postmortem record primitive | postmortem_recall.py | ~50 | ~450 |
| 2 — Cross-session index store | postmortem_recall_index.py | ~50 | ~400 |
| 3 — CONTEXT_EXPANSION injector | postmortem_recall_injector.py | ~50 | ~350 |
| 4 — Recurrence consumer | postmortem_recall_consumer.py | ~50 | ~400 |
| 5 — Graduation + AST pins + seeds | (no new module — modifies existing) | ~50 | ~600 |

**Total**: ~5 commits, ~250 tests, ~2,200 net new lines.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Prompt bloat** — recall section consumes too much CONTEXT_EXPANSION budget, starving GENERATE of useful context | `JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS` (default 2000, ceiling 8000) hard-bounds the section. Top-K cap (default 3, ceiling 10) bounds the record count. Truncation is character-bounded with explicit `… [truncated]` marker. |
| **False-positive recall** — irrelevant postmortem injected because failure_class match alone produced LOW relevance | RelevanceLevel threshold default MEDIUM (env-tunable down to LOW for HARDEN posture). MEDIUM requires failure_class + (file OR symbol). Operator can tighten to HIGH (failure_class + file + symbol) via env. |
| **Index file corruption** — corrupt JSONL line breaks all reads | Schema-tolerant parser silently drops corrupt lines (mirrors Priority #1 Slice 2 + Move 4 Slice 2 patterns). Index file is RECOVERABLE via `rebuild_index_from_sessions()` from session ground truth. |
| **Cross-process index race** | Tier 1 #3 `flock_append_line` for incremental writes; `flock_critical_section` for rebuild atomic-write. AST-pinned by Slice 5. |
| **Stale postmortems** (months-old recurrence boost still firing) | `JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS` (default 30, ceiling 365) bounds index reads. Boost TTL `JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS` (default 6, ceiling 168) decays the recurrence-boost surface. |
| **PostmortemRecall + Move 6 Quorum compounding cost** | NO compounding by design: PostmortemRecall lives at CONTEXT_EXPANSION (per-op pre-generation), Move 6 Quorum lives post-GENERATE. They compose linearly, not multiplicatively. PostmortemRecall actually REDUCES Quorum's invocation rate by preventing the recurrences that would have triggered APPROVAL_REQUIRED tier escalation. |
| **Crash mid-CONTEXT_EXPANSION breaks GENERATE** | **Robust degradation contract is load-bearing**: every public Slice 3 function catches every exception, returns empty string fallback. GENERATE phase NEVER sees a raise from PostmortemRecall — it either gets an injection or an empty string. AST-pinned by Slice 5. |
| **Index file unbounded growth on long-running deployments** | `JARVIS_POSTMORTEM_RECALL_MAX_INDEX_SIZE` (default 5000, ceiling 50000) bounds record count via read-trim-atomic-write rotation. |
| **Recurrence-boost loop** — boost extends top-K → more recalls → more drift detection → more boosts → unbounded | TTL decay + ceiling cap on top-K. Boost cannot exceed `JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING` (default 10). Operator-bounded by construction. |
| **Sensitive postmortem text in prompts** (secrets / paths) | Reuse `_sanitize_field()` from LastSessionSummary — already battle-tested for the same source data. Test pin: synthetic record with API key → renders as `[REDACTED]`. |

## Authority invariants (AST-pinned by Slice 5 graduation pins)

  * `postmortem_recall.py` (Slice 1) — stdlib ONLY. NEVER
    imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers
    / doubleword_provider / urgency_router / auto_action_
    router / subagent_scheduler / tool_executor /
    semantic_guardian / semantic_firewall / risk_engine.
    Strongest possible authority invariant — mirrors Priority
    #1 Slice 1's pure-stdlib pattern. ast_canonical reused
    via literal-parity test (NOT direct import) so module
    stays pure.

  * `postmortem_recall_index.py` (Slice 2) — stdlib + Slice 1
    + Tier 1 #3 (`cross_process_jsonl`) + LastSessionSummary
    parser (`_parse_summary` import-from). MUST reference
    `flock_append_line` + `flock_critical_section` AND
    `_parse_summary` from `last_session_summary`.

  * `postmortem_recall_injector.py` (Slice 3) — stdlib +
    Slice 1 + Slice 2 + LastSessionSummary
    (`_sanitize_field`). NEVER imports orchestrator. READ-
    ONLY contract on the index file.

  * `postmortem_recall_consumer.py` (Slice 4) — stdlib +
    Slice 1 + Slice 3 + adaptation.ledger
    (`MonotonicTighteningVerdict` ONLY) + cross_process_jsonl.
    MUST reference `MonotonicTighteningVerdict` (Phase C
    cross-stack vocabulary integration AST-pinned).

  * No mutation tools referenced anywhere (AST walk verifies).
  * No exec/eval/compile (mirrors Move 6 Slice 2's critical
    safety pin — recall renderer NEVER executes shipped code).
  * Slice 1 + 2 + 3 + 4 are sync; Slice 5 surfaces (REPL/
    GET, deferred to 5b) wrap async.

## Knobs (Slice 5 graduation defaults)

### Master + sub-gates
  * `JARVIS_POSTMORTEM_RECALL_ENABLED` — master, **graduated true**
  * `JARVIS_POSTMORTEM_INDEX_ENABLED` — sub-gate, **graduated true**
  * `JARVIS_POSTMORTEM_INJECTION_ENABLED` — sub-gate, **graduated true**
  * `JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED` — sub-gate, **graduated true**

### Recall behavior
  * `JARVIS_POSTMORTEM_RECALL_TOP_K` (default 3, floor 1,
    ceiling 10) — max records per injection
  * `JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING` (default 10,
    floor 3, ceiling 30) — absolute ceiling for boost-
    extended top-K
  * `JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS` (default 30,
    floor 1, ceiling 365) — record age cutoff
  * `JARVIS_POSTMORTEM_RECALL_RELEVANCE_THRESHOLD` (string
    default `"medium"`; valid: `"low"|"medium"|"high"`)

### Prompt budget
  * `JARVIS_POSTMORTEM_RECALL_MAX_PROMPT_CHARS` (default
    2000, floor 500, ceiling 8000)

### Index store
  * `JARVIS_POSTMORTEM_RECALL_MAX_INDEX_SIZE` (default
    5000, floor 100, ceiling 50000)
  * `JARVIS_POSTMORTEM_RECALL_INDEX_PATH` (default
    `.jarvis/postmortem_recall_index.jsonl`)

### Recurrence boost
  * `JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS` (default 6,
    floor 1, ceiling 168)
  * `JARVIS_POSTMORTEM_RECALL_BOOST_MAX_COUNT` (default 5,
    floor 1, ceiling 20) — max boost magnitude per failure_
    class

### Recency decay
  * `JARVIS_POSTMORTEM_RECALL_HALFLIFE_DAYS` (default 14.0,
    floor 0.5, ceiling 90.0) — mirrors Priority #1's
    halflife default for cross-arc consistency

## Cost contract preservation (PRD §26.6)

PostmortemRecall is **read-only over existing artifacts**:

  * Reads `.ouroboros/sessions/*/summary.json` (already on disk)
  * Reads `.jarvis/postmortem_recall_index.jsonl` (own append-only file)
  * Reads `.jarvis/coherence_advisory.jsonl` (Priority #1 Slice 4 output)
  * Reads env (no I/O)
  * **Zero LLM calls**. Zero additional generation amplification.
  * Runs at CONTEXT_EXPANSION (per-op pre-generation), NOT per-LLM-call.
  * AST-pinned: Priority #2 modules MUST NOT import
    `providers` / `doubleword_provider` / `urgency_router` /
    `candidate_generator` (Slice 5 pin).
  * Recurrence boost adjusts in-memory recall budget ONLY —
    actual operator-tunable flag flips still require
    `MetaAdaptationGovernor` approval (Phase C cage rule).

## Slice independence

Each slice independently mergeable:

  * Slice 1 ships primitive — Slices 2-5 not landed → no
    behavior change (primitive unused).
  * Slice 2 ships index store — usable by tests but
    rebuild_index_from_sessions() not auto-triggered until
    Slice 5 boot wiring.
  * Slice 3 ships injector — sub-gate default-false until
    Slice 5; orchestrator hook not wired until Slice 5.
  * Slice 4 ships consumer — sub-gate default-false until
    Slice 5; advisory chain still produces records (Priority
    #1 Slice 4 dormant action) but no consumer activation
    until graduation.
  * Slice 5 graduates — flags default-true unlock the full
    pipeline.

This matches Move 4 + Move 5 + Move 6 + Priority #1
substrate-first cadence.

## What this Move does NOT prescribe

  * **No new ENFORCEMENT** — every recall is advisory; the
    model sees the prompt section but NOTHING auto-blocks
    if the model ignores it. Iron Gate / SemanticGuardian /
    risk-tier ladder remain the structural enforcers.
  * **No replacement of EpisodicFailureMemory** —
    EpisodicFailureMemory is per-op retry context (within
    a single operation); PostmortemRecall is cross-session
    context. They compose linearly: per-op memory governs
    GENERATE retry; cross-session recall governs first-pass
    GENERATE composition. Zero overlap, zero replacement.
  * **No new auto-flag-flipping path** — recurrence boost
    adjusts in-memory recall budget only. Any actual flag
    flip requires MetaAdaptationGovernor approval.
  * **No counterfactual replay integration** — that's
    Priority #3. Priority #2 produces the recurrence-
    reduction signal Priority #3's replay engine will
    consume.
  * **No symbol-graph extraction** — symbol matching uses
    file_path + symbol_name + ast_signature only. Full
    cross-symbol dependency tracing is out-of-scope.

## Closure criterion

Priority #2 closes when:

  * All 5 slices land (commits + regression tests green)
  * Master + 3 sub-gate flags graduated default-true
  * shipped_code_invariants AST pins register and currently-
    hold (target: 36 total invariants post-Priority-2)
  * SSE event `EVENT_TYPE_POSTMORTEM_RECALL_INJECTED` live
  * `memory/project_priority_2_postmortem_recall_closure.md`
    written
  * MEMORY.md indexed
  * **End-to-end recurrence-prevention proof** in graduation
    test suite: synthetic summary.json → index build →
    matching op → CONTEXT_EXPANSION includes recall section
    → recurrence boost activated after Priority #1 advisory
    → next op gets extended top-K
  * Slice 5b (REPL + 4 GET routes) deferred per Priority #1
    precedent

## Why this is RSI-load-bearing

Priority #1 closed the **temporal-safety envelope**: drift
is now detectable. Priority #2 closes the **recurrence-
prevention loop**: detection translates to actual prevention.

Without Priority #2:

  * Priority #1's `RECURRENCE_DRIFT` advisories are
    operator-readable but not operationally consumed.
  * Same `failure_class` postmortem can recur indefinitely
    across sessions because no mechanism injects the prior-
    failure context.
  * Move 6 (Generative Quorum) cannot graduate empirically
    because there's no measurable recurrence-reduction
    baseline to justify K× cost amplification.

With Priority #2:

  * Every CONTEXT_EXPANSION at-risk for known failure
    classes receives prior-failure context.
  * Recurrence-drift advisories activate operationally:
    detected drift → boost recall → next op sees more
    history → models steer away from the failure mode.
  * Empirical recurrence-reduction baseline becomes
    measurable across sessions, providing the operator with
    quantitative evidence that justifies Move 6's master
    flag graduation: "Quorum's K× cost is justified BECAUSE
    we measure 60% recurrence reduction over baseline."

This is what closes the gap from B+ empirical floor to A−
empirical floor. **Priority #3 (Counterfactual Replay)
compounds**: replay every blocked-or-recurrent op WITH and
WITHOUT PostmortemRecall enabled, measuring the prevention
delta empirically and feeding the answer back into Priority
#1's drift budgets.

The Reverse Russian Doll's outer shell now scales
**preventatively**, not just observationally — the immune
system doesn't just see recurrence, it actively counteracts
it. Anti-Venom remains the structural enforcer; Priority #2
is the cognitive scaffolding that biases the next-op
synthesis toward non-recurrence by construction.
