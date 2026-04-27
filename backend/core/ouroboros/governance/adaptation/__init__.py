"""Reverse Russian Doll Pass C — Adaptive Anti-Venom.

This package contains the **Order-2 adaptation** primitives — the
substrate that lets each Anti-Venom layer (Iron Gate, SemanticGuardian,
ScopedToolBackend, risk-tier ladder, ExplorationLedger) grow stricter
in response to operational evidence, while structurally guaranteeing
the cage never loosens via this surface (loosening is a Pass B
manifest amendment).

Per Pass C design draft (`memory/project_reverse_russian_doll_pass_c.md`).

Slice 1 (this slice) ships the AdaptationLedger substrate only —
append-only audit log + monotonic-tightening invariant validator +
status enums + frozen dataclasses. No mining surfaces, no operator
REPL. Slices 2-6 add the 5 adaptive surfaces + meta-governor.

## Authority invariants (Pass C §4 + §5.2)

  * Append-only file (`.jarvis/adaptation_ledger.jsonl`); never
    rewritten. Only proposal-state transitions land as new lines.
  * Every `propose()` call validates monotonic-tightening BEFORE
    write — the universal cage rule.
  * `approve()` is the ONLY path that flips `applied_at` non-null
    and is the structural marker that an adaptation is now live.
  * Loosening operations (deprecating a detector, lowering a floor,
    raising a budget, removing a tier) CANNOT happen via Pass C.
    They go through Pass B's `/order2 amend` REPL.
  * No imports of orchestrator / policy / iron_gate /
    risk_tier_floor / change_engine / candidate_generator / gate /
    semantic_guardian / semantic_firewall / scoped_tool_backend.
    The substrate stays acyclic — Slices 2-5 import the substrate;
    the substrate imports nothing governance-specific.

## Default-off

`JARVIS_ADAPTATION_LEDGER_ENABLED` (default `false`). When off:
ledger does not load, no proposals get written, no REPL surface,
no SSE. Behavior identical to today's static cage.
"""
