"""
Goal Decomposition Planner — Roadmap Goal → DAG of Sub-Goals
=============================================================

Closes §41.4 Phase 1 second arc (PRD v3.0+). Sits ABOVE the
existing op-level :class:`plan_generator.PlanGenerator` — that
substrate plans the steps WITHIN a single op (which files to
edit, in what order, what to test). This substrate plans the
ops BETWEEN ops: one operator-signed
:class:`roadmap_reader.RoadmapGoal` → N dependent
:class:`SubGoal` artifacts → DAG validated → emitted as
IntentEnvelopes via the canonical router.

The two planners compose orthogonally:

  RoadmapReader →
    Goal Decomposition Planner →
      [N IntentEnvelopes, source="roadmap_decomposed"] →
        UnifiedIntakeRouter →
          (per-op) ROUTE → PLAN (plan_generator) → GENERATE → APPLY

This substrate is the ONLY one that touches multi-op
dependency topology. Iron Gate / SemanticGuardian /
risk_tier_floor / change_engine still apply per-sub-goal in
the canonical pipeline; the decomposer adds NO new authority
surface beyond "translate one goal into N envelopes with
explicit deps".

Architectural choices:

* **Pluggable decomposer** — the substrate's default heuristic
  decomposer is rule-based (no LLM): splits by target_files,
  enumerates description bullets, marks cage-touching sub-goals
  SEQUENTIAL. Operator can inject a model-backed decomposer
  via the ``decomposer`` parameter for richer planning. The
  substrate stays useful out-of-the-box AND scales to
  LLM-quality decomposition when the operator wires it.
* **DAG validation** — topological sort with cycle detection.
  Cycles produce ``DECOMPOSITION_FAILED`` verdict; the original
  goal stays unprocessed (no partial emit).
* **Completion tracking** — append-only §33.4 ledger records
  every status transition. Operator queries
  :func:`get_parent_progress` to see aggregate completion
  ratio + per-sub-goal status.

Composition contract:

* :class:`roadmap_reader.RoadmapGoal` — input type (frozen
  artifact). Substrate reuses it; no parallel goal type.
* :func:`intake.intent_envelope.make_envelope` — canonical
  envelope factory; substrate adds source-distinguishing
  evidence (``"parent_goal_id"``, ``"sub_goal_id"``,
  ``"depends_on"``) so downstream sensors / observability can
  filter on decomposed envelopes specifically.
* :func:`intake.unified_intake_router.UnifiedIntakeRouter.ingest`
  — canonical submit path (router-injectable for testing).
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave
  2 #5) — cage-touch detection on sub-goal target_files.
* :func:`cross_process_jsonl.flock_append_line` — §33.4
  completion ledger at
  ``.jarvis/goal_decomposition_ledger.jsonl``.

NEVER raises. Empty goal / decomposer failure / cycle
detection failure / envelope construction failure all degrade
to a verdict, not exception.

Closed 4-value :class:`DecompositionVerdict`:

  NO_GOAL                  input goal None / empty
  VALID                    decomposed successfully + DAG valid
  TOO_COMPLEX              sub_goal_count > max threshold
  DECOMPOSITION_FAILED     cycle detected OR decomposer raised

Closed 4-value :class:`SubGoalKind`:

  ATOMIC                   single-file change, no deps
  SEQUENTIAL               has explicit upstream sub-goal deps
  PARALLEL                 safe to run alongside siblings
  EXPLORATORY              read-only investigation (no APPLY)

Closed 4-value :class:`CompletionStatus`:

  PROPOSED                 emitted but not yet routed
  IN_PROGRESS              router accepted, op in flight
  COMPLETED                COMPLETE phase fired
  FAILED                   postmortem / cancellation

§33.1 cognitive substrate
``JARVIS_GOAL_DECOMPOSITION_ENABLED`` default-**FALSE**.

Authority asymmetry (AST-pinned): stdlib + lazy-imported
``roadmap_reader`` + ``intake.intent_envelope`` +
``governance_boundary_gate`` + ``cross_process_jsonl``. Does
NOT import orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
tool_executor / plan_generator (the substrate is ABOVE the
PLAN phase, not coupled to it).
"""
from __future__ import annotations

import ast
import asyncio
import enum
import json
import logging
import os
import re
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


GOAL_DECOMPOSITION_SCHEMA_VERSION: str = "goal_decomposition.1"


_ENV_MASTER = "JARVIS_GOAL_DECOMPOSITION_ENABLED"
_ENV_PERSIST = "JARVIS_GOAL_DECOMPOSITION_PERSIST_ENABLED"
_ENV_MAX_SUB_GOALS = "JARVIS_GOAL_DECOMPOSITION_MAX_SUB_GOALS"
_ENV_MAX_DAG_DEPTH = "JARVIS_GOAL_DECOMPOSITION_MAX_DAG_DEPTH"
_ENV_DEFAULT_REPO_NAME = "JARVIS_GOAL_DECOMPOSITION_REPO_NAME"
_ENV_LEDGER_PATH = "JARVIS_GOAL_DECOMPOSITION_LEDGER_PATH"
_ENV_ENVELOPE_SOURCE = "JARVIS_GOAL_DECOMPOSITION_ENVELOPE_SOURCE"

_DEFAULT_MAX_SUB_GOALS = 20
_DEFAULT_MAX_DAG_DEPTH = 10
_DEFAULT_REPO_NAME = "jarvis"
_DEFAULT_LEDGER_REL = ".jarvis/goal_decomposition_ledger.jsonl"
# Reuse the canonical "roadmap" source so the existing
# UrgencyRouter classification + observability filtering apply
# unchanged. Operator may override to a different valid source
# (one of intake._VALID_SOURCES) via env if they want to route
# decomposed envelopes through a different sensor pipeline.
_DEFAULT_ENVELOPE_SOURCE = "roadmap"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
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


def max_sub_goals() -> int:
    return _read_clamped_int(
        _ENV_MAX_SUB_GOALS, _DEFAULT_MAX_SUB_GOALS, 1, 10_000,
    )


def max_dag_depth() -> int:
    return _read_clamped_int(
        _ENV_MAX_DAG_DEPTH, _DEFAULT_MAX_DAG_DEPTH, 1, 100,
    )


def repo_name() -> str:
    raw = os.environ.get(_ENV_DEFAULT_REPO_NAME, "").strip()
    return raw if raw else _DEFAULT_REPO_NAME


def envelope_source() -> str:
    raw = os.environ.get(_ENV_ENVELOPE_SOURCE, "").strip().lower()
    return raw if raw else _DEFAULT_ENVELOPE_SOURCE


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class DecompositionVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    NO_GOAL = "no_goal"
    VALID = "valid"
    TOO_COMPLEX = "too_complex"
    DECOMPOSITION_FAILED = "decomposition_failed"


class SubGoalKind(str, enum.Enum):
    """Closed 4-value sub-goal kind — bytes-pinned via AST."""

    ATOMIC = "atomic"
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    EXPLORATORY = "exploratory"


class CompletionStatus(str, enum.Enum):
    """Closed 4-value completion status — bytes-pinned via AST."""

    PROPOSED = "proposed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


_VERDICT_GLYPH: Dict[str, str] = {
    DecompositionVerdict.NO_GOAL.value: "◌",
    DecompositionVerdict.VALID.value: "✓",
    DecompositionVerdict.TOO_COMPLEX.value: "✗",
    DecompositionVerdict.DECOMPOSITION_FAILED.value: "🚫",
}


_KIND_GLYPH: Dict[str, str] = {
    SubGoalKind.ATOMIC.value: "·",
    SubGoalKind.SEQUENTIAL.value: "→",
    SubGoalKind.PARALLEL.value: "‖",
    SubGoalKind.EXPLORATORY.value: "🔍",
}


_STATUS_GLYPH: Dict[str, str] = {
    CompletionStatus.PROPOSED.value: "○",
    CompletionStatus.IN_PROGRESS.value: "◐",
    CompletionStatus.COMPLETED.value: "●",
    CompletionStatus.FAILED.value: "✗",
}


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def kind_glyph(kind: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(kind, "value"):
            return _KIND_GLYPH.get(str(kind.value), "?")
        return _KIND_GLYPH.get(
            str(kind or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def status_glyph(status: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(status, "value"):
            return _STATUS_GLYPH.get(str(status.value), "?")
        return _STATUS_GLYPH.get(
            str(status or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _coerce_kind(raw: Any) -> SubGoalKind:
    if isinstance(raw, SubGoalKind):
        return raw
    try:
        s = str(getattr(raw, "value", raw) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return SubGoalKind.ATOMIC
    for k in SubGoalKind:
        if k.value == s:
            return k
    return SubGoalKind.ATOMIC


def _coerce_status(raw: Any) -> CompletionStatus:
    if isinstance(raw, CompletionStatus):
        return raw
    try:
        s = str(getattr(raw, "value", raw) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return CompletionStatus.PROPOSED
    for s_enum in CompletionStatus:
        if s_enum.value == s:
            return s_enum
    return CompletionStatus.PROPOSED


# §33.5 frozen artifacts


@dataclass(frozen=True)
class SubGoal:
    """One decomposed sub-goal."""

    sub_goal_id: str
    parent_goal_id: str
    title: str
    description: str
    kind: SubGoalKind
    target_files: Tuple[str, ...]
    depends_on_sub_ids: Tuple[str, ...]
    estimated_complexity: str  # trivial | moderate | complex
    boundary_crossed: bool
    schema_version: str = GOAL_DECOMPOSITION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sub_goal_id": self.sub_goal_id[:128],
            "parent_goal_id": self.parent_goal_id[:128],
            "title": self.title[:256],
            "description": self.description[:1024],
            "kind": self.kind.value,
            "target_files": list(self.target_files),
            "depends_on_sub_ids": list(self.depends_on_sub_ids),
            "estimated_complexity": (
                self.estimated_complexity[:32]
            ),
            "boundary_crossed": bool(self.boundary_crossed),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DecomposedPlan:
    """A full decomposition of one parent goal."""

    parent_goal_id: str
    sub_goals: Tuple[SubGoal, ...]
    dag_valid: bool
    dag_depth: int
    topological_order: Tuple[str, ...]
    diagnostic: str
    schema_version: str = GOAL_DECOMPOSITION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent_goal_id": self.parent_goal_id[:128],
            "sub_goals": [s.to_dict() for s in self.sub_goals],
            "dag_valid": bool(self.dag_valid),
            "dag_depth": int(self.dag_depth),
            "topological_order": list(self.topological_order),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SubGoalEmitOutcome:
    """One per-sub-goal emit outcome."""

    sub_goal_id: str
    emitted: bool
    idempotency_key: str
    error: str = ""
    schema_version: str = GOAL_DECOMPOSITION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "emit",
            "sub_goal_id": self.sub_goal_id[:128],
            "emitted": bool(self.emitted),
            "idempotency_key": self.idempotency_key[:64],
            "error": self.error[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CompletionRecord:
    """One sub-goal status transition. Append-only ledger entry."""

    sub_goal_id: str
    parent_goal_id: str
    status: CompletionStatus
    note: str
    transitioned_at_unix: float
    schema_version: str = GOAL_DECOMPOSITION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "completion",
            "sub_goal_id": self.sub_goal_id[:128],
            "parent_goal_id": self.parent_goal_id[:128],
            "status": self.status.value,
            "note": self.note[:512],
            "transitioned_at_unix": float(
                self.transitioned_at_unix,
            ),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ParentProgress:
    """Aggregate progress over a parent goal's sub-goals."""

    parent_goal_id: str
    total_sub_goals: int
    proposed_count: int
    in_progress_count: int
    completed_count: int
    failed_count: int
    completion_ratio: float
    schema_version: str = GOAL_DECOMPOSITION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent_goal_id": self.parent_goal_id[:128],
            "total_sub_goals": int(self.total_sub_goals),
            "proposed_count": int(self.proposed_count),
            "in_progress_count": int(self.in_progress_count),
            "completed_count": int(self.completed_count),
            "failed_count": int(self.failed_count),
            "completion_ratio": float(self.completion_ratio),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DecompositionReport:
    """Top-level decomposition report."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: DecompositionVerdict
    plan: Optional[DecomposedPlan]
    emit_outcomes: Tuple[SubGoalEmitOutcome, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = GOAL_DECOMPOSITION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "plan": (
                self.plan.to_dict() if self.plan else None
            ),
            "emit_outcomes": [
                o.to_dict() for o in self.emit_outcomes
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers


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


def _flock_append(payload: Mapping[str, Any]) -> bool:
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


def _load_ledger_rows(
    *,
    max_total: Optional[int] = None,
    path_override: Optional[Path] = None,
) -> Tuple[Dict[str, Any], ...]:
    """Plain stdlib read of the §33.4 ledger. NEVER raises."""
    cap = max_total if max_total is not None else 10_000
    target = path_override or ledger_path()
    rows: List[Dict[str, Any]] = []
    try:
        if not target.exists():
            return ()
        with target.open("r", encoding="utf-8") as fp:
            for raw in fp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                rows.append(obj)
                if len(rows) >= cap:
                    break
    except Exception:  # noqa: BLE001
        return tuple(rows)
    return tuple(rows)


# Default heuristic decomposer (pure, no LLM)


_ENUM_BULLET_RE = re.compile(
    r"^\s*(?:\d+\.|[-*•])\s+(.+)$",
    re.MULTILINE,
)


def heuristic_decompose(goal: Any) -> Tuple[SubGoal, ...]:
    """Pure-function rule-based decomposer. NEVER raises.

    Algorithm (deterministic):

    1. If goal has >1 target_files → one SubGoal per file.
       Each is ATOMIC unless cage-touching (→ SEQUENTIAL).
    2. Otherwise, parse description for enumerated bullets
       (lines matching ``^\\d+\\. ...`` or ``^- ...``). Each
       bullet becomes one SubGoal, sequentially dependent on
       its predecessor.
    3. Otherwise return a single SubGoal copying the goal
       verbatim (ATOMIC).

    Returns a tuple — empty when goal is malformed."""
    if goal is None:
        return ()
    try:
        parent_id = str(getattr(goal, "goal_id", "") or "")
        title = str(getattr(goal, "title", "") or "")
        description = str(getattr(goal, "description", "") or "")
        target_files = tuple(
            getattr(goal, "target_files", ()) or ()
        )
    except Exception:  # noqa: BLE001
        return ()
    if not parent_id or not title:
        return ()
    out: List[SubGoal] = []
    # Path A: split by target_files (one SubGoal per file).
    if len(target_files) > 1:
        for i, tf in enumerate(target_files):
            files = (tf,)
            boundary = _is_boundary_crossed(files)
            kind = (
                SubGoalKind.SEQUENTIAL
                if boundary
                else SubGoalKind.PARALLEL
            )
            deps: Tuple[str, ...] = ()
            if boundary and out:
                # Cage-touching sub-goals run after previous
                # ones (sequential ordering for safety).
                deps = (out[-1].sub_goal_id,)
            sub_id = f"{parent_id}::file-{i:02d}"
            out.append(SubGoal(
                sub_goal_id=sub_id,
                parent_goal_id=parent_id,
                title=f"{title} — {tf}",
                description=description,
                kind=kind,
                target_files=files,
                depends_on_sub_ids=deps,
                estimated_complexity="moderate",
                boundary_crossed=boundary,
            ))
        return tuple(out)
    # Path B: split by enumerated bullets in description.
    matches = _ENUM_BULLET_RE.findall(description)
    bullets = [m.strip() for m in matches if m.strip()]
    if len(bullets) > 1:
        prev_id = ""
        for i, bullet in enumerate(bullets):
            files = target_files
            boundary = _is_boundary_crossed(files)
            kind = (
                SubGoalKind.SEQUENTIAL
                if prev_id else (
                    SubGoalKind.SEQUENTIAL if boundary
                    else SubGoalKind.ATOMIC
                )
            )
            deps = (prev_id,) if prev_id else ()
            sub_id = f"{parent_id}::step-{i:02d}"
            out.append(SubGoal(
                sub_goal_id=sub_id,
                parent_goal_id=parent_id,
                title=f"{title} — step {i+1}",
                description=bullet[:1024],
                kind=kind,
                target_files=files,
                depends_on_sub_ids=deps,
                estimated_complexity="moderate",
                boundary_crossed=boundary,
            ))
            prev_id = sub_id
        return tuple(out)
    # Path C: single atomic sub-goal.
    boundary = _is_boundary_crossed(target_files)
    return (SubGoal(
        sub_goal_id=f"{parent_id}::atomic",
        parent_goal_id=parent_id,
        title=title,
        description=description,
        kind=(
            SubGoalKind.SEQUENTIAL
            if boundary
            else SubGoalKind.ATOMIC
        ),
        target_files=target_files,
        depends_on_sub_ids=(),
        estimated_complexity="moderate",
        boundary_crossed=boundary,
    ),)


# ---------------------------------------------------------------------------
# B2: Test-first prerequisite injection (decompose_for_block)
# ---------------------------------------------------------------------------


def decompose_for_block(
    goal: Any,
    *,
    zero_coverage: bool,
    scoper: Any = None,
) -> Tuple[SubGoal, ...]:
    """Decompose a GOAL for an OperationAdvisor BLOCK into topo-ordered SubGoals.

    When ``zero_coverage=True`` the decomposer prepends a mandatory
    "Generate PyTest suite" sub-goal (kind=SEQUENTIAL) that every mutation
    sub-goal BLOCKS ON (via ``depends_on_sub_ids``), so the AI builds its
    own safety net before mutating.

    When ``zero_coverage=False`` the test-gen sub-goal is omitted; only the
    (symbol-scoped) mutation sub-goal(s) are returned.

    Target narrowing: B1 ``isolate_symbols`` is called per-file to narrow
    ``target_files`` to the symbol-bearing files.  On any scoper error the
    whole-file fallback is used (fail-soft).

    Args:
        goal: RoadmapGoal-like object — must have ``goal_id``, ``title``,
              ``description``, ``target_files``.
        zero_coverage: When True, prepend a test-gen sub-goal.
        scoper: Callable with the same signature as ``isolate_symbols``
                (injectable for testing; defaults to B1 ``isolate_symbols``).

    Returns:
        Non-empty topo-ordered tuple of :class:`SubGoal` artifacts.
        NEVER raises.
    """
    # Lazy-import B1 module to avoid circular-import risk at module load.
    if scoper is None:
        try:
            from backend.core.ouroboros.governance.ast_symbol_scoper import (  # noqa: PLC0415
                isolate_symbols as _isolate_symbols,
            )
            scoper = _isolate_symbols
        except Exception:  # noqa: BLE001
            scoper = None

    # --- Extract goal fields (fail-soft) ------------------------------------
    try:
        parent_id = str(getattr(goal, "goal_id", "") or "")
        title = str(getattr(goal, "title", "") or "")
        description = str(getattr(goal, "description", "") or "")
        raw_files = getattr(goal, "target_files", None)
        if raw_files is None:
            target_files: Tuple[str, ...] = ()
        else:
            target_files = tuple(str(f) for f in raw_files)
    except Exception:  # noqa: BLE001
        target_files = ()
        parent_id = ""
        title = ""
        description = ""

    # --- Whole-file fallback sub-goal (used on error paths) -----------------
    def _fallback() -> Tuple[SubGoal, ...]:
        files = target_files or ("",)
        boundary = _is_boundary_crossed(files)
        return (SubGoal(
            sub_goal_id=f"{parent_id or 'unknown'}::step-00",
            parent_goal_id=parent_id or "unknown",
            title=title or "mutate",
            description=description,
            kind=SubGoalKind.SEQUENTIAL if boundary else SubGoalKind.ATOMIC,
            target_files=files,
            depends_on_sub_ids=(),
            estimated_complexity="moderate",
            boundary_crossed=boundary,
        ),)

    if not parent_id or not title:
        return _fallback()

    # --- Symbol-scope each target file via B1 scoper -----------------------
    def _scoped_files_for(files: Tuple[str, ...]) -> Tuple[str, ...]:
        """Return the set of files that have matching symbols (or all files
        if the scoper is unavailable / fails)."""
        if not files or scoper is None:
            return files
        bearing: list[str] = []
        for fp in files:
            try:
                targets = scoper(fp, description)
                # ScopedTarget.symbol == "" means whole-file fallback from B1
                # — still counts as "bearing" (the file is a target).
                bearing.append(fp)
            except Exception:  # noqa: BLE001
                bearing.append(fp)
        return tuple(bearing) if bearing else files

    symbol_files = _scoped_files_for(target_files)
    if not symbol_files:
        symbol_files = target_files or ("",)

    boundary = _is_boundary_crossed(symbol_files)

    # --- Build sub-goals ---------------------------------------------------
    out: List[SubGoal] = []

    if zero_coverage:
        # step-00: mandatory test-gen prerequisite
        test_id = f"{parent_id}::step-00"
        # Derive a short symbol hint for the title from the description
        # (first word that looks like a Python identifier, or empty).
        _sym_hint = ""
        for _tok in description.split():
            _clean = _tok.strip(".,;:()[]")
            if _clean.isidentifier() and len(_clean) > 3:
                _sym_hint = _clean
                break
        test_title = (
            f"Generate PyTest suite for {_sym_hint}" if _sym_hint
            else f"Generate PyTest suite for {title}"
        )
        out.append(SubGoal(
            sub_goal_id=test_id,
            parent_goal_id=parent_id,
            title=test_title,
            description=(
                f"Generate a comprehensive PyTest test suite covering "
                f"the symbols targeted by: {description[:512]}"
            ),
            kind=SubGoalKind.SEQUENTIAL,
            target_files=symbol_files,
            depends_on_sub_ids=(),
            estimated_complexity="moderate",
            boundary_crossed=boundary,
        ))

        # step-01: mutation sub-goal that blocks on the test
        mutation_id = f"{parent_id}::step-01"
        out.append(SubGoal(
            sub_goal_id=mutation_id,
            parent_goal_id=parent_id,
            title=title,
            description=description,
            kind=SubGoalKind.SEQUENTIAL,
            target_files=symbol_files,
            depends_on_sub_ids=(test_id,),
            estimated_complexity="moderate",
            boundary_crossed=boundary,
        ))
    else:
        # No test-gen sub-goal; just the (symbol-scoped) mutation sub-goal.
        mutation_id = f"{parent_id}::step-00"
        out.append(SubGoal(
            sub_goal_id=mutation_id,
            parent_goal_id=parent_id,
            title=title,
            description=description,
            kind=SubGoalKind.SEQUENTIAL if boundary else SubGoalKind.ATOMIC,
            target_files=symbol_files,
            depends_on_sub_ids=(),
            estimated_complexity="moderate",
            boundary_crossed=boundary,
        ))

    return tuple(out)


# DAG validation


def _topological_sort(
    sub_goals: Sequence[SubGoal],
) -> Tuple[bool, Tuple[str, ...], int]:
    """Kahn's algorithm. Returns
    ``(is_valid, sorted_ids, max_depth)``.

    ``is_valid=False`` indicates either a cycle or a dep
    pointing to an unknown sub_id.
    NEVER raises."""
    by_id: Dict[str, SubGoal] = {
        s.sub_goal_id: s for s in sub_goals
    }
    if len(by_id) != len(sub_goals):
        # Duplicate sub_goal_ids — invalid DAG.
        return False, (), 0
    # Validate all deps refer to known sub-goals.
    for s in sub_goals:
        for d in s.depends_on_sub_ids:
            if d not in by_id:
                return False, (), 0
    # In-degree count.
    in_deg: Dict[str, int] = {sid: 0 for sid in by_id.keys()}
    out_adj: Dict[str, List[str]] = {
        sid: [] for sid in by_id.keys()
    }
    for s in sub_goals:
        for d in s.depends_on_sub_ids:
            in_deg[s.sub_goal_id] += 1
            out_adj[d].append(s.sub_goal_id)
    # Kahn's queue: start with all zero in-degree nodes,
    # sorted alphabetically for deterministic output.
    queue: List[str] = sorted(
        [sid for sid, d in in_deg.items() if d == 0]
    )
    sorted_ids: List[str] = []
    depth_by_id: Dict[str, int] = {sid: 0 for sid in queue}
    max_depth_seen = 0 if queue else 0
    while queue:
        current = queue.pop(0)
        sorted_ids.append(current)
        for nxt in sorted(out_adj.get(current, [])):
            in_deg[nxt] -= 1
            nxt_depth = depth_by_id.get(current, 0) + 1
            if nxt_depth > depth_by_id.get(nxt, 0):
                depth_by_id[nxt] = nxt_depth
            if in_deg[nxt] == 0:
                queue.append(nxt)
                if depth_by_id[nxt] > max_depth_seen:
                    max_depth_seen = depth_by_id[nxt]
    if len(sorted_ids) != len(sub_goals):
        # Cycle detected — not all nodes reached.
        return False, (), 0
    return True, tuple(sorted_ids), max_depth_seen


# Envelope construction


def _make_envelope_for_sub_goal(
    sub_goal: SubGoal,
    *,
    repo_override: Optional[str] = None,
    source_override: Optional[str] = None,
) -> Optional[Any]:
    """Compose intake.intent_envelope.make_envelope for one
    sub-goal. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: E501
            make_envelope,
        )
    except ImportError:
        return None
    try:
        # Map SubGoalKind → urgency. Cage-touching / sequential
        # ⇒ tighter urgency band so they don't race siblings.
        kind = sub_goal.kind
        if (
            kind is SubGoalKind.SEQUENTIAL
            or sub_goal.boundary_crossed
        ):
            urgency = "high"
        elif kind is SubGoalKind.EXPLORATORY:
            urgency = "low"
        else:
            urgency = "normal"
        env = make_envelope(
            source=(
                source_override
                if source_override is not None
                else envelope_source()
            ),
            description=(
                f"{sub_goal.title}\n\n{sub_goal.description}"
            ),
            target_files=sub_goal.target_files
            if sub_goal.target_files
            else ("(no target files specified)",),
            repo=repo_override if repo_override else repo_name(),
            confidence=0.9,
            urgency=urgency,
            evidence={
                "parent_goal_id": sub_goal.parent_goal_id,
                "sub_goal_id": sub_goal.sub_goal_id,
                "sub_goal_kind": sub_goal.kind.value,
                "depends_on_sub_ids": list(
                    sub_goal.depends_on_sub_ids,
                ),
                "boundary_crossed": (
                    sub_goal.boundary_crossed
                ),
                "estimated_complexity": (
                    sub_goal.estimated_complexity
                ),
                "signature": sub_goal.sub_goal_id,
            },
            requires_human_ack=False,
            signal_id=(
                f"goal_decomp_{sub_goal.sub_goal_id[:80]}"
            ),
        )
        return env
    except Exception:  # noqa: BLE001
        return None


# Public API — decomposition


def decompose_goal(
    goal: Any,
    *,
    decomposer: Optional[Callable[[Any], Sequence[SubGoal]]] = None,
) -> Tuple[DecompositionVerdict, Optional[DecomposedPlan], str]:
    """Pure decomposition. NEVER raises. Returns
    ``(verdict, plan_or_None, diagnostic)``.

    Side-effect-free: no envelopes emitted, no ledger writes.
    Operator may call this to inspect the decomposition without
    triggering autonomous work."""
    if goal is None:
        return (
            DecompositionVerdict.NO_GOAL,
            None,
            "goal is None",
        )
    try:
        goal_id = str(getattr(goal, "goal_id", "") or "")
        title = str(getattr(goal, "title", "") or "")
    except Exception:  # noqa: BLE001
        return (
            DecompositionVerdict.NO_GOAL,
            None,
            "goal missing required attributes",
        )
    if not goal_id or not title:
        return (
            DecompositionVerdict.NO_GOAL,
            None,
            "goal missing id or title",
        )

    impl = decomposer if decomposer is not None else heuristic_decompose
    try:
        sub_goals = tuple(impl(goal))
    except Exception as exc:  # noqa: BLE001
        return (
            DecompositionVerdict.DECOMPOSITION_FAILED,
            None,
            f"decomposer raised: {exc!r}"[:200],
        )

    if not sub_goals:
        return (
            DecompositionVerdict.DECOMPOSITION_FAILED,
            None,
            "decomposer returned empty tuple",
        )

    cap = max_sub_goals()
    if len(sub_goals) > cap:
        return (
            DecompositionVerdict.TOO_COMPLEX,
            None,
            f"sub_goal count {len(sub_goals)} > max {cap}",
        )

    is_valid, sorted_ids, depth = _topological_sort(sub_goals)
    if not is_valid:
        return (
            DecompositionVerdict.DECOMPOSITION_FAILED,
            None,
            "DAG invalid: cycle detected OR unknown dep ref",
        )

    depth_cap = max_dag_depth()
    if depth > depth_cap:
        return (
            DecompositionVerdict.TOO_COMPLEX,
            None,
            f"DAG depth {depth} > max {depth_cap}",
        )

    plan = DecomposedPlan(
        parent_goal_id=goal_id,
        sub_goals=sub_goals,
        dag_valid=True,
        dag_depth=depth,
        topological_order=sorted_ids,
        diagnostic=(
            f"{len(sub_goals)} sub-goal(s), DAG depth {depth}"
        ),
    )
    return DecompositionVerdict.VALID, plan, plan.diagnostic


async def emit_sub_goal_envelopes(
    plan: DecomposedPlan,
    *,
    router: Any = None,
    now_unix: Optional[float] = None,
) -> Tuple[SubGoalEmitOutcome, ...]:
    """Emit one IntentEnvelope per sub-goal in topological
    order. NEVER raises.

    When ``router`` is None: dry-run mode — envelopes are
    constructed (validation runs through IntentEnvelope's own
    __post_init__) but NOT submitted. Outcomes record the
    dry-run status."""
    outcomes: List[SubGoalEmitOutcome] = []
    if plan is None or not plan.sub_goals:
        return ()
    started = time.time() if now_unix is None else float(now_unix)
    # Emit in topological order so deps land first.
    by_id: Dict[str, SubGoal] = {
        s.sub_goal_id: s for s in plan.sub_goals
    }
    for sid in plan.topological_order:
        sub = by_id.get(sid)
        if sub is None:
            continue
        env = _make_envelope_for_sub_goal(sub)
        if env is None:
            outcomes.append(SubGoalEmitOutcome(
                sub_goal_id=sid,
                emitted=False,
                idempotency_key="",
                error="envelope construction failed",
            ))
            continue
        if router is None:
            outcomes.append(SubGoalEmitOutcome(
                sub_goal_id=sid,
                emitted=False,
                idempotency_key=getattr(env, "idempotency_key", ""),
                error="router not provided (dry-run)",
            ))
            continue
        try:
            result = await router.ingest(env)
            outcomes.append(SubGoalEmitOutcome(
                sub_goal_id=sid,
                emitted=True,
                idempotency_key=str(result or "")[:64],
                error="",
            ))
            # Mark PROPOSED status in the ledger so completion
            # tracking can later observe the lifecycle start.
            mark_sub_goal_status(
                sub_goal_id=sid,
                parent_goal_id=sub.parent_goal_id,
                status=CompletionStatus.PROPOSED,
                note="emitted via router",
                now_unix=started,
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(SubGoalEmitOutcome(
                sub_goal_id=sid,
                emitted=False,
                idempotency_key=getattr(env, "idempotency_key", ""),
                error=f"ingest failed: {exc!r}"[:200],
            ))
    return tuple(outcomes)


async def decompose_and_emit(
    goal: Any,
    *,
    decomposer: Optional[Callable[[Any], Sequence[SubGoal]]] = None,
    router: Any = None,
    now_unix: Optional[float] = None,
) -> DecompositionReport:
    """Top-level: decompose + emit. NEVER raises."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return DecompositionReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=DecompositionVerdict.NO_GOAL,
            plan=None,
            emit_outcomes=(),
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )
    verdict, plan, diagnostic = decompose_goal(
        goal, decomposer=decomposer,
    )
    outcomes: Tuple[SubGoalEmitOutcome, ...] = ()
    if verdict is DecompositionVerdict.VALID and plan is not None:
        outcomes = await emit_sub_goal_envelopes(
            plan, router=router, now_unix=started,
        )
    report = DecompositionReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        plan=plan,
        emit_outcomes=outcomes,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def decompose_and_emit_sync(
    goal: Any,
    *,
    decomposer: Optional[Callable[[Any], Sequence[SubGoal]]] = None,
    router: Any = None,
    now_unix: Optional[float] = None,
) -> DecompositionReport:
    """Sync wrapper. NEVER raises. Returns NO_GOAL when invoked
    inside a running event loop."""
    started = time.time() if now_unix is None else float(now_unix)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        return DecompositionReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=DecompositionVerdict.NO_GOAL,
            plan=None,
            emit_outcomes=(),
            diagnostic=(
                "sync wrapper invoked inside running event "
                "loop — use decompose_and_emit() instead"
            ),
            elapsed_s=0.0,
        )
    try:
        return asyncio.run(decompose_and_emit(
            goal, decomposer=decomposer,
            router=router, now_unix=now_unix,
        ))
    except Exception as exc:  # noqa: BLE001
        return DecompositionReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=DecompositionVerdict.DECOMPOSITION_FAILED,
            plan=None,
            emit_outcomes=(),
            diagnostic=f"sync wrapper failed: {exc!r}"[:200],
            elapsed_s=0.0,
        )


# Completion tracking


def mark_sub_goal_status(
    *,
    sub_goal_id: str,
    parent_goal_id: str,
    status: Any,
    note: str = "",
    now_unix: Optional[float] = None,
) -> Optional[CompletionRecord]:
    """Append a status transition to the §33.4 ledger. NEVER
    raises. Returns the frozen record (or None when master is
    off / persistence disabled / ledger write failed)."""
    sid = str(sub_goal_id or "").strip()
    pid = str(parent_goal_id or "").strip()
    if not sid or not pid:
        return None
    coerced = _coerce_status(status)
    now = time.time() if now_unix is None else float(now_unix)
    record = CompletionRecord(
        sub_goal_id=sid,
        parent_goal_id=pid,
        status=coerced,
        note=str(note or "")[:512],
        transitioned_at_unix=now,
    )
    if not _flock_append(record.to_dict()):
        return record  # frozen artifact returned even if write fails
    return record


def get_parent_progress(
    parent_goal_id: str,
    *,
    rows_override: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Optional[ParentProgress]:
    """Aggregate sub-goal status into per-parent progress.
    Reads §33.4 ledger. NEVER raises.

    Returns None when master is off OR no records exist for
    this parent."""
    if not master_enabled():
        return None
    pid = str(parent_goal_id or "").strip()
    if not pid:
        return None
    rows = (
        rows_override
        if rows_override is not None
        else _load_ledger_rows()
    )
    # Walk completion rows; keep MOST-RECENT status per
    # sub_goal_id (rows are append-only, so the last entry
    # for a sub_id is its current status).
    latest_by_sub: Dict[str, str] = {}
    for r in rows:
        try:
            if r.get("kind") != "completion":
                continue
            if str(r.get("parent_goal_id") or "") != pid:
                continue
            sub_id = str(r.get("sub_goal_id") or "")
            if not sub_id:
                continue
            status = str(r.get("status") or "")
            latest_by_sub[sub_id] = status
        except Exception:  # noqa: BLE001
            continue
    if not latest_by_sub:
        return None
    total = len(latest_by_sub)
    proposed = sum(
        1 for v in latest_by_sub.values()
        if v == CompletionStatus.PROPOSED.value
    )
    in_prog = sum(
        1 for v in latest_by_sub.values()
        if v == CompletionStatus.IN_PROGRESS.value
    )
    completed = sum(
        1 for v in latest_by_sub.values()
        if v == CompletionStatus.COMPLETED.value
    )
    failed = sum(
        1 for v in latest_by_sub.values()
        if v == CompletionStatus.FAILED.value
    )
    ratio = (completed / total) if total > 0 else 0.0
    return ParentProgress(
        parent_goal_id=pid,
        total_sub_goals=total,
        proposed_count=proposed,
        in_progress_count=in_prog,
        completed_count=completed,
        failed_count=failed,
        completion_ratio=ratio,
    )


def _persist_report(report: DecompositionReport) -> None:
    """§33.4 audit write of decomposition report + emit
    outcomes. NEVER raises."""
    if report.verdict is DecompositionVerdict.NO_GOAL:
        return
    _flock_append({
        "kind": "decomposition", "payload": report.to_dict(),
    })
    for outcome in report.emit_outcomes:
        _flock_append(outcome.to_dict())


def _publish_event(report: DecompositionReport) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.verdict is DecompositionVerdict.NO_GOAL:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_GOAL_DECOMPOSED,
            publish_task_event,
        )
        emitted = sum(
            1 for o in report.emit_outcomes if o.emitted
        )
        publish_task_event(
            EVENT_TYPE_GOAL_DECOMPOSED,
            (
                f"system::goal_decomposition::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "sub_goal_count": (
                    len(report.plan.sub_goals)
                    if report.plan else 0
                ),
                "dag_depth": (
                    report.plan.dag_depth if report.plan else 0
                ),
                "emitted_count": emitted,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_decomposition_panel(
    report: Optional[DecompositionReport] = None,
    *,
    progress: Optional[ParentProgress] = None,
) -> str:
    """NEVER raises."""
    if report is None and progress is None:
        if not master_enabled():
            return (
                f"goal decomposition: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "goal decomposition: no report"
    parts: List[str] = []
    if report is not None:
        if not report.master_enabled:
            return (
                f"goal decomposition: disabled "
                f"({_ENV_MASTER}=false)"
            )
        vg = verdict_glyph(report.verdict)
        lines = [
            f"🧩 Goal Decomposition  {vg} {report.verdict.value}",
        ]
        if report.plan is not None:
            lines.extend([
                f"  parent_goal_id     : "
                f"{report.plan.parent_goal_id[:64]}",
                f"  sub_goals          : "
                f"{len(report.plan.sub_goals)}",
                f"  dag_depth          : {report.plan.dag_depth}",
            ])
            for s in report.plan.sub_goals[:8]:
                kg = kind_glyph(s.kind)
                deps_count = len(s.depends_on_sub_ids)
                lines.append(
                    f"    {kg} {s.sub_goal_id[:40]:<40} "
                    f"({s.kind.value}) deps={deps_count}"
                )
            if len(report.plan.sub_goals) > 8:
                lines.append(
                    f"    ... (+{len(report.plan.sub_goals) - 8} "
                    "more)"
                )
        if report.emit_outcomes:
            emitted = sum(
                1 for o in report.emit_outcomes if o.emitted
            )
            lines.append(
                f"  emitted            : {emitted}"
                f"/{len(report.emit_outcomes)}"
            )
        lines.append(
            f"  diagnostic         : {report.diagnostic}"
        )
        parts.append("\n".join(lines))
    if progress is not None:
        lines2 = [
            f"📊 Progress  {progress.parent_goal_id[:32]}",
            f"  total              : {progress.total_sub_goals}",
            f"  proposed           : {progress.proposed_count} "
            f"{status_glyph(CompletionStatus.PROPOSED)}",
            f"  in_progress        : {progress.in_progress_count} "
            f"{status_glyph(CompletionStatus.IN_PROGRESS)}",
            f"  completed          : {progress.completed_count} "
            f"{status_glyph(CompletionStatus.COMPLETED)}",
            f"  failed             : {progress.failed_count} "
            f"{status_glyph(CompletionStatus.FAILED)}",
            f"  completion_ratio   : "
            f"{progress.completion_ratio:.2f}",
        ]
        parts.append("\n".join(lines2))
    return "\n\n".join(parts)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "goal_decomposition_planner.py"
    )

    _EXPECTED_VERDICTS = {
        "no_goal", "valid", "too_complex",
        "decomposition_failed",
    }
    _EXPECTED_KINDS = {
        "atomic", "sequential", "parallel", "exploratory",
    }
    _EXPECTED_STATUSES = {
        "proposed", "in_progress", "completed", "failed",
    }

    def _validate_taxonomy(
        class_name: str, expected: set,
    ):
        def _validate(tree: ast.AST, source: str) -> tuple:  # noqa: ARG001
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name == class_name
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
                    missing = expected - found
                    extra = found - expected
                    if missing:
                        return (
                            f"{class_name} missing: "
                            f"{sorted(missing)}",
                        )
                    if extra:
                        return (
                            f"{class_name} drift: "
                            f"{sorted(extra)}",
                        )
                    return ()
            return (f"{class_name} class not found",)
        return _validate

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
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
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
        if "intent_envelope" not in source:
            violations.append(
                "must compose intake.intent_envelope "
                "(canonical envelope factory)",
            )
        if "make_envelope" not in source:
            violations.append(
                "must use make_envelope (no parallel "
                "envelope construction)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl "
                "(§33.4 ledger)",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (cage detection)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "goal_decomposition_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "DecompositionVerdict 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "DecompositionVerdict", _EXPECTED_VERDICTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "goal_decomposition_kind_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "SubGoalKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "SubGoalKind", _EXPECTED_KINDS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "goal_decomposition_status_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CompletionStatus 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "CompletionStatus", _EXPECTED_STATUSES,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "goal_decomposition_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — sits ABOVE plan_generator "
                "+ orchestrator. MUST NOT import any of: "
                "orchestrator / iron_gate / policy / "
                "providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / tool_executor / "
                "plan_generator. Substrate emits envelopes "
                "via canonical router only."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "goal_decomposition_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "goal_decomposition_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes intake.intent_envelope."
                "make_envelope (canonical envelope factory) + "
                "Wave 2 #5 governance_boundary_gate + "
                "cross_process_jsonl. No parallel envelope "
                "construction, no parallel cage detection, "
                "no parallel JSONL."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "goal_decomposition_planner.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Goal Decomposition Planner master. §33.1 "
                "default-FALSE. Closes §41.4 Phase 1 second "
                "arc (PRD v3.0+). Sits ABOVE plan_generator: "
                "one RoadmapGoal → N dependent SubGoals → "
                "DAG-validated → IntentEnvelopes emitted via "
                "canonical UnifiedIntakeRouter. Iron Gate / "
                "SemanticGuardian / risk_tier_floor apply "
                "per-sub-goal unchanged."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_MAX_SUB_GOALS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_SUB_GOALS,
            description=(
                "Cap on sub-goals per decomposition. "
                "Decomposition with N > cap returns "
                "TOO_COMPLEX. Default 20. Clamped [1, 10_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_SUB_GOALS}=50",
        ),
        FlagSpec(
            name=_ENV_MAX_DAG_DEPTH,
            type=FlagType.INT,
            default=_DEFAULT_MAX_DAG_DEPTH,
            description=(
                "Cap on DAG depth (longest dep chain). "
                "Deeper DAGs return TOO_COMPLEX. Default 10. "
                "Clamped [1, 100]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_DAG_DEPTH}=20",
        ),
        FlagSpec(
            name=_ENV_DEFAULT_REPO_NAME,
            type=FlagType.STR,
            default=_DEFAULT_REPO_NAME,
            description=(
                "Repo name for emitted envelopes. Default "
                "'jarvis'."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_DEFAULT_REPO_NAME}=jarvis-fork",
        ),
        FlagSpec(
            name=_ENV_ENVELOPE_SOURCE,
            type=FlagType.STR,
            default=_DEFAULT_ENVELOPE_SOURCE,
            description=(
                "Envelope source field. Default 'roadmap' "
                "(reuses RoadmapReader's source so existing "
                "filters apply). Must be a valid value in "
                "intake._VALID_SOURCES."
            ),
            category=Category.ROUTING,
            source_file=src,
            example=f"{_ENV_ENVELOPE_SOURCE}=auto_proposed",
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
    "GOAL_DECOMPOSITION_SCHEMA_VERSION",
    "DecompositionVerdict",
    "SubGoalKind",
    "CompletionStatus",
    "SubGoal",
    "DecomposedPlan",
    "SubGoalEmitOutcome",
    "CompletionRecord",
    "ParentProgress",
    "DecompositionReport",
    "master_enabled",
    "persistence_enabled",
    "max_sub_goals",
    "max_dag_depth",
    "repo_name",
    "envelope_source",
    "ledger_path",
    "verdict_glyph",
    "kind_glyph",
    "status_glyph",
    "heuristic_decompose",
    "decompose_for_block",
    "decompose_goal",
    "emit_sub_goal_envelopes",
    "decompose_and_emit",
    "decompose_and_emit_sync",
    "mark_sub_goal_status",
    "get_parent_progress",
    "format_decomposition_panel",
    "register_shipped_invariants",
    "register_flags",
]
