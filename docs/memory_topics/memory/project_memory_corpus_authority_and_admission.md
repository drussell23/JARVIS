---
title: Memory corpus authority + admission ledger
modules: [backend/core/ouroboros/governance/memory_corpus.py, backend/core/ouroboros/governance/memory_admission.py, backend/core/ouroboros/governance/module_routing.py, backend/core/ouroboros/battle_test/memory_surface.py, backend/core/ouroboros/governance/phase_runners/context_expansion_runner.py]
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
