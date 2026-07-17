"""Structural payload-integrity validation at provider return boundaries.

# Why this exists

Raising the Aegis upstream read ceiling to 600s (the shape-aware read-budget fix)
means a very long generation is allowed to complete — but it also means that if an
upstream provider severs the socket mid-transmission (a hard-wall cut, a network
drop, a provider crash at token N), the CLIENT receives a *partial* body. A
partial JSON body is not a benign parse error: it is a payload that was cut, and
treating it as "malformed but repairable" is actively dangerous here because the
existing deterministic repair (``providers._repair_json``) explicitly *closes
open containers* — it will happily turn

    {"changes":[{"file":"a.py","content":"def foo(

into

    {"changes":[{"file":"a.py","content":"def foo("}]}

a corrupt-but-parseable blueprint that then flows into the governed loop. So a
severed payload must be DETECTED and rejected as truncation BEFORE any brace-
closing repair runs.

# Contract

``validate_json_payload(raw)`` returns the parsed dict, or:
  * raises ``PayloadTruncationError`` if the payload is structurally INCOMPLETE
    (unbalanced containers / unterminated string at EOF) — the sever case, so the
    caller routes it to the failure lifecycle, NOT a generic parse error;
  * raises the underlying ``json.JSONDecodeError`` if the payload is COMPLETE but
    malformed mid-body AND deterministic repair (reused, not reimplemented) can't
    recover it — a genuine syntax fault, distinct from truncation.

Truncation detection is a deterministic single pass over the text tracking string
state and container depth (an LLM cannot recover severed bytes, so no heal call —
this composes with, but never invokes, ``json_healer``). DRY: the non-truncation
repair path reuses ``providers._repair_json``; the markdown-fence strip mirrors
the healer's own. NEVER introduces a new JSON parser or regex-repair library.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class PayloadTruncationError(ValueError):
    """A provider payload was severed mid-transmission (structurally incomplete).

    Distinct from a syntax error: the bytes are not malformed, they are MISSING.
    Carries the observed structural state so the failure lifecycle can log why.
    """

    def __init__(
        self, detail: str, *, depth: int = 0, in_string: bool = False,
        received_bytes: int = 0,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.depth = depth
        self.in_string = in_string
        self.received_bytes = received_bytes


# ---------------------------------------------------------------------------
# Deterministic truncation detection
# ---------------------------------------------------------------------------


def _strip_markdown_fence(text: str) -> str:
    """Strip a leading/trailing ``` fence (mirrors json_healer / the DreamEngine
    ad-hoc strip this replaces). Only strips a matched pair; a lone opening fence
    is itself a truncation signal and is left for the structural scan."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    # Drop the opening fence line; drop a closing fence line only if present.
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def structural_scan(text: str) -> Tuple[int, bool, bool]:
    """Single-pass structural state of *text* as JSON.

    Returns ``(depth, in_string, saw_container)``:
      * ``depth`` — net open ``{``/``[`` containers at EOF (0 = balanced,
        >0 = truncated open, <0 = malformed-but-not-this-truncation-class).
      * ``in_string`` — True if a string literal is still open at EOF (severed
        mid-string — a classic mid-token cut).
      * ``saw_container`` — whether any container/opening token appeared at all
        (distinguishes "empty/garbage" from "a real object that got cut").

    String-aware: braces inside strings don't count, and ``\\`` escapes the next
    char so an escaped quote doesn't falsely close a string. NEVER raises.
    """
    depth = 0
    in_string = False
    escaped = False
    saw_container = False
    try:
        for ch in text:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                saw_container = True
            elif ch in "{[":
                depth += 1
                saw_container = True
            elif ch in "}]":
                depth -= 1
    except Exception:  # noqa: BLE001 — a scan bug must not mask the real payload
        logger.debug("[PayloadIntegrity] structural_scan faulted", exc_info=True)
    return depth, in_string, saw_container


def is_truncated(text: str) -> bool:
    """True iff *text* looks structurally severed: a real container/string was
    opened and never closed (net open depth > 0, or a string open at EOF)."""
    stripped = _strip_markdown_fence(text)
    if not stripped:
        return False
    depth, in_string, saw_container = structural_scan(stripped)
    return saw_container and (depth > 0 or in_string)


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def validate_json_payload(
    raw: str, *, allow_repair: bool = True,
) -> Dict[str, Any]:
    """Parse *raw* into a dict at a provider return boundary, distinguishing
    truncation from syntax fault.

    Order matters:
      1. Truncation FIRST — a severed payload raises ``PayloadTruncationError``
         and never reaches the brace-closing repair (which would mask it).
      2. Strict parse.
      3. Deterministic repair (reused ``providers._repair_json``) for a complete-
         but-malformed body — only when ``allow_repair``.

    Raises ``PayloadTruncationError`` (severed) or ``json.JSONDecodeError``
    (genuine syntax fault). ``ValueError`` if the parsed root is not an object.
    """
    text = _strip_markdown_fence(raw or "")
    if not text:
        raise PayloadTruncationError("empty payload", received_bytes=0)

    if is_truncated(text):
        depth, in_string, _ = structural_scan(text)
        raise PayloadTruncationError(
            f"severed payload: depth={depth} in_string={in_string} "
            f"bytes={len(text)}",
            depth=depth, in_string=in_string, received_bytes=len(text),
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if not allow_repair:
            raise
        # Complete-but-malformed → reuse the EXISTING deterministic repair
        # (trailing commas, single quotes, unquoted keys, control chars). This
        # payload is NOT truncated (checked above), so _repair_json's container-
        # closing can't mask a sever here.
        repaired = _deterministic_repair(text)
        if repaired is None:
            raise
        data = repaired

    if not isinstance(data, dict):
        raise ValueError(
            f"payload root is {type(data).__name__}, expected object"
        )
    return data


def _deterministic_repair(text: str) -> Optional[Dict[str, Any]]:
    """Reuse ``providers._repair_json`` (the load-bearing deterministic sweep)
    and re-parse. Returns the dict on success, None if repair still can't parse.
    Import is lazy + fail-soft so a providers import problem never turns a
    syntax fault into a crash."""
    try:
        from backend.core.ouroboros.governance.providers import _repair_json
    except Exception:  # noqa: BLE001
        return None
    try:
        obj = json.loads(_repair_json(text))
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "PayloadTruncationError",
    "validate_json_payload",
    "is_truncated",
    "structural_scan",
]
