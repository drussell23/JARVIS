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
