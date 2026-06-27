---
title: P0.5 — Cross-session direction memory (offline scope draft)
modules: [backend/core/ouroboros/governance/git_momentum.py, tests/governance/test_git_momentum.py, tests/governance/test_direction_inferrer_arc_context.py]
status: merged
source: draft_p0_5_scope.md
---

# P0.5 — Cross-session direction memory (offline scope draft)

**Status**: scope draft only. Awaiting operator "P0.5 go" before any PR opens.
**Predecessors**: P0 graduated 2026-04-26 (PR #21471 pending merge at draft time).
**PRD reference**: §9 Phase 1 entry "P0.5 — Cross-session direction memory" (line 440).

> ⚠️ **PRD §1 typo to fix in this slice or earlier**: §1 status row currently labels P0.5 as "POSTMORTEM root-cause taxonomy expansion". The §9 truth is "Cross-session direction memory". Either bundle the §1 fix into this slice's PR, or land a tiny pre-slice doc-only PR.

## Problem (from PRD §9)

`DirectionInferrer` reads current-session signals only (12 ambient signals, all in-process). The operator's actual long-arc direction — visible via `LastSessionSummary` v1.1a tokens AND in the last 100 commits' subject/scope distribution — is not fed back. Posture decisions are point-in-time, not arc-aware.

## Solution shape

Extend `DirectionInferrer` with two new signal sources, both authority-free + read-only:

1. **LSS arc signal** — read `get_default_summary().format_for_prompt()` at posture evaluation time. Extract the dense tokens (`apply=MODE/N`, `verify=P/T`, `commit=HASH`) plus session count and chars_out as inputs to a new "recent_outcome" signal channel.

2. **100-commit momentum signal** — `git log --oneline -100 --pretty=format:"%H|%s"` parsed for scope/type histograms (already emitted by `StrategicDirection._infer_recent_momentum` per CLAUDE.md). Reuse that helper if exposed; otherwise lift into a thin pure module so both `StrategicDirection` and `DirectionInferrer` can consume it.

Output: posture decisions log BOTH the immediate signal stack AND the new arc context. Operator visibility via new `/posture explain` REPL command.

## Acceptance criteria (from PRD)

- [ ] `DirectionInferrer` reads LSS + recent commit history at evaluation time
- [ ] Posture decisions logged with both immediate signals AND arc context
- [ ] `/posture explain` REPL command shows the arc reasoning

## Effort + size

PRD: ~200 LOC + 12 tests. Realistic actual based on similar Wave 1 #1 + LSS work: 250–400 LOC + 18–25 tests once you account for the extracted helper module + REPL command + observability surface.

## Slice plan (3 slices, mirrors W3(7) cancel-token + W2(4) curiosity arc shape)

### Slice 1 — extracted momentum helper (no DirectionInferrer change)

* New module `backend/core/ouroboros/governance/git_momentum.py`. Pure function `compute_recent_momentum(repo_root, n=100) -> MomentumSnapshot`. Returns dataclass with `commit_count`, `scope_histogram`, `type_histogram`, `latest_subjects`, `wall_seconds_span`. **Authority-free**: zero imports of orchestrator/policy/iron_gate/etc.
* `StrategicDirection._infer_recent_momentum` is partially refactored to import + delegate to this (no behavior change for that consumer).
* New tests `tests/governance/test_git_momentum.py` — fixture repos, edge cases (0 commits, malformed subjects, very long subjects, non-conventional commits). ~10 tests.
* No env knob. No new authority surface. Pure mechanical extraction.

### Slice 2 — DirectionInferrer arc consumer (default-off)

* `DirectionInferrer` adds a new method `_consider_arc_context(snapshot, lss_one_liner)` that takes the Slice 1 momentum + LSS digest and returns an `ArcContextSignal` dataclass. The method is called from the existing posture-evaluation function but the signal is **observation-only by default** — does not influence posture vote unless `JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED=true`.
* Posture decision log line gains a new `arc_context=...` field always populated (cheap, observability), but `arc_weight=0.0` unless flag on.
* Tests `tests/governance/test_direction_inferrer_arc_context.py` — ~8 tests covering observation-only mode, weighted mode, signal absence (clean repo), signal saturation (heavy momentum).

### Slice 3 — REPL `/posture explain` + graduation

* New REPL subcommand `/posture explain` renders the most recent posture decision as a formatted block: immediate signals (existing) → arc context (new) → final posture. ~80 LOC handler + 5 tests.
* Graduation: flip `JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED` default `false` → `true`. Pre-graduation pin renamed per the embedded instruction pattern. Source-grep pin updated. PRD §1 row marked `[x]` (and the "POSTMORTEM root-cause taxonomy expansion" mislabel finally fixed).
* Hot-revert: single env knob.
* Re-runs of the existing `tests/governance/test_direction_inferrer*.py` regression suite must stay green with both flag states.

## Authority invariants (all slices)

* Read-only on git + LSS + posture history. Never mutates code, env, or .ouroboros/.
* `git_momentum.py` AST-pinned to NOT import: `orchestrator`, `policy`, `iron_gate`, `risk_tier`, `change_engine`, `candidate_generator`, `gate`, `semantic_guardian`.
* DirectionInferrer arc-context branch wrapped in try/except → DEBUG breadcrumb on failure → posture evaluation never blocks.
* Graduation pin: per-session arc reads bounded (1 momentum compute + 1 LSS read per posture evaluation, no fan-out).

## Operator-visible risk profile

* **Default-off behavior in Slices 1+2** — flag flip only at Slice 3 graduation, after the same evidence pattern P0 used (deterministic regression + smoke + AST pins + reachability supplement if live cadence flakes).
* **No new authority surface** — arc context is one more input to the existing posture vote; vote arithmetic itself is unchanged.
* **Hot-revert at every layer**: per-flag env knob, source-grep pinned, restoration is single-commit.

## Why this is the right next slice (per PRD §9 ordering)

P0 (just-graduated) gives the model "memory of past failures." P0.5 gives the model "memory of past direction." Together they're the two halves of cross-session learning. P1 (Curiosity v2) needs both — it consumes postmortem clusters AND posture history to propose self-formed goals. So P0.5 is on the critical path for everything in Phase 2.

## What this slice does NOT do (out of scope)

* Does NOT touch sensor governor, sensor caps, or intake routing.
* Does NOT add new posture values (stays at EXPLORE/CONSOLIDATE/HARDEN/MAINTAIN).
* Does NOT add new flag categories to FlagRegistry beyond the one new env knob.
* Does NOT modify SemanticIndex (that's Phase C work).
* Does NOT add LLM calls in DirectionInferrer (still §5 Tier 0 deterministic).

## Open questions for operator at "P0.5 go"

1. Should the arc context appear in EVERY posture decision log line (small constant cost) or only when posture CHANGES (less noise but harder to audit)?
2. Should `/posture explain` show only the latest decision, or accept a count arg (`/posture explain --last 5`)?
3. PRD §1 typo fix — bundle into Slice 1, or pre-emptive doc-only PR?
4. 100-commit window — fixed at 100, or env-tunable (`JARVIS_GIT_MOMENTUM_WINDOW`, default 100)?

---

**This file is offline scope only.** No code, no commits, no PR. Awaits operator "P0.5 go".
