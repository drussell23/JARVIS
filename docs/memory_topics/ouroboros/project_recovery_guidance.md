---
title: Operator flips the two flags
modules: [scripts/livefire_recovery.py, backend/core/ouroboros/governance/recovery_advisor.py, backend/core/ouroboros/governance/recovery_formatter.py, backend/core/ouroboros/governance/recovery_announcer.py, backend/core/ouroboros/governance/recovery_repl.py, backend/core/ouroboros/governance/recovery_store.py, backend/core/ouroboros/governance/comms/karen_voice.py, tests/governance/test_recovery_graduation.py]
status: historical
source: project_recovery_guidance.md
---

Recovery Guidance + Voice Loop Closure — CLOSED 2026-04-21 (5-slice
arc). Closes two coupled CC-parity gaps in one pass:

1. *"No error-recovery guidance — when an op fails, the operator sees
   a stack trace, not '3 things to try next.'"*
2. *"No voice output loop — voice is input-only in most paths. Closing
   the loop (TTS the pipeline decisions) would unlock hands-free
   operation."*

**Why one arc:** gap #2 was partially addressed already — Karen voice
+ VoiceNarrator + CommProtocol narration of INTENT/DECISION/POSTMORTEM
were in place. The actual void was that POSTMORTEM had nothing
structured to *say* when an op failed — just a raw ``root_cause``.
Shipping a RecoveryAdvisor + formatter + voice announcer gave Karen
new content AND closed the error-recovery gap with the same primitive.

**What shipped:**
- Slice 1: ``recovery_advisor.py`` — rule-based advisor. ``FailureContext``
  + ``RecoverySuggestion`` + ``RecoveryPlan`` frozen dataclasses.
  **14 dedicated rules** (one per known failure class) + generic
  fallback + exception catch-all. Schema ``recovery_plan.v1``.
  Pure-code Tier 0 reflex (§5 Manifesto). 42 tests.
- Slice 2: ``recovery_formatter.py`` — three surfaces:
  ``render_text`` for REPL, ``render_voice`` for TTS-safe Karen
  narration (strips env vars, flags, backticks; uses ordinals +
  count phrase), ``render_json`` for IDE observability. Schema
  ``recovery_formatter.v1``. 25 tests.
- Slice 3: ``recovery_announcer.py`` — Karen voice wrapper mirroring
  ``karen_voice.py`` conventions. Opt-in via two env flags
  (``OUROBOROS_NARRATOR_ENABLED`` master + ``JARVIS_RECOVERY_VOICE_ENABLED``
  sub-switch, both default off for recovery surface). Lazy ``safe_say``
  import (headless-safe, no audio stack at construction). Drop-oldest
  queue (8 slots), 3.0s min-gap rate limiter, per-plan idempotency
  LRU. Schema ``recovery_announcer.v1``. 25 tests.
- Slice 4: ``recovery_repl.py`` + ``recovery_store.py`` — ``/recover``
  dispatcher with verbs ``<op-id>`` / ``<op-id> speak`` / ``session
  <sid>`` / ``help``. ``RecoveryPlanStore`` is a bounded per-op LRU
  (capacity 128) that tests + production wire as a plan provider.
  Historical path reads from ``SessionRecord`` (``stop_reason`` +
  ``cost_spent_usd`` flow into a fresh FailureContext → advise →
  render_text). 30 tests.
- Slice 5: ``test_recovery_graduation.py`` (23 pins) +
  ``scripts/livefire_recovery.py`` (10 scenarios, 50 checks
  including full hands-free Karen loop).

**Critical graduation pins:**
- ``test_module_has_no_authority_imports`` — grep-pinned authority
  invariant on all 5 new modules. **Note landmine**: the regex used
  ``\b<module>\b`` word boundaries to avoid false-matching
  ``unified_voice_orchestrator`` on the substring ``orchestrator``.
- ``test_advisor_modules_do_not_import_model_surface`` — no-LLM pin.
  Recovery is deterministic by design; forbids imports of providers /
  candidate_generator / plan_generator / semantic_triage.
- ``test_every_known_stop_reason_has_dedicated_rule`` — rule coverage
  pin. Every entry in ``known_stop_reasons()`` must route to a
  dedicated rule, not the generic fallback.
- ``test_announcer_construction_does_not_import_audio`` — headless
  safety pin. Mirror of same pin on ``karen_voice.py``.
- ``test_recovery_voice_default_off`` — opt-in posture pin. Default
  silent; narration requires two explicit env flags.

**Schema versions pinned:** ``recovery_plan.v1``,
``recovery_formatter.v1``, ``recovery_announcer.v1``,
``recovery_store.v1``.

**§1 invariant (grep-enforced):** all 5 new modules import zero of
orchestrator / policy_engine / iron_gate / risk_tier_floor /
semantic_guardian / tool_executor / candidate_generator /
change_engine.

**Rule table (14 dedicated + generic):**
- ``cost_cap_exceeded`` → /cost drill-down + cap widening
- ``validation_retries_exhausted`` → FAIL grep + retry bypass + L2 timebox
- ``l2_repair_exhausted`` → exploration check + iteration bump + disable L2
- ``approval_required`` → /plan approve/reject/show
- ``approval_timeout`` → timeout extend + re-approve + mode disable
- ``iron_gate_rejected`` → read feedback + widen exploration + richer intent
- ``exploration_insufficient`` → diversify + floor lower + shadow mode
- ``ascii_gate_rejected`` → grep unicode + prompt-level pin + disable gate
- ``multi_file_coverage_insufficient`` → /plan show + explicit file list + disable gate
- ``provider_exhaustion`` → status dashboards + force route + fallback window
- ``policy_denied`` → POLICY_DENIED grep + FORBIDDEN_PATH check + elevate tier
- ``cancelled_by_operator`` → /resume + inspect reason + smaller op
- ``idle_timeout`` → increase idle + warm-up op + disable timeout
- ``unhandled_exception`` → debug.log less + verbose trace + file incident
- Plus generic fallback for unknown stop_reasons (debug.log + /session show + resubmit)

**Voice loop closure:**
```bash
# Operator flips the two flags
export OUROBOROS_NARRATOR_ENABLED=true
export JARVIS_RECOVERY_VOICE_ENABLED=true

# Op fails, plan stored automatically
# Operator hears "Here are three things to try. First, inspect which
# phase ate the budget..." via /recover <op-id> speak

# Or operators who still want tool narration but silent recovery:
export OUROBOROS_NARRATOR_ENABLED=true   # tool + phase narration
# (leave JARVIS_RECOVERY_VOICE_ENABLED unset; recovery stays silent)
```

**Files shipped:**
- ``backend/core/ouroboros/governance/recovery_advisor.py`` (new)
- ``backend/core/ouroboros/governance/recovery_formatter.py`` (new)
- ``backend/core/ouroboros/governance/recovery_announcer.py`` (new)
- ``backend/core/ouroboros/governance/recovery_repl.py`` (new)
- ``backend/core/ouroboros/governance/recovery_store.py`` (new)
- 5 test files + graduation + live-fire script

**Test tally:** 145 arc tests green (42 + 25 + 25 + 30 + 23) + 50
live-fire checks across 10 scenarios.

**Landmines resolved:**
- Authority grep needs ``\b<mod>\b`` word boundaries — without them,
  ``unified_voice_orchestrator`` trips the ``orchestrator`` filter.
  This landmine will recur; always use word boundaries in forbidden-
  imports greps.
- TTS rendering: ``$`` for dollar amounts is legit prose; forbid only
  shell prefixes (``$ /...``) and env var tokens (``JARVIS_``, ``--``,
  backticks). Test assertion regex tightened accordingly.
- Case preservation in REPL output: tests that ``.lower()`` the
  haystack must lowercase the needle too — ``"Karen"`` becomes
  ``"karen"`` after case fold.
- Recovery voice is opt-in (two flags) NOT auto-on with the master
  switch. Operators who enable tool narration don't automatically get
  recovery narration piled on — honors the "no dashboard by default"
  spirit of ``feedback_tui_design.md``.

**Integration with prior arcs:**
- Historical ``/recover session <sid>`` reads from Session History
  Browser arc (``SessionRecord.stop_reason`` + ``cost_spent_usd``).
- ``cost_cap`` rule suggests ``/cost <op-id>`` from the Per-Phase
  Cost Drill-Down arc — the two surfaces compose cleanly.
- Karen voice reuses ``karen_voice.py`` conventions verbatim — lazy
  import + env gates + queue + rate limit. Operators who already know
  the tool narration pattern get recovery narration for free.

**Not done (follow-up candidates):**
- Hook into orchestrator POSTMORTEM: requires orchestrator to know
  about ``RecoveryPlanStore``. Currently the REPL works against a
  provider the operator wires explicitly. Full auto-stash needs a
  module-level observer like the CostGovernor finalize pattern.
- IDE observability surface: ``GET /observability/recovery/<op-id>``
  endpoint. The ``render_json`` function is ready; wiring a route is
  a one-file addition if needed.
- LLM-assisted advisor: the rule table is the baseline. A Tier 1
  (Claude) path that reads the postmortem and adds context-specific
  suggestions is a future slice. Would live in a NEW module that
  composes rule-based plan + LLM enrichment — keeps the rule table
  as the deterministic backbone.
