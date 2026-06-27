---
title: Project W2 4 Curiosity Scope
modules: [backend/core/ouroboros/governance/curiosity_engine.py, backend/core/ouroboros/governance/tool_executor.py, docs/operations/]
status: merged
source: project_w2_4_curiosity_scope.md
---

## Status

- **Operator-authorized 2026-04-25** post-Seed-Arc closure. Quote: "let's work on the next thing on our list" — W2(4) is next per the noted slate after Harness Epic + Seed Arc + Path 3 follow-up.
- **Prerequisites met**: W2(5) PhaseRunner extraction CLOSED (2026-04-23, 8 graduated flags). Wave 1 #1 DirectionInferrer + StrategicPosture CLOSED (2026-04-21).
- **Standing orders**: thin slices, default-off, hot-revert paths, no live-fire/battle until per-slice operator authorization. Same discipline as W3(7) and the harness epic.

## The rooted problem (one paragraph)

Today's `ask_human` Venom tool is gated by `NOTIFY_APPLY+` risk tier (`tool_executor.py:2328` Rule 14). The model can only ask for human input on Yellow/Orange ops where it's already paused for human review. This means **the model never asks clarifying questions during exploratory work** (Green ops, multi-file investigation) — exactly when human input would have the highest leverage. Operators see ops fail mid-VERIFY because the model misunderstood ambiguous requirements that one clarifying question could have resolved. W2(4) widens `ask_human` to fire **proactively during exploration** when posture is EXPLORE or CONSOLIDATE, gated by per-session budget cap and authority-preserving controls.

## Goals (priority order)

1. **Enable proactive curiosity during exploration**: model can use `ask_human` in CLASSIFY / CONTEXT_EXPANSION / GENERATE phases on Green-tier ops when posture allows.
2. **Per-session budget cap**: hard ceiling on questions per session (default 3). Hard ceiling on total cost burn (default $0.05 per question — the model's "should I ask?" decision call). Operators see exactly what was spent in summary.json.
3. **Posture gating**: questions only fire when DirectionInferrer reports EXPLORE or CONSOLIDATE — HARDEN posture suppresses curiosity (focus on stabilization, not new questions).
4. **Authority preserved**: `ask_human` stays authority-free (it's already a question/answer surface, not a mutation). The widening is purely "when does it fire", not "what can it do". The Iron Gate, risk-tier-floor, semantic-firewall surfaces are untouched.
5. **Observability**: every curiosity question persisted to session state + emitted as SSE event + readable via `/observability/cancels`-style GET endpoint. Per Manifesto §8.

## Non-goals

- **Auto-answering questions**: human-in-the-loop only. No "auto-answer from session history" shortcut — that's a different feature.
- **Cross-session learning**: questions don't persist across sessions in a queryable form (the per-session JSONL is enough for postmortem). UserPreferenceMemory may incidentally capture answers, but that's its own surface.
- **Replacing existing NOTIFY_APPLY+ ask_human path**: the existing Yellow/Orange path stays exactly as-is. W2(4) adds a *parallel* low-risk-tier path; it doesn't replace.
- **Voice or modal UX**: text questions only. Voice integration is a future concern.
- **Curiosity outside Venom tool loop**: no new "curiosity sensor" or "curiosity sub-agent". The model decides via its existing tool-use mechanism whether to invoke `ask_human`.
- **Implementation in this scope doc**: operator picks slices, then implementation begins.

## Authority posture (per Manifesto)

- **§1 additive only** — `ask_human` already exists as authority-free; this widens *when/where* it fires, not *what it can do*.
- **§5 Tier −1** — model-generated question text is persisted to session state; must pass Semantic Firewall sanitization (credential patterns, prompt-injection patterns) before persist + before emission to operator.
- **§6 Iron Gate unchanged** — exploration ledger, ASCII strict, dependency integrity, multi-file coverage all unchanged. Curiosity questions are not gated by Iron Gate (they're not patches).
- **§7 Approval surface untouched** — orange-PR / NOTIFY_APPLY paths unchanged.
- **§8 Observability** — every question emits a structured log line + persisted record + SSE event.

## Slice plan (4 slices, mirrors W3(7) and harness epic patterns)

### Slice 1 — Curiosity primitive + per-session budget tracker

**Module**: `backend/core/ouroboros/governance/curiosity_engine.py` (new)

**Primitive**: `CuriosityBudget` class
- Per-op tracking: questions_asked, cost_burn_usd, posture_at_arm
- `try_charge(question_text, est_cost_usd, posture)` → returns `Allowed | Denied(reason)`
- Ledger: `.jarvis/curiosity_ledger.jsonl` — schema `curiosity.1`, additive, single-writer
- Helper: `current_curiosity_budget()` ContextVar pattern (parallel to W3(7) cancel_token)

**Env knobs**:
- `JARVIS_CURIOSITY_ENABLED` — master, default `false`
- `JARVIS_CURIOSITY_QUESTIONS_PER_SESSION` — default `3`
- `JARVIS_CURIOSITY_COST_CAP_USD` — default `0.05` (per question)
- `JARVIS_CURIOSITY_POSTURE_ALLOWLIST` — default `EXPLORE,CONSOLIDATE`

**Tests**: ~15 unit tests (budget tracker semantics, posture gate, env knobs, ledger write).

### Slice 2 — Tool-policy widening + Venom integration

**Module**: `backend/core/ouroboros/governance/tool_executor.py` Rule 14 modification

**Change**: when `JARVIS_CURIOSITY_ENABLED=true` AND posture in allowlist AND budget not exhausted, `ask_human` is allowed at SAFE_AUTO risk tier. Existing NOTIFY_APPLY+ path unchanged. Master-flag-off → byte-for-byte pre-W2(4).

**Cross-component hook test** (per wiring checklist): tool policy decision sees the contextvar; budget decrements on each successful invocation.

**Tests**: ~12 unit tests (policy gate composition, budget decrement, posture-disabled rejection, master-off no-regression).

### Slice 3 — Persistence + SSE + IDE GET endpoint

**Modules**:
- `curiosity_engine.py` — `_persist()` to `curiosity_ledger.jsonl`
- `ide_observability_stream.py` — new SSE event `curiosity_question_emitted` (12th in vocab, additive)
- `ide_observability.py` — new routes `/observability/curiosity` + `/observability/curiosity/{question_id}`
- `cancel_token.py`-style `bridge_curiosity_to_sse(record)` helper

**Tests**: ~15 (SSE vocabulary, GET routes, persistence schema, master-off no-publish).

### Slice 4 — Graduation pins + master flip

Per W3(7) Slice 7 + harness epic Slice 4 pattern:
- Master flag flip default `false → true` IF safe (likely yes — sub-flag default cap-3 means worst case is 3 questions per session, easily within session noise floor)
- Comprehensive graduation pin tests (~25)
- Hot-revert documentation in `docs/operations/`

## Integration points to confirm during Slice 1

The W2(5) PhaseRunner extraction means CLASSIFY / CONTEXT_EXPANSION / PLAN are now discrete runners. The curiosity hook lives in **GENERATE phase** (where Venom tool loop runs). The question of "where in CLASSIFY does curiosity hook in?" raised in the prior scope draft is now resolved: **it doesn't hook in during CLASSIFY** — the model only invokes `ask_human` during its own tool-loop turn (always GENERATE). Slice 2's policy widening + Slice 1's budget contextvar set in the GENERATE runner before tool loop starts.

## Env flag matrix (post-graduation defaults)

| Flag | Default | Purpose |
|---|---|---|
| `JARVIS_CURIOSITY_ENABLED` | TBD (Slice 4 decides) | Master |
| `JARVIS_CURIOSITY_QUESTIONS_PER_SESSION` | `3` | Per-session ceiling |
| `JARVIS_CURIOSITY_COST_CAP_USD` | `0.05` | Per-question cost cap |
| `JARVIS_CURIOSITY_POSTURE_ALLOWLIST` | `EXPLORE,CONSOLIDATE` | Posture gate |
| `JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED` | `true` (when master on) | JSONL artifact write |
| `JARVIS_CURIOSITY_SSE_ENABLED` | `false` | SSE event publish (operator opt-in) |

## Hot-revert recipe

Single env flip: `JARVIS_CURIOSITY_ENABLED=false` → all sub-flags force-disabled (mirrors W3(7) cancel master-off semantics) → byte-for-byte pre-W2(4). No code revert needed. Pinned by graduation pins (Slice 4).

## Risk + mitigation

| Risk | Likelihood | Mitigation |
|---|---|---|
| Model spams clarifying questions during high-noise sessions | Medium | Per-session 3-question hard cap |
| Question text leaks credentials | Low | Semantic Firewall sanitization (Tier −1) before persist + emit |
| Operator fatigue from too many questions | Medium | Posture gate suppresses HARDEN; per-session cap; SSE emit lets operator filter on `/observability/curiosity` |
| Budget tracker becomes a contention point in concurrent ops | Low | Per-op `CuriosityBudget` instance (no shared lock) — same pattern as `CancelToken` per-op registry |
| Curiosity questions block forward progress | Low | `ask_human` already has its own timeout in Venom tool loop; widening doesn't change that |

## Operator decision points (before slice authorization)

1. **Slice breakdown approval** — does the 4-slice plan match the right granularity? Or consolidate Slices 1+2 into a single "primitive + integration" slice?
2. **Default master state at Slice 4** — flip to `true` (mirroring W3(7) where sub-flag actuators stay default-off) or keep at `false` (cautious — operator opts in)?
3. **Posture allowlist default** — `EXPLORE,CONSOLIDATE` per the prior draft, or include `MAINTAIN` too? HARDEN excluded by design (suppresses curiosity during stabilization).
4. **Per-question cost cap** — $0.05 default per the prior draft. Tighter (~$0.02) since "should I ask?" is a short LLM call?
5. **Live-fire timing** — first live-fire after Slice 2 (when curiosity actually fires)? Or wait until Slice 4 (full epic graduated)?

## Cross-links

- `project_wave2_scope_draft.md` — original W2 scope from 2026-04-21 (W2(5) and W2(4) sketched together).
- `project_wave3_item7_mid_op_cancel_scope.md` — slice / graduation pattern reference.
- `project_harness_epic_scope.md` — second slice / graduation pattern reference.
- `feedback_orchestrator_wiring_invariant_checklist.md` — cross-component hook test contract Slice 2 must follow.
- `tool_executor.py:2328` — current Rule 14 ask_human gate (the widening site).
- Wave 1 #1 (DirectionInferrer + posture) — `posture_observer.py` reader.
