"""Proactive AST-Chunked Generation — big-file map-reduce for DoubleWord.

DoubleWord exhausts / times out when asked to EMIT a large file: forcing it to
regenerate a 932-line module in one streaming turn is what chokes the RT lane
(soak bt-2026-07-22-*). The reactive stack (#70016-#70019) absorbs the blips;
this is the PREDICTIVE half — don't hand DW the whole file at all.

Instead, before generation, slice out ONLY the target symbol (the buggy
function) via the EXISTING ``ast_slicer.ASTChunker`` — the same slicer the
``read_file`` tool uses (DRY, zero parallel AST logic). DW then fixes a small,
focused chunk; the fix is stitched back into the full file locally by
line-range. The egress payload collapses from ~900 lines to ~50, so DW never
chokes — and the multi-round ReAct loop over a big file becomes tractable.

Pure + deterministic where possible; env-gated; never raises on the hot path.
This module is the "break it down + stitch back" primitive. The generation-path
wiring (routing a big-file candidate through it) composes on top.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Ouroboros.ChunkedGeneration")

_ENABLED_ENV = "JARVIS_DW_BIG_FILE_CHUNKING_ENABLED"
_THRESHOLD_ENV = "JARVIS_DW_BIG_FILE_LINE_THRESHOLD"
_DEFAULT_THRESHOLD = 300


def chunking_enabled() -> bool:
    """Master gate (default TRUE). OFF → a big-file candidate is generated
    whole (legacy), risking DW egress-overweight / timeout."""
    return os.environ.get(_ENABLED_ENV, "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def big_file_line_threshold() -> int:
    """A file with more than this many lines is a chunking candidate (default
    300). Env ``JARVIS_DW_BIG_FILE_LINE_THRESHOLD``. Clamped >= 50."""
    try:
        return max(50, int(os.environ.get(_THRESHOLD_ENV, str(_DEFAULT_THRESHOLD))))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


def is_big_file(source: str, *, threshold: Optional[int] = None) -> bool:
    """True when *source* is large enough that emitting it whole risks a DW
    choke — the PROACTIVE gate. Never raises."""
    if not source:
        return False
    lines = source.count("\n") + 1
    return lines > (threshold if threshold is not None else big_file_line_threshold())


def should_chunk(source: str, symbol: Optional[str]) -> bool:
    """Route to chunked generation when enabled, the file is big, AND we have a
    localized target symbol to slice. A big file with NO resolvable symbol
    falls through to whole-file generation (chunking can't help without a
    localized target)."""
    return bool(
        chunking_enabled() and symbol and is_big_file(source)
    )


class _CoarseTokenCounter:
    """Minimal ``TokenCounterProtocol`` — a 4-chars-per-token estimate. The
    chunker only needs it to populate ``token_count``; the stitch uses line
    ranges, so precision is irrelevant here."""

    def count(self, text: str) -> int:
        return max(1, len(text) // 4)


def extract_target_chunk(
    source: str, file_path: str, symbol: str,
):
    """Slice the ``symbol`` (function/method/class) out of *source* via the
    existing ``ASTChunker`` (DRY). Returns the ``CodeChunk`` (carrying
    ``source_code`` + ``start_line``/``end_line``) or ``None`` if the symbol
    isn't found / the file won't parse. Never raises.

    Matches by bare name OR qualified-name tail, so ``_topological_sort`` finds
    ``SagaApplyStrategy._topological_sort``."""
    try:
        from backend.core.ouroboros.governance.ast_slicer import ASTChunker
    except Exception:  # noqa: BLE001
        return None
    try:
        # ASTChunker needs a token_counter with a ``.count(str) -> int``
        # method; a coarse 4-chars-per-token estimate is fine — we use the
        # chunk's line range, not its token count, for the stitch.
        chunker = ASTChunker(_CoarseTokenCounter())
    except Exception:  # noqa: BLE001 — degrade if the constructor shape differs
        return None
    want = symbol.split(".")[-1].strip()
    try:
        chunks = chunker.extract_chunks_from_source(
            source, Path(file_path), target_names={want}, include_all=False,
        )
    except Exception:  # noqa: BLE001
        return None
    # Prefer an exact name/qualified-tail match; else the first returned chunk.
    for c in chunks or ():
        name = getattr(c, "name", "")
        qn = getattr(c, "qualified_name", "") or ""
        if name == want or qn.split(".")[-1] == want:
            return c
    return (chunks or [None])[0]


def stitch_replacement(
    full_source: str, chunk, new_body: str,
) -> Optional[str]:
    """Replace the *chunk*'s line-range in *full_source* with *new_body*,
    returning the full stitched file. Preserves every other line byte-for-byte.
    Re-indents *new_body* to the chunk's original leading indent if the model
    returned it dedented. Returns ``None`` on an out-of-range chunk. Never
    raises.

    ``chunk.start_line`` / ``chunk.end_line`` are 1-indexed inclusive (the
    ASTChunker convention)."""
    try:
        start = int(getattr(chunk, "start_line", 0))
        end = int(getattr(chunk, "end_line", 0))
    except (TypeError, ValueError):
        return None
    lines = full_source.splitlines(keepends=True)
    n = len(lines)
    if start < 1 or end < start or end > n:
        return None
    # Original leading indent (from the chunk's first source line).
    orig_first = lines[start - 1]
    orig_indent = orig_first[: len(orig_first) - len(orig_first.lstrip(" \t"))]
    body = _reindent(new_body.rstrip("\n"), orig_indent)
    # Preserve the trailing newline shape of the replaced region.
    if lines[end - 1].endswith("\n") and not body.endswith("\n"):
        body += "\n"
    stitched = lines[: start - 1] + [body] + lines[end:]
    return "".join(stitched)


def _reindent(body: str, target_indent: str) -> str:
    """If *body*'s first line has LESS indent than *target_indent* (the model
    returned a dedented function), shift the whole block right so it lands at
    the chunk's original nesting. If it already matches or is deeper, leave it
    (the model preserved indentation). Deterministic; never raises."""
    body_lines = body.split("\n")
    if not body_lines:
        return body
    first = body_lines[0]
    first_indent = first[: len(first) - len(first.lstrip(" \t"))]
    if len(first_indent) >= len(target_indent):
        return body  # already at (or deeper than) the target nesting
    pad = target_indent[len(first_indent):]
    return "\n".join((pad + ln) if ln.strip() else ln for ln in body_lines)


def build_focused_prompt(chunk, instruction: str) -> str:
    """Compose a minimal, chunk-scoped generation prompt — only the target
    function + the fix instruction, NOT the whole file. Keeps the DW egress
    tiny. The caller stitches the returned function back via
    :func:`stitch_replacement`."""
    name = getattr(chunk, "name", "the target function")
    src = getattr(chunk, "source_code", "") or ""
    return (
        f"Rewrite ONLY the following Python function `{name}` to satisfy the "
        f"instruction. Return the COMPLETE function definition (same name and "
        f"signature), correctly indented, and NOTHING else — no prose, no "
        f"surrounding code.\n\nInstruction: {instruction}\n\n"
        f"```python\n{src}\n```"
    )


__all__ = [
    "big_file_line_threshold",
    "build_focused_prompt",
    "chunking_enabled",
    "extract_target_chunk",
    "is_big_file",
    "should_chunk",
    "stitch_replacement",
]
