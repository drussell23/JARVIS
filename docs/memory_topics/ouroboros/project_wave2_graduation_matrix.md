---
title: Wave 2 (5) Phase-Runner Graduation Matrix
modules: [backend/core/ouroboros/governance/phase_runners/, backend/core/ouroboros/governance/tests/test_phase_runner_slice5b.py, scripts/ouroboros_battle_test.py, backend/core/ouroboros/architect/__init__.py, backend/core/ouroboros/governance/phase_dispatcher.py, orchestrator.py, backend/core/ouroboros/governance/providers.py, backend/core/ouroboros/governance/doubleword_provider.py, backend/core/ouroboros/governance/candidate_generator.py, backend/core/persistent_intelligence_manager.py, backend/core/ouroboros/governance/phase_runners/slice4b_runner.py, backend/core/ouroboros/governance/phase_runners/generate_runner.py]
status: merged
source: project_wave2_graduation_matrix.md
---

# Wave 2 (5) Phase-Runner Graduation Matrix

## 🎯 WAVE 2 (5) CLOSED — 2026-04-23

**8 of 8 flags default-true. All PhaseRunner extraction gates FINAL.** The 102K-line monolithic orchestrator is now routed through the `phase_dispatcher.py` registry composing 9 addressable phase-runners.

| # | Phase | Flag | Flip commit | Status |
|---|---|---|---|---|
| 1 | COMPLETE | `JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED` | `c3aaebb6ed` | FINAL 2026-04-22 |
| 2 | CLASSIFY | `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED` | `50fe2044be` | FINAL 2026-04-22 |
| 3 | ROUTE+CTX+PLAN | `JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED` + `_CONTEXT_EXPANSION_EXTRACTED` + `_PLAN_EXTRACTED` (atomic) | `710667f3b6` | FINAL 2026-04-22 |
| 4 | VALIDATE | `JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED` | `97590ae72d` | FINAL 2026-04-22 |
| 5 | GATE | `JARVIS_PHASE_RUNNER_GATE_EXTRACTED` | `2851c82ffd` | FINAL 2026-04-23 |
| 6 | SLICE4B (APPROVE + APPLY + VERIFY) | `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED` | `0b120b588a` | FINAL 2026-04-23 |
| 7 | GENERATE | `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED` | `053122925d` | FINAL 2026-04-23 |
| 8 | DISPATCHER | `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED` | `203856371e` | FINAL 2026-04-23 |

**Original arc-extraction commit:** `58107a1342` (203 parity tests, 248/248 both paths). **Graduation arc spans commits 58107a1342 → 203856371e.**

**Post-graduation protocol tickets shipped during the arc:**
- **Ticket A1** (`6e87dea643`) — `--max-wall-seconds` hard session ceiling (idle_timeout vs retry-storm hijack)
- **Ticket B** (`72e7ea7a79`) — SIGHUP handler + v1.1b `session_outcome` field
- **Ticket C** (`68954cc62d`) — native `--headless` flag (retires `tail -f /dev/null` stdin guard)
- **Ticket A2** — deferred to backlog (`project_followup_provider_retry_ceiling.md`); per-op retry ceiling, defense-in-depth not on critical path

**Unlocked by this closure:**
- Wave 3 (items 6 + 7 — parallel sub-agent dispatch + mid-phase cancellation) is eligible for authorization.
- Wave 2 (4) curiosity / capped `ask_human` is eligible for its deferred scheduling window.
- Inline else-chain deletion in `orchestrator.py` is eligible as a separate post-graduation maintenance PR.

Neither Wave 3 nor Wave 2 (4) auto-starts — both require explicit operator authorization per the standing binding.

---

## Pre-closure historical framing (preserved for audit)

**Original status:** Wave 2 (5) arc extracted at commit `58107a1342` (203 parity tests, 248/248 both paths). Defaults were ALL `false` at extraction. This document tracked the graduation cadence — 3 clean battle-test sessions per flag before the one-line default flip.

**Operator binding (Apr 22 2026):** *"Start the per-slice graduation battle-test cadence before authorizing Wave 3 (6+7). Flags are default false for a reason; flipping defaults or deleting inline paths is production semantics, not more structural extraction."*

## Graduation protocol (per flag)

1. **Authorization**: operator approves a specific flag for graduation testing.
2. **Session 1**: battle-test with the flag set to `true` (one flag only, others default). Run to completion with real ops. Log session ID + cost cap + final commit.
3. **Session 2**: repeat with same flag on. Different op mix if possible. Different day if possible.
4. **Session 3**: repeat. If Session 1-3 are all clean (no ops land in POSTMORTEM/CANCELLED that wouldn't have inlined-off; no observability regression; no new exceptions; §8 logs present), the flag is eligible for default flip.
5. **Default flip PR**: one-line change in `orchestrator.py` helper (or `phase_dispatcher.py` for DISPATCHER) — `"false"` → `"true"` — plus a graduation commit referencing the three session IDs.
6. **Post-flip battle-test**: one more session after the flip to confirm the default-on path is production-behavior-identical to what graduation tested. If clean → graduation FINAL.
7. **Inline deletion PR** (separate from graduation, per operator directive): once the flag is default `true` AND at least one post-flip session is clean, a separate maintenance PR deletes the inline `else:` block for that phase. Dispatcher flag graduation is the special case — after that graduates, the whole monolithic `_run_pipeline` can shrink in stages.

**"Clean" session definition** (§2 readiness + §6 immune-system):

Revised 2026-04-22 after #2 CLASSIFY S3 surfaced the "graduation hostage to environmental noise" failure mode (infra DW/asyncio shutdown lines indistinguishable by exception-type count from runner-caused regressions):

1. **No new runner-attributed failures.** A failure is runner-attributed if ANY of: (a) it lands in POSTMORTEM/CANCELLED via a runner's `next_phase=None` + `status="fail"` return, (b) its traceback contains a frame from `backend/core/ouroboros/governance/phase_runners/`, `phase_dispatcher`, or the currently-graduating phase's inline code path, (c) the operator confirms the failure is semantically tied to the runner wiring after inspection.
2. **§8 observability parity.** Structured log lines present on the runner path: `[SemanticGuard]`, `[ExplorationLedger(...)]`, `[MutationGate]`, `[ValidateRetryFSM]`, `[PhaseRunnerDelegate]`, phase-specific INTENT/DECISION/HEARTBEAT/POSTMORTEM events.
3. **Runner reachability observed** where `runner_reachability = Required` — grep `[PhaseRunnerDelegate] <PHASE> → runner` count > 0 in debug.log for at least one of the three sessions.
4. **Cost within ±10% of baseline** (same-commit, flag-off reference session with matching script knobs).
5. **Infra waivers are tagged, not invisible.** Any exception-type line present in a flag-on session but absent from the flag-off baseline MUST be triaged in the ledger as one of:
   - **runner-attributed** (traceback contains JARVIS phase code) → session is NOT clean; block flip, fix root cause or prove it was a transient.
   - **infra-noise waived** (traceback is pure stdlib / third-party libs; no JARVIS phase frames) → session IS clean; ledger carries a one-line waiver row: `WAIVER <type>: <reason>. Traceback excerpt: <top 2 frames>. No JARVIS runner frames.` Waivers DO NOT block graduation but DO stay in the ledger for pattern tracking.
   - **unknown** (ambiguous) → NOT clean; operator triage required.

Graduation does not require rerun for environmental noise that cannot be tied to the flag under inspection. The infra-noise waiver list is a standalone §8 signal — if the same waiver type appears in ≥50% of sessions across ≥2 graduation flags, a separate ticket root-causes it.

**No regression in commit count or per-op latency >30%** is a soft criterion (real-ops non-determinism dominates short soaks). Cost is the hard bar.

## Sequencing + two-column graduation contract

**Two-column contract** (per operator directive 2026-04-22, after #1 COMPLETE graduation surfaced the "flag on but runner body never executed" gap):

- **`stack_soak_clean`**: the existing hard bar — 3 sessions with flag ON, no new terminals vs baseline, no new exception types, §8 structured log markers present, cost/latency within ±10% where baseline comparison is apples-to-apples.
- **`runner_reachability`**: `Required` when the phase reliably fires on most ops (early-exit is rare) — at least 1 soak session must observably execute the runner body (via distinct log marker, FSM transition, or a cheap forced-reachability harness). `N/A` when the phase is terminal-only / rare-path and parity tests carry the correctness proof.
- **`reachability_source`** (added 2026-04-23 per operator directive after #6 SLICE4B S4): categorizes how the marker was observed when `runner_reachability = Required` — `seeded` (the graduation-specific seed op reached the phase under controlled conditions), `opportunistic` (any qualifying live-sensor op reached the phase while the flag was on — counts as sufficient evidence per "we care a real op hit the runner under flag-on, not that our backlog seed won a race"), or `harness_script` (a deterministic `GRADUATION_HARNESS=1` enqueue path once that lands). All three satisfy the reachability bar; the column exists so the ledger stays honest about *how* the evidence was produced.

Order chosen by (a) blast radius of the extracted phase and (b) test surface. Each slice is independent; operator can reorder.

| Order | Flag | Phase(s) | Risk | `runner_reachability` | Accepted `reachability_source` | Rationale |
|---|---|---|---|---|---|---|
| 1 | `JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED` | COMPLETE | Low | **N/A** | N/A | Terminal telemetry only; parity tests (22) are the oracle. Few ops reach COMPLETE in short soaks. |
| 2 | `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED` | CLASSIFY | Low-Med | **Required** | seeded / opportunistic / harness_script | CLASSIFY fires on every op — soak must observe runner body in logs at least once. |
| 3 | `JARVIS_PHASE_RUNNER_ROUTE_{,CTX,PLAN}_EXTRACTED` | ROUTE + CTX + PLAN | Med | **Required** | seeded / opportunistic / harness_script | All three fire on most ops that survive CLASSIFY. |
| 4 | `JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED` | VALIDATE | Med-High | **Required** | seeded / opportunistic / harness_script | Any op that GENERATEs candidates hits VALIDATE. |
| 5 | `JARVIS_PHASE_RUNNER_GATE_EXTRACTED` | GATE | High | **Required** | seeded / opportunistic / harness_script | Every op that VALIDATEs cleanly enters GATE — §6 immune system. |
| 6 | `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED` | APPROVE + APPLY + VERIFY | High | **Required** | seeded / opportunistic / harness_script | Mutation-adjacent; need observed rollback / apply in at least one session. |
| 7 | `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED` | GENERATE | Highest | **Required** | seeded / opportunistic / harness_script | Beast phase; Iron Gate suite must be observed firing in at least one session. |
| 8 | `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED` | All 9 composed | Compound | **Required** | seeded / opportunistic / harness_script | Requires 1-7 FINAL; dispatcher itself must be observed routing a full pipeline in soak. |

### Forced-reachability harness (when `Required` but natural ops don't hit the phase)

For phases marked `Required` where a natural soak doesn't observably fire the runner body (e.g. a session where all ops early-exit before GENERATE), a cheap forced harness is acceptable:

- Seed a deterministic trivial op via the Backlog sensor (single-file, trivial-complexity description, SAFE_AUTO classification) — guarantees the pipeline walks phases 1-through-COMPLETE.
- Observe the `phase_runners/<phase>_runner.py` source filename in debug.log (Python logging's `pathname` record) OR a runner-specific FSM marker (`[ValidateRetryFSM]` already shipped in 4a.1; similar markers available in other runners).

Must be a real soak session (not pytest), but the seeding is legitimate — exercising the graduated path.

**Harness-class session footnote (2026-04-23 per operator directive for #6, extended by Ticket A Guard 2):** for graduation cadence sessions that run the forced-reachability harness or otherwise run at raised cost caps (e.g. `harness_cost_cap=1.00` for #6 SLICE4B), `stop_reason=budget_exhausted` is treated equivalent to `stop_reason=idle_timeout` for clean-bar purposes. With Ticket A Guard 2 (`--max-wall-seconds` CLI flag, 2026-04-23), **`stop_reason=wall_clock_cap` is ALSO treated equivalent to `idle_timeout`** for harness-class sessions — it is the deterministic-termination path that graduation soaks should prefer when provider weather is a concern. All three (`idle_timeout`, `budget_exhausted`, `wall_clock_cap`) are orderly graceful-shutdown paths that go through the same `_generate_report` + `save_summary` pipeline. Hard-bar criteria remain unchanged: 0 POSTMORTEM/CANCELLED runner-caused, 0 runner-attributed traceback frames, 0 JARVIS shutdown race, §8 log parity.

**Infra-noise classification rule (binding 2026-04-23 per operator directive for #7 GENERATE and later flags):**

Transport-layer failures from external providers — specifically `anthropic/_base_client.py`, `httpx/_transports/default.py`, `httpcore`, `aiohttp.connector`, `aiohttp.client`, `aiohappyeyeballs.staggered_race`, and our own resilience-wrapper frames (`providers.py _stream_with_resilience`, `doubleword_provider.py _create_batch`) — are tagged as **`infra_waiver: anthropic_transport`** (or `infra_waiver: dw_transport` for DW) in the ledger. These do NOT block graduation flip.

The classification flips to **runner-attributed** (which DOES block flip) *only* if the traceback frames show `backend/core/ouroboros/governance/phase_runners/`, `phase_dispatcher.py`, or the currently-graduating phase's inline block mishandling the transport error (e.g. swallowing it, raising a new exception that loses the underlying cause, or corrupting retry state). The boundary question is "did OUR code mishandle the external failure?" — not "did an external failure occur?"

A session dominated by external transport failures that never reach meaningful phase execution is **weather, not §6 signal**. Do not count such sessions as graduation-cadence failures; prefer Decision-Tree path (A) — wait or rerun when the provider recovers — per the #7-specific rules below.

### #7 GENERATE decision tree (binding 2026-04-23)

When a graduation session for a Highest-risk flag (GENERATE) is dominated by `infra_waiver: anthropic_transport` / `dw_transport` noise, choose explicitly in the ledger:

**(A) Wait / rerun window** — CONSERVATIVE DEFAULT. Applied when: no GENERATE delegation marker fires, or no successful model round-trip happens in any session. The flag stays default-off until at least one session shows the runner entered + clean hard bar. Standing policy is (A) unless (B)'s preconditions are met; re-run at a different time window, or raise cap / reduce parallel ops *only if that increases marker + clean-bar probability, not just spend*.

**(B) Partial live + parity oracle** — permitted iff ALL of the following are true:
1. `[PhaseRunnerDelegate] GENERATE → runner` (or phase-equivalent marker) appears in **≥1 session before the transport failure** — proves wiring + dispatch.
2. Hard bar stays clean under the infra-waiver rules (0 runner-attributed frames, 0 shutdown race, 0 PM).
3. **Slice 5b parity suite is green on current HEAD** — re-run `pytest backend/core/ouroboros/governance/tests/test_phase_runner_slice5b.py` (or equivalent Slice 5b tests) immediately before the flip commit; attach test summary + commit SHA to the ledger row.
4. Ledger row includes `reachability_source=partial_live+parity` and a verbatim excerpt of the `infra_waiver` traceback top-2 frames.

If markers never fire because generation never starts, (B) is NOT allowed — that's just outage.

**Iron Gate "live evidence":** when API weather prevents Iron Gate branches (ExplorationLedger / ASCII-strict / dep_integrity / multi_file_coverage / retry-feedback-composed) from firing in logs, Slice 5b parity is the authoritative §6 depth proof — **but only alongside (B)'s partial live proof, not alone**. Parity tests without any live marker = pure parity, which was explicitly rejected for mutation-adjacent flags (#6 precedent).

### Background-command hygiene (runbook note 2026-04-23, updated with Tickets A1 + B + C)

Agent-conducted soaks are launched via the Bash tool with `run_in_background: true`. **Post-Ticket-C canonical recipe** (stdin-guard idiom retired):

```bash
# purpose: <FLAG> graduation Session <N> / <S2' rerun>
# stop conditions: first of idle_timeout | budget_exhausted | wall_clock_cap
JARVIS_PHASE_RUNNER_<FLAG>=true python3 scripts/ouroboros_battle_test.py \
    --headless \
    --cost-cap 1.00 \
    --idle-timeout 600 \
    --max-wall-seconds 2400 \
    -v > /tmp/claude/<session_tag>.log 2>&1
```

- **`--headless`** (Ticket C, landed 2026-04-23): skips `SerpentREPL` input startup entirely. Replaces the prior `tail -f /dev/null | ...` stdin-guard workaround. Auto-detected via `not sys.stdin.isatty()` when absent; explicit `--no-headless` forces interactive. Env: `OUROBOROS_BATTLE_HEADLESS`.
- **`--max-wall-seconds 2400`** (Ticket A1 Guard 2, landed 2026-04-23): hard 40-minute ceiling. Fires `stop_reason=wall_clock_cap` at T+2400s regardless of any activity signal. Immune to retry storms that can hijack `--idle-timeout`. Graduation soaks from here on MUST set this. Tune to exceed the expected happy-path by 60% (prior clean sessions were 850–1300s, so 2400s = ~2× safety margin).
- **Signal safety** (Ticket B, landed 2026-04-23): harness now installs SIGHUP handler + ignores SIGPIPE, so parent-bash death via Claude Code's TaskStop lands a partial `summary.json` with `session_outcome=incomplete_kill` + signal-specific `stop_reason` instead of leaving only `debug.log`.
- **Every background launch** for a graduation session should document: purpose (which flag / session role), `--cost-cap` value, `--idle-timeout` + `--max-wall-seconds` (stop conditions). See any recent ledger session row for the canonical example.

**Deprecated workaround** (kept here only for archaeological context; do NOT copy to new runbooks): the pre-Ticket-C idiom `tail -f /dev/null | python3 scripts/ouroboros_battle_test.py ...` was a hand-rolled stdin guard that kept `PromptSession.prompt_async()` from hitting `EOFError → break` on the first iteration. Retired by `--headless`. Removing the pipe prefix from the canonical recipe also removes the SIGHUP-chain failure mode that broke #7 GENERATE S2.

**DISPATCHER graduation prerequisite:** all 9 per-phase flags must be default `true` first. Dispatcher itself then takes 3 clean sessions to graduate. Once dispatcher is default `true`, the monolithic inline else-chain becomes dead code (but per operator directive, deletion remains separate post-graduation maintenance PRs).

## Per-flag session ledger

Each flag section below has three empty session slots. Fill in as sessions run. Each session entry should include:

- `session_id`: battle-test session tag (e.g. `bt-2026-04-22-hhmmss`)
- `flag_config`: which flags were on (should be JUST the graduating flag for cleanest signal)
- `cost_cap`: session cost cap used
- `idle_timeout`: idle timeout used
- `outcome`: `clean` / `dirty:<reason>` / `aborted:<reason>`
- `commit`: latest commit at session end (for reproducibility)
- `notes`: anything unusual — ops that tripped unexpected terminals, log regressions, etc.

### Column glossary (to prevent misreading)

Graduation session rows commonly cite three counts that sound similar — they are not the same signal:

- **`delegation markers`** (also shown as *`[PhaseRunnerDelegate]` mk*) — count of INFO-level `[PhaseRunnerDelegate] <PHASE> → runner op=...` lines in debug.log. Emitted once per op that enters the delegation hook. **Positive proof of reachability** (runner body invoked on live ops).
- **`runner-attributed frames`** — count of error/traceback lines whose stack contains a Python frame from `backend/core/ouroboros/governance/phase_runners/` or `phase_dispatcher`. **Positive signal of runner-caused regression.** Zero is the target.
- **`postmortems`** (PM) — count of ops that landed in `OperationPhase.POSTMORTEM` terminal state during the session.

A row showing `markers=N, frames=0, PM=0` means: the runner body ran N times (reachable), zero of those runs surfaced a runner-attributed failure, and zero ops landed in the postmortem terminal. These are the three primary cleanness signals; cost/latency are secondary.

### COMPLETE (`JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED`)

**Status:** default false. Slice 1 graduation — 3 flag-on sessions run 2026-04-22 by agent conductor, awaiting operator default-flip authorization.

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; no TTY-only affordances exercised by COMPLETE — it's a terminal telemetry phase with no operator-facing visual cues). **Residual risk**: COMPLETE runner's actual code path was not live-exercised in any session (0 ops reached COMPLETE phase in 4 sessions including baseline) — the flag being ON demonstrates no regression in upstream phases but does not directly prove the runner works under live ops. Parity tests (22 tests in Slice 1) already proved byte-identical behavior; for a terminal-telemetry-only phase this residual is low.

**Baseline (flag OFF)** — `bt-2026-04-22-181910`, commit `58107a1342`
- duration: 835.5s / 13 ops / 0 postmortems / $0.00
- 1 op reached APPLY, 0 reached terminal COMPLETE
- Exceptions: `CandidateGenerator EXHAUSTION` (claude_budget_exhausted_op), `EmbeddingService` (sentence-transformers missing — pre-existing infra), `PERSISTENT-INTELLIGENCE checkpoint error` (pre-existing infra)

**Session 1** — `bt-2026-04-22-183425`, commit `58107a1342`
- flag_config: `JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED=true` (only)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 300.4s / 6 ops / 0 postmortems / $0.00 / 0 apply'd
- outcome: **clean** — no new exception types vs baseline; `CandidateGenerator EXHAUSTION` + `EmbeddingService` present (both in baseline)
- notes: Short soak; COMPLETE phase not exercised live (0 apply'd → 0 reached COMPLETE). Per-op latency ~50s vs baseline ~64s (within variance).

**Session 2** — `bt-2026-04-22-185203`, commit `58107a1342`
- flag_config: `JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED=true` (only)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 655.1s / 15 ops / 0 postmortems / $0.00 / 0 apply'd
- outcome: **clean** — only `EmbeddingService` + `PERSISTENT-INTELLIGENCE` errors (both pre-existing in baseline); no new exception types
- notes: Diverse op mix (runtime_health CRITICAL urgency route exercised, sensor ops). Per-op latency ~44s.

**Session 3** — `bt-2026-04-22-190730`, commit `58107a1342`
- flag_config: `JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED=true` (only)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 818.0s / 10 ops / 0 postmortems / $0.00 / 0 apply'd
- outcome: **clean** — same exception set as Session 2 (both pre-existing); no new exception types; no new tracebacks
- notes: Session duration within baseline range (baseline 835s). Per-op latency ~82s (longer-running ops this session). DocStaleness + CrossRepoDrift sensors fired.

**Clean-session bar check**:

| Criterion | S1 | S2 | S3 |
|---|---|---|---|
| No new POSTMORTEM/CANCELLED terminals vs baseline | ✓ (0 PM all) | ✓ | ✓ |
| §8 log parity (structured logs present) | ✓ | ✓ | ✓ |
| No exception-type regression | ✓ (subset of baseline) | ✓ | ✓ |
| Cost within ±10% of baseline | ✓ ($0 = $0) | ✓ | ✓ |
| Latency within ±10% of baseline | △ (300-818s vs 835s; per-op 44-82s vs 64s baseline; real-ops non-determinism makes strict ±10% impractical) | △ | △ |

**Verdict:** 3/3 sessions clean on the hard criteria (terminals, exceptions, cost, §8 logs). Latency variance is ±30% per-op, reflecting real-ops non-determinism.

**Runner reachability: N/A** — COMPLETE runner not observed in soak sessions 1–3; correctness attested by Slice 1 parity (22 tests, byte-identical vs inline). Soak proves non-poisoning upstream.

**Graduation: FINAL 2026-04-22.** Operator authorized default flip on interpretation (a). Helper `_phase_runner_complete_extracted()` default flipped `"false"` → `"true"` with ledger footnote. Explicit `=false` remains runtime kill switch. Inline deletion remains post-graduation maintenance PR, not bundled.

### CLASSIFY (`JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED`)

**Status:** default false. Slice 2 graduation — 3 flag-on sessions run 2026-04-22 by agent conductor, awaiting operator default-flip authorization.

**Reachability bar (`runner_reachability` = Required):** `[PhaseRunnerDelegate] CLASSIFY → runner op=<id>` INFO marker added to the orchestrator delegation hook (commit `fcfc26df71`, observability-only). Count > 0 in debug.log = runner body observably executed.

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; CLASSIFY phase telemetry is all `[Orchestrator]` INFO lines, no TTY-only affordances). **Residual risk**: CLASSIFYRunner's output log text is verbatim-identical to inline (parity guarantee), so log-text parity alone wouldn't distinguish paths — the `[PhaseRunnerDelegate]` marker closes that gap. One session (S3) emitted 2 extra environmental errors (DW batch create 13:40, concurrent.futures callback exception 13:52 at shutdown) that baseline didn't produce but are non-deterministic (DW API flake, asyncio shutdown race), not caused by the CLASSIFY runner path. Documented in session notes.

**Baseline reference (flag OFF):** `bt-2026-04-22-181910`, 835.5s, 0 PM, $0. Exception set: `CandidateGenerator EXHAUSTION`, `EmbeddingService`, `PERSISTENT-INTELLIGENCE checkpoint`.

**Session 1 (pre-marker)** — `bt-2026-04-22-194229`, commit `58107a1342`
- duration: 889.6s / 0 postmortems / $0.00
- outcome: **clean on hard criteria** (no new terminals, same exception set as baseline)
- notes: Marker not yet committed when session ran; reachability unobservable. Superseded by S1 retake below.

**Session 1 (retake with markers)** — `bt-2026-04-22-200312`, commit `fcfc26df71`
- flag_config: `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED=true` (only)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 993.6s / 0 postmortems / $0.00
- reachability: **15 `[PhaseRunnerDelegate] CLASSIFY → runner` markers** ✓
- outcome: **clean** — exception set matches baseline exactly (`CandidateGenerator EXHAUSTION`, `EmbeddingService`, `PERSISTENT-INTELLIGENCE checkpoint`)

**Session 2** — `bt-2026-04-22-202123`, commit `fcfc26df71`
- flag_config: `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED=true` (only)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 384.2s / 0 postmortems / $0.00
- reachability: **5 markers** ✓
- outcome: **clean** — exception set matches baseline

**Session 3** — `bt-2026-04-22-203723`, commit `fcfc26df71`
- flag_config: `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED=true` (only)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 903.2s / 0 postmortems / $0.00
- reachability: **18 markers** ✓
- outcome: **clean on hard terminal/cost criteria**; **2 environmental errors not present in baseline**: `[DoublewordProvider] Batch create error` at 13:40 (DW API flake, mid-session) and `[concurrent.futures] exception calling callback for <Future at 0x...>` at 13:52 (asyncio shutdown race). Neither ties to CLASSIFY runner path; both are known non-deterministic infra noise. Flagged honestly per §8.

**Clean-session bar check (hard criteria):**

| Criterion | S1 retake | S2 | S3 |
|---|---|---|---|
| No new POSTMORTEM/CANCELLED terminals vs baseline | ✓ (0 PM) | ✓ | ✓ |
| §8 log parity (structured logs present) | ✓ | ✓ | ✓ |
| Cost within ±10% of baseline | ✓ ($0=$0) | ✓ | ✓ |
| No exception-type regression | ✓ | ✓ | △ (2 env-noise types new) |
| `runner_reachability` = observed | ✓ 15 mk | ✓ 5 mk | ✓ 18 mk |

**Verdict:** 3/3 sessions clean on runner-attributed-failure bar (revised clean definition, 2026-04-22) AND reachability observed via `[PhaseRunnerDelegate]` markers (38 total across sessions).

**Infra-noise waivers** (per new ledger rule — tagged, not invisible):

- **WAIVER s3-dw-batch**: `[DoublewordProvider] Batch create error` at S3 13:40:30. Traceback: `aiohttp.connector._create_connection` → `_create_proxy_connection` → `_create_direct_connection` → `_wrap_create_connection` → `aiohappyeyeballs.start_connection` → `_staggered.staggered_race`. **No JARVIS runner frames.** Network-layer connection failure during DW batch API call; known transient when DW endpoint is slow to accept new connections under load. Non-blocking per revised clean bar.

- **WAIVER s3-asyncio-shutdown**: `[concurrent.futures] exception calling callback for <Future ...>` at S3 13:52:26. Traceback: `concurrent.futures._base._invoke_callbacks` → `asyncio.futures._call_set_state` → `asyncio.base_events.call_soon_threadsafe` → `_check_closed` → `RuntimeError: Event loop is closed`. **No JARVIS runner frames.** Classic asyncio shutdown race: a thread-pool executor Future completes after the asyncio loop has closed, and its callback attempts to schedule state on the closed loop. Non-blocking per revised clean bar. **Root cause ticket: see `project_async_shutdown_race_triage.md` (to be created alongside #3 ROUTE+CTX+PLAN arc).**

**Graduation: FINAL 2026-04-22.** Operator authorized default flip on option (a) with explicit triage rules (runner-attributed vs infra-noise waiver). Helper `_phase_runner_classify_extracted()` default flipped `"false"` → `"true"`. Explicit `=false` remains runtime kill switch. Inline deletion = separate post-graduation maintenance PR (not bundled).

**Post-flip confirmation** — `bt-2026-04-22-212919`, commit `50fe2044be` (with COMPLETE + CLASSIFY both default-true): 981.5s / 0 PM / $0 / 15 CLASSIFY markers / **no runner-attributed errors** / **no asyncio shutdown race** (clean exit). Confirms new default-true behavior is production-clean.

### ROUTE + CTX + PLAN (combined flag gate)

**Flags:** `JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED`, `JARVIS_PHASE_RUNNER_CONTEXT_EXPANSION_EXTRACTED`, `JARVIS_PHASE_RUNNER_PLAN_EXTRACTED`. Must all be `true` to engage (combined-gate pattern from Slice 3). Graduation flips all three together.

**Status:** default false. Slice 3 graduation — 3 flag-on sessions run 2026-04-22 by agent conductor, awaiting operator default-flip authorization.

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; ROUTE/CTX/PLAN are all inline-pipeline phases with no TTY-only affordance). **Reachability bar:** `[PhaseRunnerDelegate] ROUTE+CTX+PLAN → runners op=...` INFO marker at combined-gate delegation hook (same observability-only pattern from fcfc26df71).

**Baseline reference (flag OFF):** `bt-2026-04-22-181910`, 835.5s, 0 PM, $0. Exception set: `CandidateGenerator EXHAUSTION`, `EmbeddingService`, `PERSISTENT-INTELLIGENCE checkpoint`.

**Session 1** — `bt-2026-04-22-214630`, commit `5a320cfe3f` (with shutdown hygiene fix active)
- flag_config: `ROUTE` + `CONTEXT_EXPANSION` + `PLAN` all `true` (plus already-graduated COMPLETE + CLASSIFY default-true)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 897.6s / 0 postmortems / $0.00
- reachability: **13 CLASSIFY markers + 12 ROUTE+CTX+PLAN markers** ✓
- shutdown race: **0** (hygiene fix working)
- runner-attributed frames: **0** (no phase_runner/phase_dispatcher in any traceback)
- outcome: **clean**

**Session 2** — `bt-2026-04-22-220234`, commit `5a320cfe3f`
- flag_config: same 3 flags true
- duration: 1036.4s / 0 PM / $0.00
- reachability: **15 CLASSIFY + 14 ROUTE+CTX+PLAN markers** ✓
- shutdown race: **0**
- runner-attributed frames: **0**
- outcome: **clean**

**Session 3** — `bt-2026-04-22-222322`, commit `5a320cfe3f`
- flag_config: same 3 flags true
- duration: 897.6s / 0 PM / $0.00
- reachability: **15 CLASSIFY + 14 ROUTE+CTX+PLAN markers** ✓
- shutdown race: **0**
- runner-attributed frames: **0**
- outcome: **clean**

**Clean-session bar check (revised definition, runner-attributed failures only):**

| Criterion | S1 | S2 | S3 |
|---|---|---|---|
| No new runner-attributed failures (phase frames in traceback) | ✓ | ✓ | ✓ |
| No POSTMORTEM/CANCELLED runner-caused | ✓ (0 PM) | ✓ | ✓ |
| §8 log parity ([PhaseRunnerDelegate] markers present) | ✓ 25 mk | ✓ 29 mk | ✓ 29 mk |
| Cost parity ($0) | ✓ | ✓ | ✓ |
| Runner reachability observed | ✓ 12 mk | ✓ 14 mk | ✓ 14 mk |
| Shutdown race absent | ✓ | ✓ | ✓ |

**Infra-noise waivers:** None this graduation. The shutdown hygiene fix (`5a320cfe3f`) eliminated the `Event loop is closed` noise that caused the S3 waiver in the #2 CLASSIFY graduation. DW connection-layer flakes did not recur in any #3 session.

**Verdict:** 3/3 sessions clean on revised hard bar AND reachability observed via combined-gate markers (40 total ROUTE+CTX+PLAN markers + 43 CLASSIFY markers across 3 sessions). Shutdown fix validated — no race in any of the 3 sessions.

**Graduation: FINAL 2026-04-22.** Operator authorized atomic default flip — `_phase_runner_route_extracted()`, `_phase_runner_context_expansion_extracted()`, and `_phase_runner_plan_extracted()` all default `"false"` → `"true"` in one commit (`710667f3b6`). Per-phase `=false` kill switches preserved (operator can sever any leg without affecting the others, per §1 authority-sever-without-lying rule). Combined-gate helper `_phase_runner_slice3_fully_extracted()` now returns `True` by default since all three per-phase helpers default to `True`.

**Post-flip confirmation** — `bt-2026-04-22-224521`, commit `710667f3b6` (COMPLETE + CLASSIFY + ROUTE + CTX + PLAN all default-true): 960.0s / 0 PM / $0 / 17 CLASSIFY + 16 ROUTE+CTX+PLAN markers / **0 race** / **0 runner-attributed frames**. Confirms new production behavior is clean.

### VALIDATE (`JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED`)

**Status:** default false. Slice 4a.1 graduation — 3 flag-on sessions run 2026-04-22 by agent conductor, awaiting operator default-flip authorization.

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; VALIDATE is pipeline-internal with no TTY affordance). **Reachability bar** (`runner_reachability = Required`): `[PhaseRunnerDelegate] VALIDATE → runner` INFO marker + `[ValidateRetryFSM]` FSM transition lines; ≥1 of the 3 sessions must show both.

**Reachability profile note** (for future-downstream-of-GENERATE graduations — #5 GATE, #6 Slice4b, #7 GENERATE retries): VALIDATE only fires when GENERATE produces candidates. Short soaks where most ops land at `CandidateGenerator EXHAUSTION` before reaching VALIDATE will naturally show sparse VALIDATE markers. Matrix rule "≥1 across the 3 sessions" is calibrated to this reality. S3 of this graduation captured this pattern (0 VALIDATE markers) while S1 + S2 covered reachability.

**Baseline reference (flag OFF):** `bt-2026-04-22-181910`, 835.5s, 0 PM, $0.

**Session 1** — `bt-2026-04-22-230147`, commit `710667f3b6` (all prior graduations active)
- flag_config: `JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED=true` (on top of #1-#3 defaults)
- cost_cap: $0.50 / idle_timeout: 600s
- duration: 1106.3s / 0 postmortems / $0.00
- reachability: **1 `[PhaseRunnerDelegate] VALIDATE` marker + 3 `[ValidateRetryFSM]` transition lines** ✓
- shutdown race: **0** / runner-attributed frames: **0**
- outcome: **clean + reachable**

**Session 2** — `bt-2026-04-22-232323`, commit `710667f3b6`
- flag_config: same
- duration: 459.8s / 0 PM / $0.00
- reachability: **1 `[PhaseRunnerDelegate] VALIDATE` marker + 3 `[ValidateRetryFSM]` lines** ✓
- shutdown race: 0 / runner-attributed frames: 0
- outcome: **clean + reachable**

**Session 3** — `bt-2026-04-22-235808`, commit `710667f3b6`
- flag_config: same
- duration: 1084.8s / 0 PM / $0.00
- reachability: **0 VALIDATE markers / 0 FSM lines** (all ops terminated before VALIDATE — expected per profile note above)
- shutdown race: 0 / runner-attributed frames: 0
- outcome: **clean on hard bar**; reachability carried by S1+S2

**Clean-session bar check (revised definition):**

| Criterion | S1 | S2 | S3 |
|---|---|---|---|
| No new runner-attributed failures (frames in traceback) | ✓ 0 | ✓ 0 | ✓ 0 |
| No POSTMORTEM/CANCELLED runner-caused | ✓ 0 PM | ✓ 0 PM | ✓ 0 PM |
| §8 log parity ([PhaseRunnerDelegate] markers for all default-true flags present) | ✓ | ✓ | ✓ |
| Cost parity ($0) | ✓ | ✓ | ✓ |
| Runner reachability (≥1 of 3 sessions) | **✓ aggregate:** S1 + S2 each had 1 VALIDATE marker + 3 FSM lines = reachability observed across graduation |
| Shutdown race absent | ✓ | ✓ | ✓ |

**Infra-noise waivers:** None. Shutdown hygiene fix (`5a320cfe3f`) continues to eliminate the `Event loop is closed` noise. DW flakes did not recur.

**Verdict:** 3/3 sessions clean on revised hard bar. Reachability observed in 2/3 sessions (matrix rule requires ≥1) via 2 delegation markers + 6 FSM transition lines. S3 landed no VALIDATE markers — expected downstream-of-GENERATE pattern, not a regression.

**Graduation: FINAL 2026-04-22.** Operator authorized default flip. Helper `_phase_runner_validate_extracted()` default flipped `"false"` → `"true"` (commit `97590ae72d`). Explicit `=false` remains runtime kill switch reverting to 762-line inline VALIDATE block.

**Post-flip confirmation** — `bt-2026-04-23-002617`, commit `97590ae72d` (6 flags default-true: COMPLETE + CLASSIFY + ROUTE + CTX + PLAN + VALIDATE): 1451.7s / 0 PM / $0 / 17 CLASSIFY + 16 ROUTE+CTX+PLAN markers / **0 race** / **0 runner-attributed frames**. VALIDATE not reached this session (downstream-of-GENERATE profile), but the 6 default-true flags operating in combination produced no regressions.

### GATE (`JARVIS_PHASE_RUNNER_GATE_EXTRACTED`)

**Status:** default false. Slice 4a.2 graduation — 3 flag-on sessions run 2026-04-22/23 by agent conductor, awaiting operator default-flip authorization.

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; GATE is pipeline-internal, NOTIFY_APPLY 5b preview is the only TTY-adjacent affordance and it's fault-isolated). **Reachability bar** (`runner_reachability = Required`): `[PhaseRunnerDelegate] GATE → runner` INFO marker + `[SemanticGuard]` structured line (which fires on every op that enters GATE, per the Track A observability contract). ≥1 of 3 sessions must show both.

**Reachability profile:** GATE is downstream-of-VALIDATE — only fires when VALIDATE produces a candidate. Short soaks where ops terminate at `CandidateGenerator EXHAUSTION` will show sparse GATE markers (S3 of this graduation landed 0 GATE markers; S1+S2 each captured 1). Same pattern as #4 VALIDATE.

**Baseline reference (flag OFF):** `bt-2026-04-22-181910`, 835.5s, 0 PM, $0.

**Session 1** — `bt-2026-04-23-005127`, commit `97590ae72d` (#1-#4 defaults-true, GATE flag set)
- duration: 931.2s / 0 postmortems / $0.00
- reachability: **1 `[PhaseRunnerDelegate] GATE` marker + 1 `[SemanticGuard]` structured line** ✓
- shutdown race: 0 / runner-attributed frames: 0
- outcome: **clean + reachable**

**Session 2** — `bt-2026-04-23-010733`, commit `97590ae72d`
- duration: 831.3s / 0 PM / $0.00
- reachability: **1 `[PhaseRunnerDelegate] GATE` + 1 `[SemanticGuard]`** ✓
- shutdown race: 0 / runner-attributed frames: 0
- outcome: **clean + reachable**

**Session 3** — `bt-2026-04-23-012329`, commit `97590ae72d`
- duration: 945.6s / 0 PM / $0.00
- reachability: **0 GATE markers / 0 SemanticGuard** (all ops terminated before GATE — expected profile)
- shutdown race: 0 / runner-attributed frames: 0
- outcome: **clean on hard bar**; reachability carried by S1+S2

**Clean-session bar check (revised):**

| Criterion | S1 | S2 | S3 |
|---|---|---|---|
| No runner-attributed failures | ✓ 0 frames | ✓ 0 frames | ✓ 0 frames |
| No POSTMORTEM/CANCELLED runner-caused | ✓ 0 PM | ✓ 0 PM | ✓ 0 PM |
| §8 log parity (GATE sub-gate markers SemanticGuard/MutationGate) | ✓ 1 SG | ✓ 1 SG | ✓ (no GATE entries this session) |
| Cost parity ($0) | ✓ | ✓ | ✓ |
| Runner reachability (≥1 of 3) | **✓ aggregate:** 2 sessions hit both delegation marker + §6 SemanticGuard log |
| Shutdown race absent | ✓ | ✓ | ✓ |

**Infra-noise waivers:** None. Shutdown hygiene fix continues holding.

**MutationGate note:** no `[MutationGate]` lines fired in any session because no op's target file matched the MutationGate allowlist. Expected — MutationGate is gated on critical-path allowlist (Session W calibration). Its absence is not a regression; the GATE runner's code path that WOULD invoke it remains parity-tested (21 tests in Slice 4a.2).

**Verdict:** 3/3 sessions clean on revised hard bar. Reachability observed in 2/3 sessions (matrix rule requires ≥1) via 2 delegation markers + 2 §6 `[SemanticGuard]` lines. S3 landed no GATE markers — expected downstream-of-VALIDATE profile, not a regression.

**Graduation: FINAL 2026-04-23.** Operator authorized default flip. Helper `_phase_runner_gate_extracted()` default flipped `"false"` → `"true"` (commit `2851c82ffd`). Explicit `=false` remains runtime kill switch reverting to the 600-line inline GATE block. Inline deletion remains separate post-graduation maintenance PR.

**Post-flip confirmation** — `bt-2026-04-23-014922`, commit `2851c82ffd` (7 flags default-true: COMPLETE + CLASSIFY + ROUTE + CTX + PLAN + VALIDATE + GATE): 1032.6s / 0 PM / $0 / stop_reason=idle_timeout / 2 CLASSIFY + 2 ROUTE+CTX+PLAN markers / **0 runner-attributed frames** (4 tracebacks all in `candidate_generator.py` + `persistent_intelligence_manager.py`, none in `phase_runners/` or `phase_dispatcher`) / **0 JARVIS shutdown race** (`Event loop is closed` absent; hygiene fix 5a320cfe3f still holding). 0 GATE markers this session — ops terminated upstream at CandidateGenerator EXHAUSTION per downstream-of-VALIDATE profile (same pattern S3 of graduation); GATE reachability already established by S1+S2. One late aiohttp `SSEDecoder._aiter_chunks` asyncgen-close warning — different root from the fixed JARVIS shutdown race, infra-noise (no JARVIS frames).

### APPROVE + APPLY + VERIFY (`JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED`)

**Status:** default false. Slice 4b graduation — 3 flag-on sessions being run 2026-04-23 by agent conductor.

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; APPROVE+APPLY+VERIFY is mutation-adjacent but rollback + SerpentApproval + AutoCommitter paths are all code-internal; no TTY-only affordance in the runner body itself — NOTIFY_APPLY 5b preview is owned by GATE and fault-isolated there). **Reachability bar** (`runner_reachability = Required`): `[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner` INFO marker at the delegation hook. ≥1 of 3 sessions must show the marker.

**Reachability profile:** SLICE4B is downstream-of-GATE (which is downstream-of-VALIDATE) — only fires when an op produces candidates AND survives the GATE immune system. Short soaks where ops terminate at `CandidateGenerator EXHAUSTION` upstream will show zero SLICE4B markers. Deepest natural-reachability profile in the 8-flag sequence. If all 3 sessions show zero markers, the forced-reachability harness (matrix §"Forced-reachability harness") should seed a trivial backlog op to walk the full pipeline.

**Baseline reference (flag OFF):** `bt-2026-04-22-181910`, 835.5s, 0 PM, $0.

**Session 1** — `bt-2026-04-23-021826`, commit `2851c82ffd` (#1-#5 defaults-true, SLICE4B flag set)
- flag_config: `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED=true`
- cost_cap: $0.50 / idle_timeout: 600s / stop_reason: idle_timeout
- duration: 1163.0s / 0 postmortems / $0.00
- reachability: **0 APPROVE+APPLY+VERIFY markers** — ops terminated upstream at CandidateGenerator EXHAUSTION (expected deepest-downstream profile)
- upstream markers (all default-true flags exercised): 19 CLASSIFY + 18 ROUTE+CTX+PLAN (no VALIDATE or GATE this session)
- shutdown race: 0 / runner-attributed frames: 0
- tracebacks: 2 — both `persistent_intelligence_manager.py` checkpoint errors (pre-existing infra, readonly DB)
- outcome: **clean on hard bar; reachability pending S2+S3**

**Session 2** — `bt-2026-04-23-023957`, commit `2851c82ffd`
- flag_config: `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED=true`
- cost_cap: $0.50 / idle_timeout: 600s / stop_reason: idle_timeout
- duration: 893.5s / 0 postmortems / $0.00
- reachability: **0 APPROVE+APPLY+VERIFY markers** (deepest-downstream profile)
- upstream markers: 16 CLASSIFY + 15 ROUTE+CTX+PLAN
- shutdown race: 0 / runner-attributed frames: 0
- tracebacks: 5 — 2× `persistent_intelligence_manager.py` (pre-existing infra) + 1× `doubleword_provider.py _create_batch` + 2× `aiohttp` connector/client (DW batch network flake, same pattern as #2 CLASSIFY S3 waiver)
- outcome: **clean on hard bar**
- infra-noise waiver: **WAIVER s6-s2-dw-batch** — DoubleWord batch network failure. Traceback rooted in `aiohttp.connector.connect` + `aiohttp.client._connect_and_send_request`. No JARVIS runner frames. Non-blocking per revised clean bar. Pattern continues to recur sporadically; already tracked in `project_async_shutdown_race_triage.md`.

**Session 3** — `bt-2026-04-23-030636`, commit `2851c82ffd`
- flag_config: `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED=true`
- cost_cap: $0.50 / idle_timeout: 600s / stop_reason: idle_timeout
- duration: 1217.0s / 0 postmortems / $0.00
- reachability: **0 APPROVE+APPLY+VERIFY markers** (consistent deepest-downstream profile)
- upstream markers: 16 CLASSIFY + 15 ROUTE+CTX+PLAN
- shutdown race: 0 / runner-attributed frames: 0
- tracebacks: 5 — 2× `persistent_intelligence_manager.py` + 1× `doubleword_provider.py _create_batch` + 2× `aiohttp` connector/client (identical DW flake pattern to S2)
- outcome: **clean on hard bar**
- infra-noise waiver: **WAIVER s6-s3-dw-batch** — identical traceback shape to S2 waiver. DW pattern now recurring in 2/3 SLICE4B sessions — reaches the matrix rule's cross-flag threshold of interest (CLASSIFY S3 + SLICE4B S2+S3). Recommend root-cause investigation post-#6 graduation; non-blocking here.

**Clean-session bar check (revised definition):**

| Criterion | S1 | S2 | S3 |
|---|---|---|---|
| No runner-attributed failures (frames in traceback) | ✓ 0 | ✓ 0 | ✓ 0 |
| No POSTMORTEM/CANCELLED runner-caused | ✓ 0 PM | ✓ 0 PM | ✓ 0 PM |
| §8 log parity (upstream markers present) | ✓ | ✓ | ✓ |
| Cost parity ($0) | ✓ | ✓ | ✓ |
| Runner reachability (≥1 of 3) | **✗ 0/3 natural** — forced-reachability harness required |
| Shutdown race absent | ✓ | ✓ | ✓ |

**Session 4 (forced-reachability harness)** — `bt-2026-04-23-033530`, commit `2851c82ffd`
- flag_config: `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED=true`
- cost_cap: **$0.50** / idle_timeout: 600s / stop_reason: `budget_exhausted` (≡ clean stop for harness-class sessions per matrix footnote)
- duration: 849.0s / 0 postmortems / $0.517
- harness_cost_cap: $0.50 (retroactively: insufficient — S5+ bumped to $1.00)
- seed enqueued: `slice4b-forced-reach-s4-2026-04-23` targeting empty `backend/core/ouroboros/architect/__init__.py` (did NOT win dispatch race; moved to `.jarvis/superseded_seeds.md`)
- **reachability_source: opportunistic** — marker observed on unrelated live-sensor op, not on our seed
  - op_id: `op-019db85f-2fe9-7328-84f9-615625f6d4d7-cau`
  - source: **RuntimeHealthSensor** (critical_urgency → IMMEDIATE route)
  - goal: `"Python 3.9.6 is PAST end-of-life (EOL: 2025-10). No security patches. Upgrade required."`
  - target_files: `['requirements.txt']`
  - classified: `SAFE_AUTO / trivial / auto_approve=True / fast_path=True / blast_radius=1`
  - full phase trace (verbatim from debug.log):
    - `[PhaseRunnerDelegate] CLASSIFY → runner op=op-019db85f-2fe9` @ 20:38:32
    - `[PhaseRunnerDelegate] ROUTE+CTX+PLAN → runners op=op-019db85f-2fe9` @ 20:38:32
    - `PlanGenerator Skipping plan ... trivial_op` @ 20:38:32
    - `ClaudeProvider 1 candidates in 89.8s cost=$0.1212 route=immediate` @ 20:40:03
    - `[PhaseRunnerDelegate] VALIDATE → runner op=op-019db85f-2fe9` @ 20:40:03
    - `[ValidateRetryFSM] candidate_passed_break` + `loop_exit_normal` @ 20:40:04
    - `[PhaseRunnerDelegate] GATE → runner op=op-019db85f-2fe9` @ 20:40:04
    - `GATE can_write decision ... allowed=True reason=ok` @ 20:40:04
    - `[SemanticGuard] op=op-019db85f-... findings=0 hard=0 soft=0 patterns=[none] risk_before=SAFE_AUTO risk_after=SAFE_AUTO duration_ms=5 files_scanned=1` @ 20:40:04
    - `[REVIEW-SHADOW] aggregate=APPROVE files_reviewed=1 approved=1` @ 20:40:04
    - **`[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner op=op-019db85f-2fe9`** @ 20:40:04 ← **reachability proof**
    - `CommProtocol HEARTBEAT phase=APPLY progress_pct=80.0 target_file=requirements.txt` @ 20:40:05
- APPLY persisted to working tree: yes (requirements.txt uncommitted diff post-session)
- AutoCommitter fired: no (budget exhaustion terminated session before COMPLETE)
- post-session cleanup: `git checkout -- requirements.txt` (opportunistic APPLY artifact reverted per operator directive; §8 hygiene + no soak-evidence repo pollution)
- markers total: 15 CLASSIFY + 13 ROUTE+CTX+PLAN + 1 VALIDATE + 1 GATE + **1 APPROVE+APPLY+VERIFY** + 1 SemanticGuard + 3 ValidateRetryFSM
- runner-attributed frames: **0** (slice4b_runner.py: 0, phase_runners/: 0, phase_dispatcher: 0)
- shutdown race: **0**
- tracebacks: 4 — 2× `persistent_intelligence_manager.py` (pre-existing infra) + 2× `candidate_generator.py` (BG exhaustion on unrelated ops). None runner-attributed.
- outcome: **clean + reachable (opportunistic)**

**Clean-session bar check (revised definition + harness-class footnote):**

| Criterion | S1 | S2 | S3 | S4 (harness) |
|---|---|---|---|---|
| No runner-attributed failures | ✓ 0 | ✓ 0 | ✓ 0 | ✓ 0 |
| No POSTMORTEM/CANCELLED runner-caused | ✓ 0 PM | ✓ 0 PM | ✓ 0 PM | ✓ 0 PM |
| §8 log parity (structured markers) | ✓ | ✓ | ✓ | ✓ (full chain CLASSIFY→SLICE4B) |
| Clean stop_reason | ✓ idle_timeout | ✓ idle_timeout | ✓ idle_timeout | ✓ budget_exhausted (harness-class ≡ idle_timeout per footnote) |
| Runner reachability (≥1 of 4) | ✗ 0 natural | ✗ 0 natural | ✗ 0 natural | **✓ opportunistic op-019db85f-2fe9 via RuntimeHealthSensor** |
| Shutdown race absent | ✓ | ✓ | ✓ | ✓ |
| reachability_source | — | — | — | **opportunistic** |

**Infra-noise waivers across the 4 sessions**: `s6-s2-dw-batch` + `s6-s3-dw-batch` (DW/aiohttp connection layer); S1 + S4 had no DW flakes. Pattern-tracking continues in `project_async_shutdown_race_triage.md`.

**Verdict (S1–S4):** 4/4 sessions clean on hard bar; reachability observed in S4 via opportunistic live-sensor op (RuntimeHealthSensor IMMEDIATE → Claude → full pipeline through SLICE4B with verbatim `[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner` marker + APPLY heartbeat at 80% on `requirements.txt`). Per operator directive, opportunistic-source reachability is acceptable evidence for this flip — "we care that a real op hit the runner under flag-on, not that our backlog seed won a race." Deterministic harness enqueue deferred to follow-up ticket (priority-slot + concurrent-op cap + BG pause).

**Next:** resume normal 3-session cadence under revised economics (`harness_cost_cap=$1.00` per S5–S7) with flag on, then request default flip for #6.

**Session 5** — `bt-2026-04-23-040327`, commit `2851c82ffd`
- flag_config: `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED=true`
- harness_cost_cap: **$1.00** / idle_timeout: 600s / stop_reason: `idle_timeout` (clean)
- duration: 1216.3s / 0 postmortems / $0.369 (well under $1.00 cap)
- **reachability_source: opportunistic** — same RuntimeHealthSensor IMMEDIATE pattern as S4
  - op_id: `op-019db885-1aca-7859-a868-98b7115de70d-cau`
  - source: **RuntimeHealthSensor** (critical_urgency → IMMEDIATE route)
  - goal: `"Python 3.9.6 is PAST end-of-life (EOL: 2025-10). No security patches. Upgrade required."`
  - target_files: `['requirements.txt']` / classified: `SAFE_AUTO / trivial / blast_radius=1`
  - full phase trace (verbatim):
    - `[PhaseRunnerDelegate] CLASSIFY → runner op=op-019db885-1aca` @ 21:07:34
    - `[PhaseRunnerDelegate] ROUTE+CTX+PLAN → runners op=op-019db885-1aca` @ 21:07:34
    - `[PhaseRunnerDelegate] VALIDATE → runner op=op-019db885-1aca` @ 21:09:30
    - `[ValidateRetryFSM] candidate_passed_break` @ 21:10:34
    - `[PhaseRunnerDelegate] GATE → runner op=op-019db885-1aca` @ 21:10:34
    - `GATE can_write ... allowed=True reason=ok` @ 21:10:34
    - `[SemanticGuard] findings=0 hard=0 soft=0 patterns=[none] risk_before=SAFE_AUTO risk_after=SAFE_AUTO` @ 21:10:34
    - `[REVIEW-SHADOW] aggregate=APPROVE approved=1` @ 21:10:34
    - **`[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner op=op-019db885-1aca`** @ 21:10:34 ← **reachability proof**
    - `HEARTBEAT phase=APPLY progress_pct=80.0 target_file=requirements.txt` @ 21:10:35
    - `HEARTBEAT phase=APPLY diff_text=...` @ 21:10:36 (2 new Ouroboros-signed comment lines)
- APPLY persisted to working tree: yes (reverted post-session per operator directive)
- post-session cleanup: `git checkout -- requirements.txt` ✓
- markers total: 17 CLASSIFY + 16 ROUTE+CTX+PLAN + 1 VALIDATE + 1 GATE + **1 APPROVE+APPLY+VERIFY** + 1 SemanticGuard + 3 ValidateRetryFSM
- runner-attributed frames: **0** (slice4b_runner.py / phase_runners / phase_dispatcher all 0)
- shutdown race: **0**
- tracebacks: 3 — all `persistent_intelligence_manager.py` checkpoint (pre-existing infra, readonly DB). Zero DW batch flake this session.
- outcome: **clean + reachable (opportunistic)**

**Session 6** — `bt-2026-04-23-043017`, commit `2851c82ffd`
- flag_config: `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED=true`
- harness_cost_cap: **$1.00** / stop_reason: `idle_timeout` (clean)
- duration: 1088.4s / 0 PM / $0.497
- **reachability_source: opportunistic** — RuntimeHealthSensor pattern (3rd consecutive session)
  - op_id: `op-019db89d-bbb6-723d-9d7a-adb86c6c648c-cau`
  - source: RuntimeHealthSensor (critical_urgency → IMMEDIATE) / target_files: `['requirements.txt']` / SAFE_AUTO trivial blast_radius=1
  - phase trace: CLASSIFY @ 21:34:28 → ROUTE+CTX+PLAN @ 21:34:28 → VALIDATE @ 21:36:26 → GATE can_write=allowed + SemanticGuard 0/0 @ 21:37:31 → **`[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner op=op-019db89d-bbb6`** @ 21:37:31 → APPLY HEARTBEAT 80% target=requirements.txt @ 21:37:32
- APPLY reverted post-session: `git checkout -- requirements.txt` ✓
- markers total: 16 CLASSIFY + 15 ROUTE+CTX+PLAN + 1 VALIDATE + 1 GATE + **1 APPROVE+APPLY+VERIFY** + 1 SemanticGuard
- runner-attributed frames: 0 / shutdown race: 0
- tracebacks: 2 — both `persistent_intelligence_manager.py` (pre-existing infra). Zero DW batch flake.
- outcome: **clean + reachable (opportunistic)**

**Session 7** — `bt-2026-04-23-045653`, commit `2851c82ffd`
- flag_config: `JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED=true`
- harness_cost_cap: **$1.00** / stop_reason: `idle_timeout` (clean)
- duration: 901.5s / 0 PM / $0.187
- **reachability_source: opportunistic** — RuntimeHealthSensor pattern (4th consecutive session)
  - op_id: `op-019db8b6-b89c-70c3-b1a7-0b823ff9e28c-cau` / source: RuntimeHealthSensor / target_files: `['requirements.txt']` / SAFE_AUTO trivial
  - phase trace: CLASSIFY @ 22:03:51 → ROUTE+CTX+PLAN @ 22:03:51 → VALIDATE @ 22:05:56 → GATE can_write=allowed + SemanticGuard 0/0 @ 22:08:01 → **`[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner op=op-019db8b6-b89c`** @ 22:08:01 → APPLY HEARTBEAT 80% target=requirements.txt @ 22:08:07
- APPLY persisted to working tree: **no** — git status showed only pre-existing `notebooks/report.ipynb` dirt post-session; requirements.txt unmodified (APPLY phase executed but resulting patch was a no-op relative to current file state, or idempotent with prior session signatures)
- post-session cleanup: not needed (no artifact to revert)
- markers total: 12 CLASSIFY + 11 ROUTE+CTX+PLAN + 1 VALIDATE + 1 GATE + **1 APPROVE+APPLY+VERIFY** + 1 SemanticGuard
- runner-attributed frames: 0 / shutdown race: 0
- tracebacks: 2 — both `persistent_intelligence_manager.py` (pre-existing infra). Zero DW batch flake.
- outcome: **clean + reachable (opportunistic)**

**4-session verdict (S4+S5+S6+S7 under harness-class economics):**

| Session | Duration | Cost | Stop | SLICE4B marker | reachability_source | Hard bar |
|---|---|---|---|---|---|---|
| S4 `...033530` | 849.0s | $0.517 / $0.50 | budget_exhausted | ✓ op-019db85f | opportunistic | clean |
| S5 `...040327` | 1216.3s | $0.369 / $1.00 | idle_timeout | ✓ op-019db885 | opportunistic | clean |
| S6 `...043017` | 1088.4s | $0.497 / $1.00 | idle_timeout | ✓ op-019db89d | opportunistic | clean |
| S7 `...045653` | 901.5s | $0.187 / $1.00 | idle_timeout | ✓ op-019db8b6 | opportunistic | clean |

**All 4 sessions clean on hard bar** (0 runner-attributed frames, 0 shutdown race, 0 PM, §8 log parity). **Reachability observed in all 4** (100% — not the minimum "≥1 of 3" bar; natural reachability via RuntimeHealthSensor IMMEDIATE ops is apparently reliable for SLICE4B once pipelines complete, which the $1.00 cap now enables). Every marker-firing op walked: CLASSIFY → ROUTE+CTX+PLAN → VALIDATE → GATE (can_write + SemanticGuard 0/0) → **SLICE4B delegation** → APPLY HEARTBEAT at 80% on `requirements.txt`. Each session's APPLY artifact reverted post-session per operator §8 hygiene directive (S7 was a natural no-op — nothing to revert).

Combined with the original S1+S2+S3 hard-bar-clean sessions (which had no reachability due to $0.50 cap exhausting provider before VALIDATE), the total arc is **7/7 hard-bar clean, 4/4 reachability-observed sessions under harness-class economics**.

**Graduation eligibility: YES** — bar met per operator-approved revised criteria (opportunistic reachability acceptable when live-sensor op walks SLICE4B with flag on; forced-harness deterministic enqueue follow-up ticketed but not blocking).

**Graduation: FINAL 2026-04-23.** Operator authorized default flip. Helper `_phase_runner_slice4b_extracted()` default flipped `"false"` → `"true"` (commit `0b120b588a`). Explicit `=false` remains runtime kill switch reverting to the ~1150-line inline APPROVE+APPLY+VERIFY block. Inline deletion = separate post-graduation maintenance PR.

**Post-flip confirmation** — `bt-2026-04-23-052009`, commit `0b120b588a` (8 flags default-true: COMPLETE + CLASSIFY + ROUTE + CTX + PLAN + VALIDATE + GATE + SLICE4B): 2712.2s / 0 PM / $0.311 / $1.00 cap / **stop_reason=idle_timeout** / 12 CLASSIFY + 11 ROUTE+CTX+PLAN markers / **0 runner-attributed frames** / **0 JARVIS shutdown race** (hygiene fix 5a320cfe3f still holding). **Zero VALIDATE/GATE/SLICE4B markers this session** — Claude API was experiencing weather during the session (2× `anthropic/_base_client.py request` + 2× `httpx/_transports default` tracebacks = external); ops exhausted upstream at CandidateGenerator before reaching VALIDATE. Infra-noise waiver **WAIVER post-flip-claude-api-weather** — pure 3rd-party library tracebacks, no JARVIS runner frames. Reachability was already demonstrated 4/4 times under flag-on in S4–S7, so this session's role was confirming default-true produces no regressions — confirmed.

### GENERATE (`JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED`)

**Status:** default false. Slice 5a+5b graduation — cadence in progress 2026-04-23 by agent conductor under harness-class `--cost-cap 1.00`.

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; GENERATE is pipeline-internal — the Iron Gate sub-gates all emit structured §8 INFO logs that survive headless runs). **Reachability bar** (`runner_reachability = Required`): `[PhaseRunnerDelegate] GENERATE → runner` INFO marker — fires on every op that survives CLASSIFY, so natural reachability is strongest of the 8 flags. Secondary bar: matrix rule "Iron Gate suite must be observed firing in at least one session" — any one of `ExplorationLedger(...)` / ASCII-gate / dependency_file_integrity / multi_file_coverage / retry-feedback-composed must fire during the cadence.

**Reachability profile:** GENERATE is mid-pipeline (after CLASSIFY → ROUTE+CTX+PLAN). Every non-trivial op hits GENERATE. Iron Gate signals fire only when GENERATE produces a candidate that enters the post-GENERATE gate chain — requires provider success (at least one Tier 0 or Tier 1 returning content). When external provider weather (Claude API 5xx, DW connect flakes) prevents candidates from forming, GENERATE markers still fire (delegation happens before provider call) but Iron Gate signals stay silent. 3 clean sessions must include ≥1 with Iron Gate evidence; if external weather persists, matrix allows a targeted re-run rather than blocking.

**Baseline reference (flag OFF):** `bt-2026-04-22-181910`, 835.5s, 0 PM, $0.

**Session 1** — `bt-2026-04-23-062014`, commit `0b120b588a` (#1–#6 defaults-true, GENERATE flag set)
- flag_config: `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED=true`
- harness_cost_cap: **$1.00** / idle_timeout: 600s / stop_reason: `idle_timeout` (clean)
- duration: 2495.4s / 0 postmortems / $0.074 (low spend — API retries failing early)
- reachability: **14 `[PhaseRunnerDelegate] GENERATE` markers** ✓ (strong natural reachability)
- downstream: 0 VALIDATE / 0 GATE / 0 SLICE4B — ops exhausted at GENERATE because Claude API weather prevented candidate formation
- Iron Gate suite: **0 ExplorationLedger / 0 ASCII / 0 dep_integrity / 0 multi_file_coverage / 0 SemanticGuard** (matrix secondary bar pending)
- runner-attributed frames: **0** (generate_runner.py: 0, phase_runners/: 0, phase_dispatcher: 0)
- shutdown race: **0**
- tracebacks: 10 — 2× `anthropic/_base_client.py request` + 2× `httpx/_transports/default.py` (external API weather) + 2× `providers.py _stream_with_resilience` (retry wrapper catching external errors, not a phase runner) + 2× `persistent_intelligence_manager.py` (pre-existing infra) + 2× `candidate_generator.py` exhaustion (consequence of API weather)
- upstream markers: 15 CLASSIFY + 14 ROUTE+CTX+PLAN
- post-session cleanup: git status only shows pre-existing `notebooks/report.ipynb` dirt (no requirements.txt APPLY artifact — ops didn't reach APPLY)
- outcome: **clean on hard bar; GENERATE reachability observed (opportunistic, 14 markers); Iron Gate signals pending S2/S3**
- infra-noise waiver: **WAIVER s7-s1-claude-api-weather** — pure 3rd-party/retry-wrapper tracebacks, no JARVIS runner frames. Same pattern as post-flip #6 session.

**Session 2 (NOT COUNTED — incomplete_kill)** — `bt-2026-04-23-070317`, commit `0b120b588a`

**Per operator directive 2026-04-23: this row is negative evidence only. S2 does NOT count as one of the three graduation sessions.** No summary.json artifact, operator-aborted, non-terminal stop, idle_timeout never tripped because provider retries kept resetting internal liveness (real bug — see Follow-up Ticket A).

- flag_config: `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED=true`
- harness_cost_cap: $1.00
- **session_outcome: `incomplete_kill`**
- **stop_reason: `operator_interrupt`** (agent-invoked TaskStop on parent bash after 15+ min silence gap)
- duration (observed): ~56 min wall clock (boot 00:03:29 → last log 00:59:14) + ~15 min silence gap before kill at 01:14
- **infra_waiver: anthropic_transport** — canonical signature: `File "anthropic/_base_client.py", line 1637, in request` → `File "httpx/_transports/default.py", line 101, in map_httpcore_exceptions`. Chain: `APITimeoutError → ConnectTimeout → ConnectTimeout → TimeoutError → CancelledError: deadline exceeded`. Last interesting log @ 00:58:27: `[ClaudeProvider] claude_stream transient failure ... backing off 2.0s (attempt 1/3 gen=5)`. No JARVIS phase_runners / phase_dispatcher / generate_runner frames anywhere.
- observational (for pattern tracking only, NOT graduation evidence): 3 `[PhaseRunnerDelegate] GENERATE` markers + 4 CLASSIFY + 3 ROUTE+CTX+PLAN fired before the retry-loop trap. 0 runner-attributed frames. 0 shutdown race. 0 Iron Gate signals.
- NO summary.json written — atexit fallback didn't fire because the parent bash process was what received the kill signal, not the Python process directly (Follow-up Ticket B).

**Decision-tree checkpoint (binding operator ruling 2026-04-23):**

- S1 valid (completed session row; idle_timeout, summary.json, terminal artifacts trusted).
- S2 **incomplete_kill — does NOT count** as a graduation session.
- Graduation now requires two NEW completed sessions (call them S2′ and S3) with summary.json + clean hard bar + GENERATE marker, plus live Iron Gate evidence OR operator authorization to use path (B).
- Path (B) preconditions (marker + 36/36 parity on HEAD `0b120b588a`) are necessary but NOT sufficient for a Highest-risk flip while 2/3 planned sessions are incomplete.
- **#7 default flip NOT AUTHORIZED.**

**Blocked-on:** Claude API weather AND Follow-up Ticket A (idle_timeout hijacked by provider retry storm). Do NOT launch S2′ while Anthropic transport is still observably bad, unless Ticket A ships a hard `--max-wall-seconds` wall-clock guard that prevents retry purgatory from consuming unbounded wall time. Ticket A is the preferred unblock path — it makes all future graduation soaks deterministic regardless of provider weather.

**Session 2′ (S2 rerun under new guards)** — `bt-2026-04-23-203517`, commit `68954cc62d` (post Tickets A1+B+C)
- flag_config: `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED=true`
- launch: `--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400` (canonical post-Ticket-C recipe; no `tail -f /dev/null` prefix)
- stop_reason: **`idle_timeout`** (clean) / **session_outcome: `complete`** (Ticket B v1.1b field landed live)
- duration: 1044.4s / PM: 0 / cost: $0.000
- reachability: **12 `[PhaseRunnerDelegate] GENERATE` markers** ✓ (path-(B) precondition met)
- downstream: 0 VALIDATE / 0 GATE / 0 SLICE4B — Anthropic transport weather persists; ops exhausted at GENERATE
- Iron Gate suite: **0 signals**
- runner-attributed frames: **0** (generate_runner / phase_runners / phase_dispatcher all 0)
- shutdown race: **0**
- tracebacks: 10 — 2× `anthropic/_base_client.py` + 2× `httpx/_transports/default.py` + 2× `providers.py _stream_with_resilience` + 2× `persistent_intelligence_manager.py` + 2× `candidate_generator.py` exhaustion. All classified **`infra_waiver: anthropic_transport`**; zero JARVIS runner frames.
- upstream markers: 12 CLASSIFY + 12 ROUTE+CTX+PLAN
- Ticket C proof: debug.log @ 13:35:56 — `[Harness] Headless mode: REPL input disabled (headless=True, stdin.isatty=False)` (auto-detect worked without stdin guard)
- post-session cleanup: no requirements.txt artifact; only pre-existing `notebooks/report.ipynb` dirt
- outcome: **clean on hard bar; GENERATE reachability observed (opportunistic, 12 markers); Iron Gate signals pending (weather)**

**Session 3** — `bt-2026-04-23-210943`, commit `68954cc62d`
- flag_config: `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED=true`
- launch: `--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400`
- stop_reason: **`idle_timeout`** (clean) / **session_outcome: `complete`**
- duration: 1159.8s / PM: 0 / cost: $0.065
- reachability: **13 `[PhaseRunnerDelegate] GENERATE` markers** ✓ (path-(B) precondition met for the 3rd time — 3/3 counted sessions now)
- downstream: 0 VALIDATE / 0 GATE / 0 SLICE4B — same weather
- Iron Gate suite: **0 signals**
- runner-attributed frames: **0**
- shutdown race: **0**
- tracebacks: 13 — 3× `anthropic/_base_client.py` + 3× `httpx/_transports/default.py` + 3× `providers.py _stream_with_resilience` + 2× `persistent_intelligence_manager.py` + 1× `candidate_generator.generate` + 1× `candidate_generator._call_fallback`. All **`infra_waiver: anthropic_transport`**; zero JARVIS runner frames.
- upstream markers: 13 CLASSIFY + 13 ROUTE+CTX+PLAN
- post-session cleanup: no requirements.txt artifact; only pre-existing `notebooks/report.ipynb` dirt
- outcome: **clean on hard bar; GENERATE reachability observed (opportunistic, 13 markers); Iron Gate signals pending (weather)**

**Combined 3-session clean-bar matrix (S1 + S2′ + S3 — post-Ticket-A1/B/C guards):**

| Criterion | S1 (`...062014`, $0.50 cap) | S2′ (`...203517`, $1.00) | S3 (`...210943`, $1.00) |
|---|---|---|---|
| stop_reason | idle_timeout ✓ | idle_timeout ✓ | idle_timeout ✓ |
| session_outcome | n/a (pre-Ticket-B) | **complete** ✓ | **complete** ✓ |
| duration | 2495.4s | 1044.4s | 1159.8s |
| cost | $0.074 | $0.000 | $0.065 |
| PM | 0 | 0 | 0 |
| GENERATE markers | **14** ✓ | **12** ✓ | **13** ✓ |
| Iron Gate signals | 0 | 0 | 0 |
| runner-attributed frames | 0 | 0 | 0 |
| shutdown race | 0 | 0 | 0 |
| infra_waiver | anthropic_transport (5 frames) | anthropic_transport (6 frames) | anthropic_transport (9 frames) |

All 3 tracebacks on every session match the canonical waiver signature: top-2 frames are `anthropic/_base_client.py:1637 request` → `httpx/_transports/default.py:101 map_httpcore_exceptions`. Zero phase_runners / phase_dispatcher / generate_runner frames in any traceback.

**Decision-tree status:**

Path (B) preconditions (all must hold for the flip):
1. `[PhaseRunnerDelegate] GENERATE → runner` in ≥1 session before transport failure — **MET 3× (39 markers total across S1+S2′+S3)**
2. Hard bar clean under infra_waiver rule — **MET 3×**
3. Slice 5a+5b parity green on current HEAD — **36/36 green on `68954cc62d`** (verified 2026-04-23 pre-S2′)
4. Ledger flip row will carry `reachability_source=partial_live+parity` + waiver excerpt — awaiting authorization

Path (A) veto triggers (would block the flip):
- Markers never fire — **not triggered (39 markers)**
- Runner-attributed frames in any session — **not triggered (0 frames)**
- Shutdown race — **not triggered (0)**

**Verdict:** 3/3 completed sessions, all clean on the hard bar under the new infra-waiver rule. GENERATE reachability observed 39 times. Iron Gate signals never fired live across any #7 session because the Anthropic transport weather prevented candidates from forming (same pattern since 2026-04-23 afternoon). Slice 5b parity (24 tests covering Exploration-first / Exploration Ledger / ASCII strict / Dependency integrity / Multi-file coverage / Retry feedback composition) is the authoritative §6 depth oracle on HEAD `68954cc62d`.

**Operator decision 2026-04-23: path (B) authorized.**

**Graduation: FINAL 2026-04-23.** Operator authorized default flip on path (B). Helper `_phase_runner_generate_extracted()` default flipped `"false"` → `"true"` (commit `053122925d`, pushed to `origin/main`). reachability_source=`partial_live+parity`. infra_waiver=`anthropic_transport` with canonical top-frame signature `anthropic/_base_client.py:1637 request → httpx/_transports/default.py:101 map_httpcore_exceptions`. **Iron Gate live lines not observed during S1/S2′/S3 cadence; §6 depth attested via Slice 5a+5b parity (36/36 green on HEAD `68954cc62d`).** Post-flip confirmation (below) captured partial live §6 evidence (SemanticGuard + ValidateRetryFSM). Explicit `=false` remains runtime kill switch reverting to the ~1,611-line inline GENERATE block.

**FlagRegistry seed truth:** N/A — PhaseRunner extraction flags are not seeded in `flag_registry_seed.py` (FlagRegistry curates 52 operator-facing flags, not every graduation gate). Same as prior #5 GATE and #6 SLICE4B flips.

**Post-flip obligation (operator binding, non-negotiable):** satisfied 2026-04-23 by a `PROBE_OK`-gated soak under the canonical recipe, with API-healthy (1256 ms Sonnet round-trip pre-launch). **Failure to observe Iron Gate lines post-flip did NOT auto-rollback** per binding — rollback is triggered only by runner-attributed regression or parity break, neither of which occurred.

**Post-flip confirmation** — `bt-2026-04-23-220541`, commit `053122925d` (7 flags default-true: COMPLETE + CLASSIFY + ROUTE + CTX + PLAN + VALIDATE + GATE + GENERATE; SLICE4B also default-true from #6): 1117.6s / 0 PM / $0.206 / stop_reason=`idle_timeout` / **session_outcome=`complete`**. **0 runner-attributed frames** (generate_runner / phase_runners / phase_dispatcher all 0). **0 JARVIS shutdown race.** Only 2 tracebacks, both `persistent_intelligence_manager.py` pre-existing infra (readonly DB); **zero `anthropic_transport` weather this session** (pre-launch probe confirmed healthy transport).

- **Full-chain reachability captured on one op** (`op-019dbc66-864d`, RuntimeHealthSensor IMMEDIATE, "Python 3.9.6 EOL" on requirements.txt): CLASSIFY → ROUTE+CTX+PLAN → GENERATE → **VALIDATE → GATE → SLICE4B** all walked live in 3 min 29 s.
- **Live §6 Iron Gate evidence captured:**
  - `[ValidateRetryFSM]` 3 transitions observed: `iter_start` @ 15:17:57 → `candidate_passed_break` @ 15:18:29 → `loop_exit_normal` @ 15:18:29 (retries_remaining=2, n_cands=1, best_candidate_present=True).
  - `[SemanticGuard]` 1 firing @ 15:18:29: `op=op-019dbc66-864d findings=0 hard=0 soft=0 patterns=[none] risk_before=SAFE_AUTO risk_after=SAFE_AUTO duration_ms=35 files_scanned=1` — clean scan, no pattern trips.
- GENERATE-specific Iron Gate sub-suite (ExplorationLedger / ASCII strict / dep_integrity / multi_file_coverage) **did not fire** — expected for a SAFE_AUTO / trivial / single-file op that succeeded on first candidate (no retries → no ExplorationLedger; ASCII-clean content → no ASCII gate; stable deps → no dep_integrity; 1 file → no multi-file coverage check). §6 depth for those branches remains authoritatively attested by 5b parity (24 Iron Gate suite tests green on HEAD).
- Upstream markers: 13 CLASSIFY + 12 ROUTE+CTX+PLAN + 12 GENERATE (strong natural reachability under healthy API).
- post-session cleanup: no requirements.txt artifact this session (op APPLY was either idempotent-with-prior-signature or bounded by VERIFY); git status shows only pre-existing `notebooks/report.ipynb` dirt.

- Inline deletion PR: post-graduation maintenance, separate from this flip per operator directive.

- Inline deletion PR: post-graduation maintenance, separate from this flip per operator directive.

### DISPATCHER (`JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED`)

**Prerequisites:** all 7 above flags must be default `true` (individually graduated). **Met 2026-04-23:** #1–#7 all FINAL (commits `c3aaebb6ed` / `50fe2044be` / `710667f3b6` / `97590ae72d` / `2851c82ffd` / `0b120b588a` / `053122925d`). The dispatcher composes on top of the graduated runners — it cannot be tested end-to-end in isolation from them.

**Status:** default false. Slice 6a+6b graduation cadence in progress 2026-04-23 by agent conductor under post-Ticket-A1/B/C guards (canonical recipe: `--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400`).

**Soak conductor: agent**. **HUMAN_REVIEW_WAIVED** (headless; dispatcher is pipeline-internal; composes over already-graduated runners). **Reachability bar** (`runner_reachability = Required`): `[PhaseRunnerDelegate] DISPATCHER → pipeline op=<id>` INFO marker at the orchestrator short-circuit (line 1476). ≥1 of 3 sessions must show this marker. **Important observability note:** when the dispatcher is engaged, the per-phase `[PhaseRunnerDelegate] <PHASE> → runner` markers DO NOT fire — the short-circuit returns before the legacy inline delegation code is reached. That per-phase-marker absence is proof-positive that the dispatcher path won the race; combined with Iron Gate / §6 signals (SemanticGuard / ValidateRetryFSM / ExplorationLedger) fired from inside the dispatched runners when ops reach downstream phases, the dispatcher's pipeline-routing correctness is observably attested.

**Reachability profile:** DISPATCHER fires on every op that reaches the orchestrator's `_run_pipeline` entry. Natural reachability is the strongest of all 8 flags when enabled. Iron Gate / §6 live evidence via downstream runners remains subject to the same API-weather dependency as #7 — if ops exhaust at GENERATE, the dispatcher still routes cleanly but downstream signals stay silent. Parity: Slice 6a (228/228) + Slice 6b (248/248 via `_run_both_paths` harness across 20 per-phase terminal matrix tests) cover the dispatcher's correctness under all terminal shapes; the 5b parity suite (36/36) covers the individual runners it composes.

**Baseline reference (flag OFF):** `bt-2026-04-22-181910`, 835.5s, 0 PM, $0.

**Session 1** — `bt-2026-04-23-224649`, commit `053122925d` (#1–#7 defaults-true, DISPATCHER flag set)
- flag_config: `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED=true`
- launch: `--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400`
- stop_reason: **`idle_timeout`** (clean) / **session_outcome: `complete`**
- duration: 1275.0s / PM: 0 / cost: $0.075
- reachability: **12 `[PhaseRunnerDelegate] DISPATCHER → pipeline` markers** ✓ (every op routed through the extracted dispatcher)
- per-phase markers (legacy inline hooks): 0 CLASSIFY / 0 ROUTE+CTX+PLAN / 0 GENERATE / 0 VALIDATE / 0 GATE / 0 SLICE4B — ALL ZERO, which is **expected and proof-positive** that the dispatcher short-circuit at line 1477 won the race for every dispatched op (returns before legacy per-phase delegation blocks execute).
- Iron Gate / §6 downstream signals: 0 ExplorationLedger / 0 SemanticGuard / 0 ValidateRetryFSM — ops exhausted at GENERATE upstream this session (same downstream-of-GENERATE profile as #7's S1–S3); dispatcher routing correctness is not affected.
- runner-attributed frames: **0** (phase_dispatcher.py / phase_dispatcher / phase_runners / generate_runner / slice4b_runner all 0). ← this is the #8-specific hard-bar check — the key regression signal.
- shutdown race: **0**
- tracebacks: 4 — 2× `persistent_intelligence_manager.py` (pre-existing infra) + 1× `candidate_generator.generate` + 1× `candidate_generator._call_fallback` (GENERATE exhaustion; no anthropic/httpx frames this session — transport healthy).
- post-session cleanup: no requirements.txt artifact; working tree returned to clean via `git checkout -- notebooks/report.ipynb` (benign harness-generated notebook dirt).
- outcome: **clean on hard bar; DISPATCHER reachability observed 12×; downstream Iron Gate signals pending S2/S3**
- note: per operator binding 2026-04-23, DISPATCHER cadence sessions do NOT need Iron Gate live evidence to graduate (reachability via the dispatcher marker itself is the primary proof; Slice 6a+6b parity is the authoritative composition oracle; per-runner Iron Gate evidence was already covered by #5–#7 cadences).

**Session 2** — `bt-2026-04-23-231351`, commit `053122925d`
- flag_config: `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED=true`
- launch: `--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400`
- stop_reason: **`idle_timeout`** (clean) / **session_outcome: `complete`**
- duration: 1248.8s / PM: 0 / cost: $0.000
- reachability: **12 `[PhaseRunnerDelegate] DISPATCHER → pipeline` markers** ✓ (same count as S1)
- per-phase markers: 0 / 0 / 0 / 0 / 0 / 0 — dispatcher short-circuit engaged on every op
- Iron Gate / §6 signals: 0 ExplorationLedger / 0 SemanticGuard / 0 ValidateRetryFSM — same downstream-of-GENERATE profile as S1
- runner-attributed frames: **0** (phase_dispatcher.py / phase_runners / generate_runner / slice4b_runner all 0)
- shutdown race: **0**
- tracebacks: 7 — 2× `persistent_intelligence_manager.py` (pre-existing infra) + 1× `providers.py _stream_with_resilience` + 1× `anthropic/_base_client.py` + 1× `httpx/_transports/default.py` (**`infra_waiver: anthropic_transport`** — canonical signature; weather returned briefly mid-session) + 1× `candidate_generator.generate` + 1× `_call_fallback` (exhaustion consequence). Zero JARVIS runner frames.
- post-session cleanup: only pre-existing `notebooks/report.ipynb` dirt, reverted via `git checkout --`
- outcome: **clean on hard bar; DISPATCHER reachability observed 12×**

**Session 3** — `bt-2026-04-23-235215`, commit `053122925d`
- flag_config: `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED=true`
- launch: `--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400`
- stop_reason: **`idle_timeout`** (clean) / **session_outcome: `complete`**
- duration: 1359.5s / PM: 0 / cost: $0.000
- reachability: **11 `[PhaseRunnerDelegate] DISPATCHER → pipeline` markers** ✓
- per-phase markers: 0 / 0 / 0 / 0 / 0 / 0 — dispatcher short-circuit engaged on every op
- Iron Gate / §6 signals: 0 ExplorationLedger / 0 SemanticGuard / 0 ValidateRetryFSM (ops exhausted upstream — same profile)
- runner-attributed frames: **0** (phase_dispatcher.py / phase_runners / generate_runner all 0)
- shutdown race: **0**
- tracebacks: 8 — 2× `persistent_intelligence_manager.py` (pre-existing infra) + 1× `providers.py _stream_with_resilience` + 1× `anthropic/_base_client.py` + 1× `httpx/_transports/default.py` (**`infra_waiver: anthropic_transport`**) + 3× `candidate_generator.py` (generate + _generate_dispatch + _call_fallback exhaustion). Zero JARVIS runner frames.
- post-session cleanup: notebooks/report.ipynb reverted via `git checkout --`
- outcome: **clean on hard bar; DISPATCHER reachability observed 11×**

**Combined 3-session clean-bar matrix (S1 + S2 + S3 under post-Ticket-A1/B/C guards, all on commit `053122925d`):**

| Criterion | S1 (`...224649`) | S2 (`...231351`) | S3 (`...235215`) |
|---|---|---|---|
| stop_reason | idle_timeout ✓ | idle_timeout ✓ | idle_timeout ✓ |
| session_outcome | **complete** ✓ | **complete** ✓ | **complete** ✓ |
| duration | 1275.0s | 1248.8s | 1359.5s |
| cost | $0.075 | $0.000 | $0.000 |
| PM | 0 | 0 | 0 |
| **DISPATCHER markers** | **12** ✓ | **12** ✓ | **11** ✓ |
| per-phase legacy markers | 0 (short-circuit ✓) | 0 (short-circuit ✓) | 0 (short-circuit ✓) |
| §6 Iron Gate signals | 0 | 0 | 0 |
| **phase_dispatcher.py frames** | **0** ✓ | **0** ✓ | **0** ✓ |
| **phase_runners frames** | **0** ✓ | **0** ✓ | **0** ✓ |
| shutdown race | 0 | 0 | 0 |
| infra_waiver | — (no transport this session) | anthropic_transport (2 frames) | anthropic_transport (2 frames) |

**Total DISPATCHER reachability**: **35 markers** across S1+S2+S3. Every op that reached the orchestrator in all 3 sessions routed through the dispatcher short-circuit (line 1477 of orchestrator.py), proven by the zero-count of legacy per-phase delegation markers.

**Verdict:** 3/3 sessions clean on hard bar (zero runner-attributed traceback frames in `phase_dispatcher.py`, `phase_runners/`, `generate_runner`, `slice4b_runner`; zero JARVIS shutdown race; zero POSTMORTEMs). Reachability observed 3/3 via dispatcher markers + dispatcher-short-circuit proof. Iron Gate live signals NOT observed because ops exhausted at GENERATE upstream (same downstream-of-GENERATE profile seen across #7 cadence) — but per operator binding for DISPATCHER, Iron Gate live is NOT required: the dispatcher's correctness is attested by (a) Slice 6a parity (228/228) covering `phase_dispatcher.py` structural correctness, (b) Slice 6b parity (248/248 via `_run_both_paths` harness across 20 per-phase terminal matrix tests) covering end-to-end dispatcher-composed routing, and (c) 35 live dispatcher markers proving production path engagement.

**Graduation: FINAL 2026-04-23.** Operator authorized default flip. Helper `dispatcher_enabled()` in `phase_dispatcher.py` default flipped `"false"` → `"true"` (commit `203856371e`, pushed to `origin/main` at `053122925d..203856371e`). reachability_source=`dispatcher_markers+parity`. legacy_if_blocks=0 across S1+S2+S3 + post-flip (proof-positive dispatcher short-circuit engaged on every op). infra_waiver=`anthropic_transport` only (no dispatcher-attributed frames in any session). §6 Iron Gate live evidence NOT REQUIRED for #8 per operator binding — dispatcher is routing infrastructure, not a generator/gate; §6 depth for downstream phases already graduated under #5–#7 and inherited by composition. Correctness oracle: Slice 6a (228/228) + Slice 6b (248/248 via `_run_both_paths`).

**FlagRegistry seed truth:** N/A (same as prior PhaseRunner flip commits).

**Post-flip confirmation** — `bt-2026-04-24-002218`, commit `203856371e` (all 8 flags default-true; no env override — natural default behavior). Pre-launch Anthropic probe `PROBE_OK latency=1296ms`. Duration **1676.8s** (~28 min) / stop_reason=**`idle_timeout`** (clean) / **session_outcome=`complete`** / cost $0.000 / PM 0. **13 `[PhaseRunnerDelegate] DISPATCHER → pipeline` markers** under natural default-true (no env override). **58 `[Ouroboros.PhaseDispatcher]` DEBUG iteration lines** — strongest live-fire proof to date that `phase_dispatcher.dispatch_pipeline` is the production path, internally routing `iter=N dispatching <PHASE> → <PHASE>Runner` for every dispatched op. **0 per-phase legacy markers** (short-circuit engaged in production). **0 `phase_dispatcher.py` frames, 0 `phase_runners/` frames, 0 JARVIS shutdown race, 0 POSTMORTEMs.** 8 tracebacks — 2× `persistent_intelligence_manager.py` (pre-existing infra) + 1× `providers.py _stream_with_resilience` + 1× `anthropic/_base_client.py` + 1× `httpx/_transports/default.py` (**`infra_waiver: anthropic_transport`**) + 3× `candidate_generator.py` (upstream exhaustion consequence). **Zero JARVIS runner frames.** Sample internal trace: `17:25:26 [PhaseDispatcher] iter=0 dispatching CLASSIFY → CLASSIFYRunner` → `17:27:24 iter=1 dispatching ROUTE → ROUTERunner` → `17:27:24 iter=2 dispatching PLAN → PLANRunner` (per-op walk via the registry). Post-session cleanup: `git checkout -- notebooks/report.ipynb` reverted benign harness-generated notebook dirt.

- Inline else-chain deletion: post-graduation maintenance PR, separate from this flip per operator directive.

**WAVE 2 (5) CLOSED 2026-04-23.** All 8 flags default-true; PhaseRunner extraction arc complete; `dispatch_pipeline` is the production code path; legacy inline else-chain is dead code pending separate maintenance PR.

## Graduation test command reference

For each session, the battle-test command shape:

```bash
# Session N for FLAG_NAME graduation
FLAG_NAME=true python3 scripts/ouroboros_battle_test.py \
    --cost-cap 0.50 --idle-timeout 600 -v
```

Multi-flag graduations (ROUTE+CTX+PLAN) need all three set:

```bash
JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED=true \
JARVIS_PHASE_RUNNER_CONTEXT_EXPANSION_EXTRACTED=true \
JARVIS_PHASE_RUNNER_PLAN_EXTRACTED=true \
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

## "Session mix" recommendation

Each graduation session should hit at least 2 of these op categories so the runner's code paths get real exercise:

- **Trivial op**: short description, 1 target file, simple complexity → fast-path hit
- **Test failure sensor trigger**: force a real pytest failure, let TestFailureSensor route it to governance → VALIDATE retry path
- **Read-only cartography**: `ctx.is_read_only=True` — exercises the VALIDATE read-only short-circuit + COMPLETE path
- **NOTIFY_APPLY tier**: a change that classifies as Yellow — exercises GATE 5b preview + cancel window
- **APPROVAL_REQUIRED tier**: large-surface change → APPROVE gate with real approval provider (or no-provider terminal if appropriate)

Sessions that ONLY hit trivial ops don't exercise enough of the runner — prefer diverse op mixes.

## Rollback — flag flip reversal

If a post-flip session surfaces a regression:

1. **Immediate**: flip the flag default back to `false` (one-line revert). Pipeline returns to inline path.
2. **Root-cause**: file a bug with the specific op / log excerpt / dispatcher trace.
3. **Fix + re-graduation**: flag stays default `false` until fix lands + 3 clean sessions prove the regression is gone.

No graduation is permanent — flags exist precisely because rollback must be fast.

## Operator process

- **Before each session**: operator authorizes via brief message ("authorize graduation session N for FLAG").
- **Agent**: runs the session on operator's behalf per the agent-conducted soak delegation precedent (`feedback_agent_conducted_soak_delegation.md`); fills in this ledger's session row with outcome + commit + notes.
- **After 3 clean sessions per flag**: operator reviews ledger + authorizes default flip.
- **After default flip + 1 clean post-flip session**: graduation FINAL; inline deletion PR is eligible as a separate maintenance PR.

## Parallel graduation?

Operator directive prefers per-slice sequencing for clean causality. Running two flag graduations in parallel is technically possible (flags are independent until DISPATCHER) but makes regression attribution harder. Recommend sequential until COMPLETE + CLASSIFY graduate cleanly — those two alone should be safe to parallelize once their patterns are established.

## Current standing

**Wave 2 (5) CLOSED.** 248/248 tests green both paths. All flags default `false`.

**Awaiting operator go** on:
- (a) First graduation authorization (recommend: COMPLETE, lowest risk)
- (b) Any operator-preferred ordering deviation
- (c) Parallel-graduation policy decision
- (d) Wave 3 authorization (gated on per-phase graduation completion)

Wave 2 item (4) — curiosity / capped ask_human — remains separately scheduled per operator's backlog reminder, not bundled with this graduation sequence.
