"""Stateful Tool Masking — make the premature-patch state unrepresentable.

The Iron Gate rejects a GENERATE candidate emitted with too few exploration tool
calls (read_file/search_code/get_callers) — but it does so POST-hoc, failing the
whole GENERATE and paying for a fresh retry (bt-2026-07-18-102510: Claude emitted
a full_content patch with 0/1 exploration → GENERATE_RETRY, never reaching APPLY).
Enforcing exploration via prompt wording is non-deterministic and burns budget on
those retries.

This module supplies the two DETERMINISTIC, in-loop levers the Venom tool loop
applies WHILE the op is below the Iron Gate exploration floor:

  1. **Write-tool advertisement masking** — strip the "Write tools" prose block
     from the prompt sent to the model, so it isn't offered mutation tools before
     it has gathered state. Marker-based (fail-open), so no coupling to the exact
     prose (which is `%`-formatted in providers._build_tool_section).
  2. **Premature-candidate rejection notice** — the deterministic turn message the
     loop appends when the model emits a FINAL patch before exploring; the loop
     REJECTS that candidate in-loop and continues (forcing exploration in the SAME
     loop, no fresh GENERATE). The ENFORCEMENT is the loop's reject+continue; this
     text only informs the model why.

Key point vs. the naive "strip mutation tool schemas" framing: the candidate is a
final `full_content` text response (OUTPUT FORMAT 2b.1), NOT a write_file tool
call — so masking the write-tool advertisement alone does not force exploration.
The load-bearing lever is (2), the in-loop candidate rejection. (1) composes with
it to also stop the model wasting rounds attempting premature writes.

Pure + fail-open: every helper degrades to the legacy (no-mask) behavior on any
fault. NEVER raises into the tool loop.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any, FrozenSet, Optional, Tuple

# The header that opens the "Write tools" prose block in
# providers._build_tool_section (line ~2354). The block runs from here to the
# next blank line (\n\n). Matching the header (not the whole block) keeps this
# decoupled from the block's exact `%`-escaped wording.
_WRITE_TOOLS_HEADER = "**Write tools (Iron-Gate-governed"

_FALSY = ("0", "false", "no", "off")


def tool_masking_enabled() -> bool:
    """``JARVIS_TOOL_MASKING_ENABLED`` (default ON). Off → the loop behaves exactly
    as before (write tools always advertised, no in-loop candidate rejection).
    NEVER raises."""
    return os.environ.get(
        "JARVIS_TOOL_MASKING_ENABLED", "true",
    ).strip().lower() not in _FALSY


def tool_mask_reject_cap() -> int:
    """``JARVIS_TOOL_MASK_REJECT_CAP`` (default 2) — the maximum number of times a
    premature final candidate is rejected in-loop before the loop finalizes anyway
    (falling back to the legacy Iron-Gate rejection). Bounds the in-loop cost so a
    model that refuses to explore cannot spin the loop. Floor 0. NEVER raises."""
    try:
        n = int(os.environ.get("JARVIS_TOOL_MASK_REJECT_CAP", "2").strip())
        return n if n >= 0 else 2
    except (TypeError, ValueError):
        return 2


def strip_write_tools_prose(prompt: str) -> str:
    """Remove the "Write tools" advertisement block from *prompt*. The block runs
    from :data:`_WRITE_TOOLS_HEADER` to the next blank line (``\\n\\n``). Returns
    *prompt* unchanged if the header (or its terminating blank line) is absent —
    fail-open, never corrupts the prompt. NEVER raises."""
    try:
        if not prompt:
            return prompt
        i = prompt.find(_WRITE_TOOLS_HEADER)
        if i < 0:
            return prompt
        j = prompt.find("\n\n", i)
        if j < 0:
            return prompt          # malformed / block runs to EOF — don't risk it
        return prompt[:i] + prompt[j + 2:]
    except Exception:  # noqa: BLE001
        return prompt


def masked_prompt(prompt: str, *, explore_count: int, floor: int) -> str:
    """Progressive Schema Injection as a PURE function: while the op is below the
    exploration *floor*, return the prompt with the write-tool advertisement
    stripped (mutation tools omitted); at/above the floor, return the prompt
    unchanged (mutation tools restored). Masking disabled → always unchanged.
    NEVER raises (fault → the original prompt)."""
    try:
        if not tool_masking_enabled():
            return prompt
        if int(explore_count) < int(floor):
            return strip_write_tools_prose(prompt)
        return prompt
    except Exception:  # noqa: BLE001
        return prompt


def exploration_force_notice(explore_count: int, floor: int) -> str:
    """The deterministic turn message appended when a premature final candidate is
    rejected in-loop. Informational — the enforcement is the loop's reject+continue.
    NEVER raises."""
    try:
        return (
            "\n\n[SYSTEM] Stateful Tool Masking: you proposed a final patch after "
            f"{int(explore_count)}/{int(floor)} required exploration calls. Your "
            "premature patch was REJECTED (not accepted) and the write tools are "
            "WITHHELD. You MUST gather state first: call read_file / search_code / "
            "get_callers on the target before proposing any change. Explore now — "
            "the write tools unlock and your patch is accepted once you have "
            "explored.\n"
        )
    except Exception:  # noqa: BLE001
        return "\n\n[SYSTEM] Explore the target (read_file/search_code) before patching.\n"


# ---------------------------------------------------------------------------
# Lever 3 — State-Driven Schema Constraint
# ---------------------------------------------------------------------------
#
# Levers 1 and 2 above are ADVISORY and REACTIVE respectively: one edits prose
# the model may ignore, the other rejects a patch the model has already spent
# its whole token budget writing. This module's own header states the limit
# they share -- "enforcing exploration via prompt wording is non-deterministic".
#
# Measured, soak bt-2026-08-31-155308 (qwen3-coder:30b): 106 in-loop
# rejections, EVERY one at 0/2 exploration calls, and 25 of 55 generations
# never called a tool at all. The model was told, rejected, told again,
# rejected again, then the Iron Gate rejected it post-hoc. Zero candidates
# from the whole soak. A model that does not follow instructions cannot be
# made to follow them by adding instructions.
#
# So this lever removes the CAPABILITY instead of the permission. The local
# lane already constrains the sampler with a JSON Schema union of the four
# legal answer shapes (`providers.build_response_json_schema`). While the op
# is below the exploration floor, that union is narrowed to the tool-call
# shape alone -- a premature patch stops being disallowed and becomes
# UNREPRESENTABLE. The sampler cannot emit tokens the grammar does not admit,
# so compliance is arithmetic rather than persuasion.
#
# WHY NOOP IS WITHHELD TOO, and this is the load-bearing half. The obvious
# design narrows to {tool, noop} -- "let it say the work is already done".
# That would have achieved nothing: the same soak logged 209 `2b.1-noop`
# responses against 106 premature patches, so noop was the LARGER escape.
# Worse, it is an escape the Iron Gate does not cover: the gate rejects a
# premature PATCH, while a noop is accepted as a legitimate verdict
# (`_NOOP` policy: outcome "partial", trainable) -- so "already complete"
# is currently the one way to bypass the exploration floor entirely.
# Narrowing to {tool, noop} would simply convert 106 premature patches into
# 106 premature noops and read as a fix.
#
# An "already complete" claim made before reading anything is exactly as
# unjustified as a patch made before reading. Both are answers about a file
# the model has not looked at. Once the floor is met, BOTH unlock.
#
# Pure + fail-open like the rest of this module: any fault yields None,
# meaning "no narrowing", i.e. the pre-existing full union.

_EXPLORATION_STATE: "ContextVar[Optional[Tuple[int, int, bool]]]" = ContextVar(
    "jarvis_tool_masking_exploration_state", default=None,
)


def schema_masking_enabled() -> bool:
    """``JARVIS_TOOL_SCHEMA_MASKING_ENABLED`` (default ON).

    Off → the grammar is never narrowed and the lane behaves exactly as it
    did before this lever: the full four-shape union on every round, with
    levers 1 and 2 still doing what they can. Separate from
    ``JARVIS_TOOL_MASKING_ENABLED`` on purpose -- this one changes what the
    SAMPLER can emit, which is a materially stronger intervention than
    editing prose, and an operator must be able to retreat from it without
    also giving up the prompt-level masking. NEVER raises."""
    return os.environ.get(
        "JARVIS_TOOL_SCHEMA_MASKING_ENABLED", "true",
    ).strip().lower() not in _FALSY


def set_exploration_state(
    *, explore_count: int, floor: int, may_finalize: bool,
) -> "Any":
    """Publish the CURRENT exploration state for the next generation call.

    Set by the tool loop immediately before it awaits ``generate_fn``, read
    by the local client while it assembles ``response_format``. A ContextVar
    carries it because those two are separated by five frames across three
    modules (tool loop → provider → client → complete → response_format)
    that have no business growing a parameter for this, and because asyncio
    context is per-task: two ops generating concurrently cannot see each
    other's state.

    *explore_count* is the LIVE cumulative count of read-only navigation
    calls this op has actually made -- the same number lever 2 rejects on,
    read off the executed tool-call history rather than a round index. A
    round counter would be wrong: a round whose every tool call was denied
    by policy advances the round and gathers nothing.

    *may_finalize* is the release valve. The tool loop passes False only
    while it is genuinely still willing to spend another round; when the
    deadline reserve is reached it passes True, the narrowing lifts, and a
    model that could not explore still gets to answer instead of dead-ending
    against a grammar it cannot satisfy. Same reasoning as
    ``tool_mask_reject_cap`` bounding lever 2.

    Returns the ContextVar token so the caller can ``reset``. NEVER raises.
    """
    try:
        return _EXPLORATION_STATE.set(
            (int(explore_count), int(floor), bool(may_finalize))
        )
    except Exception:  # noqa: BLE001
        return None


def reset_exploration_state(token: "Any") -> None:
    """Restore the previous exploration state. NEVER raises."""
    try:
        if token is not None:
            _EXPLORATION_STATE.reset(token)
    except Exception:  # noqa: BLE001
        pass


def answer_shapes_allowed() -> "Optional[FrozenSet[str]]":
    """The answer shapes the grammar may admit right now.

    Returns None for "no narrowing" -- the full union -- which is the answer
    whenever the lever is off, no state has been published (every non-local
    provider, and the local lane outside a tool loop), the floor is already
    met, or the loop has signalled it is ready to finalize.

    Returns the tool-call shape ALONE while the op is below the floor. Not
    {tool, noop}: see the module comment above -- noop was the larger escape
    hatch and the one the Iron Gate does not cover.

    Pure; NEVER raises (fault → None → pre-existing behaviour).
    """
    try:
        if not schema_masking_enabled():
            return None
        state = _EXPLORATION_STATE.get()
        if state is None:
            return None
        explore_count, floor, may_finalize = state
        if may_finalize or floor <= 0 or explore_count >= floor:
            return None
        from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
            _TOOL_SCHEMA_VERSION,
        )
        return frozenset({_TOOL_SCHEMA_VERSION})
    except Exception:  # noqa: BLE001
        return None


def register_flags(registry: "Any") -> int:
    """Module-owned FlagRegistry registration.  NEVER raises.

    Co-located with the levers so ``/help flag`` cannot drift from what the
    predicates above actually do. Registers all three of this module's
    switches, not only the new one: the first two were never registered,
    and an operator hunting a masking problem needs to find the whole
    family from one search.
    """
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0

    src = "backend/core/ouroboros/governance/tool_masking.py"
    specs = [
        FlagSpec(
            name="JARVIS_TOOL_MASKING_ENABLED",
            type=FlagType.BOOL, default=True,
            description=(
                "Levers 1+2: strip the write-tool advertisement from the "
                "prompt while the op is below the Iron Gate exploration "
                "floor, and reject a premature final patch IN-LOOP rather "
                "than paying a whole GENERATE retry for the post-hoc Iron "
                "Gate rejection. Advisory and reactive respectively -- "
                "neither can force a model that ignores instructions."
            ),
            category=Category.SAFETY, source_file=src, example="true",
            since="stateful tool masking",
        ),
        FlagSpec(
            name="JARVIS_TOOL_MASK_REJECT_CAP",
            type=FlagType.INT, default=2,
            description=(
                "How many times a premature final candidate is rejected "
                "in-loop before the loop finalizes anyway. Bounds the "
                "in-loop cost so a model that refuses to explore cannot "
                "spin the loop."
            ),
            category=Category.CAPACITY, source_file=src, example="2",
            since="stateful tool masking",
        ),
        FlagSpec(
            name="JARVIS_TOOL_SCHEMA_MASKING_ENABLED",
            type=FlagType.BOOL, default=True,
            description=(
                "Lever 3, the State-Driven Schema Constraint: while the op "
                "is below the exploration floor, narrow the local sampler's "
                "JSON-Schema union to the tool-call shape ALONE, making a "
                "premature patch, diff or 'already complete' verdict "
                "UNREPRESENTABLE rather than rejected after the fact. "
                "Measured on qwen3-coder:30b, same prompts and seeds, only "
                "the grammar differing: full union 0/3 explored; {tool,noop} "
                "0/3 (it took the noop escape every time); {tool} 3/3. Noop "
                "is withheld deliberately -- it was the LARGER escape (209 "
                "noops vs 106 premature patches in soak bt-2026-08-31-155308) "
                "and the one the Iron Gate does not cover. Separate switch "
                "from JARVIS_TOOL_MASKING_ENABLED because changing what the "
                "sampler CAN emit is a stronger intervention than editing "
                "prose; off restores the full four-shape union."
            ),
            category=Category.SAFETY, source_file=src, example="true",
            since="schema state machine (2026-08-31)",
        ),
    ]
    try:
        registry.bulk_register(specs, override=True)
    except Exception:  # noqa: BLE001
        return 0
    return len(specs)
