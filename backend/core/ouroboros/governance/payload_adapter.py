"""Translate the provider's caged output into the contract a caller wants.

The premise this corrects
-------------------------

APPLY does **not** want a unified diff. ``ChangeRequest.proposed_content`` is
"the new content to write to the file", and ``ChangeEngine`` writes it
directly; the ``difflib`` call in that module renders a heartbeat for the
cockpit, not the payload. The provider is caged to full content and APPLY
consumes full content -- they already agree, and inserting a diff between
them would add a lossy conversion between two formats that never disagreed.

There is exactly one real mismatch, and it is the one that kept the
micro-fix dead. ``InteractiveRepairLoop`` asks for
``{start_line, end_line, replacement}``; the provider answered with a
``2b.1`` candidate envelope. Live, 2026-09-19::

    Iter 0 located AttributeError at tests/test_jarvis_reload_manager.py:57
    Iter 0: micro-fix response did not parse as the
      {start_line,end_line,replacement} contract (1023 chars) — first 200:
      '{"schema_version": "2b.1", "candidates": [{"candidate_id": "0", ...'

Fighting that with prompt text loses: the cage is structural -- a grammar,
force-full-content, and a system prompt that belongs to ContextExpander.
So this module meets the cage instead. A full file is strictly more
information than a line range, and the range is recoverable from it by
comparison. Deterministic, no model involved, no second opinion about what
the model meant.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.PayloadAdapter")

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", re.MULTILINE)


@dataclass(frozen=True)
class LinePatch:
    """A replacement for ``[start_line, end_line]``, 1-based inclusive."""

    start_line: int
    end_line: int
    replacement: str

    def as_contract(self) -> Dict[str, Any]:
        """The shape ``_parse_micro_fix`` expects."""
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "replacement": self.replacement,
        }

    def render(self) -> str:
        span = (
            f"L{self.start_line}"
            if self.start_line == self.end_line
            else f"L{self.start_line}-{self.end_line}"
        )
        return f"{span} <- {len(self.replacement.splitlines())} line(s)"


def strip_fences(raw: str) -> str:
    """Remove markdown fences a model added around JSON. NEVER raises."""
    try:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = _FENCE.sub("", text).strip()
        return text
    except Exception:  # noqa: BLE001
        return raw or ""


def candidate_contents(raw: str, *, file_path: str = "") -> Optional[str]:
    """The proposed full text inside a ``2b.1`` envelope, or ``None``.

    Accepts the envelope, a bare candidate, and the multi-file ``files[]``
    shape, because all three are things this provider actually emits. When
    *file_path* is given it selects the matching entry -- a repair aimed at
    one file must not silently adopt the content of another, which is how
    the live failure proposed a change to the SOURCE module while repairing
    a TEST. NEVER raises.
    """
    try:
        data = json.loads(strip_fences(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    def _body(entry: Dict[str, Any]) -> str:
        for key in ("full_content", "raw_content", "content"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _matches(entry: Dict[str, Any]) -> bool:
        if not file_path:
            return True
        entry_path = str(entry.get("file_path", "") or "")
        return bool(entry_path) and (
            entry_path == file_path
            or entry_path.endswith(file_path)
            or file_path.endswith(entry_path)
        )

    pools: List[Dict[str, Any]] = []
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        pools.extend(c for c in candidates if isinstance(c, dict))
    pools.append(data)

    fallback = ""
    for entry in pools:
        nested = entry.get("files")
        if isinstance(nested, list):
            for sub in nested:
                if isinstance(sub, dict) and _body(sub):
                    if _matches(sub):
                        return _body(sub)
                    fallback = fallback or _body(sub)
        body = _body(entry)
        if body:
            if _matches(entry):
                return body
            fallback = fallback or body

    if fallback and file_path:
        logger.info(
            "[PayloadAdapter] envelope proposed content for a different file "
            "than %s — refusing the mismatch rather than patching the wrong "
            "target", file_path,
        )
        return None
    return fallback or None


def line_patch(original: str, proposed: str) -> Optional[LinePatch]:
    """The minimal contiguous span that turns *original* into *proposed*.

    One span, not a list of hunks: the caller's contract is a single
    replacement, and collapsing several edits into the range that encloses
    them is exact -- the replacement text carries the untouched lines
    between them verbatim. Larger than strictly necessary, never wrong.

    ``None`` when the texts are identical: no edit is not an edit, and
    reporting one would make a no-op look like a repair.
    """
    if original == proposed:
        return None
    try:
        before = original.splitlines(keepends=True)
        after = proposed.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
        spans = [op for op in matcher.get_opcodes() if op[0] != "equal"]
        if not spans:
            return None
        first, last = spans[0], spans[-1]
        i1, i2 = first[1], last[2]
        j1, j2 = first[3], last[4]
        replacement = "".join(after[j1:j2])
        # 1-based inclusive. A pure insertion has i1 == i2; the contract has
        # no way to say "between lines", so it replaces the line before the
        # insertion point and re-emits it ahead of the new text.
        if i1 == i2:
            anchor = max(1, i1)
            replacement = "".join(before[anchor - 1:anchor]) + replacement
            return LinePatch(anchor, anchor, replacement)
        return LinePatch(i1 + 1, i2, replacement)
    except Exception:  # noqa: BLE001
        logger.debug("[PayloadAdapter] line patch derivation failed", exc_info=True)
        return None


def micro_fix_contract(
    raw: str, *, original: str, file_path: str = "",
) -> Optional[Dict[str, Any]]:
    """A ``{start_line, end_line, replacement}`` dict from a caged response.

    The fallback path for ``_parse_micro_fix``: tried only after the strict
    contract fails, so a provider that answers correctly is never routed
    through a comparison it does not need. NEVER raises.
    """
    proposed = candidate_contents(raw, file_path=file_path)
    if proposed is None:
        return None
    patch = line_patch(original, proposed)
    if patch is None:
        logger.info(
            "[PayloadAdapter] caged response for %s proposed no change — "
            "not a repair", file_path or "target",
        )
        return None
    logger.info(
        "[PayloadAdapter] translated a caged full-content response for %s "
        "into %s", file_path or "target", patch.render(),
    )
    return patch.as_contract()


__all__ = [
    "LinePatch",
    "candidate_contents",
    "line_patch",
    "micro_fix_contract",
    "strip_fences",
]
