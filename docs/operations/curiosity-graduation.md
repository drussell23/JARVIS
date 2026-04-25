# W2(4) Curiosity Engine — Graduation + Hot-Revert Runbook

**Graduated**: 2026-04-25 (W2(4) Slice 4)
**Default**: `JARVIS_CURIOSITY_ENABLED=true`
**Source**: `backend/core/ouroboros/governance/curiosity_engine.py`
**Pinned by**: `tests/governance/test_w2_4_graduation_pins_slice4.py`

---

## What this enables

Before W2(4), `ask_human` (Venom tool) was gated by `NOTIFY_APPLY+` risk
tier (`tool_executor.py` Rule 14) — the model could only ask clarifying
questions on Yellow/Orange ops where it was already paused for human
review. After W2(4) graduation, the model can also ask clarifying
questions on Green (`SAFE_AUTO`) ops during exploratory work, gated by
**all four** of the following:

1. Master flag on (`JARVIS_CURIOSITY_ENABLED=true`, default).
2. A `CuriosityBudget` is bound to the ambient `ContextVar` (set by the
   GENERATE phase runner before the Venom tool loop starts).
3. Current strategic posture is in the allowlist (default
   `EXPLORE,CONSOLIDATE`; HARDEN/MAINTAIN excluded by design).
4. Per-session quota + per-question cost cap not yet exhausted (defaults:
   3 questions, $0.05 each).

Each question (allowed or denied) is persisted to
`.ouroboros/sessions/<session-id>/curiosity_ledger.jsonl` (schema
`curiosity.1`) and optionally surfaced via SSE
(`curiosity_question_emitted`) and the
`GET /observability/curiosity{,/<question_id>}` IDE routes.

---

## Hot-revert recipe (single env knob)

```bash
export JARVIS_CURIOSITY_ENABLED=false
```

That single flip force-disables every sub-flag (mirrors W3(7) cancel
master-off composition):

| Sub-flag                                       | Master-off behavior        |
|------------------------------------------------|----------------------------|
| `JARVIS_CURIOSITY_QUESTIONS_PER_SESSION`       | forced to `0`              |
| `JARVIS_CURIOSITY_COST_CAP_USD`                | forced to `0.0`            |
| `JARVIS_CURIOSITY_POSTURE_ALLOWLIST`           | forced to empty set        |
| `JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED`      | forced to `false`          |
| `JARVIS_CURIOSITY_SSE_ENABLED`                 | forced to `false`          |

`Rule 14` falls through to the legacy `tool.denied.ask_human_low_risk`
reject at SAFE_AUTO. Byte-for-byte pre-W2(4) behavior. No code revert
required, no service restart needed beyond reloading env (battle-test
harness restart suffices).

---

## Env knob reference

| Knob | Default | Purpose |
|---|---|---|
| `JARVIS_CURIOSITY_ENABLED` | `true` | **Master**. Single hot-revert env knob. |
| `JARVIS_CURIOSITY_QUESTIONS_PER_SESSION` | `3` | Per-session hard cap. |
| `JARVIS_CURIOSITY_COST_CAP_USD` | `0.05` | Per-question soft cap. |
| `JARVIS_CURIOSITY_POSTURE_ALLOWLIST` | `EXPLORE,CONSOLIDATE` | Posture gate. |
| `JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED` | `true` (when master on) | JSONL artifact write. |
| `JARVIS_CURIOSITY_SSE_ENABLED` | `false` | SSE event publish (operator opt-in). |

All `JARVIS_CURIOSITY_*` knobs gate on `if not curiosity_enabled():`
first — see `test_pin_master_off_composition_all_subflag_readers` for
the structural invariant.

---

## Authority preservation (preserved across all 4 slices)

- **§1 additive only** — `ask_human` was already authority-free; W2(4)
  widens *when* it can fire, not *what it can do*.
- **§5 Tier 0** — posture gate is deterministic code, no LLM call.
- **§6 Iron Gate** unchanged — exploration ledger, ASCII strict,
  dependency integrity, multi-file coverage all unchanged.
- **§7 Approval surface** untouched — orange-PR / NOTIFY_APPLY paths
  unchanged.
- **§8 Observability** — every question (allowed AND denied) writes a
  JSONL ledger record; optionally surfaces via SSE + IDE GET.
- **BLOCKED tier** still rejects ask_human regardless of master state
  (no gate softening — pinned by `test_rule_14_blocked_tier_rejected`).

---

## Operator-facing audit

Tail the per-session ledger to watch curiosity decisions in real time:

```bash
tail -f .ouroboros/sessions/<session-id>/curiosity_ledger.jsonl
```

Each record (schema `curiosity.1`):

```json
{
  "schema_version": "curiosity.1",
  "question_id": "<uuid>",
  "op_id": "<op-id>",
  "posture_at_charge": "EXPLORE" | "CONSOLIDATE" | ...,
  "question_text": "<model-supplied>",
  "est_cost_usd": 0.05,
  "issued_at_monotonic": <float>,
  "issued_at_iso": "<ISO-8601 UTC>",
  "result": "allowed" | "denied:master_off" | "denied:posture_disallowed" |
            "denied:questions_exhausted" | "denied:cost_exceeded" |
            "denied:invalid_question"
}
```

Or query via the IDE GET endpoint (loopback-only, rate-limited):

```bash
curl 'http://127.0.0.1:<port>/observability/curiosity?op_id=<op-id>&result=allowed'
curl 'http://127.0.0.1:<port>/observability/curiosity/<question_id>'
```

To opt into the live SSE event stream:

```bash
export JARVIS_CURIOSITY_SSE_ENABLED=true   # opt-in
export JARVIS_IDE_STREAM_ENABLED=true      # already default true
# Subscribe to /observability/stream — filter event_type=curiosity_question_emitted
```

---

## Graduation evidence

- 158/158 combined regression green pre-graduation (Slices 1+2+3 +
  cancel SSE Slice 6 + IDE observability + IDE stream).
- 17 graduation pins in `test_w2_4_graduation_pins_slice4.py` enforce
  the post-graduation contract on every commit.
- Live-fire smoke: `scripts/livefire_w2_4_curiosity.py` boots the
  primitive + Rule 14 + SSE bridge + JSONL persist end-to-end with
  default (master-on) env, asserts the full chain.

If any graduation pin breaks: either fix the regression, or update
both the pin AND this runbook (the master-off invariant is
non-negotiable per the operator binding).
