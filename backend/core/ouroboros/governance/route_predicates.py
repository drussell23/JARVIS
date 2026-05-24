"""Route predicates — pure-data, dependency-free.

Slice 12AD seam closure: this module is the single canonical home for
**route-name set membership predicates** that previously lived inlined
in providers.py and adjacent modules. Centralising them means a
refactor that adds a new ``ProviderRoute`` value (e.g.
``WIRING_VALIDATION``) updates one place, not N.

Why this exists
---------------
Before Slice 12AD, two providers.py call sites (`ClaudeProvider` +
`PrimeProvider`) had identical inlined literals
``_route in ("background", "speculative")`` to decide whether to skip
Venom's multi-round tool loop. That duplication was:

  * a drift hazard — a new route added in :class:`ProviderRoute` could
    silently miss either site, leaving one provider in inconsistent
    skip-policy with the other;
  * impossible to test as a unit — the predicate was string-compared
    inside async generation paths;
  * impossible to enforce as a structural pin — AST searches for
    "route-skip policy" found tuple literals, not a named identifier.

This module replaces both literals with a single named ``frozenset`` +
``should_skip_venom_for_route()`` helper, exported with an ``__all__``
so an AST-pin can verify the providers.py call sites import from here
and the inline literals are gone.

Discipline
----------
* **NO imports from urgency_router** — to avoid circular-import risk
  during providers.py boot. Route names are tracked as bare strings;
  the value parity with :class:`ProviderRoute` is enforced by an
  AST pin in the spine tests, not by runtime import.
* **NO env-var reads** — route taxonomy is a closed-set design
  decision; runtime knobs belong elsewhere (e.g. cost_governor's
  ``JARVIS_OP_COST_ROUTE_*`` factors).
* **NEVER raises** — predicates accept any string, return bool.
* **frozenset, not set** — immutable; safe to share, safe to use
  as default for keyword args.

Manifesto compliance
--------------------
* §5 Intelligence-driven routing — set-membership over a closed
  taxonomy, not regex or LLM classification.
* §7 Absolute observability — named predicate is greppable; the
  AST pin enforces single-seam usage.
"""

from __future__ import annotations

from typing import FrozenSet

__all__ = [
    "GEMMA_PROMPT_PRUNE_ROUTES",
    "VENOM_SKIP_ROUTES",
    "should_prune_prompt_for_route",
    "should_skip_venom_for_route",
]


# ---------------------------------------------------------------------------
# VENOM_SKIP_ROUTES — routes that structurally skip Venom tool loop
# ---------------------------------------------------------------------------
#
# Members (string-equal to ``ProviderRoute.<NAME>.value``):
#
#   * ``"background"``       — cost-optimized DW-only route; Claude is
#                              never invoked, so tool-loop is moot.
#   * ``"speculative"``      — fire-and-forget DW batch; no time budget
#                              for tool rounds.
#   * ``"wiring_validation"`` — Slice 12AD: smoke/wiring-validation
#                              fixtures with ``purpose=="wiring_validation"
#                              ∧ real_benchmark==False``. A no-op patch
#                              is the structurally-correct answer
#                              (trivially-passing test). Multi-round
#                              tool exploration would burn budget on a
#                              fixture that needs none.
#
# **EXCEPTION** (mirrored by callers): read-only ops keep the tool loop
# enabled regardless of route, because tool_executor refuses mutation
# tools under the read-only contract (cost-escalation impossible) and
# the loop is the only way read-only cartography ops produce useful
# output. Callers apply the read-only escape hatch around the
# predicate; this module stays route-only.

VENOM_SKIP_ROUTES: FrozenSet[str] = frozenset({
    "background",
    "speculative",
    "wiring_validation",
})


def should_skip_venom_for_route(route: str) -> bool:
    """True iff the given route name belongs to :data:`VENOM_SKIP_ROUTES`.

    Pure set-membership check — no env reads, no logging, no I/O.
    Accepts any string (including ``""``); only exact lowercase matches
    against the closed set return True.

    Callers are responsible for the read-only escape hatch:

        skip = should_skip_venom_for_route(route) and not is_read_only

    Mirrors the historical inlined predicate
    ``_route in ("background", "speculative")`` (pre-Slice-12AD) so
    refactor diff is bit-equivalent for those two routes.
    """
    return route in VENOM_SKIP_ROUTES


# ---------------------------------------------------------------------------
# GEMMA_PROMPT_PRUNE_ROUTES — routes that route to Gemma 4 31B
# ---------------------------------------------------------------------------
#
# Members:
#
#   * ``"background"``  — DW-only route; runs on Gemma 4 31B which
#                         can't survive 11K-token prompts in a 180s
#                         budget. The BG prompt is pruned to goal +
#                         target file + output schema only.
#   * ``"speculative"`` — same model, same pruning rationale.
#
# **Deliberately does NOT include** ``"wiring_validation"`` — the
# Slice 12AD route bypasses Venom for cost (set-membership in
# :data:`VENOM_SKIP_ROUTES`) but the actual provider call still goes
# through the standard Claude path (single-chunk generation against
# the no-op-passes structural contract). No Gemma involvement → no
# prompt-size pruning needed. Separating this set from
# :data:`VENOM_SKIP_ROUTES` makes that distinction explicit and
# defends against future drift.

GEMMA_PROMPT_PRUNE_ROUTES: FrozenSet[str] = frozenset({
    "background",
    "speculative",
})


def should_prune_prompt_for_route(route: str) -> bool:
    """True iff the given route runs on a model whose context
    budget requires pruning the GENERATE prompt down to the
    goal + target file + output schema essentials.

    Currently: BACKGROUND + SPECULATIVE (Gemma 4 31B). Not
    WIRING_VALIDATION (routes through Claude, no pruning needed).

    Pure set-membership; NEVER raises.
    """
    return route in GEMMA_PROMPT_PRUNE_ROUTES
