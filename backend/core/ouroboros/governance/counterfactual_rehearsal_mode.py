"""
Counterfactual Rehearsal Mode
==============================

Closes §40 Wave 3 #7 — the first Wave 3 arc. Per the operator
binding:

  "Run proposed risky changes against last N sessions' op states
   as counterfactual replay corpus BEFORE applying. CC literally
   cannot do this (no session memory). O+V has CausalityDAG +
   session_archive — wire them. Closes a structural moat over CC."

This substrate is a **pure-function rehearsal evaluator** that
runs BEFORE a proposed change reaches APPLY. For each of the
last N postmortem failures, it asks: "did this proposed change
touch the files that failed in this postmortem?" When YES, the
postmortem is surfaced as a **rehearsal concern** — the operator
sees the historical failure-zone overlap before the change lands.

Architectural choice — Interpretation B (Postmortem Matching):

We ship the **postmortem-matching** flavor of counterfactual
rehearsal rather than full deterministic replay because:

1. Postmortem records (``PostmortemRecord``) already carry
   ``target_files``. File-overlap matching against an existing
   substrate is O(1) per record; full DAG replay would require
   the CausalityDAG + replay_determinism harness + per-session
   re-execution.
2. Postmortems are the canonical evidence of past failure
   zones. If proposed-files ∩ postmortem-files ≠ ∅, the change
   is touching code that has previously failed — operator-
   visible signal, no LLM, no replay cost.
3. The 2-3 hour ship estimate per §40.5 matches the matching-
   flavor scope; full replay is a separate slice that composes
   this substrate's verdict as input.

Composition contract — thin pure-function evaluator over canonical
substrates:

* :func:`postmortem_recall.gather_recent_postmortems` — canonical
  walker over ``.ouroboros/sessions/<id>/debug.log`` POSTMORTEM
  records. Returns ``PostmortemRecord(op_id, session_id,
  root_cause, failed_phase, target_files, ...)`` tuples.
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave 2
  #5) — decides rehearsal intensity: cage changes always
  trigger ESCALATE verdict regardless of postmortem matches.

NEVER raises. Sessions directory missing / postmortem logs
unreadable / candidate target_files empty all degrade to
``CLEAN`` or ``EMPTY_TARGET`` verdict, not exception.

Closed 4-value :class:`RehearsalVerdict` (top-level):

  CLEAN              ✓ no postmortem overlap and no cage touch
  CONCERN_RAISED     ⚠ ≥1 historical failure-zone overlap
  ESCALATE           🚨 proposed change touches governance/ —
                       operator must approve regardless of
                       postmortem overlap (defense-in-depth
                       with Wave 2 #5 boundary gate)
  DISABLED           ◌ master flag off OR substrate unavailable

Closed 4-value :class:`RehearsalIntensity`:

  SKIP               op has no target_files (e.g., pure analysis)
  LIGHTWEIGHT        downstream-only change — file-overlap probe
  HEAVYWEIGHT        cage-touching change — full overlap + escalate
  DISABLED           master off

§33.1 cognitive substrate ``JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED``
default-**FALSE** — operator-paced opt-in (the orchestrator may
call ``evaluate_rehearsal`` proactively as part of pre-APPLY
review, but with master off the substrate returns ``DISABLED``
and adds no cost).

Authority asymmetry (AST-pinned): imports stdlib +
governance_boundary_gate (Wave 2 #5 sibling) + postmortem_recall
ONLY. Does NOT import orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router / change_engine
/ semantic_guardian / auto_committer / risk_tier_floor. The
substrate is a read-only evaluator; consumer-side integration
(orchestrator pre-APPLY hook) is a separate slice.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


COUNTERFACTUAL_REHEARSAL_SCHEMA_VERSION: str = (
    "counterfactual_rehearsal.1"
)


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED"
_ENV_MAX_POSTMORTEMS = (
    "JARVIS_COUNTERFACTUAL_REHEARSAL_MAX_POSTMORTEMS"
)
_ENV_CONCERN_THRESHOLD = (
    "JARVIS_COUNTERFACTUAL_REHEARSAL_CONCERN_THRESHOLD"
)

_DEFAULT_MAX_POSTMORTEMS = 50
_DEFAULT_CONCERN_THRESHOLD = 1
_MIN_MAX_POSTMORTEMS = 1
_MAX_MAX_POSTMORTEMS = 10_000


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Substrate returns ``DISABLED`` verdict
    when off (zero cost). Flip ``JARVIS_COUNTERFACTUAL_REHEARSAL_
    ENABLED=true`` to run the matching probe pre-APPLY.
    """
    return _flag(_ENV_MASTER, default=False)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def max_postmortems_to_match() -> int:
    """Maximum postmortem records inspected per evaluation.
    Defaults to 50; clamped to [1, 10_000]."""
    return _read_clamped_int(
        _ENV_MAX_POSTMORTEMS,
        _DEFAULT_MAX_POSTMORTEMS,
        _MIN_MAX_POSTMORTEMS,
        _MAX_MAX_POSTMORTEMS,
    )


def concern_threshold() -> int:
    """Number of file-overlap matches required to raise
    ``CONCERN_RAISED``. Defaults to 1 (any overlap warrants
    operator visibility). Clamped to [1, 10_000]."""
    return _read_clamped_int(
        _ENV_CONCERN_THRESHOLD,
        _DEFAULT_CONCERN_THRESHOLD,
        1,
        10_000,
    )


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class RehearsalVerdict(str, enum.Enum):
    """Closed 4-value top-level verdict — bytes-pinned via AST."""

    CLEAN = "clean"
    CONCERN_RAISED = "concern_raised"
    ESCALATE = "escalate"
    DISABLED = "disabled"


class RehearsalIntensity(str, enum.Enum):
    """Closed 4-value intensity dial — bytes-pinned via AST."""

    SKIP = "skip"
    LIGHTWEIGHT = "lightweight"
    HEAVYWEIGHT = "heavyweight"
    DISABLED = "disabled"


_VERDICT_GLYPH: Dict[str, str] = {
    RehearsalVerdict.CLEAN.value: "✓",
    RehearsalVerdict.CONCERN_RAISED.value: "⚠",
    RehearsalVerdict.ESCALATE.value: "🚨",
    RehearsalVerdict.DISABLED.value: "◌",
}


def verdict_glyph(verdict: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class RehearsalConcern:
    """One matched postmortem — frozen audit record."""

    op_id: str
    session_id: str
    failed_phase: str
    root_cause: str
    overlapping_files: Tuple[str, ...]
    timestamp_iso: str
    schema_version: str = COUNTERFACTUAL_REHEARSAL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "session_id": self.session_id,
            "failed_phase": self.failed_phase,
            "root_cause": self.root_cause[:256],
            "overlapping_files": list(self.overlapping_files),
            "timestamp_iso": self.timestamp_iso,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class RehearsalReport:
    """Aggregate rehearsal report — frozen §33.5 artifact."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: RehearsalVerdict
    intensity: RehearsalIntensity
    candidate_target_files: Tuple[str, ...]
    postmortems_scanned: int
    concerns: Tuple[RehearsalConcern, ...]
    """Bounded at 32 entries — pathologically wide overlap
    doesn't bloat downstream consumers."""
    boundary_crossed: bool
    diagnostic: str
    elapsed_s: float
    schema_version: str = COUNTERFACTUAL_REHEARSAL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "intensity": self.intensity.value,
            "candidate_target_files": list(
                self.candidate_target_files,
            ),
            "postmortems_scanned": int(self.postmortems_scanned),
            "concerns": [c.to_dict() for c in self.concerns],
            "boundary_crossed": bool(self.boundary_crossed),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Path normalization (composes Wave 2 #5)
# ===========================================================================


_CONCERN_BOUND = 32


def _normalize_target_files(
    candidate_target_files: Optional[Sequence[Any]],
) -> Tuple[str, ...]:
    """Compose the canonical ``governance_boundary_gate._normalize_path``
    helper to coerce mixed-type / mixed-case path inputs into a
    canonical tuple of forward-slash repo-relative strings.
    NEVER raises."""
    if not candidate_target_files:
        return ()
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            _normalize_path,
        )
    except Exception:  # noqa: BLE001
        # Substrate unavailable — fall back to naive str coercion.
        out: List[str] = []
        for raw in candidate_target_files:
            try:
                s = str(raw or "").replace("\\", "/").strip()
                if s:
                    out.append(s)
            except Exception:  # noqa: BLE001
                continue
        return tuple(out)
    out2: List[str] = []
    for raw in candidate_target_files:
        try:
            s = _normalize_path(raw)
            if s:
                out2.append(s)
        except Exception:  # noqa: BLE001
            continue
    return tuple(out2)


def _is_boundary_crossed(
    target_files: Sequence[str],
) -> bool:
    """Compose canonical Wave 2 #5 boundary gate. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(target_files))
    except Exception:  # noqa: BLE001
        return False


def _load_postmortems(
    max_total: int,
) -> Tuple[Any, ...]:
    """Compose canonical postmortem_recall.gather_recent_postmortems.
    NEVER raises — returns empty tuple on any failure."""
    try:
        from backend.core.ouroboros.governance.postmortem_recall import (  # noqa: E501
            gather_recent_postmortems,
        )
        records = gather_recent_postmortems(max_total=max_total)
        return tuple(records)
    except Exception:  # noqa: BLE001
        return ()


# ===========================================================================
# Pure-function file-overlap probe
# ===========================================================================


def _file_overlap(
    candidate_files: FrozenSet[str],
    postmortem_files: Sequence[str],
) -> Tuple[str, ...]:
    """Pure intersection of candidate target_files ∩ postmortem
    target_files. Returns the sorted overlap tuple (deterministic
    ordering for audit replay). NEVER raises."""
    if not candidate_files:
        return ()
    overlap: List[str] = []
    seen: set = set()
    for raw in postmortem_files:
        try:
            from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
                _normalize_path,
            )
            norm = _normalize_path(raw)
        except Exception:  # noqa: BLE001
            norm = str(raw or "").replace("\\", "/").strip()
        if not norm or norm in seen:
            continue
        if norm in candidate_files:
            overlap.append(norm)
            seen.add(norm)
    overlap.sort()
    return tuple(overlap)


def _concern_from_record(
    record: Any,
    overlap: Tuple[str, ...],
) -> Optional[RehearsalConcern]:
    """Pure-function projection of a PostmortemRecord into a
    RehearsalConcern. NEVER raises. Returns None when overlap is
    empty (caller filters)."""
    if not overlap:
        return None
    try:
        return RehearsalConcern(
            op_id=str(getattr(record, "op_id", "")),
            session_id=str(getattr(record, "session_id", "")),
            failed_phase=str(getattr(record, "failed_phase", "")),
            root_cause=str(getattr(record, "root_cause", "")),
            overlapping_files=overlap,
            timestamp_iso=str(
                getattr(record, "timestamp_iso", ""),
            ),
        )
    except Exception:  # noqa: BLE001
        return None


# ===========================================================================
# Top-level evaluator
# ===========================================================================


def evaluate_rehearsal(
    candidate_target_files: Optional[Sequence[Any]],
    *,
    postmortem_records: Optional[Sequence[Any]] = None,
    now_unix: Optional[float] = None,
) -> RehearsalReport:
    """Pure-function rehearsal evaluator. NEVER raises.

    Parameters
    ----------
    candidate_target_files:
        Proposed change's target file paths. Mixed types tolerated
        (str / Path / bytes / None) — normalized via canonical
        ``governance_boundary_gate._normalize_path``.
    postmortem_records:
        Caller-injectable override (testing seam). Defaults to
        the canonical
        ``postmortem_recall.gather_recent_postmortems()`` walker
        bounded by :func:`max_postmortems_to_match`.

    Returns
    -------
    RehearsalReport
        Frozen §33.5 versioned artifact with one of the four
        canonical verdicts.
    """
    started = time.time() if now_unix is None else float(now_unix)
    normalized = _normalize_target_files(candidate_target_files)

    if not master_enabled():
        return RehearsalReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=RehearsalVerdict.DISABLED,
            intensity=RehearsalIntensity.DISABLED,
            candidate_target_files=normalized,
            postmortems_scanned=0,
            concerns=(),
            boundary_crossed=False,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false — "
                "operator opt-in workflow"
            ),
            elapsed_s=0.0,
        )

    if not normalized:
        return RehearsalReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=RehearsalVerdict.CLEAN,
            intensity=RehearsalIntensity.SKIP,
            candidate_target_files=(),
            postmortems_scanned=0,
            concerns=(),
            boundary_crossed=False,
            diagnostic=(
                "no candidate target_files — rehearsal skipped"
            ),
            elapsed_s=time.time() - started,
        )

    boundary_crossed = _is_boundary_crossed(normalized)
    intensity = (
        RehearsalIntensity.HEAVYWEIGHT
        if boundary_crossed
        else RehearsalIntensity.LIGHTWEIGHT
    )

    # Load postmortem corpus (caller-injected OR canonical walker).
    if postmortem_records is None:
        records = _load_postmortems(max_postmortems_to_match())
    else:
        records = tuple(postmortem_records)

    candidate_set: FrozenSet[str] = frozenset(normalized)
    concerns: List[RehearsalConcern] = []
    for record in records:
        try:
            pm_files = getattr(record, "target_files", ())
            overlap = _file_overlap(
                candidate_set, tuple(pm_files),
            )
            concern = _concern_from_record(record, overlap)
            if concern is None:
                continue
            if len(concerns) < _CONCERN_BOUND:
                concerns.append(concern)
        except Exception:  # noqa: BLE001 — defensive per-record
            continue

    threshold = concern_threshold()
    match_count = len(concerns)

    if boundary_crossed:
        verdict = RehearsalVerdict.ESCALATE
        diagnostic = (
            f"cage-touching change ({len(normalized)} target(s)) "
            f"requires APPROVAL_REQUIRED regardless of postmortem "
            f"overlap; {match_count} historical concern(s) found"
        )
    elif match_count >= threshold:
        verdict = RehearsalVerdict.CONCERN_RAISED
        # Build a diagnostic enumerating top-3 sessions impacted
        top_sessions = sorted({c.session_id for c in concerns})[:3]
        ellipsis = (
            f" (+{len({c.session_id for c in concerns}) - 3} more)"
            if len({c.session_id for c in concerns}) > 3 else ""
        )
        diagnostic = (
            f"proposed change overlaps {match_count} historical "
            f"postmortem(s) across sessions: "
            f"{','.join(top_sessions)}{ellipsis} — operator should "
            "review failure-zone touches before APPLY"
        )
    else:
        verdict = RehearsalVerdict.CLEAN
        diagnostic = (
            f"clean: {len(records)} postmortem(s) scanned, 0 "
            "file-overlap concerns raised"
        )

    return RehearsalReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        intensity=intensity,
        candidate_target_files=normalized,
        postmortems_scanned=len(records),
        concerns=tuple(concerns),
        boundary_crossed=boundary_crossed,
        diagnostic=diagnostic,
        elapsed_s=time.time() - started,
    )


# ===========================================================================
# Renderer
# ===========================================================================


def format_rehearsal_panel(
    report: Optional[RehearsalReport] = None,
    *,
    candidate_target_files: Optional[Sequence[Any]] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"counterfactual rehearsal: disabled "
                f"({_ENV_MASTER}=false)"
            )
        report = evaluate_rehearsal(candidate_target_files)
    if not report.master_enabled:
        return (
            f"counterfactual rehearsal: disabled "
            f"({_ENV_MASTER}=false)"
        )
    glyph = verdict_glyph(report.verdict)
    lines = [
        f"🎭 Counterfactual Rehearsal  {glyph} "
        f"{report.verdict.value}",
        f"  intensity            : {report.intensity.value}",
        f"  candidate_files      : {len(report.candidate_target_files)}",
        f"  postmortems_scanned  : {report.postmortems_scanned}",
        f"  concerns             : {len(report.concerns)}",
        f"  boundary_crossed     : {report.boundary_crossed}",
    ]
    if report.concerns:
        lines.append("  failure-zone overlaps:")
        for c in report.concerns[:5]:
            session_short = (c.session_id or "?")[:16]
            overlap_summary = ", ".join(c.overlapping_files[:3])
            if len(c.overlapping_files) > 3:
                overlap_summary += (
                    f" (+{len(c.overlapping_files) - 3} more)"
                )
            lines.append(
                f"    - session={session_short} "
                f"phase={c.failed_phase or '?'} "
                f"files=[{overlap_summary}]"
            )
        if len(report.concerns) > 5:
            lines.append(
                f"    ... (+{len(report.concerns) - 5} more)"
            )
    lines.append(f"  diagnostic           : {report.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "counterfactual_rehearsal_mode.py"
    )

    _EXPECTED_VERDICTS = {
        "clean", "concern_raised", "escalate", "disabled",
    }
    _EXPECTED_INTENSITIES = {
        "skip", "lightweight", "heavyweight", "disabled",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "RehearsalVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"RehearsalVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"RehearsalVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("RehearsalVerdict class not found",)

    def _validate_intensity_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "RehearsalIntensity"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_INTENSITIES - found
                extra = found - _EXPECTED_INTENSITIES
                if missing:
                    return (
                        f"RehearsalIntensity missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"RehearsalIntensity drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("RehearsalIntensity class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (no parallel "
                "cage-prefix detection)",
            )
        if "gather_recent_postmortems" not in source:
            violations.append(
                "must compose canonical "
                "postmortem_recall.gather_recent_postmortems "
                "(no parallel postmortem walker)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_rehearsal_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "RehearsalVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_rehearsal_intensity_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "RehearsalIntensity 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_intensity_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_rehearsal_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure-function evaluator. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_rehearsal_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "counterfactual_rehearsal_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 2 #5 "
                "governance_boundary_gate + canonical "
                "postmortem_recall.gather_recent_postmortems — "
                "no parallel cage-prefix detection, no "
                "parallel postmortem walker."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "counterfactual_rehearsal_mode.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Counterfactual rehearsal mode master switch. "
                "§33.1 cognitive substrate default-FALSE. "
                "When on, pre-APPLY evaluation matches the "
                "proposed change's target_files against the "
                "last N postmortem failures' target_files "
                "and surfaces overlaps as CONCERN_RAISED. "
                "Cage-touching changes route to ESCALATE "
                "regardless (defense-in-depth with Wave 2 #5)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_MAX_POSTMORTEMS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_POSTMORTEMS,
            description=(
                "Maximum postmortem records scanned per "
                "rehearsal evaluation. Clamped to [1, 10_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_POSTMORTEMS}=200",
        ),
        FlagSpec(
            name=_ENV_CONCERN_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_CONCERN_THRESHOLD,
            description=(
                "Number of file-overlap matches required to "
                "raise CONCERN_RAISED. Defaults to 1 — any "
                "historical overlap warrants operator review."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_CONCERN_THRESHOLD}=3",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "COUNTERFACTUAL_REHEARSAL_SCHEMA_VERSION",
    "RehearsalVerdict",
    "RehearsalIntensity",
    "RehearsalConcern",
    "RehearsalReport",
    "master_enabled",
    "max_postmortems_to_match",
    "concern_threshold",
    "verdict_glyph",
    "evaluate_rehearsal",
    "format_rehearsal_panel",
    "register_shipped_invariants",
    "register_flags",
]
