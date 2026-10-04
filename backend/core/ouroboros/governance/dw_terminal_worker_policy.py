"""Slice 45 — DW terminal-worker tool policy (env-aware leaf).

Why this exists
---------------
v40b (bt-2026-05-29-200702) produced the arc's first DW candidate, but the
Iron Gate rejected it: ``exploration_insufficient: 0/1`` — the model made
**0 tool calls**. Root cause is a *predicate mismatch* between two layers
(NOT a parse bug, NOT model incapacity — Phase 1 trace
``scripts/trace_qwen_tool_syntax.py`` proved Qwen-397B emits a flawless
``2b.2-tool`` envelope the instant the tool section is advertised):

  * PROMPT layer (``providers._build_tool_section``):
        ``should_skip_venom_for_route("background")`` -> returns ""  ->
        the model is NEVER shown the tool list or the 2b.2-tool schema.
  * EXEC layer (``doubleword_provider`` ``_skip_tools = complexity ==
        "trivial"``): a non-trivial BACKGROUND op still RUNS the Venom
        tool loop -> ``parse_fn`` is called on output the model was never
        instructed to shape -> ``None`` every round -> 0 tool calls ->
        Iron Gate 0/1 -> deadlock.

The historical suppression (Slice 12AF) assumed BACKGROUND never runs the
loop because "Claude is invoked later, so tools are moot." That assumption
is **false when Claude is disabled** (``JARVIS_PROVIDER_CLAUDE_DISABLED``):
DW is then the *terminal worker* for the op and must be allowed to explore
to clear the Iron Gate.

What this module does
---------------------
Provides the single env-aware predicate that both ``providers.py``
(``_build_tool_section``) and ``doubleword_provider.py``
(``_will_skip_tools``) consult to decide whether a VENOM-skip route should
nonetheless be advertised + run the tool loop, and
:func:`route_skips_tool_loop`, the exec-layer gate the ``PrimeProvider`` and
``ClaudeProvider`` seats run their loop on, so a seat that is shown the tools
always runs the loop that consumes them. It is a deliberate **leaf**
(env reads only, no governance imports) so both callers can import it with
zero circular-import risk. ``route_predicates.py`` stays env-free by
design; the env-aware policy belongs here.

Discipline
----------
* Scope is **BACKGROUND only**. SPECULATIVE stays fire-and-forget (no time
  budget for tool rounds); WIRING_VALIDATION's no-op patch is correct by
  contract. Both remain suppressed.
* Gated by ``JARVIS_DW_BACKGROUND_VENOM_ENABLED`` (default ``true``) AND
  ``claude_is_disabled()``. When Claude is enabled OR the master flag is
  off, the predicate returns ``False`` everywhere -> byte-identical legacy.
* NEVER raises — accepts any string, returns bool.

Manifesto compliance: §5 intelligence-driven routing (policy over a closed
signal, not regex/LLM); §7 observability (named, greppable, env-tunable).
"""
from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The single route this policy widens. Kept as a module constant so an
# AST-pin can assert scope did not silently expand to speculative /
# wiring_validation.
TERMINAL_WORKER_ROUTE = "background"

# Master flag name (exported for FlagRegistry seeding + tests).
MASTER_FLAG = "JARVIS_DW_BACKGROUND_VENOM_ENABLED"

__all__ = [
    "TERMINAL_WORKER_ROUTE",
    "MASTER_FLAG",
    "claude_is_disabled",
    "background_is_terminal_worker",
    "route_skips_tool_loop",
]


def _truthy(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


def claude_is_disabled() -> bool:
    """True iff the Claude (Anthropic) provider is disabled for this run.

    Mirrors the ``JARVIS_PROVIDER_CLAUDE_DISABLED`` posture already
    consumed by Slices 19a / 20A / 22 / 23. Pure env read; NEVER raises.
    """
    from backend.core.ouroboros.governance.paid_lanes import (  # noqa: PLC0415
        paid_lane_switched_on,
    )
    return not paid_lane_switched_on("claude")


def background_is_terminal_worker(route: str) -> bool:
    """True iff ``route`` is the BACKGROUND route AND DW is acting as the
    terminal worker (Claude disabled) AND the master flag is on.

    When True, callers MUST advertise the tool section + let the Venom
    loop run so the model can explore and clear the Iron Gate — even
    though ``route`` is a member of ``VENOM_SKIP_ROUTES``.

    Scope is intentionally narrow (BACKGROUND only). Returns ``False`` for
    every other route, for a Claude-enabled run, and when the master flag
    is off — preserving byte-identical legacy behavior. NEVER raises.
    """
    if route != TERMINAL_WORKER_ROUTE:
        return False
    if not _truthy(MASTER_FLAG, "true"):
        return False
    return claude_is_disabled()


def route_skips_tool_loop(route: str, *, is_read_only: bool = False) -> bool:
    """True iff a provider seat must NOT run the Venom tool loop for ``route``.

    The exec-layer twin of the prompt layer's tool advertisement
    (``providers._build_tool_section`` / ``_should_use_lean_prompt``), which
    already honours :func:`background_is_terminal_worker`. A seat that gates
    its loop on ``should_skip_venom_for_route`` alone disagrees with that
    prompt: when Claude is disabled a BACKGROUND op is SHOWN the tools, the
    model answers with a ``2b.2-tool`` call, the seat has skipped the loop
    that would consume it, and the parser rejects the call it invited
    (``tool_call_returned_under_venom_skip``). Soak bt-2026-10-04-204411 lost
    every VALIDATE_RETRY regeneration and the first generation of a fresh op
    that way on the local 30B, which is served through ``PrimeProvider``.

    Composition (one decision for every seat):

      * route not in ``VENOM_SKIP_ROUTES``  -> loop runs;
      * read-only op                        -> loop runs (mutation tools are
        refused by policy Rule 0d, so there is no cost escalation);
      * BACKGROUND terminal worker          -> loop runs (the prompt
        advertised the tools);
      * otherwise                           -> skipped.

    NEVER raises. A failed terminal-worker probe resolves to "not a terminal
    worker", the same safe default ``_build_tool_section`` applies, so the
    two layers stay in agreement even on that path.
    """
    from backend.core.ouroboros.governance.route_predicates import (  # noqa: PLC0415
        should_skip_venom_for_route,
    )
    if not should_skip_venom_for_route(str(route or "")):
        return False
    if is_read_only:
        return False
    try:
        return not background_is_terminal_worker(str(route or ""))
    except Exception:  # noqa: BLE001 -- predicate contract: never raise
        return True
