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
- Slice 7 exception: when ``intake_evidence_json`` carries
  ``attribution.status == "resolved"`` (Slice-6 test→source bridge) and
  ``JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED`` is not falsy, covering
  at least ONE target file passes — attributed scope is permissive
  (either locus may be the fix target), not an exhaustive change-set.
  Zero-coverage candidates are still rejected.
- Slice 8 containment: with ``attribution.status == "resolved"`` and
  ``JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED`` truthy, ANY candidate path
  outside the attributed ``target_files`` rejects
  (``scope_containment``) — even at full coverage. The attributed loci
  are the authoritative write-set for the op.

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
_ENV_SUBSET = "JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED"
_ENV_CONTAINMENT = "JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED"

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


def subset_coverage_enabled() -> bool:
    """Slice 7 master switch (default ON): resolved-attribution
    TestFailure scope is judged with subset semantics — covering >=1
    target file suffices. OFF restores the pre-Slice-7 strict superset
    demand for every op."""
    raw = os.environ.get(_ENV_SUBSET, "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


def containment_enabled() -> bool:
    """Slice 8 (default ON): with RESOLVED attribution the attributed
    loci are authoritative — a candidate touching any path outside them
    is rejected. Provider-agnostic: unlike the DW-only intersection
    guard, this runs at the gate for every route (final-review I1)."""
    raw = os.environ.get(_ENV_CONTAINMENT, "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


def _attribution_resolved(intake_evidence_json: str) -> bool:
    """True iff the op's intake evidence carries ``attribution.status ==
    "resolved"`` — only the Slice-6 test→source attribution bridge stamps
    that status, so it is a sufficient discriminator for a permissive
    (either-locus-may-be-the-fix) TestFailure scope. Fail-CLOSED to
    ``False``: any import/parse fault keeps the strict superset demand,
    i.e. the pre-Slice-7 behavior."""
    if not intake_evidence_json:
        return False
    try:
        from backend.core.ouroboros.governance.intent.test_source_attribution import (  # noqa: E501
            attribution_status,
        )
        return attribution_status(intake_evidence_json) == "resolved"
    except Exception:  # noqa: BLE001 — waiver is a relaxation, never fatal
        return False


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
    *,
    intake_evidence_json: str = "",
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

    attr_resolved = _attribution_resolved(intake_evidence_json)

    covered_targets = len(normalized_targets) - len(missing)

    # Slice 8 — containment: with RESOLVED attribution the attributed
    # loci are the authoritative write-set; any candidate path outside
    # them is rejected regardless of coverage. Fires ONLY when we cover
    # at least one target (otherwise it's a coverage failure, not a
    # containment issue). Fires before the full-coverage early return
    # (extras at full coverage are equally out-of-scope).
    # Non-attributed ops keep legacy semantics.
    if attr_resolved and containment_enabled() and covered_targets >= 1:
        _target_set = {t for t in normalized_targets if t}
        _outside = sorted(p for p in covered if p and p not in _target_set)
        if _outside:
            reason = (
                f"{REASON_PREFIX}: scope_containment: resolved-attribution "
                f"candidate touches {len(_outside)} file(s) outside the "
                f"attributed loci: {', '.join(_outside[:5])}"
            )
            logger.warning("[MultiFileCoverageGate] %s", reason)
            return (reason, _outside)

    if not missing:
        return None

    if (
        covered_targets >= 1
        and subset_coverage_enabled()
        and attr_resolved
    ):
        # Slice 7 (Run #17): resolved-attribution scope is PERMISSIVE —
        # covering >=1 target suffices. (Containment above has already
        # bounded the candidate to the attributed loci when enabled.)
        logger.info(
            "[MultiFileCoverageGate] subset-coverage waiver: resolved "
            "attribution — candidate covers %d/%d target file(s)",
            covered_targets,
            len(normalized_targets),
        )
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


# ---------------------------------------------------------------------------
# Candidate-set normalization (Iron Gate 5 companion) — root cause of the
# multi-file cadence wall (2026-09-06): a mid-size model tends to split ONE
# multi-file change across sibling SINGLE-file candidates (c1=fileA, c2=fileB)
# instead of emitting one files:[...] candidate. ``check_candidate`` is applied
# to EVERY candidate and rejects the whole generation if ANY is partial, so the
# split never lands even though every file WAS generated. This normalizes the
# candidate SET so every surviving candidate covers all targets — reusing the
# gate's OWN path primitives (``_candidate_paths`` / ``_normalize_path``) so the
# composed set can never drift from what ``check_candidate`` judges.
# ---------------------------------------------------------------------------
_ENV_COMPOSE = "JARVIS_MULTIFILE_COMPOSE_ENABLED"


def compose_enabled() -> bool:
    """Default ON. Off → normalization is a byte-identical no-op. Also gated by
    ``is_enabled()`` — no point composing a shape the gate is not enforcing."""
    raw = os.environ.get(_ENV_COMPOSE, "true").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    return is_enabled()


def normalize_candidate_set(
    candidates: "Sequence[Dict[str, Any]]",
    target_files: "Sequence[str]",
    project_root: "Optional[Path]" = None,
) -> "Tuple[Dict[str, Any], ...]":
    """Return a candidate set in which EVERY candidate covers all *target_files*,
    when the model already produced all the content (just in the wrong shape).
    Two adaptive normalizations for a multi-target op, in order:

      1. If >=1 candidate already covers all targets (a proper files:[...] set),
         keep ONLY those complete candidates (drop partial single-file siblings
         that would otherwise trip the all-candidates coverage check).
      2. Else, if the DISTINCT single-file candidates together cover the target
         set, COMPOSE them into ONE files:[...] candidate — the model split one
         change across candidates; reassemble it for the atomic APPLY.

    Otherwise (genuine partial coverage, single target, or disabled) return the
    input unchanged so ``check_candidate`` rejects and drives the existing
    targeted retry. Pure; NEVER raises — any fault returns the input untouched."""
    try:
        cands = list(candidates or ())
        targets = [str(t) for t in (target_files or ()) if str(t).strip()]
        if not compose_enabled() or len(targets) <= 1 or not cands:
            return tuple(cands)
        tset = {
            _normalize_path(t, project_root)
            for t in targets
            if _normalize_path(t, project_root)
        }
        if not tset:
            return tuple(cands)

        # (1) complete candidates already present -> keep only them
        complete = [
            c for c in cands if tset.issubset(_candidate_paths(c, project_root))
        ]
        if complete:
            if len(complete) == len(cands):
                return tuple(cands)  # nothing partial to drop -> byte-identical
            logger.info(
                "[MultiFileCoverageGate] normalize: kept %d complete candidate(s), "
                "dropped %d partial sibling(s)",
                len(complete), len(cands) - len(complete),
            )
            return tuple(complete)

        # (2) compose disjoint single-file candidates covering the target set
        import hashlib
        by_path = {}
        for c in cands:
            paths = _candidate_paths(c, project_root)
            if len(paths) != 1:
                continue  # only genuine single-file candidates are compose sources
            p = next(iter(paths))
            fc = c.get("full_content")
            if p in tset and isinstance(fc, str) and fc and p not in by_path:
                by_path[p] = c
        if not tset.issubset(set(by_path)):
            return tuple(cands)  # cannot fully cover -> let the gate reject + retry

        composed_files = []
        for t in targets:
            tn = _normalize_path(t, project_root)
            if tn not in by_path:
                continue
            src = by_path[tn]
            fc = src["full_content"]
            composed_files.append({
                "file_path": t,
                "full_content": fc,
                "rationale": (src.get("rationale") or "composed from per-file candidate"),
                "file_hash": hashlib.sha256(fc.encode()).hexdigest(),
            })
        if len(composed_files) < len(targets):
            return tuple(cands)
        _primary = composed_files[0]
        composed = {
            "candidate_id": "composed-multifile",
            "file_path": _primary["file_path"],
            "full_content": _primary["full_content"],
            "rationale": (
                "auto-composed: model returned per-file candidates that together "
                "cover all target files"
            ),
            "files": composed_files,
            "candidate_hash": hashlib.sha256(
                "".join(f["file_hash"] for f in composed_files).encode()
            ).hexdigest(),
            "source_hash": cands[0].get("source_hash", ""),
            "source_path": cands[0].get("source_path", ""),
        }
        logger.info(
            "[MultiFileCoverageGate] normalize: composed %d per-file candidate(s) "
            "into one multi-file candidate covering %d target(s)",
            len(composed_files), len(targets),
        )
        return (composed,)
    except Exception:  # noqa: BLE001 — normalization is additive, never fatal
        return tuple(candidates or ())
