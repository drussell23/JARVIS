---
title: Display past session timeline (existing, untouched)
modules: [scripts/ouroboros_battle_test.py]
status: merged
source: project_phase_1_closure.md
---

**Phase 1 architecturally complete + empirically green 2026-04-28.**

PRD §24.10 Critical Path #1 (Determinism Substrate) — the foundation
for replayable RSI. Without deterministic entropy + clock + decision
capture, no decision can be replayed; bug reproduction is best-effort;
counterfactual analysis is impossible; Wang's Markov-chain RSI
convergence proof has no foundation.

## Closure summary

Eight slices merged in a single day after operator authorization:

| Slice | What | PR |
|---|---|---|
| 1.1 | Entropy + clock primitives | #29012 |
| 1.2 | DecisionRuntime + `decide()` integration | #29035 |
| 1.3 | phase_capture + ROUTE phase wired | #29074 |
| 1.3.a | CLASSIFY phase wired (`advisor_verdict`) | #29098 |
| 1.3.b | GENERATE phase wired (`provider_selection` digest) | #29099 |
| 1.3.c | GATE phase wired (`risk_tier_assignment`) | #29102 |
| 1.4 | Replay CLI (`--rerun <session-id>`) | #29097 |
| 1.5 | Graduation flip — CLOSES PHASE 1 | #29106 |

**Combined regression spine: 785/785 green** at closure (352/352 on the
Phase 1 sub-suite specifically).

## Layered architecture (no duplication)

| Layer | Owner | Module |
|---|---|---|
| Random + clock | Mine, Slice 1.1 | `determinism/entropy.py` + `determinism/clock.py` |
| Canonical hashing | Antigravity (parallel) | `observability/determinism_substrate.py` |
| Audit causal-trace ledger | Antigravity (parallel) | `observability/decision_trace_ledger.py` |
| Pure-function replay | Antigravity (parallel) | `observability/replay_harness.py` |
| Decision runtime + decide() | Mine, Slice 1.2 | `determinism/decision_runtime.py` |
| Production callsite wrapper | Mine, Slice 1.3 | `determinism/phase_capture.py` |
| Session-level replay CLI | Mine, Slice 1.4 | `determinism/session_replay.py` |

Antigravity's three modules are COMPLEMENTARY — different abstraction
levels of the same Phase 1 arc. My Slice 1.2 imports their
`canonical_serialize` + `canonical_hash` for cross-arch-stable
hashing, zero duplication.

## Four production decision sites wired

| Slice | Phase | Decision kind | Pattern |
|---|---|---|---|
| 1.3 | ROUTE | `route_assignment` | direct decide() — UrgencyRouter is deterministic, REPLAY can skip |
| 1.3.a | CLASSIFY | `advisor_verdict` | direct decide() — OperationAdvisor.advise is deterministic |
| 1.3.b | GENERATE | `provider_selection` | closure-over-generation — LLM call always runs live; closure captures digest |
| 1.3.c | GATE | `risk_tier_assignment` | closure-over-risk_tier — gate logic always runs; closure captures terminal verdict |

All four wirings have:
- Lazy import of `phase_capture` (defensive against module-level import failures)
- Defensive `try/except` with `logger.debug` (no flag-off noise)
- Source-level markers (`Slice 1.3.x`, `kind="..."`) so refactors that
  strip the wiring fail tests
- Adapter registration at module load (idempotent, defensive)

## Master flags graduated

All four default-true post-Slice-1.5 with hot-revert preserved:

```
JARVIS_DETERMINISM_ENTROPY_ENABLED        (default true)
JARVIS_DETERMINISM_CLOCK_ENABLED          (default true)
JARVIS_DETERMINISM_LEDGER_ENABLED         (default true)
JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED  (default true)
JARVIS_DETERMINISM_REPLAY_CLI_ENABLED     (default true — Slice 1.4 already on)
```

Asymmetric env semantics (mirrors Phase 12.2 Slice E):
- `unset / "" / whitespace` → True (graduated default, unset marker)
- `"1" / "true" / "yes" / "on"` → True (explicit opt-in)
- `"0" / "false" / "no" / "off"` → False (hot-revert)
- any other non-empty string → False (strict opt-in to non-default)

## CLI surface

```bash
# Display past session timeline (existing, untouched)
python3 scripts/ouroboros_battle_test.py --replay <session-id>

# Deterministic re-execution against recorded ledger (Slice 1.4)
python3 scripts/ouroboros_battle_test.py --rerun <session-id>
python3 scripts/ouroboros_battle_test.py --rerun <session-id> --rerun-mode verify
```

`--rerun` automatically:
1. Locates `.jarvis/determinism/<session-id>/seed.json`
2. Locates `.jarvis/determinism/<session-id>/decisions.jsonl`
3. Validates replay-readiness (fail-fast on missing state)
4. Applies all 7 env vars atomically
5. Boots the harness in REPLAY mode

## Storage layout

Per-session under `.jarvis/determinism/<session-id>/`:
- `seed.json` — session 64-bit seed (Slice 1.1)
- `decisions.jsonl` — append-only decision ledger (Slice 1.2)

Atomic temp+rename for seeds, fcntl-flock for decisions. Cross-process safe.

## Authority invariants pinned

- `determinism/` package NEVER imports orchestrator / phase_runner (base) / candidate_generator
- All public methods NEVER raise (except VERIFY-strict mode in decide)
- Phase wirings (`phase_runners/*_runner.py`) import `phase_capture` LAZILY
- Source-level pins on each phase wiring (`Slice 1.3.x` + `kind="..."` + `capture_phase_decision` markers)
- Adapter registration helpers wrapped in top-level try/except (defensive at import time)
- AST-walked invariant: heavy_probe / decision_runtime never call ledger mutators

## Coordination outcomes (Antigravity bundled commits)

Three Antigravity coordination events during Phase 1:

1. **Slice 1.2** — Antigravity shipped a parallel `decision_runtime.py`
   2 minutes before my commit. Convergent design (+17/-6 net diff).
   My contribution: off-by-one fix in `_peek_ordinal`.

2. **Slice 1.3** — Antigravity bundled my Slice 1.3 with their
   ConvergenceGovernor + ExplorationCalculus (Phase 3 prep).

3. **Slice 1.3.a** — Antigravity auto-committed my classify_runner.py
   changes; bundled with `test_convergence_governor.py`.

All resolved cleanly via PR descriptions documenting the bundling.
No actual code conflicts (different namespaces).

## What's next (deferred — operator-gated)

**Phase 2 (Closed-Loop Self-Verification)** — held per directive
2026-04-28: "Do not begin speccing Phase 2 until I explicitly
authorize the new arc." Wait for operator green-light.

**Slice 1.X cleanup** — held:
- Unify master flags under `JARVIS_DETERMINISM_ENABLED` umbrella
- Coordinate naming with Antigravity's `observability/determinism_substrate.py`
  (their narrowly-scoped hashing module shares the broader package name)

## Quick-reference flag inventory

```
# Phase 1 (graduated 2026-04-28)
JARVIS_DETERMINISM_ENTROPY_ENABLED            (default true)
JARVIS_DETERMINISM_CLOCK_ENABLED              (default true)
JARVIS_DETERMINISM_LEDGER_ENABLED             (default true)
JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED      (default true)
JARVIS_DETERMINISM_REPLAY_CLI_ENABLED         (default true)
JARVIS_DETERMINISM_LEDGER_MODE                (passthrough/record/replay/verify)
JARVIS_DETERMINISM_LEDGER_VERIFY_RAISES       (default false)
JARVIS_DETERMINISM_LEDGER_DIR                 (default .jarvis/determinism)
JARVIS_DETERMINISM_LEDGER_FLUSH_S             (default 1.0)
JARVIS_DETERMINISM_STATE_DIR                  (default .jarvis/determinism)
JARVIS_DETERMINISM_CLOCK_TRACE_MAX            (default 100000)
OUROBOROS_DETERMINISM_SEED                    (operator override; decimal or 0xHEX)
OUROBOROS_DETERMINISM_CLOCK_MODE              (passthrough/record/replay)
OUROBOROS_BATTLE_SESSION_ID                   (existing harness var)

# Antigravity's parallel flags (their own graduation slice)
JARVIS_DETERMINISM_SUBSTRATE_ENABLED          (their canonical hashing)
JARVIS_DECISION_TRACE_LEDGER_ENABLED          (their audit trail)
```
