---
title: Project Followup Seed Exploration Arc
modules: [tests/fixtures/wave3_forced_reach_seed.json, backend/core/ouroboros/governance/plan_exploit.py, tests/governance/test_plan_exploit_ledger_merge.py, backend/core/ouroboros/governance/candidate_generator.py]
status: merged
source: project_followup_seed_exploration_arc.md
---

## Status

- **OPEN.** Operator parked from Slice 5b closure 2026-04-24.
- **No live battle-test sessions authorized.** Next deliverable is a design doc + offline analysis. Subsequent live sessions only after operator scopes the arc explicitly.

## Problem statement

The forced-reachability seed has been the test fixture across F1 Slice 4 / W3(6) Slice 5b graduation S1→S8. Across S3, S6, S7, and S8 (the sessions that reached GENERATE), the seed produced a Claude completion via §3 PLAN-EXPLOIT (3 concurrent streams, 8–9 merged files) that was rejected by the Iron Gate exploration ledger:

```
exploration_insufficient: 0/2 exploration tool calls (expected >= 2).
You MUST call read_file/search_code/get_callers at least 2 times BEFORE
proposing any patch. Use the tool loop to read the target file and grep
for callers, then return your patch.
```

The Iron Gate is doing exactly what it's designed to do (Manifesto §6 — the gate must hold). The seed's prompt/fixture isn't structured to make the model reliably call exploration tools before generating the patch.

## What we know

- **Seed prompt** (`tests/fixtures/wave3_forced_reach_seed.json`): 1.6KB description block whose payload is the multi-context drift narrative ("REFRAMED TWICE", S1 + S2 + F5 lessons). The actual *task* is a docstring expansion across 3 files; the *task statement* is buried in the second-to-last sentence after a 3-paragraph narrative.
- **Three target files**: `roadmap/hypothesis_envelope_factory.py`, `exploration_envelope_factory.py`, `governance/context_memory_loader.py`. All exist on main with module-level docstrings.
- **PLAN-EXPLOIT runs successfully** (3 concurrent streams, ~$0.40–$0.55, 60–119s wall, 8–9 merged files). The model produces *patches* — it just doesn't *explore first*.
- **Exploration ledger threshold**: 2 tool calls minimum from a 5-category set (`read_file`/`search_code`/`get_callers`/`list_symbols`/`glob_files`). The model emitted 0 in S6/S7/S8.
- **PLAN-SHADOW DAG built**: `dag_units=3 edges=0 roots=3 parallel_pairs=3 validation_valid=True` — so the multi-file shape is recognized.

## Hypotheses (rank-ordered, none verified)

1. **Prompt narrative buries the task** — the 1.6KB "REFRAMED TWICE" backstory is being treated as goal context, not as instruction. Model interprets the actual ask (docstring expansion) as a small enough delta that pre-exploration "isn't needed". Test: shorten the seed task statement to the imperative core (<300 chars), preserve the file targets.
2. **Concurrent stream split skips exploration unanimously** — §3 PLAN-EXPLOIT spawns 3 independent streams, each with a per-file scope. The Iron Gate counts exploration across the *aggregated* op, but each stream sees only one file and never feels the need to grep for callers. Test: count tool calls per-stream in PLAN-EXPLOIT; hypothesis predicts 0 across all 3.
3. **No exploration-required signal in the seed** — the docstring-only task genuinely doesn't need to grep callers (it's a no-behavior docstring). Iron Gate's `>=2` threshold is a *blanket* requirement; it doesn't differentiate "writes that affect callers" vs "docstring-only no-behavior". Test: build a seed where the task IS exploration-required (e.g., "rename function `foo` to `bar` across these 3 files") and verify the gate is still strict but the model engages tools naturally.
4. **Pre-loaded exploration credit fixture** — fixture could pre-stage 2+ synthetic `read_file` records via `_PreloadedExplorationRecord` (already supported per W2(5) Slice 5b memory). Test: add records to the fixture; verify they count toward the ledger; rerun S9.

## Hypothesis tests (offline, no live battle burn)

Each test produces a static artifact (log analysis, fixture variant, or unit test) BEFORE any live session is authorized.

### Test A — log audit on existing sessions
Walk S6/S7/S8 debug logs and count Venom tool calls per round attributed to the seed op. Verify hypothesis 2's prediction (0 across all PLAN-EXPLOIT streams). Estimated cost: 30 min reading existing logs; no new session.

### Test B — minimal seed prompt variant
Author a second fixture `wave3_forced_reach_seed_minimal.json` with the same 3 file targets but the task statement reduced to <300 chars imperative form. Side-by-side against current seed in a unit-test harness that calls `_ExplorationLedger.evaluate(prompt, response)` with hand-built model responses. No live session.

### Test C — exploration-required seed variant
Author a third fixture where the task is genuinely exploration-required (e.g., a cross-file rename or a refactor that needs caller graph). Side-by-side as Test B. Predicts: even with current model behavior, the task itself forces exploration tools.

### Test D — pre-loaded exploration credit
Extend the existing fixture with `_PreloadedExplorationRecord` entries for each target file. Unit-test that the ledger accepts these as credit toward the `>=2` threshold. No live session.

## Acceptance criteria for the arc to close

- One or more of A/B/C/D produces a *deterministic offline result* (test passes / log audit confirms hypothesis).
- Operator authorizes one or more live sessions to verify the offline result holds in the wild.
- A clean session (seed reaches APPLY in headless via SAFE_AUTO fixture, OR explicitly tagged seed-traversal proof under the existing APPROVAL_REQUIRED flow) is recorded.
- Slice 5b ledger row `seed_slice5b_traversal` flips PASS.

## Out of scope

- Iron Gate exploration ledger softening / threshold adjustment / per-task heuristics. Operator-binding: gate stays strict.
- Multi-file generation softening. §3 Disciplined Concurrency is working as intended; PLAN-EXPLOIT is graduated.
- The harness epic items (SIGTERM/summary, wall_clock_cap, etc.) — those continue parallel under their own ticket.

## Cross-links

- `project_f1_w3_slice5b_s1_s6_checkpoint.md` — Slice 5b closure; this arc spawned from its open `seed_slice5b_traversal` row.
- `feedback_headless_completion_contract.md` — bar redefinition that retired APPLY-required graduation. This arc is the path to APPLY in headless if/when operator wants it.
- `feedback_orchestrator_wiring_invariant_checklist.md` — independent retro from the same arc; not blocking this seed work.
- `tests/fixtures/wave3_forced_reach_seed.json` — current seed under audit.

---

## Test A executed 2026-04-25 — ALL PRIOR HYPOTHESES REFUTED, NEW ROOT CAUSE

Operator-authorized 2026-04-25 ("let's work on the next thing on our list"). Test A = log audit on existing sessions S6/S7/S8 (the 3 sessions that reached PLAN-EXPLOIT). Pure offline analysis, no live battle burn.

### Method

For each session, find the seed op_id from the INTENT log line, then count `tool_round_complete` entries attributed to that op (using the truncated `op-XXXXXXXX-` log format), aggregate the tool name distribution.

### Result — counts of ACTUAL exploration tool calls per seed op

| Session | Seed op | Tool rounds | Total exploration calls | Distribution |
|---|---|---|---|---|
| S6 (`bt-2026-04-24-225137`) | `op-019dc1b1-4baa` | 3 | **20** | 14× read_file, 6× glob_files |
| S7 (`bt-2026-04-25-001939`) | `op-019dc201-d385` | 2 | **11** | 9× read_file, 2× glob_files |
| S8 (`bt-2026-04-25-003533`) | `op-019dc210-5fe3` | 5 | **21** | 17× read_file, 3× glob_files, 1× search_code |

### What Iron Gate said vs what actually happened

S7 timeline (representative — same pattern in S8):

```
17:22:01  ToolLoop tool_round_complete op=op-019dc201- tools=6 (6× read_file)
17:22:02  ToolLoop tool_round_complete op=op-019dc201- tools=5 (3× read_file, 2× glob_files)
17:23:55  PLAN-EXPLOIT status=completed dag_units=3 concurrent_streams=3 merged_files=9
17:23:55  Iron Gate WARNING: exploration_insufficient: 0/2 (attempt=1 cumulative, preloaded=0) for op=op-019dc201-
```

The model made **11 exploration tool calls** (well above the 2-call minimum), the streams produced 9 merged files, and Iron Gate STILL rejected with `0/2 cumulative, preloaded=0`.

### Hypothesis status — all four refuted; new root cause identified

| Original hypothesis (A→D) | Test A verdict |
|---|---|
| (1) Prompt narrative buries the task | **REFUTED** — model is calling exploration tools (11–21 per session) |
| (2) Concurrent stream split skips exploration unanimously | **REFUTED** — tool calls ARE happening; the Iron Gate just doesn't see them |
| (3) No exploration-required signal in seed | **REFUTED** — model invokes exploration tools voluntarily |
| (4) Pre-loaded credit fixture | Possible workaround but doesn't address root cause |

### NEW root cause (Iron Gate ↔ PLAN-EXPLOIT integration bug)

**The exploration ledger doesn't aggregate child-stream credit to the parent op under PLAN-EXPLOIT's §3 Disciplined Concurrency mode.**

The seed op `op-019dc201-d385` had 11 tool calls logged against it (per `tool_round_complete op=op-019dc201-`), but Iron Gate's evaluation reads `cumulative=0 preloaded=0`. The credit is being made — just not into the bucket that Iron Gate inspects.

Likely mechanism (to verify in Test B):
- PLAN-EXPLOIT spawns N child streams via `_generate_unit` (or similar). Each stream may run its tool loop in a child asyncio task with its own exploration-ledger contextvar.
- The parent op's exploration ledger is a separate instance — it sees `cumulative=0`.
- The merge step (`_merge_results`) merges *candidate files* but not the per-stream exploration records.

This is **structurally the same class of bug** as W3(6) Slice 4 (the `_subagent_scheduler` attribute mismatch fixed in `d378dea968`). Both are integration gaps between a feature (PLAN-EXPLOIT / parallel_dispatch) and the surrounding governance machinery (Iron Gate / dispatcher cancel-check). The wiring invariant checklist
(`feedback_orchestrator_wiring_invariant_checklist.md`) applies here too — every new sub-system that produces evidence the orchestrator consumes (exploration credit, in this case) must be checked end-to-end via cross-component tests.

### Implication for the seed_slice5b_traversal ledger row

The seed never had a chance to traverse Iron Gate **structurally** under PLAN-EXPLOIT mode. This is not a fixture problem; it's an integration bug. Hypothesis (A) Test results were always going to look like "exploration_insufficient" regardless of what the seed prompt said, because the credit-aggregation gap is upstream of the seed's behavior.

### Recommended next slice (operator decision)

Two paths, in increasing scope:

1. **Quick offline test (Test B-prime — narrow)**: write a unit test that constructs an Iron Gate exploration ledger, threads it through a mock `PLAN-EXPLOIT.execute` call that records tool usage in N child streams, and asserts the parent ledger sees the aggregated credit. Confirms the bug at the unit level. ~30 min, no live session.

2. **Code fix (Test B-prime + integration patch)**: add a "merge exploration ledger" step in `plan_exploit._merge_results` (or whatever the canonical merge point is). Each child stream returns its tool-records along with its candidates; merge into the parent ledger. Probably ~30 LOC + 5 unit tests. After this lands, the seed will traverse Iron Gate cleanly without any prompt/fixture changes.

3. **Live-fire validation (after operator authorization + B-prime + fix)**: re-run the seed with master defaults; confirm `exploration_insufficient` no longer fires. This is the seed_slice5b_traversal ledger flip.

My read: Test A produced enough evidence to skip Tests B/C/D (which were prompt-engineering hypotheses) and go straight to fix-the-bug (path 2). The cost is comparable but the outcome is structural rather than fixture-specific.

---

## Path 2 EXECUTED 2026-04-25 — fix landed in `_merge_results`

Operator authorization (verbatim): "Pick: Path 2 (fix + tests) now; fold Path 1 into that PR as failing regression tests pre-fix. Requirements: document merge semantics (additive rules, dedupe, single-writer), no gate softening, cross-component hook test per wiring checklist."

### What shipped

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/plan_exploit.py` | `_merge_results` now aggregates `tool_execution_records` + `prompt_preloaded_files` from every per-unit `GenerationResult`. Docstring documents the 4 contract rules: additive / dedupe-deferred / single-writer / no-gate-softening. |
| `tests/governance/test_plan_exploit_ledger_merge.py` (NEW) | 9 tests: (A) Path 1 regression — 6 tests fail pre-fix, pass post-fix; (B) merge semantics; (C) cross-component hook test pinning `ExplorationLedger.from_records(merged.tool_execution_records)` sees aggregated calls; (D) no-gate-softening sanity; (E) source-grep pin against drift. |

### Pre-fix → post-fix delta

```
Pre-fix:  6 failed, 3 passed
Post-fix: 9 passed
```

The 6 originally-failing tests are the regression tests asserting the Iron Gate ledger consumer sees credit. They failed pre-fix exactly as Test A predicted (`merged.tool_execution_records == ()` even when children had records).

### Merge semantics (per operator requirement)

- **Additive**: parent's `tool_execution_records` is the **concatenation** of all child streams' records. Same for `prompt_preloaded_files`.
- **Dedupe deferred**: merge step does NOT pre-deduplicate by `(tool_name, arguments_hash)`. Downstream consumers (`ExplorationLedger.diversity_score`) own the dedup policy because pre-deduping at merge would lose call-count signals downstream consumers may want.
- **Single-writer**: only `_merge_results` writes the merged `tool_execution_records` field on the parent generation. Child streams write their own per-stream records during their tool loop; the merge collects, never re-writes.
- **No gate softening**: Iron Gate floor + diversity scoring are unchanged. Pinned by `test_no_gate_softening_iron_gate_scoring_unchanged`.

### Cross-component hook test (per wiring checklist)

`test_iron_gate_ledger_consumes_merged_records` constructs a merged generation from 3 child streams that each had 2 exploration tool calls, calls `ExplorationLedger.from_records(merged.tool_execution_records)` (the orchestrator's exact consumer pattern), and asserts the ledger sees 6 calls + diversity_score > 0. Closes the same class of integration gap as W3(6) Slice 4 (`_subagent_scheduler` attribute mismatch).

### Slice 5b ledger row update

Row label changed from `seed_slice5b_traversal: FAIL` → `seed_slice5b_traversal: blocked-by-integration-bug (identified, fix in PR)`. After Path 3 live-fire confirms the fix, this flips to PASS.

### Path 3 (live-fire) — executed 2026-04-25 — exploration ledger PASS

Operator-authorized: "let's do a Path 3 (live-fire)". Session `bt-2026-04-25-033803` on `24c307a440`.

**Result**: `exploration_insufficient: 0` (any op, any phase). Tool calls aggregated (3 rounds = 17 calls). Iron Gate ledger consumer received credit. **The Path 2 fix is live-proven.**

**New finding**: seed died from a different (pre-existing) class — Claude per-call timeout cap (`_FALLBACK_MAX_TIMEOUT_S=120s`) bit each of 3 PLAN-EXPLOIT child streams independently. Same class as S5/S6. NOT a Path 2 regression.

---

## Path 3 follow-up — PLAN-EXPLOIT per-stream timeout override (executed 2026-04-25)

Operator-authorized: "let's go with Seed exploration arc Path 3 follow-up and resolve that problem".

### Root cause

PLAN-EXPLOIT child streams call `generator.generate(...)` which routes through `CandidateGenerator._call_fallback`. That method's `_max_cap` is the global `_FALLBACK_MAX_TIMEOUT_S=120s` (sized for serial calls with retry rounds). Applied per-stream in parallel mode, this artificially constrains streams doing legitimate full-file generation when the parent has 220s+ remaining.

Live-fire evidence: each of 3 child streams hit `class=A_or_B_timeout err=TimeoutError elapsed=120.14s remaining=99.84s pre_sem_remaining_s=219.98` — meaning the parent had 220s remaining when each call started, but the inner cap took it to 120s.

### What ships

| Module | Change |
|---|---|
| `backend/core/ouroboros/governance/plan_exploit.py` | + `plan_exploit_active_var: ContextVar[bool]` (default False); + `plan_exploit_per_stream_timeout_s()` env reader (default 300s, env `JARVIS_PLAN_EXPLOIT_PER_STREAM_TIMEOUT_S`); set/reset around the gather() in `try_parallel_generate`. |
| `backend/core/ouroboros/governance/candidate_generator.py` | `_call_fallback` reads the contextvar via `.get(False)` (default-safe) and applies the override via `_max_cap = max(_max_cap, _plan_exploit_timeout())` (widen-only — never shrinks an already-larger cap from COMPLEX route or BG/SPEC subagent extension). |

### Contract pinned

- **ContextVar default-False** — outside PLAN-EXPLOIT context, behavior is byte-for-byte unchanged (no regression on serial calls).
- **Override only widens, never shrinks** — `max()` semantics preserve COMPLEX-route + BG/SPEC subagent extension caps if they're already larger.
- **Reset semantics** — contextvar reset after the gather so adjacent paths (legacy serial-generate fallback, sequential calls in same task) don't inherit the longer cap.
- **Env tunable** — operator can set `JARVIS_PLAN_EXPLOIT_PER_STREAM_TIMEOUT_S` to any positive float. `0` effectively disables the override (max(120, 0) = 120 = no change).

### Tests: 12/12 in `test_plan_exploit_per_stream_timeout.py`

- (D) Env tunable + parse defaults + garbage fallback + negative-clamp (4)
- (A+E) ContextVar default-False + set/reset round-trip (2)
- (B+C) Override widens, doesn't shrink (2)
- (F) Cross-component hook source-grep pins (2)
- (G) ContextVar propagates through `asyncio.create_task` (1)
- (H) No-regression sanity outside PLAN-EXPLOIT context (1)

Combined regression: 30/30 across new + Path 2 + Slice 5 cancel propagation.

### Next: a second live-fire to confirm seed APPLY no longer blocked by per-call timeout

After this PR merges, a second live-fire should show:
1. exploration_insufficient: 0 (Path 2 still holds)
2. PLAN-EXPLOIT status=completed (not status=fallback) — because each stream now has 300s instead of 120s
3. Seed reaches APPROVAL_REQUIRED (orange-tier headless block) — that's the seed's natural terminal in headless mode per `feedback_headless_completion_contract.md`.

Operator-binding: live-fire only after explicit "seed live verify go".
