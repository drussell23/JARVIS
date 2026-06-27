---
title: Project Move 3 Closure
modules: []
status: merged
source: project_move_3_closure.md
---

**Closed 2026-04-30.** Move 3 of the §27 v6 brutal-review autonomy
roadmap — closes the "missing teeth" gap diagnosed by §27.4.3.

**Why:** Pass C surfaces emit *proposals*, verification emits
*pass/fail per claim*, Priority 1's confidence-collapse pipeline
emits *verdicts*. None of those signals auto-modify the NEXT op
when a recent claim failed. This router is the missing teeth — a
thin advisory primitive that consumes those existing signals and
produces an explicit ``AdvisoryAction`` proposal on every terminal
postmortem.

**How to apply:** The router is graduated into shadow mode —
``JARVIS_AUTO_ACTION_ROUTER_ENABLED`` defaults true, advisory
proposals land in `.jarvis/auto_action_proposals.jsonl` plus an
SSE event on every actionable proposal. Operators review via the
``/auto-action`` REPL command (recent / stats / `<op_id>` filter)
or the ``GET /observability/auto-action[/stats]`` endpoints. The
mutation boundary (``JARVIS_AUTO_ACTION_ENFORCE``) stays locked
off until separate later authorization.

## What graduated (Slice 4)

`JARVIS_AUTO_ACTION_ROUTER_ENABLED` flipped false → true.
Asymmetric env semantics: empty/unset = default-on; explicit falsy
hot-reverts. `JARVIS_AUTO_ACTION_ENFORCE` stays default-false.

## The 4-slice arc

| Slice | Commit | Tests | Net |
|---|---|---|---|
| 1 — Primitive | `18a90afe0c` | 25 | New module ~470 lines, 5-value enum, frozen dataclasses, dispatcher decision tree, cost-contract guard |
| 2 — Signal readers | `1a806a10ba` | 18 | 3 readers + `gather_context` consuming existing ledgers (postmortem, adaptation, plus stub for Slice 3 confidence verdicts) |
| 3 — Shadow integration | `1e2e46afbd` | 31 | VerdictRingBuffer + ProposalLedger + PostPostmortemObserver + AutoActionShadowObserver + 2 producer wirings (postmortem_observability, confidence_observability) |
| 4 — Operator surfaces + graduation | this commit | 23 | SSE event + `/observability/auto-action` GET routes + `/auto-action` REPL + master flag flip |

**Total: 4 commits, 97 new regression tests, ~2,600 net new lines (module + tests + integrations + docs).**

## Architecture overview

```
producers                          consumers
─────────                          ─────────
postmortem_observability ────┐
  publish_terminal_postmortem │
                              ▼
confidence_observability ─►  AutoActionShadowObserver
  publish_confidence_drop      ├─ gather_context()
  publish_confidence_approach  │     ├─ recent_postmortem_outcomes()
                               │     ├─ recent_confidence_verdicts()
                               │     │     ↑ _VerdictRingBuffer
                               │     └─ recent_adaptation_proposals()
                               ├─ propose_advisory_action()
                               │     └─ AdvisoryActionType (5-value enum)
                               ├─ AutoActionProposalLedger.append()
                               │     └─ .jarvis/auto_action_proposals.jsonl
                               └─ publish_auto_action_proposal_emitted()
                                     └─ SSE EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED
                                                    │
                                                    ▼
                          ┌─────────────────────────┴──────────────────┐
                          │                                            │
                          ▼                                            ▼
                  /auto-action REPL                      GET /observability/auto-action
                  /auto-action stats                     GET /observability/auto-action/stats
                  /auto-action <op_id>
```

## Cost contract preservation (PRD §26.6, structurally un-bypassable)

`_propose_action` raises `CostContractViolation` if
`current_route in {background, speculative}` AND
`proposed_risk_tier in {approval_required, blocked}` (the tiers
that today imply re-routing through STANDARD/IMMEDIATE / higher-cost
provider paths). AST-pinned + behaviorally tested. None of the 5
current action types directly carry a route field, so the guard is
naturally satisfied — but encoding it structurally future-proofs
against later additions.

## Authority invariants

* Module imports only stdlib + `cost_contract_assertion` + (Slice 4)
  `aiohttp.web` for response construction. NO orchestrator,
  candidate_generator, providers, urgency_router, iron_gate,
  change_engine, policy, semantic_guardian, semantic_firewall,
  doubleword_provider, or phase_runners imports — AST-pinned.
* All bridges (postmortem_observability, confidence_observability)
  use lazy imports inside try/except so the producer module is safe
  to use without the auto_action_router module installed.
* Observer hook NEVER propagates exceptions into the publish path
  (cost-contract violations excepted — fatal-by-design).

## Mutation boundary still locked

`JARVIS_AUTO_ACTION_ENFORCE` remains default-false. Even with
master flag on, the orchestrator never modifies `ctx` based on
advisory proposals. Future arc graduates enforce mode separately
after operator review of shadow ledger evidence.

## Operator binding (J.A.R.M.A.T.R.I.X.)

5-value enum is the entire decision space. `NO_ACTION` is an
EXPLICIT happy-path return value — never None, never an implicit
fall-through. Every input maps to exactly one of:
NO_ACTION / DEFER_OP_FAMILY / DEMOTE_RISK_TIER /
ROUTE_TO_NOTIFY_APPLY / RAISE_EXPLORATION_FLOOR.

## Knobs

* `JARVIS_AUTO_ACTION_ROUTER_ENABLED` — master flag, **graduated true**
* `JARVIS_AUTO_ACTION_ENFORCE` — mutation boundary, **locked false**
* `JARVIS_AUTO_ACTION_HISTORY_K` — history window (default 8, floor 2)
* `JARVIS_AUTO_ACTION_FAILURE_RATE_TRIP` — family failure trip (0.5)
* `JARVIS_AUTO_ACTION_ESCALATE_VERDICT_TRIP` — confidence escalate trip (0.5)
* `JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN` — ring buffer (32, floor 8)
* `JARVIS_AUTO_ACTION_LEDGER_PATH` — JSONL path override

## What remains (NOT in this arc)

* **Enforce-mode graduation** — separate later authorization. The
  orchestrator's hook seam to actually mutate `ctx` based on advisory
  proposals (defer op family, demote risk tier, etc.) is a future
  arc that requires reviewing shadow-mode evidence first.
* **op_family inference at the orchestrator hook** — Slice 3's
  per-op ctx registry is wired but the orchestrator doesn't yet
  call `register_op_context` at op-start. The dispatcher's family
  filter is dormant until that wiring lands.

## Net trajectory after Move 3

§27 grade table — Self-tightening immunity dimension lifts from
A− toward A. The verification → action loop now closes; the only
remaining gap to full A is the enforce-mode mutation boundary,
which is gated on shadow-mode soak evidence.
