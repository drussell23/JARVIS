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
nonetheless be advertised + run the tool loop. It is a deliberate **leaf**
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
]


def _truthy(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


def claude_is_disabled() -> bool:
    """True iff the Claude (Anthropic) provider is disabled for this run.

    Mirrors the ``JARVIS_PROVIDER_CLAUDE_DISABLED`` posture already
    consumed by Slices 19a / 20A / 22 / 23. Pure env read; NEVER raises.
    """
    return _truthy("JARVIS_PROVIDER_CLAUDE_DISABLED", "")


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
