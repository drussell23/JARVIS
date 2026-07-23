"""In-Memory Syntax Pre-Compiler — the Swarm cannot write corrupted code to disk.

A node can be valid IN ISOLATION yet fracture the whole file when grafted at the
seam (a stray/missing bracket that only manifests in context). Relying on the
downstream VERIFY phase (pytest / Ouroboros) to catch that wastes ReAct cycles
AND risks a corrupt file landing on disk. The root fix is structural validation
IN MEMORY, at Fan-In, before any file I/O.

  * **Atomic in-memory compilation gate:** after a candidate node is grafted into
    the source STRING (via the existing ``stitch_replacement``) but before it is
    returned toward APPLY, precompile the ENTIRE string.
  * **Polymorphic validation routing:** dispatched through the ``ChunkerFactory``
    — ``ast.parse`` (Python), ``json.loads`` (JSON), ``yaml.safe_load`` (YAML),
    bracket-balance (TSX / text). Reuses each strategy's ``validate_detail``.
  * **ReAct feedback (``StitchCollisionError``):** on a fracture the trial graft
    is discarded (never written) and the exact syntax detail is routed back into
    the sub-agent's ReAct loop (the #70026 error-observation feedback) as a
    ``StitchCollisionError`` observation, so the agent self-corrects within its
    token budget — the file is never touched.

DRY: composes ``stitch_replacement`` (the stitcher) + ``ChunkerFactory`` (the
polymorphic router). Never raises from the detail path (a precompile fault is
reported, not thrown).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("Ouroboros.StitchPrecompiler")


class StitchCollisionError(Exception):
    """A grafted node breaks the file's structure at the seam. Carried back into
    the ReAct loop as a self-correction observation; never a fatal to the op."""

    def __init__(
        self, detail: str, *, file_path: str = "", language: str = "",
        lineno: Optional[int] = None,
    ) -> None:
        self.detail = detail
        self.file_path = file_path
        self.language = language
        self.lineno = lineno
        super().__init__(f"StitchCollisionError[{language or '?'}]: {detail}")


def precompile_detail(text: str, file_path: str) -> Optional[str]:
    """In-memory polymorphic syntax pre-compile of the WHOLE stitched string.
    Returns ``None`` if structurally valid, else a human-readable error detail.
    Routes via ``ChunkerFactory`` → the strategy's ``validate_detail``. Never
    raises (a precompile fault becomes a reported detail)."""
    try:
        from backend.core.ouroboros.governance.polyglot_chunker import ChunkerFactory
        return ChunkerFactory.for_file(file_path).validate_detail(text)
    except Exception as exc:  # noqa: BLE001
        return f"precompile_error:{type(exc).__name__}"


def precompile_or_raise(text: str, file_path: str, *, language: str = "") -> str:
    """Gate: return *text* iff it precompiles; else raise ``StitchCollisionError``.
    Use where a hard stop (not a ReAct retry) is wanted."""
    detail = precompile_detail(text, file_path)
    if detail:
        raise StitchCollisionError(detail, file_path=file_path, language=language)
    return text


def make_seam_validator(
    full_source: str, file_path: str, chunk,
) -> Callable[[str], Optional[str]]:
    """Build the ReAct-loop seam validator: ``(node) -> Optional[str]``. It TRIAL-
    grafts *node* into *full_source* (the existing ``stitch_replacement``) then
    precompiles the WHOLE result in-memory. Returns the fracture detail (the
    ``StitchCollisionError`` observation) or ``None`` when the graft is clean.
    Touches NO disk — the trial stitch is a pure string operation."""
    def _validate(node: str) -> Optional[str]:
        if not node or not node.strip():
            return "empty node — return the complete definition"
        try:
            from backend.core.ouroboros.governance.chunked_generation import (
                stitch_replacement,
            )
            candidate = stitch_replacement(full_source, chunk, node)
        except Exception as exc:  # noqa: BLE001
            return f"graft_error:{type(exc).__name__}"
        if candidate is None:
            return "graft out-of-range (chunk line span invalid)"
        return precompile_detail(candidate, file_path)

    return _validate


__all__ = [
    "StitchCollisionError",
    "make_seam_validator",
    "precompile_detail",
    "precompile_or_raise",
]
