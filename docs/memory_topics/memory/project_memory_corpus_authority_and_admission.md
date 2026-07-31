---
title: Memory corpus authority + admission ledger
modules: [backend/core/ouroboros/governance/intake/sensors/memory_hygiene_sensor.py, backend/core/ouroboros/governance/operator_rules.py, backend/core/ouroboros/governance/user_preference_memory.py, backend/core/ouroboros/governance/memory_scope.py, backend/core/ouroboros/governance/subagent_orchestrator.py, backend/core/ouroboros/governance/subagent_contracts.py, backend/core/ouroboros/governance/memory_utility.py, backend/core/ouroboros/governance/ops_digest_observer.py, backend/core/ouroboros/governance/memory_corpus.py, backend/core/ouroboros/governance/memory_admission.py, backend/core/ouroboros/governance/module_routing.py, backend/core/ouroboros/battle_test/memory_surface.py, backend/core/ouroboros/governance/phase_runners/context_expansion_runner.py]
status: active
source: session 2026-07-30, CC-parity memory arc
---

# Memory corpus authority + admission ledger

Closes the gap between what O+V remembers and what reaches a GENERATE
prompt. Started as a CC-parity list (index budget / `/context` / timestamps /
subagent scoping); the first measurement found a live defect that outranked
all of them.

## The defect: 301 ghost topics

`docs/memory_topics/` held **764 `.md` files. Git tracked 383.** The rest were
iCloud conflict copies in ` 2`-suffixed directories, gitignored at
`.gitignore:270`, created because this repo lives under a synced
`~/Documents`.

`memory_surface.py` had been fixed to ask `git ls-files`.
`module_routing._load_topic_fragments_worker` — the code that actually feeds
prompts — still walked `rglob("*.md")`. Content-hash dedup dropped the 80
byte-identical copies and **left 301 DIVERGED snapshots standing as
first-class topics**: same title, same `modules:` frontmatter, older body,
competing for the same three slots in every routed prompt.

**The display was fixed; the source was not.** Two readers, two definitions
of "the corpus", and the one that mattered was the one nobody was watching.
That is the same class as every other finding this week — a value computed
correctly and dropped one frame short of the eye.

## The fix: one authority, not one filter

`memory_corpus.py` is the single definition both consume. The corpus is what
the REPOSITORY declares, not what the filesystem holds.

Filtering the ` 2` name would have fixed one cause. Asking git generalises
past every sibling cause for free — editor backups, a vendored second
checkout, stray downloads — because "ignored" is the repo stating that a file
is not part of itself. No pattern list to maintain.

Degradation is STAMPED, never silent: `CorpusProvenance.{GIT_TRACKED,
WALK_FALLBACK,ABSENT}` rides with every listing, and `383 (git_tracked)` vs
`684 (walk_fallback ⚠)` are visibly different claims. Git answering "none"
while files exist falls back to the tree rather than reporting an empty mind
on the strength of a path bug.

Hash dedup stays as the second line — it catches duplication a VCS cannot
see, such as one topic copy-pasted to two tracked paths.

## Referential staleness beats a timestamp

CC stamps a `modified` date. That measures the wrong thing: a topic written
six months ago about a module nobody has touched is not stale, and one
written last week about a module rewritten yesterday is.

O+V's topics already declare `modules:`, so staleness here compares the
topic's last-commit time against the last-commit time of the modules it
CLAIMS. `Drift.{FRESH,DRIFTED,ORPHANED,UNBOUND,UNKNOWN}`.

Load-bearing invariants:

- **UNKNOWN is never penalised** (`rank_multiplier == 1.0`). Penalising on
  absence of evidence would bury every topic older than the scan window —
  a rewrite of the corpus disguised as a heuristic. Same rule the
  blast-radius gutter keeps: unmeasured must not render as measured.
- **Drift is a WEIGHT, not a filter** (`JARVIS_MEMORY_DRIFT_PENALTY`, 0.6).
  A drifted topic is still the best surviving record of an intention.
- **Same-second ties read FRESH.** `%ct` is second-granular, so a topic
  committed in the SAME commit as its subject is indistinguishable from one
  a second later. Resolving ties to DRIFTED would demote exactly the topic
  most likely to be correct.
- **Bounded scan, still decidable.** One `git log -n4000 --name-only` pass
  builds `path -> newest commit epoch` for the whole corpus (the naive shape
  is ~1,400 `git log -1` spawns). A path outside the window is simply older
  than the window — which still DECIDES the comparison whenever one side is
  inside it. Only both-outside is UNKNOWN.
- **Ambiguous bare module names are not guessed.** Frontmatter mixes
  qualified and bare paths; a basename matching two files resolves to None
  rather than to a confident verdict about the wrong module.

Cost: 2.9s cold, **21ms warm across a fresh process** — the map persists to
`.jarvis/memory_touch_map.json` keyed on HEAD (exactly correct or discarded;
a TTL would have to guess how fast the repo moves).

## The admission ledger — O+V's `/context`

`memory_admission.py`, modelled on `context_manifest.CompactionManifest`
(same row/record/registry shape, same structured-reason discipline) rather
than as a second pattern. Not `CompactionManifest` itself: that records
scored dialogue chunks keyed by sequence position, this records documents
keyed by content hash carrying a corpus provenance and a drift reading it
has no field for.

**Rows are written for topics that did NOT get in, and that is most of the
value.** "These three loaded" is inferable from the prompt; "this one lost
to budget by 200 characters" and "this one was withheld as orphaned" are the
facts that explain a bad generation and exist nowhere else. The row cap drops
withheld rows only — never an admission.

Every record names its `MemoryConsumer` (main / explore / review / plan /
general / operator / unknown). **Finding: no subagent routes memory today** —
only `context_expansion_runner` and the inline orchestrator path call
`route()`. So O+V subagents inherit no architecture memory at all. The field
is honest infrastructure for when they do; it is not yet a wired policy.

`/memory context [-v]` renders it. `/memory`'s routing row now reports the
last OBSERVED pass instead of a flag reading — "on · available" was true the
entire time ghosts were being injected.

## Flags

`JARVIS_MEMORY_CORPUS_AUTHORITY` · `JARVIS_MEMORY_STALENESS_ENABLED` ·
`JARVIS_MEMORY_DRIFT_PENALTY` · `JARVIS_MEMORY_STALENESS_SCAN_COMMITS` ·
`JARVIS_MEMORY_CORPUS_TTL_S` · `JARVIS_MEMORY_CORPUS_GIT_TIMEOUT_S` ·
`JARVIS_MEMORY_TOPIC_MAX_BYTES` · `JARVIS_MEMORY_ADMISSION_ENABLED` ·
`JARVIS_MEMORY_ADMISSION_MAX_OPS` · `JARVIS_MEMORY_ADMISSION_MAX_ROWS`.
All default-TRUE / bounded; authority-OFF restores the legacy walk exactly,
ghosts included.

## Live measurement

`corpus 383 [git_tracked] · 381 untracked excluded · admitted 3/383
considered · 1500/2000 chars (75%)`. Drift census over the ranked head:
54 drifted, 8 fresh, 1 orphaned, 1 unbound.

## Slice 1 — the outcome-feedback loop (`memory_utility.py`)

Selection was open-loop. A topic in forty verified ops and one in twelve
failed ops ranked identically forever. `content_hash` is the join key — a
topic that MOVES keeps its history; a topic that is EDITED starts neutral,
because the evidence was about the old text.

Reuses rather than redeclares: polarity (`_outcome_polarity_weight`) and the
exponential half-life (`action_outcome_recency_halflife_days`) both come from
`action_outcome_memory`; near-duplicate detection reads the `content_hash`-keyed
embedding cache `module_routing` already persists.

**Neutral is the decayed CORPUS MEAN, not a constant.** A fixed midpoint would
drift the whole corpus up or down together during a good or bad week, so every
topic would be re-ranked by the weather rather than its own contribution.
Measuring against the corpus cancels that exactly and needs no magic number.

Refusals are the load-bearing part:
- cold start → 1.0, never negative (same invariant as `Drift.UNKNOWN`)
- a zero-total VERIFY proves nothing and is not credited as a pass
- `scoped_to_applied_op=False` (repo-wide health) is NOT this op's result
- confidence saturates `1 - exp(-mass/scale)`, so one coincidence moves a
  topic ~4% and a sustained pattern moves it more — the false-attribution
  guard, since an op usually fails for reasons unrelated to its three topics
- near-duplicate propagation is NEGATIVE-only and similarity-scaled: demoting
  a topic without its twin leaves the router a free fallback into the same
  failure, while spreading praise would rank a redundant corpus above a
  concise one

**`ops_digest_observer` grew a fan-out.** The registry held ONE slot, owned by
the harness's `SessionRecorder`, so a second consumer had to displace it or
duplicate the call at every emit site — both the same defect. `register_*`
keeps its meaning; `add_ops_digest_listener` is additive; `get_*` returns the
primary OBJECT ITSELF when no listener exists, so every existing identity
assertion still holds. A raising listener is isolated.

The listener arms at the first real `route()` — not at import or boot, because
the subscription is only meaningful once memory is actually being injected and
this is the one path that proves it. Verb: `/memory utility`.

Flags: `JARVIS_MEMORY_UTILITY_ENABLED` · `_GAIN` (0.5) · `_EVIDENCE_SCALE`
(3.0, a scale not a threshold — nothing switches when crossed) · `_MIN`/`_MAX`
clamp · `_MAX_OBS` · `JARVIS_MEMORY_NEAR_DUP_COSINE` (0.97) ·
`JARVIS_MEMORY_NEAR_DUP_PROPAGATION`.

E2E through the real telemetry seam: route → 3 topics at 1.0 → listener armed
→ `on_verify_completed(0/6)` → 3 observations → a second passing op sets the
corpus mean to 0.75 → the failed op's topics demote to ×0.96 at confidence
0.28. 31 tests.

## Slice 2 — subagent memory scoping (`memory_scope.py`)

The defect was NOT "subagents lack memory". It was a boundary whose crossing
rule existed only as the ABSENCE of code — answered by omission, at four call
sites, unreadable afterwards.

**Reach, stated up front.** Three of the four subagents are deterministic:
EXPLORE greps, PLAN partitions (`llm_planner` never wired), REVIEW scores
(`provider_used="deterministic"`). Only GENERAL drives a model
(`run_general_tool_loop` via a wired `llm_driver`) — and its policy is NONE.
So the rendered SECTION has exactly one potential consumer and is deliberately
denied to it; the POLICY and its audit trail are live now. Injecting a section
into three subagents that cannot read one would have been the wired-but-inert
trap in its purest form.

Per-type defaults, each argued from epistemic role, not tuned:
- **EXPLORE = NONE** — its value is INDEPENDENT evidence. The parent's memory
  is the hypothesis; EXPLORE is the test. Feeding the hypothesis to the test
  is how a search finds what it was told to expect.
- **REVIEW = COMPLEMENT** — route fresh, then EXCLUDE what the parent was
  shown. A reviewer handed the author's topics inherits the author's blind
  spot and cannot catch a mistake the memory itself caused. **This scope does
  not exist in Claude Code** — it requires knowing what the parent actually
  saw, which is what the admission ledger records.
- **PLAN = INHERIT** — shares the parent's goal rather than checking it;
  reusing the rendered section costs nothing and cannot disagree with it.
- **GENERAL = NONE** — Semantic Firewall, most injection-vulnerable surface;
  memory is attack surface with no bearing on its mechanical tasks.

**GENERAL cannot be widened by an env var.** `_FORBIDDEN` refuses
inherit/independent/complement with a logged reason. A policy a deployment can
widen by setting a string routes around the firewall's reasoning without ever
touching the firewall. Narrowing is always allowed — the refusal is
directional.

`SCOPE_EXCLUDED` is its own admission reason: "deliberately not shown" and
"ranked low" are different facts about the same absence, and folding them
together erases the only evidence a policy acted.

Every dispatch files an admission record under its own consumer — **including
the denied ones**, where the record is the only evidence a boundary decision
was made rather than forgotten. Applied at `_apply_memory_scope`, called on
BOTH dispatch paths (parallel legs concurrently via `asyncio.gather`), kept
OUT of `_build_sub_context` so the builder stays a pure sync constructor.

**Enforcement, not just declaration.** An AST invariant asserts no executor
reads `strategic_memory_prompt` / `human_instructions` off `parent_ctx`
(attribute walk + `getattr` string form). AST rather than grep because these
files are mostly prose and a substring match would fail on a docstring
forever. A second test proves the detector detects, and a third pins
`SubagentContext(` to exactly one construction site — a boundary enforced at
some construction sites is one that does not exist.

Live: parent admits 3 topics → explore 0c, plan 1931c inherited, review 1929c
with 3 parent topics excluded, general 0c. Verb `/memory scope`. 29 tests;
452 green across all subagent suites.

## Slice 3 — path-scoped operator rules (`operator_rules.py`)

**The third integration point was fiction.** `user_preference_memory`'s
docstring describes `StrategicDirectionService` accepting a `user_prefs` param
and appending a "User Preferences" section "filtered by relevance to the op"
scored by "path overlap + tag match + type weight".
`StrategicDirectionService.__init__` takes `project_root` ALONE, `user_prefs`
appears nowhere in that module, and no relevance function was ever written.
(#2 FORBIDDEN_PATH → `tool_executor` and #3 rejection-learning → orchestrator
are both live; verified.)

So operator rules could BLOCK a write and be LEARNED from a rejection, but
could never GUIDE a generation. One layer further out than the rest of this
arc: not a value dropped before the eye, but a value the OPERATOR supplied
that never reached the model at all.

**Glob matching is path-aware, because `fnmatch` is not.** `fnmatch`'s `*`
matches `/`, so `*.md` matched `docs/README.md` — an operator scoping a rule
to top-level markdown would silently have it fire on every nested file. The
over-matching twin of the substring problem and just as invisible.
`_glob_re` translates explicitly: `**` spans directories, `*` stops at a
separator, `?` is one non-separator, `[!...]`→`[^...]`, everything else
escaped. Directory-prefix shorthand (`backend/voice`) is honoured before the
glob path because operators write directories far more often than `/**`.
Case-SENSITIVE because git is.

**The widening invariant.** `UserMemory.matches_path` is a SECURITY path —
FORBIDDEN_PATH consults it before every mutating tool call. Glob support is a
UNION with the legacy substring test, never a replacement. Replacing it would
have looked cleaner and quietly UNPROTECTED every entry written in the old
style: `backend/voice` stops matching `backend/voice/x.py` under pure glob.
Widen a guard; never narrow one as a side effect of improving it.

Scoring: `0.6·specificity + 0.25·coverage + 0.15·tag_overlap`, times a
per-type weight. Specificity is derived from the pattern's own shape
(saturating `1-0.5^literal_segments`), so it needs no importance table against
a tree that moves weekly. Unscoped rules are GLOBAL and eligible (CC
semantics) but outranked by any scoped match — the budget goes to the rule
that knows something about THIS op.

Edge cases: negation (`!backend/vendor/**`); absolute target files
relativised; zero-target ops get global rules only; ORPHANED rules (paths
matching nothing in the repo) are withheld, reusing `Drift.ORPHANED`'s idea —
and the repo probe FAILS OPEN so an unreadable tree cannot discard a live
rule.

Rules ride the SAME admission ledger as topics (`corpus_provenance:
"operator_rules"`), so `/memory context` answers "what was in that prompt"
once rather than twice. `compose_for_op` is the single entry point and records
as a side effect — a caller cannot inject rules without the injection being
observable. Wired on BOTH CONTEXT_EXPANSION paths, pinned by a test.

Live: a governance op gets `async-first` + the global rule and NOT the voice
rule; a voice op gets `voice-latency` FIRST (deeper scope), then `async-first`,
then the global. Verb `/memory rules`. 39 tests; 194 green across
preference/forbidden/protected/tool_executor/context_expansion suites.

## Slice 4 — PROACTIVE memory (`memory_hygiene_sensor.py`)

Everything above is PULL: `route()` answers at CONTEXT_EXPANSION,
`compose_for_op()` at the same seam, utility read at the next pull. Memory
only spoke when spoken to — right for a reactive assistant, wrong as the
PRIMARY shape for an organism that self-initiates.

In this codebase proactive has a precise meaning: **being a SIGNAL SOURCE**,
emitting `IntentSignal` envelopes into `UnifiedIntakeRouter` so the governed
loop schedules work nobody asked for. A background thread recomputing a score
is not proactive; it is a cache warmer. So memory became the 18th sensor. The
reactive path is untouched and stays SECONDARY.

Five finding kinds, each possible ONLY because the earlier slices produced the
evidence: `drifted` / `orphaned` (from `memory_corpus`), `unreachable` (the
admission ledger watched a topic lose N times and never win — before the
ledger there was no way to tell that from "nobody needed it yet"), `suspect`
(**`memory_utility` says this topic correlates with FAILED ops — memory
reporting its own suspected falsity**, which CC structurally cannot produce),
`uncovered`.

**Event-primary** per Gap #4: `fs.changed.*` filtered to `docs/memory_topics`,
plus a NEW registry-wide listener on `AdmissionRegistry` — a routing pass IS
the event that changes what "unreachable" means. Per-op ledgers are created
lazily, so a subscriber wanting "every pass" had nothing to attach to until
the registry grew `add_listener`. Polling is the 1h fallback, not the hot path.

**Bounds, because the flood was real.** Live scan: **234 findings** (232
drifted, 2 orphaned). Uncapped that is 234 chore-ops on first boot. Debounce
the FS burst, cap 3/scan, rank orphaned>suspect>drifted>unreachable>uncovered,
dedup on `(kind, content_hash)` — payload-keyed so a repair SELF-CLEARS and
the repaired text is re-judged on its merits (a path key would suppress the
finding forever after one failed repair). A rejected envelope is not marked
seen.

**Cost**: registered in all THREE places — `_VALID_SOURCES` (else every
envelope is dropped), `SignalSource` (else typed consumers can't classify),
`_BACKGROUND_SOURCES` (else 15x on Claude). Envelopes carry `urgency="low"`.
The whitelist's own comment records the last miss: $0.53 burned on doc scans.

**Defect found + fixed**: `add_listener` idempotency used `is`, but
`obj.method` builds a NEW object per access — the sensor subscribes with
`self._on_admission`, so a re-subscribe would double-count the very evidence
`unreachable` is counted from. `_same_callable` compares the
(instance, function) pair.

Default **OFF** (`JARVIS_MEMORY_HYGIENE_SENSOR_ENABLED`) — a sensor that
enqueues autonomous work earns default-on via a soak, not by being new.
20 tests; 179 green across the memory subsystem.

## Ghost reconciliation (2026-07-30)

`scripts/reconcile_ghost_topics.py`. 381 ghosts: 80 IDENTICAL, 244
STALE_SUBSET, 57 DIVERGED (module unions only), 0 ORPHANED, 0 CONFLICT.
57 files changed, one `modules:` line each, **zero body changes**. On disk ==
tracked == 384; zero ghost dirs.

The DIVERGED bucket nearly did damage: its first cut would have re-added
`modules:` entries a later enrichment pass had PRUNED. An entry is reclaimable
only if it resolves to a regular file — the structural test for "could this
ever be a routing signal", since `_structural_score` matches on path-tail and
a directory entry can never match while its tail (`voice`, `api`) CAN collide
spuriously. The 27 excluded entries are NAMED, not counted.

## Still open

- **Index budget.** CC hard-errors past 200 lines / 25 KB. `INDEX.md` is
  406 lines / 62 KB — and has **no programmatic reader** in this repo (only
  `scripts/migrate_memory_topics.py` writes it). Building a guard for it
  would be a guard on a file nothing reads. The budget that actually binds
  is the PROMPT budget, and that is now measured per-op in the admission
  record (`char_budget`, `budget_used_fraction`, `BUDGET_EXHAUSTED`).
- **Path-scoped operator rules** (CC's `.claude/rules/` with `paths:`).
  O+V has the stronger mechanism — AST module binding — but only for topics,
  not for operator-written rules.
- **Outcome feedback.** Selection is still open-loop: a topic injected into
  40 successful ops and one injected into 12 that failed VALIDATE rank
  identically forever. `action_outcome_memory` / `failure_mode_memory` hold
  the signal; closing that loop is the RSI move CC structurally cannot make.
- **`agent_memory.py` is INERT** — `governed_loop_service.py:6798` assigns
  `self._agent_memory_factory = get_agent_memory` and nothing ever calls it.
  20 KB, zero live consumers. (`context_memory_loader` was checked and IS
  live, on the default op path.)
- Not live-fired in a running cockpit; proven at the composition layer plus
  a real-corpus run.
