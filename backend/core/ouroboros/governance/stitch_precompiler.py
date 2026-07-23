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

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional

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


class RebaseCollisionError(StitchCollisionError):
    """A node that is valid against the ORIGINAL source fractures the file when
    combined with a PRIOR agent's committed graft (cross-agent semantic
    collision). Routed back so the agent self-corrects against the REBASED
    buffer — the same feedback bus as StitchCollisionError, one kind deeper."""


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


def _python_name_ok(node: str, symbol: str) -> bool:
    """Cheap Python identity check: the node defines a function named after the
    symbol's tail. Non-fatal — True on any trouble. Reuses the agentic verifier."""
    try:
        from backend.core.ouroboros.governance.agentic_super_agent import (
            _verify_node_against_ast,
        )
        stub = type("_T", (), {"symbol": symbol})()
        ok, _ = _verify_node_against_ast(node, stub)
        return bool(ok)
    except Exception:  # noqa: BLE001
        return True


class RollingVFS:
    """Rolling Virtual File System — the memory-bounded, rebasing Fan-In buffer.

    During an extreme map-reduce Fan-In, N sub-agents' DW calls fan OUT in
    parallel, but their heavy in-memory ``ast.parse`` + string allocations must
    NOT run concurrently (parallel AST bloat is the host-OOM root cause). This
    serializes every graft-and-validate behind ONE ``asyncio.Lock`` (the
    Sequential Compilation Lock) AND maintains a single mutating in-memory buffer:

      * Each agent, on passing the lock, has its candidate node RE-EXTRACTED from
        the CURRENT (rolling) buffer — line-drift-aware — trial-grafted, and the
        WHOLE precompiled. On success the graft is COMMITTED, establishing the
        new base state the next agent rebases onto.
      * If a node is valid against the ORIGINAL source but fractures the CURRENT
        buffer (a prior agent changed the shared context), it is rejected with a
        ``RebaseCollisionError`` — forcing self-correction against the rebased
        text. A node broken in isolation is a plain ``StitchCollisionError``.

    Never writes to disk (the buffer is a pure string). Never raises."""

    def __init__(self, source: str, file_path: str) -> None:
        self._original = source
        self._buffer = source
        self._file_path = file_path
        self._lock = asyncio.Lock()          # Sequential Compilation Lock
        self._committed: List[str] = []
        self._active = 0
        self._max_active = 0

    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def committed(self) -> List[str]:
        return list(self._committed)

    @property
    def max_concurrent_validations(self) -> int:
        """Peak agents inside the compile lock at once — MUST stay 1 (proof the
        heavy parse never ran in parallel)."""
        return self._max_active

    def seam_validator_for(self, symbol: str) -> Callable[[str], Awaitable[Optional[str]]]:
        """Return the async seam validator bound to *symbol* and THIS rolling
        buffer — drop-in for ``run_agentic_repair``'s ``seam_validator`` hook."""
        async def _validate(node: str) -> Optional[str]:
            from backend.core.ouroboros.governance.chunked_generation import (
                stitch_replacement,
            )
            from backend.core.ouroboros.governance.polyglot_chunker import (
                polymorphic_extract_target,
            )
            if not node or not node.strip():
                return "StitchCollisionError: empty node — return the complete definition"
            async with self._lock:                 # ── serialized heavy parse ──
                self._active += 1
                self._max_active = max(self._max_active, self._active)
                try:
                    await asyncio.sleep(0)          # yield: expose any lock breach
                    chunk = polymorphic_extract_target(self._buffer, self._file_path, symbol)
                    if chunk is None:
                        return (
                            f"RebaseCollisionError (prior agent modified context): "
                            f"symbol '{symbol}' is no longer resolvable in the rolling "
                            f"buffer — a prior graft removed or renamed it."
                        )
                    candidate = stitch_replacement(self._buffer, chunk, node)
                    if candidate is None:
                        return (
                            "RebaseCollisionError (prior agent modified context): "
                            "graft out-of-range after rebase."
                        )
                    # Structural precompile FIRST — the real syntax detail. Was this
                    # node fine against the ORIGINAL source? If so the fracture is a
                    # PRIOR agent's doing → RebaseCollisionError; else StitchCollision.
                    detail = precompile_detail(candidate, self._file_path)
                    if detail:
                        label = (
                            "RebaseCollisionError (prior agent modified context)"
                            if self._valid_against_original(symbol, node)
                            else "StitchCollisionError"
                        )
                        return f"{label}: {detail}"
                    # It parses — now the Python symbol-identity check.
                    language = getattr(chunk, "language", "python")
                    if language == "python" and not _python_name_ok(node, symbol):
                        return (
                            f"StitchCollisionError: node parses but does not define "
                            f"'{symbol.split('.')[-1]}'"
                        )
                    # ── COMMIT — new base state for the next agent to rebase on ──
                    self._buffer = candidate
                    self._committed.append(symbol)
                    return None
                finally:
                    self._active -= 1

        return _validate

    def _valid_against_original(self, symbol: str, node: str) -> bool:
        try:
            from backend.core.ouroboros.governance.chunked_generation import (
                stitch_replacement,
            )
            from backend.core.ouroboros.governance.polyglot_chunker import (
                polymorphic_extract_target,
            )
            oc = polymorphic_extract_target(self._original, self._file_path, symbol)
            if oc is None:
                return False
            cand = stitch_replacement(self._original, oc, node)
            return cand is not None and precompile_detail(cand, self._file_path) is None
        except Exception:  # noqa: BLE001
            return False


__all__ = [
    "RebaseCollisionError",
    "RollingVFS",
    "StitchCollisionError",
    "make_seam_validator",
    "precompile_detail",
    "precompile_or_raise",
]
