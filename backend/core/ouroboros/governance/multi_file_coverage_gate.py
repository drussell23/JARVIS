"""
Multi-file coverage gate — Iron Gate 5
======================================

Rejects candidates that target more than one file but only populate the
single-file schema (``file_path`` + ``full_content``) or populate a
``files: [...]`` list that fails to cover every path named in
``context.target_files``.

Background (Session O, 2026-04-15)
----------------------------------

Session O (``bt-2026-04-15-175547``) closed the full governed APPLY arc
for the first time — 1 of 4 target files landed on disk. The winning
candidate returned legacy ``{file_path, full_content}`` instead of
``{files: [...]}``, so ``_apply_multi_file_candidate`` was never
invoked. The model had no prior reason to know it should use the
multi-file shape: the prompt never showed it. This gate is the hard
enforcement half of the fix (prompt-side hint is in ``providers.py``).

Contract
--------

- If the operation targets 0 or 1 files → gate no-ops (single-file
  ops are out of scope).
- If ``JARVIS_MULTI_FILE_ENFORCEMENT`` is ``false``/``0``/``no``/``off``
  the gate no-ops regardless.
- Otherwise: every path in ``target_files`` must be covered by an entry
  in the candidate's ``files: [...]`` list, matched by normalized path
  (see :func:`_normalize_path`). Legacy-shape candidates that provide
  only ``file_path`` on a multi-target op are rejected with every
  target listed as missing.

Return shape from :func:`check_candidate`:
    ``None`` — gate passes, candidate is fine
    ``(reason, missing_paths)`` — gate fails, candidate should be rejected
        and the missing paths shown back to the model in retry feedback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.MultiFileCoverageGate")


_ENV_ENABLED = "JARVIS_MULTI_FILE_ENFORCEMENT"
_ENV_MULTI_GEN = "JARVIS_MULTI_FILE_GEN_ENABLED"

# Public reason prefix used in retry-feedback classification.
REASON_PREFIX = "multi_file_coverage_insufficient"


def is_enabled() -> bool:
    """Master switch for the gate.

    Defaults ON. Disabled when the coverage env var is explicitly
    falsy OR when the underlying multi-file generation master switch is
    off (no point enforcing coverage for a shape the orchestrator is
    refusing to honor at APPLY).
    """
    raw = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    raw_multi = os.environ.get(_ENV_MULTI_GEN, "true").strip().lower()
    if raw_multi in ("false", "0", "no", "off"):
        return False
    return True


def _normalize_path(path: str, project_root: Optional[Path] = None) -> str:
    """Normalize a path for coverage comparison.

    - Absolute paths are converted to relpath against ``project_root``
      when possible, else left absolute.
    - ``./`` prefixes and duplicate slashes are removed.
    - Case-sensitive — matches filesystem behavior on the runtime host.
      macOS's APFS is case-insensitive, but Linux CI is not; staying
      strict lets us catch model-side "tests/foo.py" vs "Tests/foo.py"
      drift before APPLY.
    """
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute() and project_root is not None:
        try:
            p = p.resolve().relative_to(project_root.resolve())
        except ValueError:
            # Candidate targets a file outside the project root — leave
            # absolute and the coverage comparison will mark it unmatched
            # against any repo-relative target.
            return os.path.normpath(str(p))
    return os.path.normpath(str(p))


def _candidate_paths(
    candidate: Dict[str, Any],
    project_root: Optional[Path],
) -> Set[str]:
    """Extract the set of paths a candidate actually covers.

    Mirrors :meth:`Orchestrator._iter_candidate_files` so the gate
    decision matches APPLY behavior exactly:

    1. If ``files: [...]`` is a non-empty list of dicts with valid
       ``file_path`` + ``full_content``, that's the authoritative set.
    2. Otherwise fall back to the legacy single-file ``file_path``
       (which is what APPLY will actually write).
    """
    covered: Set[str] = set()
    files_field = candidate.get("files")
    if isinstance(files_field, list) and files_field:
        for entry in files_field:
            if not isinstance(entry, dict):
                continue
            fp = entry.get("file_path", "") or ""
            if not fp:
                continue
            # Distinguish missing key (model forgot the field, almost
            # always a hallucination) from explicit empty string
            # (valid "truncate file to empty" edit). `get` with a
            # default can't tell those apart, so check membership.
            if "full_content" not in entry:
                continue
            fc = entry["full_content"]
            if not isinstance(fc, str):
                continue
            covered.add(_normalize_path(str(fp), project_root))
        if covered:
            return covered
    # Legacy single-file fallback.
    primary = candidate.get("file_path", "") or ""
    if primary:
        covered.add(_normalize_path(str(primary), project_root))
    return covered


def check_candidate(
    candidate: Dict[str, Any],
    target_files: Sequence[str],
    project_root: Optional[Path] = None,
) -> Optional[Tuple[str, List[str]]]:
    """Return ``None`` if the candidate covers every target file.

    Otherwise return ``(reason, missing_paths)`` where ``missing_paths``
    is the list of normalized target paths that the candidate did not
    populate. The caller raises a ``RuntimeError`` with ``reason`` as
    its message and stashes ``missing_paths`` on a private attribute
    so the retry-feedback builder can echo them to the model.
    """
    if not is_enabled():
        return None
    targets = [str(t) for t in (target_files or ()) if t]
    if len(targets) <= 1:
        return None

    normalized_targets = [_normalize_path(t, project_root) for t in targets]
    covered = _candidate_paths(candidate, project_root)
    missing = [t for t in normalized_targets if t and t not in covered]

    if not missing:
        return None

    reason = (
        f"{REASON_PREFIX}: candidate covers "
        f"{len(normalized_targets) - len(missing)}/{len(normalized_targets)} "
        f"target file(s); missing {len(missing)}"
    )
    logger.warning(
        "[MultiFileCoverageGate] %s — targets=%d covered=%d missing=%s",
        reason,
        len(normalized_targets),
        len(covered),
        missing[:5],
    )
    return (reason, missing)


def render_missing_block(
    missing_paths: Iterable[str],
    target_files: Sequence[str],
) -> str:
    """Format a short block naming the missing paths for retry feedback.

    Echoes back the original target_files order (stable presentation)
    even though the internal comparison uses normalized paths.
    """
    missing_set = set(missing_paths)
    ordered: List[str] = []
    for t in target_files:
        if _normalize_path(str(t)) in missing_set:
            ordered.append(str(t))
    if not ordered:
        ordered = list(missing_set)
    lines = "\n".join(f"  - {p}" for p in ordered[:16])
    return (
        "\nMISSING TARGET FILES (your candidate did not cover these):\n"
        f"{lines}\n"
    )
