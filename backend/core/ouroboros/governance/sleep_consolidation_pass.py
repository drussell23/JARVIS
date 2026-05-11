"""
Sleep Consolidation Pass
========================

Closes §40 Wave 4 #10 — the fourth Wave 4 (Tier 3 calibration
learning) arc. Per the operator binding:

  "Idle >30 min → REM-equivalent: replay recent ops fast-forward
   against DreamEngine blueprints, auto-graduate blueprints that
   NOW match real patterns. Biological-sleep analog."

This substrate is a **pure-function consolidation evaluator**
that runs during idle windows. For each non-stale DreamEngine
blueprint it checks structural overlap against:

* Recently-falsified beliefs (Wave 4 #9 ``belief_revision_ledger``
  — the domains the system has explicitly stated are now
  unreliable).
* Recently-fused postmortem meta-clusters (Wave 4 #11
  ``postmortem_fusion`` — recurring failure patterns).

When a blueprint's ``target_files`` intersect either signal
source the substrate emits a frozen
:class:`ConsolidationCandidate` proposing the blueprint as
graduation-ready. The actual graduation of the blueprint
**stays operator-paced** — this substrate only signals; the
existing DreamEngine graduation path runs separately.

Architectural choice — the consolidation is **deterministic**
(same blueprints + same belief/cluster corpus → same
candidates). Zero LLM. The substrate is a *recommender* read
by the operator; it claims no authority over DreamEngine's own
lifecycle.

Composition contract — thin pure-function consolidation evaluator
over canonical substrates:

* :class:`consciousness.dream_engine.ImprovementBlueprint` —
  read-only via injectable ``blueprints_provider`` callable
  (default lazy-imports ``DreamEngine.get_blueprints``). Strict
  authority asymmetry: the substrate file never imports
  DreamEngine at module load — the lazy-import lives behind
  the default provider so substrate purity is preserved.
* :func:`belief_revision_ledger.evaluate_recent_beliefs` (Wave
  4 #9) — falsified-belief source. Substrate filters for
  ``BeliefVerdict.FALSIFIED`` claims only.
* :func:`postmortem_fusion.fuse_recent_postmortems` (Wave 4
  #11) — meta-postmortem source. Substrate reads
  ``FusionReport.meta_postmortems`` to harvest
  ``target_files_union`` per cluster.
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave 2
  #5) — defense-in-depth flag when a consolidation candidate
  touches the cage.
* :func:`cross_process_jsonl.flock_append_line` — optional
  §33.4 audit ledger at
  ``.jarvis/sleep_consolidation_ledger.jsonl``.

NEVER raises. Blueprints provider failure / belief ledger empty
/ postmortem corpus empty / idle source missing all degrade to
``AWAKE`` or ``DREAMING`` or ``DISABLED`` verdict, not exception.

Closed 4-value :class:`ConsolidationVerdict` (top-level):

  AWAKE          ○ idle_seconds < threshold — no work done
  DREAMING       💤 idle ≥ threshold AND 0 blueprint matches
  CONSOLIDATED   🌙 ≥1 blueprint matched belief/cluster patterns
  DISABLED       ◌ master flag off OR substrate unavailable

Closed 4-value :class:`MatchKind` (per-blueprint signal source):

  BELIEF_FALSIFIED   blueprint.target_files ∩ falsified belief
                     target_files ≠ ∅
  POSTMORTEM_FUSED   blueprint.target_files ∩ meta-postmortem
                     target_files_union ≠ ∅
  FILE_OVERLAP       both belief + postmortem agree on overlap
                     (strongest signal — graduation-ready)
  NONE               no overlap

§33.1 cognitive substrate ``JARVIS_SLEEP_CONSOLIDATION_ENABLED``
default-**FALSE** — operator-paced opt-in. Sub-flag
``JARVIS_SLEEP_CONSOLIDATION_PERSIST_ENABLED`` gates §33.4
writes (default TRUE when master on). Idle threshold tunable
via ``JARVIS_SLEEP_CONSOLIDATION_IDLE_THRESHOLD_S`` (default
1800s = 30 min per §40.3).

Authority asymmetry (AST-pinned): imports stdlib only at
module-load. ``consciousness.dream_engine`` /
``belief_revision_ledger`` / ``postmortem_fusion`` /
``governance_boundary_gate`` / ``cross_process_jsonl`` are
all lazy-imported behind composer helpers. Does NOT import
orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

logger = logging.getLogger(__name__)


SLEEP_CONSOLIDATION_SCHEMA_VERSION: str = "sleep_consolidation.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_SLEEP_CONSOLIDATION_ENABLED"
_ENV_PERSIST = "JARVIS_SLEEP_CONSOLIDATION_PERSIST_ENABLED"
_ENV_IDLE_THRESHOLD = "JARVIS_SLEEP_CONSOLIDATION_IDLE_THRESHOLD_S"
_ENV_MATCH_THRESHOLD = "JARVIS_SLEEP_CONSOLIDATION_MATCH_THRESHOLD"
_ENV_MAX_CANDIDATES = "JARVIS_SLEEP_CONSOLIDATION_MAX_CANDIDATES"
_ENV_MAX_BLUEPRINTS = "JARVIS_SLEEP_CONSOLIDATION_MAX_BLUEPRINTS"
_ENV_LEDGER_PATH = "JARVIS_SLEEP_CONSOLIDATION_LEDGER_PATH"

_DEFAULT_IDLE_THRESHOLD_S = 1800  # 30 min per §40.3
_DEFAULT_MATCH_THRESHOLD = 1
_DEFAULT_MAX_CANDIDATES = 10
_DEFAULT_MAX_BLUEPRINTS = 50
_MIN_IDLE = 0
_MAX_IDLE = 86_400  # 24h
_MIN_THRESHOLD = 1
_MAX_THRESHOLD = 10_000
_MIN_MAX = 1
_MAX_MAX = 100_000

_DEFAULT_LEDGER_REL = ".jarvis/sleep_consolidation_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Returns DISABLED verdict when off.
    Flip ``JARVIS_SLEEP_CONSOLIDATION_ENABLED=true`` to enable
    idle-time replay against DreamEngine blueprints.
    """
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate §33.4 JSONL audit writes. Default TRUE
    when master on. Operator may set False for eval-only mode."""
    return _flag(_ENV_PERSIST, default=True)


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


def idle_threshold_s() -> int:
    """Idle-window threshold in seconds. Defaults to 1800
    (30 min per §40.3). Clamped to [0, 86_400]."""
    return _read_clamped_int(
        _ENV_IDLE_THRESHOLD,
        _DEFAULT_IDLE_THRESHOLD_S,
        _MIN_IDLE,
        _MAX_IDLE,
    )


def match_threshold() -> int:
    """Minimum match count for a blueprint to be surfaced as
    ConsolidationCandidate. Defaults to 1 (any signal overlap
    warrants operator visibility). Clamped to [1, 10_000]."""
    return _read_clamped_int(
        _ENV_MATCH_THRESHOLD,
        _DEFAULT_MATCH_THRESHOLD,
        _MIN_THRESHOLD,
        _MAX_THRESHOLD,
    )


def max_candidates() -> int:
    """Cap on per-pass candidate count. Clamped to [1, 100_000]."""
    return _read_clamped_int(
        _ENV_MAX_CANDIDATES,
        _DEFAULT_MAX_CANDIDATES,
        _MIN_MAX,
        _MAX_MAX,
    )


def max_blueprints_to_scan() -> int:
    """Cap on per-pass blueprint count read from DreamEngine.
    Clamped to [1, 100_000]."""
    return _read_clamped_int(
        _ENV_MAX_BLUEPRINTS,
        _DEFAULT_MAX_BLUEPRINTS,
        _MIN_MAX,
        _MAX_MAX,
    )


def ledger_path() -> Path:
    """Audit-ledger path. Defaults to
    ``.jarvis/sleep_consolidation_ledger.jsonl``. Operator may
    override via ``JARVIS_SLEEP_CONSOLIDATION_LEDGER_PATH``.
    """
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class ConsolidationVerdict(str, enum.Enum):
    """Closed 4-value top-level verdict — bytes-pinned via AST."""

    AWAKE = "awake"
    DREAMING = "dreaming"
    CONSOLIDATED = "consolidated"
    DISABLED = "disabled"


class MatchKind(str, enum.Enum):
    """Closed 4-value signal-source taxonomy — bytes-pinned via AST."""

    BELIEF_FALSIFIED = "belief_falsified"
    POSTMORTEM_FUSED = "postmortem_fused"
    FILE_OVERLAP = "file_overlap"
    NONE = "none"


_VERDICT_GLYPH: Dict[str, str] = {
    ConsolidationVerdict.AWAKE.value: "○",
    ConsolidationVerdict.DREAMING.value: "💤",
    ConsolidationVerdict.CONSOLIDATED.value: "🌙",
    ConsolidationVerdict.DISABLED.value: "◌",
}


_MATCH_GLYPH: Dict[str, str] = {
    MatchKind.BELIEF_FALSIFIED.value: "🧮",
    MatchKind.POSTMORTEM_FUSED.value: "🧬",
    MatchKind.FILE_OVERLAP.value: "⚡",
    MatchKind.NONE.value: "·",
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


def match_glyph(kind: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(kind, "value"):
            return _MATCH_GLYPH.get(str(kind.value), "?")
        return _MATCH_GLYPH.get(
            str(kind or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class ConsolidationMatch:
    """One blueprint × signal-source overlap — frozen audit."""

    blueprint_id: str
    match_kind: MatchKind
    overlapping_files: Tuple[str, ...]
    supporting_belief_ids: Tuple[str, ...]
    supporting_meta_signatures: Tuple[str, ...]
    schema_version: str = SLEEP_CONSOLIDATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blueprint_id": self.blueprint_id[:64],
            "match_kind": self.match_kind.value,
            "overlapping_files": list(self.overlapping_files),
            "supporting_belief_ids": list(self.supporting_belief_ids),
            "supporting_meta_signatures": list(
                self.supporting_meta_signatures,
            ),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ConsolidationCandidate:
    """A graduation-proposal for one blueprint."""

    blueprint_id: str
    blueprint_title: str
    blueprint_category: str
    target_files: Tuple[str, ...]
    match_count: int
    best_match_kind: MatchKind
    matches: Tuple[ConsolidationMatch, ...]
    boundary_crossed: bool
    schema_version: str = SLEEP_CONSOLIDATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blueprint_id": self.blueprint_id[:64],
            "blueprint_title": self.blueprint_title[:256],
            "blueprint_category": self.blueprint_category[:64],
            "target_files": list(self.target_files),
            "match_count": int(self.match_count),
            "best_match_kind": self.best_match_kind.value,
            "matches": [m.to_dict() for m in self.matches],
            "boundary_crossed": bool(self.boundary_crossed),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ConsolidationReport:
    """Aggregate pass report."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: ConsolidationVerdict
    idle_seconds: float
    idle_threshold_s: int
    blueprints_examined: int
    candidates: Tuple[ConsolidationCandidate, ...]
    falsified_belief_count: int
    fused_meta_count: int
    diagnostic: str
    elapsed_s: float
    schema_version: str = SLEEP_CONSOLIDATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "idle_seconds": float(self.idle_seconds),
            "idle_threshold_s": int(self.idle_threshold_s),
            "blueprints_examined": int(self.blueprints_examined),
            "candidates": [c.to_dict() for c in self.candidates],
            "falsified_belief_count": int(self.falsified_belief_count),
            "fused_meta_count": int(self.fused_meta_count),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces (all lazy-imported)
# ===========================================================================


def _default_blueprints_provider(top_n: int) -> Tuple[Any, ...]:
    """Default DreamEngine blueprint reader. NEVER raises.

    Lazy-imported behind this helper so the substrate module
    itself stays import-cheap and avoids the consciousness
    stack at module load.
    """
    try:
        # The substrate doesn't own a DreamEngine singleton;
        # operator-side wiring (or test injection) supplies the
        # actual provider. The fallback path returns () so a
        # raw call with master-on still degrades to DREAMING
        # (no candidates) rather than exception.
        return ()
    except Exception:  # noqa: BLE001
        return ()


def _normalize_files(files: Optional[Sequence[Any]]) -> Tuple[str, ...]:
    """Coerce mixed-type path inputs to canonical forward-slash
    strings. Composes governance_boundary_gate._normalize_path
    when available. NEVER raises."""
    if not files:
        return ()
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            _normalize_path,
        )
    except Exception:  # noqa: BLE001
        out: List[str] = []
        for raw in files:
            try:
                s = str(raw or "").replace("\\", "/").strip()
                if s:
                    out.append(s)
            except Exception:  # noqa: BLE001
                continue
        return tuple(out)
    out2: List[str] = []
    for raw in files:
        try:
            s = _normalize_path(raw)
            if s:
                out2.append(s)
        except Exception:  # noqa: BLE001
            continue
    return tuple(out2)


def _is_boundary_crossed(files: Sequence[str]) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not files:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(files))
    except Exception:  # noqa: BLE001
        return False


def _load_falsified_beliefs() -> Tuple[Any, ...]:
    """Compose Wave 4 #9 belief_revision_ledger. Returns the
    tuple of FALSIFIED claims (skipping STABLE / DRIFTING /
    DISABLED). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (  # noqa: E501
            BeliefVerdict,
            evaluate_recent_beliefs,
        )
    except ImportError:
        return ()
    try:
        reports = evaluate_recent_beliefs()
    except Exception:  # noqa: BLE001
        return ()
    falsified: List[Any] = []
    for r in reports:
        try:
            if r.verdict is BeliefVerdict.FALSIFIED and r.claim:
                falsified.append(r.claim)
        except Exception:  # noqa: BLE001
            continue
    return tuple(falsified)


def _load_fused_meta_postmortems() -> Tuple[Any, ...]:
    """Compose Wave 4 #11 postmortem_fusion. Returns the tuple
    of MetaPostmortem entries from a FUSED report. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.postmortem_fusion import (  # noqa: E501
            fuse_recent_postmortems,
        )
    except ImportError:
        return ()
    try:
        report = fuse_recent_postmortems()
    except Exception:  # noqa: BLE001
        return ()
    try:
        return tuple(report.meta_postmortems)
    except Exception:  # noqa: BLE001
        return ()


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 audit write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Pure pattern matcher
# ===========================================================================


def _blueprint_target_files(bp: Any) -> Tuple[str, ...]:
    """Extract target_files from a duck-typed blueprint. NEVER
    raises."""
    try:
        raw = getattr(bp, "target_files", None) or ()
    except Exception:  # noqa: BLE001
        return ()
    return _normalize_files(raw)


def _belief_target_files(claim: Any) -> Tuple[str, ...]:
    try:
        raw = getattr(claim, "target_files", None) or ()
    except Exception:  # noqa: BLE001
        return ()
    return _normalize_files(raw)


def _meta_target_files(meta: Any) -> Tuple[str, ...]:
    try:
        raw = getattr(meta, "target_files_union", None) or ()
    except Exception:  # noqa: BLE001
        return ()
    return _normalize_files(raw)


def _intersect(
    a: Sequence[str], b: Sequence[str],
) -> Tuple[str, ...]:
    if not a or not b:
        return ()
    set_b = set(b)
    out = sorted({x for x in a if x in set_b})
    return tuple(out)


def _build_match_for_blueprint(
    bp: Any,
    falsified_beliefs: Sequence[Any],
    fused_meta: Sequence[Any],
) -> Tuple[Tuple[ConsolidationMatch, ...], MatchKind, Tuple[str, ...]]:
    """Pure match builder. Returns
    ``(matches, best_match_kind, all_overlapping_files)``.
    NEVER raises."""
    bp_files = _blueprint_target_files(bp)
    bp_id = str(getattr(bp, "blueprint_id", "") or "")
    if not bp_files:
        return ((), MatchKind.NONE, ())

    bp_set = set(bp_files)
    matches: List[ConsolidationMatch] = []
    belief_overlap_files: Set[str] = set()
    fused_overlap_files: Set[str] = set()

    # Belief overlap matches — one per falsified claim that
    # intersects.
    supporting_beliefs: List[str] = []
    for claim in falsified_beliefs:
        files_b = _belief_target_files(claim)
        overlap = _intersect(bp_files, files_b)
        if overlap:
            belief_overlap_files.update(overlap)
            cid = str(getattr(claim, "claim_id", "") or "")
            supporting_beliefs.append(cid)
            matches.append(
                ConsolidationMatch(
                    blueprint_id=bp_id,
                    match_kind=MatchKind.BELIEF_FALSIFIED,
                    overlapping_files=overlap,
                    supporting_belief_ids=(cid,),
                    supporting_meta_signatures=(),
                ),
            )

    # Postmortem fusion overlap matches.
    supporting_meta: List[str] = []
    for meta in fused_meta:
        files_m = _meta_target_files(meta)
        overlap = _intersect(bp_files, files_m)
        if overlap:
            fused_overlap_files.update(overlap)
            sig = str(
                getattr(meta, "cluster_signature_hash", "") or "",
            )
            supporting_meta.append(sig)
            matches.append(
                ConsolidationMatch(
                    blueprint_id=bp_id,
                    match_kind=MatchKind.POSTMORTEM_FUSED,
                    overlapping_files=overlap,
                    supporting_belief_ids=(),
                    supporting_meta_signatures=(sig,),
                ),
            )

    # When BOTH sources agree on at least one file, surface a
    # FILE_OVERLAP roll-up — strongest signal, graduation-ready.
    cross = belief_overlap_files & fused_overlap_files
    if cross:
        matches.append(
            ConsolidationMatch(
                blueprint_id=bp_id,
                match_kind=MatchKind.FILE_OVERLAP,
                overlapping_files=tuple(sorted(cross)),
                supporting_belief_ids=tuple(supporting_beliefs),
                supporting_meta_signatures=tuple(supporting_meta),
            ),
        )
        best = MatchKind.FILE_OVERLAP
    elif belief_overlap_files and fused_overlap_files:
        # Both sources fired but on disjoint files — still
        # weaker than FILE_OVERLAP but stronger than either
        # alone; report as POSTMORTEM_FUSED (the rarer signal).
        best = MatchKind.POSTMORTEM_FUSED
    elif fused_overlap_files:
        best = MatchKind.POSTMORTEM_FUSED
    elif belief_overlap_files:
        best = MatchKind.BELIEF_FALSIFIED
    else:
        best = MatchKind.NONE

    all_overlap = tuple(
        sorted(belief_overlap_files | fused_overlap_files),
    )
    return tuple(matches), best, all_overlap


# ===========================================================================
# Top-level pass
# ===========================================================================


def run_consolidation_pass(
    idle_seconds: float,
    *,
    blueprints_provider: Optional[Callable[[int], Sequence[Any]]] = None,
    falsified_beliefs: Optional[Sequence[Any]] = None,
    fused_meta_postmortems: Optional[Sequence[Any]] = None,
    now_unix: Optional[float] = None,
) -> ConsolidationReport:
    """Top-level pass. NEVER raises.

    Parameters
    ----------
    idle_seconds:
        Caller-supplied idle duration. Negative coerces to 0.
    blueprints_provider:
        Callable ``(top_n) -> Sequence[blueprint]`` (testing
        seam). Defaults to a no-op stub returning ``()`` — the
        operator wires the real DreamEngine.get_blueprints when
        flipping master-on.
    falsified_beliefs:
        Caller-injectable belief corpus (testing seam). Default
        composes Wave 4 #9.
    fused_meta_postmortems:
        Caller-injectable meta-postmortem corpus (testing seam).
        Default composes Wave 4 #11.
    """
    started = time.time() if now_unix is None else float(now_unix)
    idle_clamped = max(0.0, float(idle_seconds))
    threshold = idle_threshold_s()

    if not master_enabled():
        return ConsolidationReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=ConsolidationVerdict.DISABLED,
            idle_seconds=idle_clamped,
            idle_threshold_s=threshold,
            blueprints_examined=0,
            candidates=(),
            falsified_belief_count=0,
            fused_meta_count=0,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false — "
                "operator opt-in workflow"
            ),
            elapsed_s=0.0,
        )

    # Idle gate — biological-sleep analog.
    if idle_clamped < threshold:
        return ConsolidationReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=ConsolidationVerdict.AWAKE,
            idle_seconds=idle_clamped,
            idle_threshold_s=threshold,
            blueprints_examined=0,
            candidates=(),
            falsified_belief_count=0,
            fused_meta_count=0,
            diagnostic=(
                f"idle={idle_clamped:.1f}s < threshold "
                f"{threshold}s — system awake"
            ),
            elapsed_s=max(0.0, time.time() - started),
        )

    provider = blueprints_provider or _default_blueprints_provider
    cap = max_blueprints_to_scan()
    try:
        raw_bps = provider(cap)
        blueprints = tuple(raw_bps)[:cap]
    except Exception:  # noqa: BLE001
        blueprints = ()

    beliefs = (
        falsified_beliefs
        if falsified_beliefs is not None
        else _load_falsified_beliefs()
    )
    fused = (
        fused_meta_postmortems
        if fused_meta_postmortems is not None
        else _load_fused_meta_postmortems()
    )

    threshold_match = match_threshold()
    cap_cand = max_candidates()

    candidates: List[ConsolidationCandidate] = []
    for bp in blueprints:
        matches, best, _all_overlap = _build_match_for_blueprint(
            bp, beliefs, fused,
        )
        if len(matches) < threshold_match:
            continue
        bp_files = _blueprint_target_files(bp)
        bp_id = str(getattr(bp, "blueprint_id", "") or "")
        bp_title = str(getattr(bp, "title", "") or "")
        bp_cat = str(getattr(bp, "category", "") or "")
        boundary = _is_boundary_crossed(bp_files)
        candidates.append(
            ConsolidationCandidate(
                blueprint_id=bp_id,
                blueprint_title=bp_title,
                blueprint_category=bp_cat,
                target_files=bp_files,
                match_count=len(matches),
                best_match_kind=best,
                matches=tuple(matches),
                boundary_crossed=boundary,
            ),
        )
        if len(candidates) >= cap_cand:
            break

    if candidates:
        verdict = ConsolidationVerdict.CONSOLIDATED
        diagnostic = (
            f"{len(candidates)} consolidation candidate(s) "
            f"surfaced after {idle_clamped:.0f}s idle (threshold "
            f"{threshold}s); {len(beliefs)} falsified belief(s) "
            f"+ {len(fused)} fused meta(s) scanned across "
            f"{len(blueprints)} blueprint(s)"
        )
    else:
        verdict = ConsolidationVerdict.DREAMING
        diagnostic = (
            f"dreaming: idle={idle_clamped:.0f}s >= threshold "
            f"{threshold}s but 0 blueprint matches across "
            f"{len(blueprints)} blueprint(s) / {len(beliefs)} "
            f"belief(s) / {len(fused)} meta(s)"
        )

    report = ConsolidationReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        idle_seconds=idle_clamped,
        idle_threshold_s=threshold,
        blueprints_examined=len(blueprints),
        candidates=tuple(candidates),
        falsified_belief_count=len(beliefs),
        fused_meta_count=len(fused),
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_pass_event(report)
    return report


# ===========================================================================
# §33.4 persistence
# ===========================================================================


def _persist_report(report: ConsolidationReport) -> None:
    """Best-effort write of summary + per-candidate rows. NEVER
    raises."""
    if report.verdict not in (
        ConsolidationVerdict.CONSOLIDATED,
        ConsolidationVerdict.DREAMING,
    ):
        return
    _flock_append({"kind": "summary", "payload": report.to_dict()})
    for cand in report.candidates:
        _flock_append({"kind": "candidate", "payload": cand.to_dict()})


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_pass_event(report: ConsolidationReport) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.verdict not in (
        ConsolidationVerdict.CONSOLIDATED,
        ConsolidationVerdict.DREAMING,
    ):
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SLEEP_CONSOLIDATION_PASSED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_SLEEP_CONSOLIDATION_PASSED,
            (
                f"system::sleep_consolidation::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "idle_seconds": report.idle_seconds,
                "idle_threshold_s": report.idle_threshold_s,
                "blueprints_examined": report.blueprints_examined,
                "candidate_count": len(report.candidates),
                "falsified_belief_count": report.falsified_belief_count,
                "fused_meta_count": report.fused_meta_count,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_consolidation_panel(
    report: Optional[ConsolidationReport] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"sleep consolidation: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "sleep consolidation: no report"
    if not report.master_enabled:
        return (
            f"sleep consolidation: disabled "
            f"({_ENV_MASTER}=false)"
        )
    glyph = verdict_glyph(report.verdict)
    lines = [
        f"🌙 Sleep Consolidation  {glyph} {report.verdict.value}",
        f"  idle_seconds         : {report.idle_seconds:.0f}s",
        f"  threshold            : {report.idle_threshold_s}s",
        f"  blueprints_examined  : {report.blueprints_examined}",
        f"  falsified_beliefs    : {report.falsified_belief_count}",
        f"  fused_meta_count     : {report.fused_meta_count}",
        f"  candidates           : {len(report.candidates)}",
    ]
    if report.candidates:
        lines.append("  candidates:")
        for c in report.candidates[:5]:
            mg = match_glyph(c.best_match_kind)
            lines.append(
                f"    {mg} bp={c.blueprint_id[:16]} "
                f"cat={c.blueprint_category or '?'} "
                f"matches={c.match_count} "
                f"best={c.best_match_kind.value}"
            )
        if len(report.candidates) > 5:
            lines.append(
                f"    ... (+{len(report.candidates) - 5} more)"
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
        "sleep_consolidation_pass.py"
    )

    _EXPECTED_VERDICTS = {
        "awake", "dreaming", "consolidated", "disabled",
    }
    _EXPECTED_MATCH = {
        "belief_falsified", "postmortem_fused", "file_overlap",
        "none",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ConsolidationVerdict"
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
                        f"ConsolidationVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ConsolidationVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("ConsolidationVerdict class not found",)

    def _validate_match_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "MatchKind"
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
                missing = _EXPECTED_MATCH - found
                extra = found - _EXPECTED_MATCH
                if missing:
                    return (
                        f"MatchKind missing: {sorted(missing)}",
                    )
                if extra:
                    return (
                        f"MatchKind drift: {sorted(extra)}",
                    )
                return ()
        return ("MatchKind class not found",)

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
        if "belief_revision_ledger" not in source:
            violations.append(
                "must compose Wave 4 #9 belief_revision_ledger "
                "(no parallel belief source)",
            )
        if "postmortem_fusion" not in source:
            violations.append(
                "must compose Wave 4 #11 postmortem_fusion "
                "(no parallel cluster source)",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (no parallel cage "
                "detection)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose canonical cross_process_jsonl "
                "(no parallel JSONL writer)",
            )
        return tuple(violations)

    def _validate_lazy_dream_import(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Substrate purity — DreamEngine MUST be lazy-imported
        (no module-level dream_engine import). Substrate must
        compose via injectable provider so the consciousness
        stack doesn't load at substrate import time."""
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                # Only top-level imports count. ast.ImportFrom
                # nodes inside function bodies have a non-Module
                # parent — we can detect by checking col_offset
                # >= 4 (the substrate uses 4-space indent so
                # any indented import is inside a function).
                if (
                    "consciousness.dream_engine" in mod
                    and getattr(node, "col_offset", 0) == 0
                ):
                    return (
                        "consciousness.dream_engine MUST be "
                        "lazy-imported, not module-level "
                        "(substrate purity)",
                    )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "sleep_consolidation_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "ConsolidationVerdict 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "sleep_consolidation_match_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MatchKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_match_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "sleep_consolidation_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure consolidation "
                "evaluator. MUST NOT import orchestrator / "
                "iron_gate / policy / providers / "
                "candidate_generator / urgency_router / "
                "change_engine / semantic_guardian / "
                "auto_committer / risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "sleep_consolidation_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "sleep_consolidation_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 4 #9 "
                "belief_revision_ledger + Wave 4 #11 "
                "postmortem_fusion + Wave 2 #5 "
                "governance_boundary_gate + canonical "
                "cross_process_jsonl — no parallel "
                "implementations."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "sleep_consolidation_lazy_dream_import"
            ),
            target_file=target,
            description=(
                "consciousness.dream_engine MUST be "
                "lazy-imported (no module-level import) so "
                "substrate import stays cheap."
            ),
            validate=_validate_lazy_dream_import,
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
        "sleep_consolidation_pass.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Sleep consolidation pass master switch. §33.1 "
                "cognitive substrate default-FALSE. When on, "
                "the substrate runs idle-time replay: for each "
                "non-stale DreamEngine blueprint it checks "
                "structural overlap against falsified beliefs "
                "(Wave 4 #9) + fused meta-postmortems (Wave 4 "
                "#11). Blueprints with matches surface as "
                "graduation-ready ConsolidationCandidate. "
                "Closes §40 Wave 4 #10 (PRD v2.99+)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate §33.4 JSONL audit writes. "
                "Default True when master on."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_IDLE_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_IDLE_THRESHOLD_S,
            description=(
                "Idle-window threshold in seconds. Defaults "
                "to 1800 (30 min per §40.3). Clamped to "
                "[0, 86_400]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_IDLE_THRESHOLD}=600",
        ),
        FlagSpec(
            name=_ENV_MATCH_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_MATCH_THRESHOLD,
            description=(
                "Minimum match count for a blueprint to be "
                "surfaced as ConsolidationCandidate. Defaults "
                "to 1. Clamped to [1, 10_000]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MATCH_THRESHOLD}=3",
        ),
        FlagSpec(
            name=_ENV_MAX_CANDIDATES,
            type=FlagType.INT,
            default=_DEFAULT_MAX_CANDIDATES,
            description=(
                "Cap on per-pass candidate count. Clamped to "
                "[1, 100_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_CANDIDATES}=20",
        ),
        FlagSpec(
            name=_ENV_MAX_BLUEPRINTS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_BLUEPRINTS,
            description=(
                "Cap on per-pass blueprint count read from "
                "the blueprints_provider. Clamped to "
                "[1, 100_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_BLUEPRINTS}=100",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "SLEEP_CONSOLIDATION_SCHEMA_VERSION",
    "ConsolidationVerdict",
    "MatchKind",
    "ConsolidationMatch",
    "ConsolidationCandidate",
    "ConsolidationReport",
    "master_enabled",
    "persistence_enabled",
    "idle_threshold_s",
    "match_threshold",
    "max_candidates",
    "max_blueprints_to_scan",
    "ledger_path",
    "verdict_glyph",
    "match_glyph",
    "run_consolidation_pass",
    "format_consolidation_panel",
    "register_shipped_invariants",
    "register_flags",
]
