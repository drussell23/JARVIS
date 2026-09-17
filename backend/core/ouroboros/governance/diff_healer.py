"""Repair a rejected hunk instead of discarding the work that produced it.

## The measured constraint

With the schema finally reaching the local lane, the 30B emits patches — and
43-56% of them do not apply. The rejections are not random: they are
overwhelmingly mid-hunk, `context diverges after N matching line(s)`, meaning
the model reproduced several context lines correctly and then wrote one the
file does not contain. 26 of 28 rejections across two soaks had that shape;
only 2 were a placement failure.

That is a narrow defect. The model's INTENT — the ``+`` and ``-`` lines, the
actual edit — is usually fine; what drifts is its recollection of the
surrounding text. Throwing the candidate away discards a correct change because
its packaging was wrong.

## Why a second, smaller prompt rather than a regeneration

The existing realignment loop re-runs the whole op with the rejection appended,
and recovers 4 of 7 rejected ops. This is the cheaper rung below it: one
focused call that shows the model the hunk it wrote, the file text as it
actually reads, and asks for the context lines to be corrected — nothing else.
A smaller question with the answer visible in the prompt is a much easier
question than "generate this change again".

It runs on the local lane at zero marginal cost, on a path whose only
alternative is certain failure.

## What it may and may not change

The healer may rewrite ONLY context lines. The ``+`` and ``-`` lines are the
op's actual intent, they were sanctioned by the same pipeline that sanctioned
the goal, and a "repair" that quietly alters them would launder a different
change through a mechanism the operator believes only fixes whitespace. This is
enforced structurally, not asked for politely: the healed hunk's non-context
lines must match the original's, or the heal is discarded.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.DiffHealer")

__all__ = [
    "HealableRejection",
    "healer_enabled",
    "build_alignment_prompt",
    "extract_healed_diff",
    "heal_rejection",
    "intent_preserved",
]

_ENV_ENABLED = "JARVIS_DIFF_HEALER_ENABLED"


def healer_enabled() -> bool:
    """Default ON. It runs only where the alternative is a discarded candidate,
    and only on the local lane, where a call costs nothing."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


@dataclass(frozen=True)
class HealableRejection:
    """One rejected hunk, with everything needed to ask for a correction."""

    file_path: str
    unified_diff: str
    rejection: str
    candidate_id: str = ""


def _intent_lines(diff_text: str) -> Tuple[str, ...]:
    """The ``+``/``-`` lines — the change itself, stripped of hunk headers."""
    out: List[str] = []
    for ln in (diff_text or "").splitlines():
        if ln.startswith("@@"):
            continue
        if ln.startswith(("+++", "---")):
            continue
        if ln.startswith(("+", "-")):
            out.append(ln.rstrip("\r\n"))
    return tuple(out)


def intent_preserved(original_diff: str, healed_diff: str) -> bool:
    """Whether the heal changed ONLY context lines.

    The structural guarantee: a healer that may edit ``+``/``-`` lines is not a
    healer, it is a second generator with none of the first one's governance.
    """
    return _intent_lines(original_diff) == _intent_lines(healed_diff)


def _region(source: str, rejection: str, radius: int) -> str:
    """The file text around the line the rejection names, with line numbers.

    Numbered because the model's task is to align to THIS text, and a numbered
    region is the difference between "copy these lines" and "remember this
    file" — the latter being exactly what it gets wrong.
    """
    lines = (source or "").splitlines()
    m = re.search(r"file line (\d+)", rejection or "")
    if not m:
        m = re.search(r"line (\d+)", rejection or "")
    centre = int(m.group(1)) if m else 1
    lo = max(0, centre - radius)
    hi = min(len(lines), centre + radius)
    return "\n".join(f"{i + 1:>5} | {lines[i]}" for i in range(lo, hi))


def build_alignment_prompt(
    rejection: "HealableRejection", source: str, *, radius: int,
) -> str:
    """The one-shot correction request. Deliberately narrow."""
    return (
        "A unified diff you produced was REJECTED because its context lines do "
        "not match the file.\n\n"
        f"FILE: {rejection.file_path}\n"
        f"REJECTION: {rejection.rejection}\n\n"
        "THE FILE, as it actually reads (line numbers are for reference only, "
        "do NOT include them in your answer):\n"
        "```\n" + _region(source, rejection.rejection, radius) + "\n```\n\n"
        "THE DIFF YOU PRODUCED:\n"
        "```\n" + rejection.unified_diff.strip() + "\n```\n\n"
        "Correct ONLY the context lines (those beginning with a space) and the "
        "@@ header so the hunk applies to the file above. Copy context lines "
        "VERBATIM from the file, including indentation.\n"
        "Do NOT change, add or remove any line beginning with + or - — those "
        "are the intended change and must survive exactly.\n"
        "Return ONLY the corrected unified diff, no prose, no code fences."
    )


def extract_healed_diff(raw: str) -> str:
    """The diff out of a model reply, fenced or bare. ``""`` when absent."""
    text = (raw or "").strip()
    if not text:
        return ""
    fence = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    lines = [ln for ln in text.splitlines()]
    start = next((i for i, ln in enumerate(lines) if ln.startswith("@@")), None)
    if start is None:
        return ""
    return "\n".join(lines[start:]).strip() + "\n"


async def heal_rejection(
    rejection: "HealableRejection",
    source: str,
    *,
    ask: Any,
    apply_fn: Any,
    radius: int,
    deadline: Any = None,
) -> Optional[str]:
    """Ask for a corrected hunk and return the PATCHED CONTENT, or ``None``.

    ``ask`` is the one-shot model seam (``PrimeProvider.plan``) and ``apply_fn``
    is the existing diff ladder — both injected so this module owns the repair
    POLICY and borrows every mechanism. It never raises into the generation
    path: a failed heal is indistinguishable from not having tried.

    Returns content only when the heal both APPLIED and preserved the intent.
    """
    if not healer_enabled():
        return None
    try:
        prompt = build_alignment_prompt(rejection, source, radius=radius)
        raw = await ask(prompt, deadline)
        healed = extract_healed_diff(raw if isinstance(raw, str) else "")
        if not healed:
            logger.info(
                "[DiffHealer] %s: no diff in the correction reply",
                rejection.file_path,
            )
            return None
        if not intent_preserved(rejection.unified_diff, healed):
            logger.warning(
                "[DiffHealer] %s: DISCARDED — the correction altered +/- lines, "
                "which is a different change wearing a repair's clothes",
                rejection.file_path,
            )
            return None
        patched = apply_fn(source, healed)
        logger.info(
            "[DiffHealer] %s: hunk realigned and applied", rejection.file_path,
        )
        return patched
    except Exception as exc:  # noqa: BLE001 — a failed heal is just no heal
        logger.info(
            "[DiffHealer] %s: heal did not take (%s)",
            rejection.file_path, str(exc)[:120],
        )
        return None
