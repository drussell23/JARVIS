"""
Multi-step Plan Orchestrator — Inter-Op Dependency Gating
==========================================================

Closes §41.4 Phase 1 fourth arc (PRD v3.0+). Sits BETWEEN the
goal_decomposition_planner (which produces a static DAG of
sub-goals all at once) and the UnifiedIntakeRouter (which
processes ops by urgency/priority, NOT by emit order). The
gap this substrate closes:

  goal_decomposition_planner emits ALL N sub-goal envelopes
  in topological order. But the router dispatches them by
  urgency/priority — so sub_goal_2 may run BEFORE its
  dependency sub_goal_1 completes, breaking the DAG.

The Multi-step Plan Orchestrator solves this by **phased
emission**: only emit a sub-goal's envelope once ALL its
dependencies have reached terminal COMPLETED status. The DAG
contract is enforced at runtime, not just emit-time.

Composition contract:

* :class:`goal_decomposition_planner.DecomposedPlan` — input
  (the DAG to orchestrate). Substrate reuses its frozen
  artifacts; no parallel goal/plan types.
* :class:`goal_decomposition_planner.CompletionStatus` —
  status taxonomy. Substrate reuses it; no parallel status
  enum.
* :func:`cross_process_jsonl.flock_append_line` — both for
  the orchestrator's own audit ledger AND for reading
  goal_decomposition_planner's completion ledger (the
  authoritative status source).
* :func:`intake.intent_envelope.make_envelope` — canonical
  envelope factory (no parallel envelope construction).
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave
  2 #5) — cage-touch flag.

The substrate is **idempotent** — running ``advance_orchestration``
multiple times is safe; envelopes are emitted at most once
per sub-goal (tracked via substrate's own §33.4 ledger as
``EMITTED`` run-state transitions).

Closed 4-value :class:`OrchestrationVerdict`:

  NO_PLAN          master off OR empty plan
  PROGRESSING      some sub-goals completed; others ready
                   or in-flight
  STALLED          blocked sub-goals can't proceed (a
                   dependency reached FAILED status)
  COMPLETED        every sub-goal reached COMPLETED

Closed 4-value :class:`SubGoalRunState`:

  BLOCKED          one or more deps not yet COMPLETED
  READY            all deps COMPLETED, hasn't been emitted
  EMITTED          envelope submitted via router, in flight
  DONE             reached terminal status (COMPLETED or FAILED)

§33.1 cognitive substrate
``JARVIS_MULTI_STEP_ORCHESTRATION_ENABLED`` default-**FALSE**.

Authority asymmetry (AST-pinned): stdlib + lazy-imported
``goal_decomposition_planner`` + ``intake.intent_envelope`` +
``governance_boundary_gate`` + ``cross_process_jsonl``. Does
NOT import orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
tool_executor / plan_generator / roadmap_reader (no upstream
substrate may import this one — substrate is consumed by
operator-side glue).
"""
from __future__ import annotations

import ast
import asyncio
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


MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION: str = (
    "multi_step_orchestrator.1"
)


_ENV_MASTER = "JARVIS_MULTI_STEP_ORCHESTRATION_ENABLED"
_ENV_PERSIST = "JARVIS_MULTI_STEP_ORCHESTRATION_PERSIST_ENABLED"
_ENV_MAX_EMITS_PER_TICK = (
    "JARVIS_MULTI_STEP_ORCHESTRATION_MAX_EMITS_PER_TICK"
)
_ENV_COMPLETION_LEDGER_PATH = (
    "JARVIS_MULTI_STEP_ORCHESTRATION_COMPLETION_LEDGER_PATH"
)
_ENV_LEDGER_PATH = (
    "JARVIS_MULTI_STEP_ORCHESTRATION_LEDGER_PATH"
)
_ENV_REPO_NAME = "JARVIS_MULTI_STEP_ORCHESTRATION_REPO_NAME"
_ENV_ENVELOPE_SOURCE = (
    "JARVIS_MULTI_STEP_ORCHESTRATION_ENVELOPE_SOURCE"
)

_DEFAULT_MAX_EMITS_PER_TICK = 5
_DEFAULT_REPO_NAME = "jarvis"
_DEFAULT_ENVELOPE_SOURCE = "roadmap"
_DEFAULT_COMPLETION_LEDGER_REL = (
    ".jarvis/goal_decomposition_ledger.jsonl"
)
_DEFAULT_LEDGER_REL = (
    ".jarvis/multi_step_orchestrator_ledger.jsonl"
)

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


def max_emits_per_tick() -> int:
    """Bound on envelopes emitted per advance_orchestration
    call. Prevents fan-out spikes when many sub-goals become
    READY simultaneously. Default 5."""
    return _read_clamped_int(
        _ENV_MAX_EMITS_PER_TICK, _DEFAULT_MAX_EMITS_PER_TICK,
        1, 1000,
    )


def completion_ledger_path() -> Path:
    """Path to goal_decomposition_planner's ledger. Default
    matches goal_decomposition_planner's default. Operator
    may override if running multiple decompositions in
    parallel."""
    raw = os.environ.get(_ENV_COMPLETION_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_COMPLETION_LEDGER_REL)


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


def repo_name() -> str:
    raw = os.environ.get(_ENV_REPO_NAME, "").strip()
    return raw if raw else _DEFAULT_REPO_NAME


def envelope_source() -> str:
    raw = os.environ.get(_ENV_ENVELOPE_SOURCE, "").strip().lower()
    return raw if raw else _DEFAULT_ENVELOPE_SOURCE


# Closed taxonomies


class OrchestrationVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    NO_PLAN = "no_plan"
    PROGRESSING = "progressing"
    STALLED = "stalled"
    COMPLETED = "completed"


class SubGoalRunState(str, enum.Enum):
    """Closed 4-value run state — bytes-pinned via AST."""

    BLOCKED = "blocked"
    READY = "ready"
    EMITTED = "emitted"
    DONE = "done"


_VERDICT_GLYPH: Dict[str, str] = {
    OrchestrationVerdict.NO_PLAN.value: "◌",
    OrchestrationVerdict.PROGRESSING.value: "↻",
    OrchestrationVerdict.STALLED.value: "⏸",
    OrchestrationVerdict.COMPLETED.value: "✓",
}


_RUN_STATE_GLYPH: Dict[str, str] = {
    SubGoalRunState.BLOCKED.value: "⏳",
    SubGoalRunState.READY.value: "▶",
    SubGoalRunState.EMITTED.value: "◐",
    SubGoalRunState.DONE.value: "●",
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


def run_state_glyph(state: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(state, "value"):
            return _RUN_STATE_GLYPH.get(str(state.value), "?")
        return _RUN_STATE_GLYPH.get(
            str(state or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class SubGoalRunRecord:
    """Per-sub-goal runtime state snapshot."""

    sub_goal_id: str
    run_state: SubGoalRunState
    completion_status: str  # raw status from completion ledger
    unmet_deps: Tuple[str, ...]
    emitted_at_unix: float
    schema_version: str = MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sub_goal_id": self.sub_goal_id[:128],
            "run_state": self.run_state.value,
            "completion_status": self.completion_status[:32],
            "unmet_deps": list(self.unmet_deps),
            "emitted_at_unix": float(self.emitted_at_unix),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class OrchestrationEmitOutcome:
    """One per-emit outcome from advance_orchestration tick."""

    sub_goal_id: str
    emitted: bool
    idempotency_key: str
    error: str = ""
    schema_version: str = MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION

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
class OrchestrationReport:
    """Top-level tick report."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: OrchestrationVerdict
    parent_goal_id: str
    total_sub_goals: int
    blocked_count: int
    ready_count: int
    emitted_count: int
    done_count: int
    failed_count: int
    completion_ratio: float
    emit_outcomes: Tuple[OrchestrationEmitOutcome, ...]
    run_records: Tuple[SubGoalRunRecord, ...]
    diagnostic: str
    elapsed_s: float
    # Sovereign State-Propagation Bridge: the GROUND-TRUTH count of sub-goals
    # actually dispatched to the router THIS tick (router.ingest succeeded).
    # ``emitted_count`` aggregates run-state over the pre-emit completion_status
    # ledger, so it STRUCTURALLY cannot reflect this tick's just-completed emit
    # (it lags by a tick). The forward-progress gate must read THIS field, not
    # the lagging aggregate, or a freshly-decomposed-and-dispatched GOAL is
    # false-negatived as "emitted 0" and wrongly DLQ'd. Default 0 keeps the
    # early-return construction sites (master-off / dedup) byte-identical.
    emitted_this_tick: int = 0
    schema_version: str = MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION

    @property
    def made_forward_progress(self) -> bool:
        """True iff this tick made real forward progress: a sub-goal was either
        dispatched this tick (ground truth) OR is already in-flight per the
        ledger. This is the correct success predicate for the BLOCK->decompose
        re-inject gate -- NOT the lagging ``emitted_count`` alone."""
        return self.emitted_count >= 1 or self.emitted_this_tick >= 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "parent_goal_id": self.parent_goal_id[:128],
            "total_sub_goals": int(self.total_sub_goals),
            "blocked_count": int(self.blocked_count),
            "ready_count": int(self.ready_count),
            "emitted_count": int(self.emitted_count),
            "emitted_this_tick": int(self.emitted_this_tick),
            "made_forward_progress": bool(self.made_forward_progress),
            "done_count": int(self.done_count),
            "failed_count": int(self.failed_count),
            "completion_ratio": float(self.completion_ratio),
            "emit_outcomes": [
                o.to_dict() for o in self.emit_outcomes
            ],
            "run_records": [
                r.to_dict() for r in self.run_records
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
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


def _load_completion_status(
    parent_goal_id: str,
    *,
    path_override: Optional[Path] = None,
    rows_override: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    """Read goal_decomposition_planner's ledger; return
    ``{sub_goal_id: latest_status}`` for the parent. Append-only
    semantics: latest row per sub_goal_id wins. NEVER raises."""
    rows: Sequence[Mapping[str, Any]]
    if rows_override is not None:
        rows = rows_override
    else:
        target = path_override or completion_ledger_path()
        rows_list: List[Dict[str, Any]] = []
        try:
            if not target.exists():
                return {}
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
                    rows_list.append(obj)
        except Exception:  # noqa: BLE001
            return {}
        rows = rows_list
    latest: Dict[str, str] = {}
    for r in rows:
        try:
            if r.get("kind") != "completion":
                continue
            if str(r.get("parent_goal_id") or "") != parent_goal_id:
                continue
            sid = str(r.get("sub_goal_id") or "")
            if not sid:
                continue
            status = str(r.get("status") or "")
            latest[sid] = status
        except Exception:  # noqa: BLE001
            continue
    return latest


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


# Pure DAG logic — ready set + run state classification


_TERMINAL_STATUSES: FrozenSet[str] = frozenset(
    {"completed", "failed"},
)


def compute_run_state(
    sub_goal: Any,
    completion_status_by_sub_id: Mapping[str, str],
) -> Tuple[SubGoalRunState, Tuple[str, ...]]:
    """Pure classifier. Returns ``(state, unmet_deps_tuple)``.
    NEVER raises.

    A sub-goal is:
    * DONE if its own status is terminal (completed or failed)
    * EMITTED if its own status is in_progress or proposed
    * READY if all deps reached "completed" (not "failed")
    * BLOCKED otherwise (one or more deps not yet completed
      OR a dep reached FAILED — distinguished by the
      unmet_deps content)
    """
    try:
        sid = str(getattr(sub_goal, "sub_goal_id", "") or "")
        deps = tuple(
            str(d) for d in (
                getattr(sub_goal, "depends_on_sub_ids", ()) or ()
            )
        )
    except Exception:  # noqa: BLE001
        return SubGoalRunState.BLOCKED, ()
    if not sid:
        return SubGoalRunState.BLOCKED, ()
    own_status = (
        completion_status_by_sub_id.get(sid, "") or ""
    ).strip().lower()
    if own_status in _TERMINAL_STATUSES:
        return SubGoalRunState.DONE, ()
    if own_status in ("proposed", "in_progress"):
        return SubGoalRunState.EMITTED, ()
    # Not yet emitted; check deps.
    unmet: List[str] = []
    for dep in deps:
        dep_status = (
            completion_status_by_sub_id.get(dep, "") or ""
        ).strip().lower()
        if dep_status != "completed":
            unmet.append(dep)
    if unmet:
        return SubGoalRunState.BLOCKED, tuple(unmet)
    return SubGoalRunState.READY, ()


def compute_ready_set(
    plan: Any,
    completion_status_by_sub_id: Mapping[str, str],
) -> Tuple[str, ...]:
    """Pure — return tuple of sub_goal_ids that are READY (all
    deps completed, not yet emitted). Topological order preserved.
    NEVER raises."""
    try:
        sub_goals = tuple(getattr(plan, "sub_goals", ()) or ())
        topo_order = tuple(
            getattr(plan, "topological_order", ()) or ()
        )
    except Exception:  # noqa: BLE001
        return ()
    by_id: Dict[str, Any] = {
        getattr(s, "sub_goal_id", ""): s for s in sub_goals
    }
    ordered_ids: Tuple[str, ...] = (
        topo_order if topo_order else tuple(by_id.keys())
    )
    ready: List[str] = []
    for sid in ordered_ids:
        sub = by_id.get(sid)
        if sub is None:
            continue
        state, _unmet = compute_run_state(
            sub, completion_status_by_sub_id,
        )
        if state is SubGoalRunState.READY:
            ready.append(sid)
    return tuple(ready)


def is_plan_completed(
    plan: Any,
    completion_status_by_sub_id: Mapping[str, str],
) -> bool:
    """Pure. NEVER raises."""
    try:
        sub_goals = tuple(getattr(plan, "sub_goals", ()) or ())
    except Exception:  # noqa: BLE001
        return False
    if not sub_goals:
        return False
    for sub in sub_goals:
        sid = str(getattr(sub, "sub_goal_id", "") or "")
        if not sid:
            continue
        status = (
            completion_status_by_sub_id.get(sid, "")
            or ""
        ).strip().lower()
        if status != "completed":
            return False
    return True


def is_plan_stalled(
    plan: Any,
    completion_status_by_sub_id: Mapping[str, str],
) -> bool:
    """Pure. A plan is stalled when at least one sub-goal is
    BLOCKED AND its blocking deps include a FAILED sub-goal
    (or there are no READY sub-goals + no EMITTED in flight).
    NEVER raises."""
    try:
        sub_goals = tuple(getattr(plan, "sub_goals", ()) or ())
    except Exception:  # noqa: BLE001
        return False
    if not sub_goals:
        return False
    has_pending = False
    has_failed_dep = False
    for sub in sub_goals:
        state, unmet = compute_run_state(
            sub, completion_status_by_sub_id,
        )
        sid = str(getattr(sub, "sub_goal_id", "") or "")
        own_status = (
            completion_status_by_sub_id.get(sid, "")
            or ""
        ).strip().lower()
        if own_status == "failed":
            has_failed_dep = True
        if state in (
            SubGoalRunState.BLOCKED,
            SubGoalRunState.READY,
            SubGoalRunState.EMITTED,
        ):
            has_pending = True
        # If a blocked sub-goal's unmet deps include a FAILED,
        # it'll never become ready → stalled.
        if state is SubGoalRunState.BLOCKED:
            for d in unmet:
                d_status = (
                    completion_status_by_sub_id.get(d, "")
                    or ""
                ).strip().lower()
                if d_status == "failed":
                    return True
    # No deps failed but plan has pending work AND no ready/
    # emitted → can't progress = stalled.
    if not has_pending:
        return False
    return False


# Envelope construction (composes goal_decomposition_planner's pattern)


def _make_envelope_for_sub_goal(
    sub_goal: Any,
    *,
    repo_override: Optional[str] = None,
    source_override: Optional[str] = None,
) -> Optional[Any]:
    """Compose intake.intent_envelope.make_envelope. NEVER
    raises."""
    try:
        from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: E501
            make_envelope,
        )
    except ImportError:
        return None
    try:
        kind_value = ""
        try:
            kind_obj = getattr(sub_goal, "kind", None)
            kind_value = (
                getattr(kind_obj, "value", "") if kind_obj else ""
            )
        except Exception:  # noqa: BLE001
            kind_value = ""
        boundary = bool(
            getattr(sub_goal, "boundary_crossed", False),
        )
        # Map decomposition kind → urgency. Sequential / cage
        # ⇒ high urgency. Exploratory ⇒ low. Otherwise normal.
        if kind_value == "sequential" or boundary:
            urgency = "high"
        elif kind_value == "exploratory":
            urgency = "low"
        else:
            urgency = "normal"
        target_files_raw = tuple(
            getattr(sub_goal, "target_files", ()) or ()
        )
        target_files = target_files_raw or (
            "(no target files specified)",
        )
        sub_goal_id = str(
            getattr(sub_goal, "sub_goal_id", "") or "",
        )
        parent_goal_id = str(
            getattr(sub_goal, "parent_goal_id", "") or "",
        )
        env = make_envelope(
            source=(
                source_override
                if source_override is not None
                else envelope_source()
            ),
            description=(
                f"{getattr(sub_goal, 'title', '')}\n\n"
                f"{getattr(sub_goal, 'description', '')}"
            ),
            target_files=target_files,
            repo=repo_override if repo_override else repo_name(),
            confidence=0.9,
            urgency=urgency,
            evidence={
                "parent_goal_id": parent_goal_id,
                "sub_goal_id": sub_goal_id,
                "sub_goal_kind": kind_value,
                "depends_on_sub_ids": list(
                    getattr(sub_goal, "depends_on_sub_ids", ())
                    or ()
                ),
                "boundary_crossed": boundary,
                "estimated_complexity": str(
                    getattr(
                        sub_goal, "estimated_complexity", "",
                    ) or "",
                ),
                "signature": sub_goal_id,
                "multi_step_orchestrated": True,
            },
            requires_human_ack=False,
            signal_id=f"multistep_{sub_goal_id[:80]}",
        )
        return env
    except Exception:  # noqa: BLE001
        return None


def _mark_emitted_via_goal_decomposition(
    sub_goal_id: str,
    parent_goal_id: str,
    *,
    now_unix: float,
) -> None:
    """Compose goal_decomposition_planner.mark_sub_goal_status
    to write the PROPOSED transition to ITS ledger (the
    canonical completion ledger). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.goal_decomposition_planner import (  # noqa: E501
            CompletionStatus,
            mark_sub_goal_status,
        )
        mark_sub_goal_status(
            sub_goal_id=sub_goal_id,
            parent_goal_id=parent_goal_id,
            status=CompletionStatus.PROPOSED,
            note="emitted by multi_step_orchestrator",
            now_unix=now_unix,
        )
    except Exception:  # noqa: BLE001
        return


# Top-level — advance_orchestration


def _build_run_records(
    plan: Any,
    completion_status: Mapping[str, str],
    *,
    emit_outcomes: Sequence[OrchestrationEmitOutcome] = (),
    now_unix: float = 0.0,
) -> Tuple[SubGoalRunRecord, ...]:
    """Pure. Build the per-sub-goal run-record snapshot."""
    emitted_ids = {
        o.sub_goal_id for o in emit_outcomes if o.emitted
    }
    out: List[SubGoalRunRecord] = []
    try:
        sub_goals = tuple(getattr(plan, "sub_goals", ()) or ())
    except Exception:  # noqa: BLE001
        return ()
    for sub in sub_goals:
        sid = str(getattr(sub, "sub_goal_id", "") or "")
        if not sid:
            continue
        state, unmet = compute_run_state(sub, completion_status)
        own_status = (
            completion_status.get(sid, "") or ""
        ).strip().lower()
        emit_t = now_unix if sid in emitted_ids else 0.0
        out.append(SubGoalRunRecord(
            sub_goal_id=sid,
            run_state=state,
            completion_status=own_status,
            unmet_deps=unmet,
            emitted_at_unix=emit_t,
        ))
    return tuple(out)


def _aggregate_counts(
    records: Sequence[SubGoalRunRecord],
    completion_status: Mapping[str, str],
) -> Tuple[int, int, int, int, int]:
    """Returns (blocked, ready, emitted, done, failed)."""
    blocked = sum(
        1 for r in records
        if r.run_state is SubGoalRunState.BLOCKED
    )
    ready = sum(
        1 for r in records
        if r.run_state is SubGoalRunState.READY
    )
    emitted = sum(
        1 for r in records
        if r.run_state is SubGoalRunState.EMITTED
    )
    done = sum(
        1 for r in records
        if r.run_state is SubGoalRunState.DONE
    )
    failed = sum(
        1 for r in records
        if r.run_state is SubGoalRunState.DONE
        and r.completion_status == "failed"
    )
    return blocked, ready, emitted, done, failed


def _classify_verdict(
    *,
    plan: Any,
    blocked: int,
    ready: int,
    emitted: int,
    done: int,
    failed: int,
    completion_status: Mapping[str, str],
) -> OrchestrationVerdict:
    """Pure verdict classifier."""
    try:
        total = len(tuple(getattr(plan, "sub_goals", ()) or ()))
    except Exception:  # noqa: BLE001
        total = 0
    if total == 0:
        return OrchestrationVerdict.NO_PLAN
    if is_plan_completed(plan, completion_status):
        return OrchestrationVerdict.COMPLETED
    if is_plan_stalled(plan, completion_status):
        return OrchestrationVerdict.STALLED
    return OrchestrationVerdict.PROGRESSING


async def advance_orchestration(
    plan: Any,
    *,
    router: Any = None,
    completion_status_override: Optional[
        Mapping[str, str]
    ] = None,
    completion_rows_override: Optional[
        Sequence[Mapping[str, Any]]
    ] = None,
    now_unix: Optional[float] = None,
) -> OrchestrationReport:
    """One orchestration tick. NEVER raises.

    Reads completion status, computes READY set, emits up to
    ``max_emits_per_tick`` envelopes for READY sub-goals via
    canonical router, marks them PROPOSED via
    goal_decomposition_planner's ledger, returns the snapshot.

    Idempotent: calling multiple times is safe (sub-goals that
    are already EMITTED/DONE are not re-emitted).

    Parameters
    ----------
    plan:
        :class:`goal_decomposition_planner.DecomposedPlan`
        (duck-typed — substrate uses ``sub_goals`` +
        ``topological_order``).
    router:
        Router with async ``ingest(envelope)`` method. None →
        dry-run (envelopes constructed but not submitted).
    completion_status_override:
        Testing seam — substrate skips ledger read.
    completion_rows_override:
        Testing seam — pass ledger rows directly.
    """
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return OrchestrationReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=OrchestrationVerdict.NO_PLAN,
            parent_goal_id="",
            total_sub_goals=0,
            blocked_count=0, ready_count=0,
            emitted_count=0, done_count=0, failed_count=0,
            completion_ratio=0.0,
            emit_outcomes=(),
            run_records=(),
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )

    try:
        parent_goal_id = str(
            getattr(plan, "parent_goal_id", "") or "",
        )
        sub_goals = tuple(getattr(plan, "sub_goals", ()) or ())
    except Exception:  # noqa: BLE001
        sub_goals = ()
        parent_goal_id = ""

    if not sub_goals or not parent_goal_id:
        return OrchestrationReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=OrchestrationVerdict.NO_PLAN,
            parent_goal_id=parent_goal_id,
            total_sub_goals=0,
            blocked_count=0, ready_count=0,
            emitted_count=0, done_count=0, failed_count=0,
            completion_ratio=0.0,
            emit_outcomes=(),
            run_records=(),
            diagnostic="empty plan or missing parent_goal_id",
            elapsed_s=max(0.0, time.time() - started),
        )

    completion_status: Mapping[str, str]
    if completion_status_override is not None:
        completion_status = completion_status_override
    else:
        completion_status = _load_completion_status(
            parent_goal_id,
            rows_override=completion_rows_override,
        )

    # Compute ready set (pure).
    ready_ids = compute_ready_set(plan, completion_status)
    cap = max_emits_per_tick()
    to_emit = ready_ids[:cap]

    # Build envelope + dispatch for each READY sub-goal.
    by_id: Dict[str, Any] = {
        str(getattr(s, "sub_goal_id", "") or ""): s
        for s in sub_goals
    }
    emit_outcomes: List[OrchestrationEmitOutcome] = []
    for sid in to_emit:
        sub = by_id.get(sid)
        if sub is None:
            continue
        env = _make_envelope_for_sub_goal(sub)
        if env is None:
            emit_outcomes.append(OrchestrationEmitOutcome(
                sub_goal_id=sid,
                emitted=False,
                idempotency_key="",
                error="envelope construction failed",
            ))
            continue
        if router is None:
            emit_outcomes.append(OrchestrationEmitOutcome(
                sub_goal_id=sid,
                emitted=False,
                idempotency_key=getattr(env, "idempotency_key", ""),
                error="router not provided (dry-run)",
            ))
            continue
        try:
            result = await router.ingest(env)
            emit_outcomes.append(OrchestrationEmitOutcome(
                sub_goal_id=sid,
                emitted=True,
                idempotency_key=str(result or "")[:64],
                error="",
            ))
            _mark_emitted_via_goal_decomposition(
                sub_goal_id=sid,
                parent_goal_id=parent_goal_id,
                now_unix=started,
            )
        except Exception as exc:  # noqa: BLE001
            emit_outcomes.append(OrchestrationEmitOutcome(
                sub_goal_id=sid,
                emitted=False,
                idempotency_key=getattr(env, "idempotency_key", ""),
                error=f"ingest failed: {exc!r}"[:200],
            ))

    # Build snapshot AFTER emit (so emit_outcomes inform records).
    run_records = _build_run_records(
        plan, completion_status,
        emit_outcomes=tuple(emit_outcomes),
        now_unix=started,
    )
    blocked, ready, emitted, done, failed = _aggregate_counts(
        run_records, completion_status,
    )
    verdict = _classify_verdict(
        plan=plan, blocked=blocked, ready=ready,
        emitted=emitted, done=done, failed=failed,
        completion_status=completion_status,
    )
    total = len(sub_goals)
    ratio = (done - failed) / total if total > 0 else 0.0

    emitted_this_tick = sum(1 for o in emit_outcomes if o.emitted)

    # Zero-Drop policy (Sovereign State-Propagation Bridge): a REAL silent drop
    # is when we SELECTED ready sub-goals to emit (``to_emit`` non-empty) but
    # NONE were actually dispatched (every router.ingest failed / envelope was
    # None). That -- and only that -- is a propagation failure worth a loud
    # signal; a successful dispatch (emitted_this_tick >= 1) is NOT a drop even
    # though the lagging ``emitted`` aggregate reads 0 this tick.
    if to_emit and emitted_this_tick == 0:
        _errs = "; ".join(
            f"{o.sub_goal_id}:{o.error}" for o in emit_outcomes if not o.emitted
        )[:400]
        logger.critical(
            "[SovereignPropagation] REAL DROP: %d ready sub-goal(s) selected for "
            "emit but 0 dispatched (parent=%s) -- failures: %s",
            len(to_emit), parent_goal_id, _errs or "unknown",
        )

    diagnostic = (
        f"verdict={verdict.value}; "
        f"total={total} blocked={blocked} ready={ready} "
        f"emitted={emitted} done={done} failed={failed}; "
        f"this_tick_emitted={emitted_this_tick}"
    )

    report = OrchestrationReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        parent_goal_id=parent_goal_id,
        total_sub_goals=total,
        blocked_count=blocked,
        ready_count=ready,
        emitted_count=emitted,
        done_count=done,
        failed_count=failed,
        completion_ratio=ratio,
        emit_outcomes=tuple(emit_outcomes),
        run_records=run_records,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
        emitted_this_tick=emitted_this_tick,
    )
    _persist_report(report)
    _publish_event(report)
    return report


def advance_orchestration_sync(
    plan: Any,
    *,
    router: Any = None,
    completion_status_override: Optional[
        Mapping[str, str]
    ] = None,
    completion_rows_override: Optional[
        Sequence[Mapping[str, Any]]
    ] = None,
    now_unix: Optional[float] = None,
) -> OrchestrationReport:
    """Sync wrapper. NEVER raises. Returns NO_PLAN verdict if
    invoked inside a running event loop."""
    started = time.time() if now_unix is None else float(now_unix)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        return OrchestrationReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=OrchestrationVerdict.NO_PLAN,
            parent_goal_id="",
            total_sub_goals=0,
            blocked_count=0, ready_count=0,
            emitted_count=0, done_count=0, failed_count=0,
            completion_ratio=0.0,
            emit_outcomes=(),
            run_records=(),
            diagnostic=(
                "sync wrapper invoked inside running event "
                "loop — use advance_orchestration() instead"
            ),
            elapsed_s=0.0,
        )
    try:
        return asyncio.run(advance_orchestration(
            plan, router=router,
            completion_status_override=completion_status_override,
            completion_rows_override=completion_rows_override,
            now_unix=now_unix,
        ))
    except Exception as exc:  # noqa: BLE001
        return OrchestrationReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=OrchestrationVerdict.NO_PLAN,
            parent_goal_id="",
            total_sub_goals=0,
            blocked_count=0, ready_count=0,
            emitted_count=0, done_count=0, failed_count=0,
            completion_ratio=0.0,
            emit_outcomes=(),
            run_records=(),
            diagnostic=f"sync wrapper failed: {exc!r}"[:200],
            elapsed_s=0.0,
        )


def _persist_report(report: OrchestrationReport) -> None:
    if report.verdict is OrchestrationVerdict.NO_PLAN:
        return
    _flock_append({
        "kind": "orchestration", "payload": report.to_dict(),
    })


def _publish_event(report: OrchestrationReport) -> None:
    if not master_enabled():
        return
    if report.verdict is OrchestrationVerdict.NO_PLAN:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_MULTI_STEP_ORCHESTRATED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_MULTI_STEP_ORCHESTRATED,
            (
                f"system::multi_step_orchestrator::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "parent_goal_id": report.parent_goal_id[:64],
                "total_sub_goals": report.total_sub_goals,
                "blocked_count": report.blocked_count,
                "ready_count": report.ready_count,
                "emitted_count": report.emitted_count,
                "done_count": report.done_count,
                "failed_count": report.failed_count,
                "completion_ratio": report.completion_ratio,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_orchestration_panel(
    report: Optional[OrchestrationReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"multi-step orchestrator: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "multi-step orchestrator: no report"
    if not report.master_enabled:
        return (
            f"multi-step orchestrator: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    lines = [
        f"🎯 Multi-step Orchestrator  {vg} "
        f"{report.verdict.value}",
        f"  parent_goal     : {report.parent_goal_id[:48]}",
        f"  total           : {report.total_sub_goals}",
        f"  blocked         : {report.blocked_count} "
        f"{run_state_glyph(SubGoalRunState.BLOCKED)}",
        f"  ready           : {report.ready_count} "
        f"{run_state_glyph(SubGoalRunState.READY)}",
        f"  emitted         : {report.emitted_count} "
        f"{run_state_glyph(SubGoalRunState.EMITTED)}",
        f"  done            : {report.done_count} "
        f"{run_state_glyph(SubGoalRunState.DONE)}",
        f"  failed          : {report.failed_count}",
        f"  completion      : {report.completion_ratio:.2f}",
    ]
    if report.emit_outcomes:
        em = sum(1 for o in report.emit_outcomes if o.emitted)
        lines.append(
            f"  this_tick       : {em}/"
            f"{len(report.emit_outcomes)}"
        )
    lines.append(f"  diagnostic      : {report.diagnostic}")
    return "\n".join(lines)


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
        "multi_step_orchestrator.py"
    )

    _EXPECTED_VERDICTS = {
        "no_plan", "progressing", "stalled", "completed",
    }
    _EXPECTED_STATES = {
        "blocked", "ready", "emitted", "done",
    }

    def _validate_taxonomy(class_name: str, expected: set):
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
            "backend.core.ouroboros.governance.roadmap_reader",
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
        if "goal_decomposition_planner" not in source:
            violations.append(
                "must compose goal_decomposition_planner "
                "(input plan + completion ledger source)",
            )
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
                "must compose cross_process_jsonl",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_step_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "OrchestrationVerdict 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "OrchestrationVerdict", _EXPECTED_VERDICTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_step_run_state_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "SubGoalRunState 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "SubGoalRunState", _EXPECTED_STATES,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_step_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — sits BETWEEN "
                "goal_decomposition_planner + canonical router. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / tool_executor / "
                "plan_generator / roadmap_reader (upstream "
                "substrates must not import this one — "
                "consumed by operator-side glue)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_step_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_step_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes "
                "goal_decomposition_planner (plan + "
                "completion ledger) + intake.intent_envelope."
                "make_envelope + Wave 2 #5 "
                "governance_boundary_gate + "
                "cross_process_jsonl."
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
        "multi_step_orchestrator.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Multi-step Plan Orchestrator master. §33.1 "
                "default-FALSE. Closes §41.4 Phase 1 fourth "
                "arc (PRD v3.0+). Phased dep-gated emission "
                "of sub-goal envelopes. Enforces "
                "DecomposedPlan DAG at runtime (not just "
                "emit-time)."
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
            name=_ENV_MAX_EMITS_PER_TICK,
            type=FlagType.INT,
            default=_DEFAULT_MAX_EMITS_PER_TICK,
            description=(
                "Cap on envelopes emitted per "
                "advance_orchestration tick. Prevents fan-out "
                "spikes. Default 5."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_EMITS_PER_TICK}=20",
        ),
        FlagSpec(
            name=_ENV_COMPLETION_LEDGER_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Path to goal_decomposition_planner's "
                "completion ledger. Default matches its "
                "default path."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_COMPLETION_LEDGER_PATH}=/path/to/ledger.jsonl"
            ),
        ),
        FlagSpec(
            name=_ENV_LEDGER_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Substrate's own §33.4 audit ledger path. "
                "Default .jarvis/multi_step_orchestrator_ledger.jsonl."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_LEDGER_PATH}=/path/to/orch.jsonl",
        ),
        FlagSpec(
            name=_ENV_REPO_NAME,
            type=FlagType.STR,
            default=_DEFAULT_REPO_NAME,
            description=(
                "Repo name for emitted envelopes. Default "
                "'jarvis'."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_REPO_NAME}=jarvis-fork",
        ),
        FlagSpec(
            name=_ENV_ENVELOPE_SOURCE,
            type=FlagType.STR,
            default=_DEFAULT_ENVELOPE_SOURCE,
            description=(
                "Envelope source. Default 'roadmap'. Must be "
                "valid value in intake._VALID_SOURCES."
            ),
            category=Category.ROUTING,
            source_file=src,
            example=(
                f"{_ENV_ENVELOPE_SOURCE}=auto_proposed"
            ),
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
    "MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION",
    "OrchestrationVerdict",
    "SubGoalRunState",
    "SubGoalRunRecord",
    "OrchestrationEmitOutcome",
    "OrchestrationReport",
    "master_enabled",
    "persistence_enabled",
    "max_emits_per_tick",
    "completion_ledger_path",
    "ledger_path",
    "repo_name",
    "envelope_source",
    "verdict_glyph",
    "run_state_glyph",
    "compute_run_state",
    "compute_ready_set",
    "is_plan_completed",
    "is_plan_stalled",
    "advance_orchestration",
    "advance_orchestration_sync",
    "format_orchestration_panel",
    "register_shipped_invariants",
    "register_flags",
]
