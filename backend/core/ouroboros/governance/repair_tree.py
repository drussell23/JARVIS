"""RepairTree — AlphaVerus Treefinement L2 substrate (Phase 0).
================================================================

Closes the L2 linear-FSM blind-alley failure mode surfaced by PRD
§40.7.2 (AlphaVerus, https://arxiv.org/abs/2412.06176): when
``RepairEngine._run_inner`` (linear FSM, 5 sequential iterations)
takes a wrong fix-strategy turn at iteration N, iterations N+1..max
inherit the misclassification and the 120s timebox burns out on a
dead branch. AlphaVerus's "treefinement" forks at each repair
attempt; validator feedback prunes losing branches; surviving
branches inform the next layer's GENERATE prompt (cross-branch
learning — the actual published delta over naive parallel repair).

Architecture
------------

This Phase 0 module ships **substrate only** — the closed
taxonomies, frozen dataclasses, the ``TreefinementBudget`` env
loader, the master flag accessor, and the ``RepairTreeRunner``
constructor with a deliberate ``NotImplementedError`` on
``run_tree`` (Phase 1 wires execution).

The execution machinery composes existing canonical primitives
end-to-end (zero parallel state — the load-bearing §1 Boundary
invariant):

  * ``parallel_dispatch.build_execution_graph`` — parallel layer
    fan-out (already posture-weighted, already cost-aware)
  * ``worktree_manager.WorktreeManager`` — COW git worktree per
    branch (already reap-orphaned on boot)
  * ``repair_engine._patch_sig`` — branch-equivalence key (already
    deterministic SHA over normalized diff — single source)
  * ``repair_engine.RepairBudget.max_total_validation_runs`` —
    shared validation envelope (K branches × M layers ≤ existing
    8-run cap — no parallel budget bookkeeping)
  * ``sensor_governor.emergency_brake`` — auto-demote-to-LINEAR
    signal (no parallel emergency state)
  * ``strategic_direction`` injection slot — sibling-outcomes
    block in layer-N+1 prompt (the AlphaVerus learning signal)

What this module does NOT do
----------------------------

* Mutate ``RepairBudget`` — that dataclass stays bytes-identical;
  tree-only knobs live in ``TreefinementBudget`` which composes
  ``RepairBudget`` by attribute reference at runtime.
* Touch ``RepairEngine._run_inner`` — the legacy LINEAR FSM stays
  bytes-identical and remains the default path. The strategy gate
  added in Phase 1 routes BFS/BEAM_K to ``RepairTreeRunner`` while
  LINEAR continues through ``_run_inner`` unchanged.
* Implement tree execution — Phase 1+. ``run_tree`` deliberately
  raises ``NotImplementedError`` to make accidental wiring loud
  before the runner is ready.
* Define a parallel signature primitive — branch_id is derived
  from the canonical ``repair_engine._patch_sig`` (Phase 1
  composition pin).

Reference scheme
----------------

``b-N`` (Phase 4) joins the cross-substrate ``/expand`` family —
``t-N`` tool bodies / ``d-N`` diff archive / ``o-N`` op blocks /
``n-N`` narrative frames / ``p-N`` permission decisions. The
unified ``/expand <ref>`` REPL verb in serpent_flow dispatches
by prefix.

Authority boundary
------------------

* §1 Boundary — descriptive substrate; no LLM, no I/O during
  Phase 0; never gates GENERATE / VALIDATE / APPLY (that authority
  remains with the existing validator stack)
* §6 Iron Gate preserved — tree branches each pass through
  IronGate + SemanticGuardian + TestRunner unchanged; tree adds
  parallel invocation, not new validation
* §7 fail-closed — every public method returns degraded sentinel
  on failure; ``run_tree`` (Phase 1) never raises into the
  orchestrator
* §8 observable — Phase 4 surfaces (REPL/SSE/IDE GET) read this
  substrate; never the reverse
* §33.1 graduation — master flag default-FALSE; legacy LINEAR
  path stays the production default until 3-clean-soak ladder
  proves tree mode (≥10% L2 success-rate lift OR ≥20% wall-clock
  reduction at parity success rate)
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
    runtime_checkable,
)

# ---------------------------------------------------------------------------
# Composition imports — load-bearing single-source-of-truth pins
# ---------------------------------------------------------------------------
# These three imports are the substrate's structural commitment to "no
# parallel state": branch_id derives from the canonical patch hash,
# K sizing flows through the canonical posture-weight table, branch
# isolation flows through the canonical worktree manager. Phase 5
# AST-pins each import so refactors that try to inline a parallel
# primitive (e.g., a tree-local hash function) fail the spine before
# they reach review.
from backend.core.ouroboros.governance.failure_classifier import (
    patch_signature_hash,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    posture_weight_for,
)
from backend.core.ouroboros.governance.posture import Posture
from backend.core.ouroboros.governance.worktree_manager import (
    WorktreeManager,
)

logger = logging.getLogger("Ouroboros.RepairTree")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


REPAIR_TREE_SCHEMA_VERSION: str = "repair_tree.v1"


# Master flag — §33.1 default-FALSE; flip via Phase 9 soak ladder.
MASTER_FLAG_ENV_VAR: str = "JARVIS_L2_TREEFINEMENT_ENABLED"

# Strategy selector — accepts BranchingStrategy values; invalid values
# fall back to LINEAR with a structured warning log (NEVER raises).
STRATEGY_ENV_VAR: str = "JARVIS_L2_BRANCHING_STRATEGY"

# Per-layer K cap (post posture-weighting). K^layers bounded by
# RepairBudget.max_total_validation_runs (shared envelope).
MAX_BRANCHES_PER_LAYER_ENV_VAR: str = "JARVIS_L2_MAX_BRANCHES_PER_LAYER"

# Top-M survivors per layer (BEAM_K only).
BEAM_WIDTH_ENV_VAR: str = "JARVIS_L2_BEAM_WIDTH"

# _patch_sig collision pruning toggle.
BRANCH_DEDUP_ENV_VAR: str = "JARVIS_L2_BRANCH_DEDUP_ENABLED"

# AlphaVerus sibling-outcome injection toggle (the actual delta).
CROSS_BRANCH_LEARNING_ENV_VAR: str = "JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED"

# Cost-burn fraction above which runner auto-demotes to LINEAR.
EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR: str = (
    "JARVIS_L2_TREE_EMERGENCY_DEMOTE_THRESHOLD"
)


# Defaults — referenced by both code paths and AST pin tests so drift
# is structurally detectable. K=3 + M=2 + threshold=0.85 are operator-
# approved Phase 0 defaults (chat #2026-05-11).
_DEFAULT_MAX_BRANCHES_PER_LAYER: int = 3
_DEFAULT_BEAM_WIDTH: int = 2
_DEFAULT_EMERGENCY_DEMOTE_THRESHOLD: float = 0.85

# Bound clamps — defensive ceilings to prevent env misconfiguration
# from producing pathological tree sizes.
_MAX_BRANCHES_CEILING: int = 16
_BEAM_WIDTH_CEILING: int = 16


# ===========================================================================
# Closed taxonomies (AST bytes-pinned in Phase 5)
# ===========================================================================


class BranchingStrategy(str, enum.Enum):
    """Tree-search branching strategy.

    LINEAR is the legacy default and yields byte-identical behavior
    to pre-Treefinement ``RepairEngine._run_inner``. BFS expands all
    surviving branches per layer; BEAM_K retains only the top-M
    (``BEAM_WIDTH``) by validator score.
    """

    LINEAR = "linear"
    BFS = "bfs"
    BEAM_K = "beam_k"


class BranchOutcome(str, enum.Enum):
    """Per-branch terminal verdict assigned during tree-runner pruning.

    ``WON`` is the only outcome that yields a converged candidate; all
    other terminal outcomes contribute only to the cross-branch
    learning signal (their fix hypotheses inform layer-N+1 GENERATE).
    """

    PROMOTED = "promoted"                    # passed validator → next layer
    PRUNED_VALIDATOR = "pruned_validator"    # test/guardian/iron-gate fail
    PRUNED_DUPLICATE = "pruned_duplicate"    # _patch_sig collision
    PRUNED_BUDGET = "pruned_budget"          # cost/timebox cap
    WON = "won"                              # terminal converged candidate


class LayerVerdict(str, enum.Enum):
    """Per-layer aggregate disposition.

    EXHAUSTED triggers adaptive demotion to LINEAR for the remaining
    timebox (composes the SensorGovernor emergency-brake pattern).
    """

    EXPANDED = "expanded"                # ≥1 survivor → next layer
    EXHAUSTED = "exhausted"              # all branches pruned → fallback
    WON_TERMINAL = "won_terminal"        # WON branch found → early-return
    BUDGET_TERMINAL = "budget_terminal"  # cap reached mid-layer


class PruningReason(str, enum.Enum):
    """Why a branch was pruned.

    Always set when ``BranchOutcome != PROMOTED`` and
    ``BranchOutcome != WON``. Surfaced by Phase 4 ``/repair tree``
    REPL verb + IDE GET.
    """

    DUPLICATE_PATCH_SIG = "duplicate_patch_sig"
    WORSE_THAN_SIBLING = "worse_than_sibling"
    VALIDATION_BUDGET_EXHAUSTED = "validation_budget_exhausted"
    WALL_CLOCK_CAP = "wall_clock_cap"
    SEMANTIC_GUARDIAN_HARD_FINDING = "semantic_guardian_hard_finding"
    IRON_GATE_REJECT = "iron_gate_reject"


# ===========================================================================
# Frozen dataclasses (symmetric to_dict / from_dict per §33.5)
# ===========================================================================


@dataclass(frozen=True)
class RepairBranch:
    """One tree-search branch — frozen post-construction.

    ``branch_id`` is the canonical ``repair_engine._patch_sig`` of the
    diff (Phase 1 composition pin — single signature source). The
    same diff produces the same ``branch_id``, which is the load-
    bearing dedup invariant (cross-branch ``PRUNED_DUPLICATE``).

    ``parent_branch_id`` is ``None`` for root-layer branches; for
    deeper layers it points to the surviving sibling whose context
    seeded this branch's GENERATE prompt.
    """

    branch_id: str
    parent_branch_id: Optional[str]
    layer_index: int
    failure_class: str
    fix_hypothesis: str
    diff: str
    validator_score: float
    outcome: BranchOutcome
    prune_reason: Optional[PruningReason]
    worktree_id: Optional[str]
    cost_usd: float
    validation_runs_consumed: int
    schema_version: str = REPAIR_TREE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "branch_id": self.branch_id,
            "parent_branch_id": self.parent_branch_id,
            "layer_index": self.layer_index,
            "failure_class": self.failure_class,
            "fix_hypothesis": self.fix_hypothesis,
            "diff": self.diff,
            "validator_score": self.validator_score,
            "outcome": self.outcome.value,
            "prune_reason": (
                self.prune_reason.value if self.prune_reason else None
            ),
            "worktree_id": self.worktree_id,
            "cost_usd": self.cost_usd,
            "validation_runs_consumed": self.validation_runs_consumed,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RepairBranch":
        prune_raw = payload.get("prune_reason")
        return cls(
            schema_version=payload.get(
                "schema_version", REPAIR_TREE_SCHEMA_VERSION
            ),
            branch_id=str(payload["branch_id"]),
            parent_branch_id=payload.get("parent_branch_id"),
            layer_index=int(payload["layer_index"]),
            failure_class=str(payload.get("failure_class", "")),
            fix_hypothesis=str(payload.get("fix_hypothesis", "")),
            diff=str(payload.get("diff", "")),
            validator_score=float(payload.get("validator_score", 0.0)),
            outcome=BranchOutcome(payload["outcome"]),
            prune_reason=(
                PruningReason(prune_raw) if prune_raw else None
            ),
            worktree_id=payload.get("worktree_id"),
            cost_usd=float(payload.get("cost_usd", 0.0)),
            validation_runs_consumed=int(
                payload.get("validation_runs_consumed", 0)
            ),
        )


@dataclass(frozen=True)
class RepairTreeLayer:
    """One layer's branches + aggregate verdict.

    ``parallel_units_actual`` records the K post posture-weighting
    (e.g., HARDEN posture × ``max_branches_per_layer=3`` may yield
    ``parallel_units_actual=2``). Operator-visible via Phase 4 IDE
    GET; informs graduation soak analysis.
    """

    layer_index: int
    branches: Tuple[RepairBranch, ...]
    verdict: LayerVerdict
    wall_ms: float
    parallel_units_actual: int
    schema_version: str = REPAIR_TREE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "layer_index": self.layer_index,
            "branches": [b.to_dict() for b in self.branches],
            "verdict": self.verdict.value,
            "wall_ms": self.wall_ms,
            "parallel_units_actual": self.parallel_units_actual,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RepairTreeLayer":
        return cls(
            schema_version=payload.get(
                "schema_version", REPAIR_TREE_SCHEMA_VERSION
            ),
            layer_index=int(payload["layer_index"]),
            branches=tuple(
                RepairBranch.from_dict(b)
                for b in payload.get("branches", [])
            ),
            verdict=LayerVerdict(payload["verdict"]),
            wall_ms=float(payload.get("wall_ms", 0.0)),
            parallel_units_actual=int(
                payload.get("parallel_units_actual", 0)
            ),
        )


@dataclass(frozen=True)
class RepairTreeResult:
    """Tree-runner terminal output.

    ``final_status`` is the serialized form of the canonical
    ``repair_engine.RepairResult`` (composition — no parallel result
    type). Phase 0 leaves it ``None`` since execution is deferred to
    Phase 1; Phase 1+ runner populates it from the WON branch.

    ``winning_branch_path`` is the ``branch_id`` chain root→leaf; for
    EXHAUSTED/BUDGET_TERMINAL trees with no winner it is the empty
    tuple and operators read ``layers[-1].branches`` for the best-
    survivor diagnostic.
    """

    root_op_id: str
    layers: Tuple[RepairTreeLayer, ...]
    winning_branch_path: Tuple[str, ...]
    final_status: Optional[Dict[str, Any]]
    schema_version: str = REPAIR_TREE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "root_op_id": self.root_op_id,
            "layers": [layer.to_dict() for layer in self.layers],
            "winning_branch_path": list(self.winning_branch_path),
            "final_status": self.final_status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RepairTreeResult":
        return cls(
            schema_version=payload.get(
                "schema_version", REPAIR_TREE_SCHEMA_VERSION
            ),
            root_op_id=str(payload["root_op_id"]),
            layers=tuple(
                RepairTreeLayer.from_dict(layer)
                for layer in payload.get("layers", [])
            ),
            winning_branch_path=tuple(
                str(b) for b in payload.get("winning_branch_path", [])
            ),
            final_status=payload.get("final_status"),
        )


# ===========================================================================
# Env loaders — defensive, NEVER raise (§7 fail-closed)
# ===========================================================================


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "false", "0", "no", "off")


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except (ValueError, TypeError):
        logger.warning(
            "[RepairTree] invalid %s=%r — using default %d",
            name, raw, default,
        )
        return default


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, min(maximum, float(raw)))
    except (ValueError, TypeError):
        logger.warning(
            "[RepairTree] invalid %s=%r — using default %f",
            name, raw, default,
        )
        return default


# ===========================================================================
# TreefinementBudget — composes RepairBudget; tree-only knobs live here
# ===========================================================================


@dataclass(frozen=True)
class TreefinementBudget:
    """Tree-search budget — additive composition over the existing
    ``repair_engine.RepairBudget``.

    The shared validation budget envelope
    (``RepairBudget.max_total_validation_runs``, ``timebox_s``,
    ``per_iteration_test_timeout_s``) is intentionally NOT duplicated
    here. The Phase 1 runner consults ``RepairBudget`` for those
    knobs and counts the K branches × M layers product against the
    same 8-run cap. This is the load-bearing 'no parallel state'
    invariant per §1 Boundary.

    ``TreefinementBudget`` carries only the knobs that have no
    LINEAR-FSM analog: branching strategy, K cap, beam width, dedup,
    cross-branch learning, emergency demote threshold.
    """

    enabled: bool
    branching_strategy: BranchingStrategy
    max_branches_per_layer: int
    beam_width: int
    branch_dedup_enabled: bool
    cross_branch_learning_enabled: bool
    emergency_demote_threshold: float

    @classmethod
    def from_env(cls) -> "TreefinementBudget":
        """Load tree budget from environment — NEVER raises.

        Malformed values fall back to defaults with a structured
        warning. Strategy parse error specifically falls back to
        LINEAR (the safe default — preserves byte-identical legacy
        behavior).
        """
        enabled = _env_bool(MASTER_FLAG_ENV_VAR, default=False)

        strategy_raw = (
            os.environ.get(STRATEGY_ENV_VAR, "linear") or "linear"
        ).lower().strip()
        try:
            branching_strategy = BranchingStrategy(strategy_raw)
        except ValueError:
            logger.warning(
                "[RepairTree] invalid %s=%r — falling back to LINEAR",
                STRATEGY_ENV_VAR, strategy_raw,
            )
            branching_strategy = BranchingStrategy.LINEAR

        max_branches = _env_int(
            MAX_BRANCHES_PER_LAYER_ENV_VAR,
            _DEFAULT_MAX_BRANCHES_PER_LAYER,
            minimum=1,
            maximum=_MAX_BRANCHES_CEILING,
        )
        beam_width = _env_int(
            BEAM_WIDTH_ENV_VAR,
            _DEFAULT_BEAM_WIDTH,
            minimum=1,
            maximum=_BEAM_WIDTH_CEILING,
        )
        branch_dedup = _env_bool(BRANCH_DEDUP_ENV_VAR, default=True)
        cross_branch = _env_bool(
            CROSS_BRANCH_LEARNING_ENV_VAR, default=True
        )
        demote_threshold = _env_float(
            EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
            _DEFAULT_EMERGENCY_DEMOTE_THRESHOLD,
            minimum=0.0,
            maximum=1.0,
        )

        return cls(
            enabled=enabled,
            branching_strategy=branching_strategy,
            max_branches_per_layer=max_branches,
            beam_width=beam_width,
            branch_dedup_enabled=branch_dedup,
            cross_branch_learning_enabled=cross_branch,
            emergency_demote_threshold=demote_threshold,
        )


# ===========================================================================
# Master flag accessor — composed by repair_engine + Phase 4 surfaces
# ===========================================================================


def treefinement_enabled() -> bool:
    """Master flag check — descriptive, NEVER raises.

    Default FALSE per §33.1 graduation contract. Flip via 3-clean-
    soak Phase 9 ladder when tree mode demonstrates ≥10% L2 success-
    rate lift OR ≥20% wall-clock reduction at parity success rate.
    """
    return _env_bool(MASTER_FLAG_ENV_VAR, default=False)


# ===========================================================================
# Injection Protocols — Phase 1 testability seam
# ===========================================================================
#
# The runner is fully testable in isolation by injecting these three
# Protocols. Phase 2 ships a concrete BranchValidator composing
# TestRunner + SemanticGuardian + IronGate. Phase 3 ships a concrete
# BranchGenerator composing the existing repair_engine generation path
# with the cross-branch StrategicDirection injection. The runner itself
# never imports orchestrator / iron_gate / change_engine (§1 Boundary).


@runtime_checkable
class BranchGenerator(Protocol):
    """Produces a candidate diff + fix hypothesis + cost for one branch.

    NEVER raises — generation failures (provider exhausted, prompt
    rejected, parse error) MUST surface as an empty diff with a
    descriptive ``fix_hypothesis``. The runner converts empty-diff
    branches to PRUNED_VALIDATOR with a structured prune reason.

    The ``parent_branch`` and ``sibling_outcomes`` arguments carry the
    cross-layer information signal — Phase 1 plumbs them; Phase 3 wires
    them into the actual GENERATE prompt via StrategicDirection.
    """

    async def __call__(
        self,
        *,
        op_id: str,
        layer_index: int,
        parent_branch: Optional["RepairBranch"],
        sibling_outcomes: Tuple["RepairBranch", ...],
    ) -> Tuple[str, str, float]:
        """Returns (diff, fix_hypothesis, cost_usd)."""
        ...


@runtime_checkable
class BranchValidator(Protocol):
    """Validates a candidate diff in an isolated worktree.

    NEVER raises — every infrastructure error path MUST yield a
    PRUNED_VALIDATOR outcome with the appropriate ``PruningReason``
    (e.g., ``IRON_GATE_REJECT``, ``SEMANTIC_GUARDIAN_HARD_FINDING``).
    The runner does NOT distinguish "validator crashed" from
    "validator returned PRUNED" — both feed the same pruning oracle.

    Phase 2 wires the real composition (TestRunner + SemanticGuardian
    + IronGate). Phase 1 tests inject deterministic stubs.
    """

    async def __call__(
        self,
        *,
        op_id: str,
        branch_id: str,
        diff: str,
        worktree_dir: Path,
    ) -> Tuple[BranchOutcome, float, Optional[PruningReason], int]:
        """Returns (outcome, validator_score, prune_reason, runs_consumed)."""
        ...


# Plain callable type alias — emergency brake is a synchronous predicate
# (composes the SensorGovernor.SensorState.emergency_brake field, no
# parallel emergency state per §1 Boundary).
EmergencyBrakeCheck = Callable[[], bool]

# Per-iteration deadline check — composes the orchestrator's existing
# pipeline_deadline. Returns remaining seconds or None if no deadline set.
DeadlineCheck = Callable[[], Optional[float]]


# ===========================================================================
# Helper functions — composition primitives (no parallel state)
# ===========================================================================


def _branch_id_for(diff: str) -> str:
    """Derive a branch identifier from the canonical patch hash.

    Composes ``failure_classifier.patch_signature_hash`` — the SAME
    primitive that ``repair_engine._patch_sig`` wraps for in-iteration
    dedup. This is the load-bearing 'single signature source' invariant
    (§1 Boundary): two branches with identical diffs MUST produce
    identical branch_ids regardless of which subsystem computes the
    hash.
    """
    return patch_signature_hash(diff or "")


def _compute_layer_k(
    *,
    posture: Optional[Posture],
    base_k: int,
    remaining_runs: int,
    runs_per_branch: int = 1,
) -> int:
    """Compute the K branches to attempt at one layer.

    Composes ``parallel_dispatch.posture_weight_for`` for posture
    weighting (the canonical 4-value table — no parallel posture
    weights here). Then clamps to the remaining shared validation
    envelope (``RepairBudget.max_total_validation_runs``) so the tree
    can never overshoot the canonical budget.

    Returns at minimum 1 — a layer always gets at least one attempt
    even under tight budget (the alternative would silently skip
    layers, which is observability-hostile).
    """
    weight = posture_weight_for(posture)  # 1.0 default for None
    k_weighted = max(1, int(round(base_k * weight)))
    rpb = max(1, int(runs_per_branch))
    if remaining_runs <= 0:
        return 1  # last-chance attempt; budget aggregation will mark BUDGET_TERMINAL
    k_budget_capped = max(1, remaining_runs // rpb)
    return min(k_weighted, k_budget_capped)


def _select_survivors(
    branches: Tuple[RepairBranch, ...],
    *,
    strategy: BranchingStrategy,
    beam_width: int,
) -> Tuple[RepairBranch, ...]:
    """Pick survivors that advance to the next layer.

    BFS — every PROMOTED branch survives.
    BEAM_K — top-M by validator_score (deterministic tie-break by
        branch_id lex sort to keep results reproducible across runs).
    LINEAR — never invoked here (caller short-circuits before runner).
    """
    promoted = tuple(b for b in branches if b.outcome == BranchOutcome.PROMOTED)
    if strategy == BranchingStrategy.BFS:
        return promoted
    if strategy == BranchingStrategy.BEAM_K:
        # Sort by (-score, branch_id) for deterministic ordering
        ranked = sorted(
            promoted,
            key=lambda b: (-b.validator_score, b.branch_id),
        )
        return tuple(ranked[: max(1, beam_width)])
    # LINEAR fallback — caller should have short-circuited
    return promoted


def _aggregate_layer_verdict(
    branches: Tuple[RepairBranch, ...],
    *,
    survivors: Tuple[RepairBranch, ...],
    budget_remaining: int,
) -> LayerVerdict:
    """Map per-branch outcomes to one of four closed layer verdicts."""
    if any(b.outcome == BranchOutcome.WON for b in branches):
        return LayerVerdict.WON_TERMINAL
    if budget_remaining <= 0:
        return LayerVerdict.BUDGET_TERMINAL
    if not survivors:
        return LayerVerdict.EXHAUSTED
    return LayerVerdict.EXPANDED


# ===========================================================================
# RepairTreeRunner — Phase 1 implementation
# ===========================================================================


class RepairTreeRunner:
    """BFS / BEAM_K tree-search repair orchestrator.

    Phase 1 wires the layer-dispatch loop composing the canonical
    ``posture_weight_for`` (K sizing), ``WorktreeManager`` (per-branch
    isolation), ``patch_signature_hash`` (branch dedup), and the
    shared ``RepairBudget.max_total_validation_runs`` envelope (no
    parallel budget bookkeeping).

    The runner is intentionally separate from ``RepairEngine`` —
    ``RepairEngine`` keeps its byte-identical LINEAR FSM. The strategy
    gate added in Phase 5 routes BFS/BEAM_K to this class while
    LINEAR continues through ``RepairEngine._run_inner`` unchanged
    (master-flag-FALSE rollback path).

    Authority asymmetry (§1 Boundary): the runner orchestrates;
    GENERATE authority lives with ``BranchGenerator``, VALIDATE
    authority lives with ``BranchValidator``, isolation authority
    lives with ``WorktreeManager``. This module makes no decisions
    about correctness — it only schedules.

    Fail-closed contract (§7): ``run_tree`` NEVER raises into the
    orchestrator except for ``asyncio.CancelledError`` (which
    propagates per existing repair_engine convention so the
    orchestrator handles POSTMORTEM itself). All other infrastructure
    errors quarantine to a per-branch ``PRUNED_VALIDATOR`` outcome.
    """

    # Estimated validation runs per branch when projecting K against
    # the shared budget envelope. Tuned conservative — actual
    # consumption may be higher (e.g., flake re-runs); the runner
    # tracks actual ``validation_runs_consumed`` post-hoc.
    _RUNS_PER_BRANCH_ESTIMATE: int = 1

    def __init__(
        self,
        budget: TreefinementBudget,
        *,
        repair_budget: Any = None,
        worktree_manager: Optional[WorktreeManager] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Construct a runner.

        Parameters
        ----------
        budget : TreefinementBudget
            Tree-only knobs (strategy, K, beam width, dedup, etc.).
        repair_budget : RepairBudget, optional
            Shared validation envelope. Provides
            ``max_total_validation_runs`` (default 8) and
            ``timebox_s``. Phase 1 reads only the validation cap;
            Phase 5 also consults timebox at the strategy gate.
            When None, the runner falls back to a permissive default
            (validation cap = 8) so tests don't have to construct
            a full RepairBudget.
        worktree_manager : WorktreeManager, optional
            COW git-worktree provider. When None, the runner runs in
            "no-isolation" mode — branches receive a synthetic
            worktree path tied to ``op_id`` + ``branch_id`` and the
            caller is responsible for sandboxing. Production wiring
            (Phase 5) always supplies a real WorktreeManager.
        clock : Callable[[], float], optional
            Monotonic time source. Defaults to ``time.monotonic`` per
            Vector #11 sleep/suspend-immune discipline. Tests inject
            deterministic clocks for wall_ms reproducibility.
        """
        self.budget = budget
        self._repair_budget = repair_budget
        self._worktree_manager = worktree_manager
        self._clock = clock or time.monotonic

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    async def run_tree(
        self,
        *,
        op_id: str,
        generator: BranchGenerator,
        validator: BranchValidator,
        posture: Optional[Posture] = None,
        max_layers: int = 5,
        emergency_brake_check: Optional[EmergencyBrakeCheck] = None,
        deadline_check: Optional[DeadlineCheck] = None,
    ) -> RepairTreeResult:
        """BFS / BEAM_K layer dispatch loop.

        Returns a ``RepairTreeResult`` with at minimum one layer
        (even degraded paths produce telemetry). LINEAR strategy or
        master-flag-FALSE returns an empty-layers result so the caller
        falls through to the legacy ``_run_inner`` unchanged.

        ``asyncio.CancelledError`` propagates immediately (orchestrator
        handles POSTMORTEM); all other infra errors quarantine
        per-branch.
        """
        # Strategy gate — LINEAR short-circuits so the caller can route
        # back to the legacy FSM with byte-identical behavior.
        if self.budget.branching_strategy == BranchingStrategy.LINEAR:
            return RepairTreeResult(
                root_op_id=op_id,
                layers=(),
                winning_branch_path=(),
                final_status=None,
            )

        # Master-flag check — descriptive, not authoritative. The Phase 5
        # strategy gate at RepairEngine.run() should have already
        # short-circuited if the flag is FALSE; this is defense-in-depth.
        if not treefinement_enabled():
            return RepairTreeResult(
                root_op_id=op_id,
                layers=(),
                winning_branch_path=(),
                final_status=None,
            )

        # Initial emergency-brake check — if globally braked, don't
        # even spin up layer 0.
        if emergency_brake_check and self._safe_brake_check(
            emergency_brake_check
        ):
            logger.info(
                "[RepairTree] op=%s emergency_brake active at startup; "
                "returning empty result for LINEAR fallback",
                op_id,
            )
            return RepairTreeResult(
                root_op_id=op_id,
                layers=(),
                winning_branch_path=(),
                final_status=None,
            )

        max_total_runs = self._max_validation_runs()
        runs_consumed_total = 0
        seen_branch_ids: Set[str] = set()
        layers: List[RepairTreeLayer] = []
        sibling_context: Tuple[RepairBranch, ...] = ()
        parent_for_next: Optional[RepairBranch] = None

        for layer_index in range(max(1, max_layers)):
            # Per-layer brake re-check — operator may flip mid-tree.
            if emergency_brake_check and self._safe_brake_check(
                emergency_brake_check
            ):
                # Record a synthetic budget-terminal layer so the audit
                # trail shows WHERE the tree stopped, not just that it did.
                layers.append(
                    self._budget_terminal_layer(
                        layer_index=layer_index,
                        wall_ms=0.0,
                    )
                )
                break

            # Per-layer deadline check.
            if deadline_check is not None:
                remaining = self._safe_deadline_check(deadline_check)
                if remaining is not None and remaining <= 0:
                    layers.append(
                        self._budget_terminal_layer(
                            layer_index=layer_index,
                            wall_ms=0.0,
                        )
                    )
                    break

            remaining_runs = max(0, max_total_runs - runs_consumed_total)
            k = _compute_layer_k(
                posture=posture,
                base_k=self.budget.max_branches_per_layer,
                remaining_runs=remaining_runs,
                runs_per_branch=self._RUNS_PER_BRANCH_ESTIMATE,
            )

            layer_start = self._clock()
            try:
                layer = await self._dispatch_layer(
                    op_id=op_id,
                    layer_index=layer_index,
                    k=k,
                    parent_branch=parent_for_next,
                    sibling_context=sibling_context,
                    seen_branch_ids=seen_branch_ids,
                    generator=generator,
                    validator=validator,
                    layer_start=layer_start,
                    remaining_runs=remaining_runs,
                )
            except asyncio.CancelledError:
                # Cancellation MUST propagate so the orchestrator can
                # handle POSTMORTEM. Worktree cleanup happens inside
                # _materialize_and_validate_branch via finally blocks.
                raise

            layers.append(layer)
            runs_consumed_total += sum(
                b.validation_runs_consumed for b in layer.branches
            )

            # WON terminal — early-return with the winning path.
            if layer.verdict == LayerVerdict.WON_TERMINAL:
                won = next(
                    b for b in layer.branches
                    if b.outcome == BranchOutcome.WON
                )
                winning_path = self._build_winning_path(
                    won_branch=won,
                    layers=tuple(layers),
                )
                return RepairTreeResult(
                    root_op_id=op_id,
                    layers=tuple(layers),
                    winning_branch_path=winning_path,
                    final_status=None,
                )

            # Hard breaks for terminal verdicts.
            if layer.verdict in (
                LayerVerdict.EXHAUSTED,
                LayerVerdict.BUDGET_TERMINAL,
            ):
                break

            # Setup for next layer — sibling context = ALL branches
            # (winners + losers; both are signal for cross-branch
            # learning per AlphaVerus). Parent = best survivor.
            sibling_context = layer.branches
            survivors = _select_survivors(
                layer.branches,
                strategy=self.budget.branching_strategy,
                beam_width=self.budget.beam_width,
            )
            if survivors:
                parent_for_next = max(
                    survivors, key=lambda b: b.validator_score
                )

        return RepairTreeResult(
            root_op_id=op_id,
            layers=tuple(layers),
            winning_branch_path=(),
            final_status=None,
        )

    # ---------------------------------------------------------------------
    # Internal — layer dispatch + per-branch lifecycle
    # ---------------------------------------------------------------------

    async def _dispatch_layer(
        self,
        *,
        op_id: str,
        layer_index: int,
        k: int,
        parent_branch: Optional[RepairBranch],
        sibling_context: Tuple[RepairBranch, ...],
        seen_branch_ids: Set[str],
        generator: BranchGenerator,
        validator: BranchValidator,
        layer_start: float,
        remaining_runs: int,
    ) -> RepairTreeLayer:
        """Generate K branches in parallel; materialize + validate
        each in its own worktree; aggregate to a layer verdict.

        ``asyncio.gather(return_exceptions=True)`` guarantees that one
        branch's failure cannot poison the other K-1. Per-branch
        exceptions quarantine to PRUNED_VALIDATOR with structured
        diagnostic.
        """
        # Stage 1 — parallel generation (K candidate diffs)
        gen_coros = [
            self._safe_generate(
                generator=generator,
                op_id=op_id,
                layer_index=layer_index,
                parent_branch=parent_branch,
                sibling_outcomes=sibling_context,
            )
            for _ in range(k)
        ]
        gen_results = await asyncio.gather(
            *gen_coros, return_exceptions=True
        )
        # CancelledError MUST propagate — orchestrator handles
        # POSTMORTEM. asyncio.gather(return_exceptions=True) captures
        # cancellation as a result (3.8+ behavior); we re-raise
        # explicitly so the §1 Boundary contract holds.
        for entry in gen_results:
            if isinstance(entry, asyncio.CancelledError):
                raise entry

        # Stage 2 — per-branch materialize + validate (also parallel)
        branch_coros: List[Awaitable[RepairBranch]] = []
        local_seen: Set[str] = set()  # within-layer dedup snapshot
        for gen_result in gen_results:
            if isinstance(gen_result, BaseException):
                branch_coros.append(
                    self._wrap_in_coro(
                        self._infra_failed_branch(
                            layer_index=layer_index,
                            parent_branch=parent_branch,
                            failure_class="generator_exception",
                            fix_hypothesis=(
                                f"generator raised: "
                                f"{type(gen_result).__name__}"
                            ),
                        )
                    )
                )
                continue

            diff, hypothesis, cost_usd = gen_result
            branch_id = _branch_id_for(diff)

            # Cross-branch dedup (within layer + across layers)
            if self.budget.branch_dedup_enabled and (
                branch_id in seen_branch_ids
                or branch_id in local_seen
            ):
                branch_coros.append(
                    self._wrap_in_coro(
                        self._pruned_duplicate_branch(
                            branch_id=branch_id,
                            layer_index=layer_index,
                            parent_branch=parent_branch,
                            diff=diff,
                            fix_hypothesis=hypothesis,
                            cost_usd=cost_usd,
                        )
                    )
                )
                continue

            local_seen.add(branch_id)
            branch_coros.append(
                self._materialize_and_validate_branch(
                    op_id=op_id,
                    branch_id=branch_id,
                    layer_index=layer_index,
                    parent_branch=parent_branch,
                    diff=diff,
                    fix_hypothesis=hypothesis,
                    cost_usd=cost_usd,
                    validator=validator,
                )
            )

        gathered = await asyncio.gather(
            *branch_coros, return_exceptions=True
        )
        # CancelledError propagates (same contract as Stage 1).
        for entry in gathered:
            if isinstance(entry, asyncio.CancelledError):
                raise entry
        branches: List[RepairBranch] = []
        for entry in gathered:
            if isinstance(entry, RepairBranch):
                branches.append(entry)
            else:
                # Defense in depth — gather exceptions should be
                # impossible because every branch coro catches its own,
                # but if one slips through we quarantine it.
                branches.append(
                    self._infra_failed_branch(
                        layer_index=layer_index,
                        parent_branch=parent_branch,
                        failure_class="branch_coroutine_exception",
                        fix_hypothesis=(
                            f"branch coro raised: "
                            f"{type(entry).__name__}"
                        ),
                    )
                )

        # Commit successful branch_ids to the cross-layer dedup set
        for b in branches:
            if b.outcome != BranchOutcome.PRUNED_DUPLICATE:
                seen_branch_ids.add(b.branch_id)

        # Compute survivors + verdict
        survivors = _select_survivors(
            tuple(branches),
            strategy=self.budget.branching_strategy,
            beam_width=self.budget.beam_width,
        )
        runs_this_layer = sum(
            b.validation_runs_consumed for b in branches
        )
        budget_remaining_after = remaining_runs - runs_this_layer
        verdict = _aggregate_layer_verdict(
            tuple(branches),
            survivors=survivors,
            budget_remaining=budget_remaining_after,
        )

        wall_ms = max(0.0, (self._clock() - layer_start) * 1000.0)
        return RepairTreeLayer(
            layer_index=layer_index,
            branches=tuple(branches),
            verdict=verdict,
            wall_ms=wall_ms,
            parallel_units_actual=k,
        )

    async def _materialize_and_validate_branch(
        self,
        *,
        op_id: str,
        branch_id: str,
        layer_index: int,
        parent_branch: Optional[RepairBranch],
        diff: str,
        fix_hypothesis: str,
        cost_usd: float,
        validator: BranchValidator,
    ) -> RepairBranch:
        """Create worktree → run validator → return frozen branch.

        Worktree cleanup runs in ``finally`` so even cancellation
        leaves no orphan worktrees beyond what the canonical
        ``WorktreeManager.reap_orphans`` boot sweep covers.

        Worktree creation failure surfaces as ``PRUNED_VALIDATOR``
        with ``failure_class=infra`` and structured ``fix_hypothesis``
        — never falls back to a shared tree (§1 Boundary mirror of
        the L3 ``subagent_scheduler`` discipline).
        """
        worktree_path: Optional[Path] = None
        worktree_id: Optional[str] = None
        if self._worktree_manager is not None:
            branch_name = f"ouroboros/repair-tree/{op_id}/{branch_id[:12]}"
            try:
                worktree_path = await self._worktree_manager.create(
                    branch_name
                )
                worktree_id = branch_name
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning(
                    "[RepairTree] op=%s branch=%s "
                    "worktree_create_failed: %s",
                    op_id, branch_id[:12], exc,
                )
                return RepairBranch(
                    branch_id=branch_id,
                    parent_branch_id=(
                        parent_branch.branch_id if parent_branch else None
                    ),
                    layer_index=layer_index,
                    failure_class="infra",
                    fix_hypothesis=(
                        f"worktree_create_failed:"
                        f"{type(exc).__name__}:{exc}"
                    ),
                    diff=diff,
                    validator_score=0.0,
                    outcome=BranchOutcome.PRUNED_VALIDATOR,
                    prune_reason=(
                        PruningReason.VALIDATION_BUDGET_EXHAUSTED
                    ),
                    worktree_id=None,
                    cost_usd=cost_usd,
                    validation_runs_consumed=0,
                )

        validation_dir = worktree_path or Path(
            f"/tmp/no-isolation/{op_id}/{branch_id[:12]}"
        )
        try:
            try:
                outcome, score, prune_reason, runs = await validator(
                    op_id=op_id,
                    branch_id=branch_id,
                    diff=diff,
                    worktree_dir=validation_dir,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — fail-closed contract
                logger.warning(
                    "[RepairTree] op=%s branch=%s validator_exception: %s",
                    op_id, branch_id[:12], exc,
                )
                outcome = BranchOutcome.PRUNED_VALIDATOR
                score = 0.0
                prune_reason = (
                    PruningReason.VALIDATION_BUDGET_EXHAUSTED
                )
                runs = 0

            return RepairBranch(
                branch_id=branch_id,
                parent_branch_id=(
                    parent_branch.branch_id if parent_branch else None
                ),
                layer_index=layer_index,
                failure_class=(
                    parent_branch.failure_class if parent_branch
                    else ""
                ),
                fix_hypothesis=fix_hypothesis,
                diff=diff,
                validator_score=float(score),
                outcome=outcome,
                prune_reason=prune_reason,
                worktree_id=worktree_id,
                cost_usd=cost_usd,
                validation_runs_consumed=int(runs),
            )
        finally:
            # Worktree cleanup — best-effort, swallow errors. The
            # canonical reap_orphans sweep on next boot covers anything
            # we miss (e.g., cancellation arriving during cleanup).
            if (
                self._worktree_manager is not None
                and worktree_path is not None
            ):
                try:
                    await self._worktree_manager.cleanup(worktree_path)
                except Exception:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[RepairTree] worktree cleanup failed for %s",
                        worktree_path,
                        exc_info=True,
                    )

    async def _safe_generate(
        self,
        *,
        generator: BranchGenerator,
        op_id: str,
        layer_index: int,
        parent_branch: Optional[RepairBranch],
        sibling_outcomes: Tuple[RepairBranch, ...],
    ) -> Tuple[str, str, float]:
        """Wrap generator call so exceptions surface as gather entries
        (rather than poisoning the gather)."""
        return await generator(
            op_id=op_id,
            layer_index=layer_index,
            parent_branch=parent_branch,
            sibling_outcomes=sibling_outcomes,
        )

    @staticmethod
    async def _wrap_in_coro(value: RepairBranch) -> RepairBranch:
        """Lift a synchronously-built RepairBranch into a coroutine so
        it can join the async gather alongside live materialize calls."""
        return value

    # ---------------------------------------------------------------------
    # Internal — synchronous branch builders (no I/O)
    # ---------------------------------------------------------------------

    def _infra_failed_branch(
        self,
        *,
        layer_index: int,
        parent_branch: Optional[RepairBranch],
        failure_class: str,
        fix_hypothesis: str,
    ) -> RepairBranch:
        """Synthetic branch for infrastructure failures (generator
        exception, branch coroutine exception). Surfaced in the layer
        so operators can see WHY a branch slot was wasted."""
        # branch_id derived from a synthetic seed so cross-layer dedup
        # doesn't collapse all infra failures into one entry.
        synthetic_seed = (
            f"infra:{layer_index}:{failure_class}:{fix_hypothesis}"
        )
        return RepairBranch(
            branch_id=patch_signature_hash(synthetic_seed),
            parent_branch_id=(
                parent_branch.branch_id if parent_branch else None
            ),
            layer_index=layer_index,
            failure_class=failure_class,
            fix_hypothesis=fix_hypothesis,
            diff="",
            validator_score=0.0,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.VALIDATION_BUDGET_EXHAUSTED,
            worktree_id=None,
            cost_usd=0.0,
            validation_runs_consumed=0,
        )

    def _pruned_duplicate_branch(
        self,
        *,
        branch_id: str,
        layer_index: int,
        parent_branch: Optional[RepairBranch],
        diff: str,
        fix_hypothesis: str,
        cost_usd: float,
    ) -> RepairBranch:
        return RepairBranch(
            branch_id=branch_id,
            parent_branch_id=(
                parent_branch.branch_id if parent_branch else None
            ),
            layer_index=layer_index,
            failure_class=(
                parent_branch.failure_class if parent_branch else ""
            ),
            fix_hypothesis=fix_hypothesis,
            diff=diff,
            validator_score=0.0,
            outcome=BranchOutcome.PRUNED_DUPLICATE,
            prune_reason=PruningReason.DUPLICATE_PATCH_SIG,
            worktree_id=None,
            cost_usd=cost_usd,
            validation_runs_consumed=0,
        )

    def _budget_terminal_layer(
        self,
        *,
        layer_index: int,
        wall_ms: float,
    ) -> RepairTreeLayer:
        """Synthetic empty layer recording the BUDGET_TERMINAL boundary
        so audit trails show WHERE the tree stopped."""
        return RepairTreeLayer(
            layer_index=layer_index,
            branches=(),
            verdict=LayerVerdict.BUDGET_TERMINAL,
            wall_ms=wall_ms,
            parallel_units_actual=0,
        )

    # ---------------------------------------------------------------------
    # Internal — defensive accessors
    # ---------------------------------------------------------------------

    def _max_validation_runs(self) -> int:
        """Read shared validation envelope from the injected
        RepairBudget. Fallback to 8 (the canonical default) when None
        is injected so tests don't have to construct a full
        RepairBudget. Defensive against malformed budgets — any
        exception raised by the attribute accessor (e.g., a property
        getter that explodes) falls back to the default."""
        rb = self._repair_budget
        if rb is None:
            return 8
        try:
            value = getattr(rb, "max_total_validation_runs", None)
            if value is None:
                return 8
            return int(value)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive fallback
            logger.debug(
                "[RepairTree] repair_budget.max_total_validation_runs "
                "raised; falling back to default 8",
                exc_info=True,
            )
            return 8

    @staticmethod
    def _safe_brake_check(check: EmergencyBrakeCheck) -> bool:
        """Defensive wrapper — emergency brake check failure MUST NOT
        crash the runner. Returns False (no-brake) on exception."""
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[RepairTree] emergency_brake_check raised; "
                "treating as inactive",
                exc_info=True,
            )
            return False

    @staticmethod
    def _safe_deadline_check(check: DeadlineCheck) -> Optional[float]:
        """Defensive wrapper — deadline check failure MUST NOT crash
        the runner. Returns None (no-deadline-info) on exception."""
        try:
            return check()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[RepairTree] deadline_check raised; "
                "treating as no-deadline",
                exc_info=True,
            )
            return None

    @staticmethod
    def _build_winning_path(
        *,
        won_branch: RepairBranch,
        layers: Tuple[RepairTreeLayer, ...],
    ) -> Tuple[str, ...]:
        """Walk parent_branch_id pointers root→leaf for the audit
        trail. Returns the chain ending in won_branch.branch_id."""
        # Build a quick branch_id → branch lookup across all layers.
        index: Dict[str, RepairBranch] = {}
        for layer in layers:
            for b in layer.branches:
                index[b.branch_id] = b

        # Walk parent pointers from won_branch backward.
        chain: List[str] = []
        cursor: Optional[RepairBranch] = won_branch
        seen: Set[str] = set()  # cycle guard (defense in depth)
        while cursor is not None and cursor.branch_id not in seen:
            seen.add(cursor.branch_id)
            chain.append(cursor.branch_id)
            parent_id = cursor.parent_branch_id
            cursor = index.get(parent_id) if parent_id else None
        chain.reverse()
        return tuple(chain)


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration.

    Picked up zero-edit by
    ``flag_registry_seed._discover_module_provided_flags`` walker on
    next boot (the walker scans direct submodules of
    ``backend.core.ouroboros.governance``). NEVER raises — fail-open
    per §33.1.

    Returns the count of FlagSpecs successfully registered.
    """
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for AlphaVerus Treefinement L2 "
                "tree-search repair (PRD §40.7.2 grounding — "
                "https://arxiv.org/abs/2412.06176). When false, "
                "RepairEngine.run() retains byte-identical LINEAR "
                "FSM behavior via _run_inner; RepairTreeRunner is "
                "unreachable. Default FALSE per §33.1 graduation "
                "contract — flip via 3-clean-soak Phase 9 ladder "
                "when tree mode demonstrates >=10% L2 success-rate "
                "lift OR >=20% wall-clock reduction at parity "
                "success rate."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/repair_tree.py"
            ),
            example="true",
            since="Treefinement Phase 0 (2026-05-11)",
        ),
        FlagSpec(
            name=STRATEGY_ENV_VAR,
            type=FlagType.STR,
            default="linear",
            description=(
                "Branching strategy for L2 tree-search. Values: "
                "'linear' (legacy FSM, default), 'bfs' (all "
                "survivors expand), 'beam_k' (top-M survive). "
                "Invalid values fall back to LINEAR with a "
                "structured warning log."
            ),
            category=Category.ROUTING,
            source_file=(
                "backend/core/ouroboros/governance/repair_tree.py"
            ),
            example="bfs",
            since="Treefinement Phase 0 (2026-05-11)",
        ),
        FlagSpec(
            name=MAX_BRANCHES_PER_LAYER_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_MAX_BRANCHES_PER_LAYER,
            description=(
                "K cap per layer post posture-weighting. Tree size "
                "= K^layers, bounded by RepairBudget."
                "max_total_validation_runs (shared envelope — no "
                "parallel budget state). Clamped [1, 16]. Default 3 "
                "per operator approval — leaves room for ~2.6 "
                "layers under the existing 8-run cap."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/repair_tree.py"
            ),
            example="3",
            since="Treefinement Phase 0 (2026-05-11)",
        ),
        FlagSpec(
            name=BEAM_WIDTH_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_BEAM_WIDTH,
            description=(
                "M survivors per layer (BEAM_K strategy only). "
                "Top-M ranked by validator_score advance to next "
                "layer; remainder PRUNED_VALIDATOR or "
                "WORSE_THAN_SIBLING. Clamped [1, 16]."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/repair_tree.py"
            ),
            example="2",
            since="Treefinement Phase 0 (2026-05-11)",
        ),
        FlagSpec(
            name=BRANCH_DEDUP_ENV_VAR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Whether _patch_sig collisions across siblings "
                "trigger PRUNED_DUPLICATE. Default TRUE — composes "
                "the canonical repair_engine._patch_sig signature "
                "(no parallel signature machinery). Disable only "
                "for diagnostic runs measuring tree expansion "
                "without dedup pressure."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/repair_tree.py"
            ),
            example="true",
            since="Treefinement Phase 0 (2026-05-11)",
        ),
        FlagSpec(
            name=CROSS_BRANCH_LEARNING_ENV_VAR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Whether layer-N+1 GENERATE prompt receives a "
                "'## Sibling Branch Outcomes' block listing pruned-"
                "sibling fix hypotheses + validator scores (top-2, "
                "<=200-token cap). This is the AlphaVerus delta "
                "over naive parallel repair — without it tree mode "
                "degrades to race-the-loop. Default TRUE per "
                "operator approval."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/repair_tree.py"
            ),
            example="true",
            since="Treefinement Phase 0 (2026-05-11)",
        ),
        FlagSpec(
            name=EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_EMERGENCY_DEMOTE_THRESHOLD,
            description=(
                "Cost-burn fraction above which the runner auto-"
                "demotes from BFS/BEAM_K to LINEAR for the "
                "remaining timebox. Composes the canonical "
                "SensorGovernor emergency_brake signal (no parallel "
                "emergency state). Clamped [0.0, 1.0]. Default 0.85."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/repair_tree.py"
            ),
            example="0.85",
            since="Treefinement Phase 0 (2026-05-11)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — boot-time fail-open
            logger.debug(
                "[RepairTree] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "REPAIR_TREE_SCHEMA_VERSION",
    "MASTER_FLAG_ENV_VAR",
    "STRATEGY_ENV_VAR",
    "MAX_BRANCHES_PER_LAYER_ENV_VAR",
    "BEAM_WIDTH_ENV_VAR",
    "BRANCH_DEDUP_ENV_VAR",
    "CROSS_BRANCH_LEARNING_ENV_VAR",
    "EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR",
    "BranchingStrategy",
    "BranchOutcome",
    "LayerVerdict",
    "PruningReason",
    "RepairBranch",
    "RepairTreeLayer",
    "RepairTreeResult",
    "TreefinementBudget",
    "RepairTreeRunner",
    "BranchGenerator",
    "BranchValidator",
    "EmergencyBrakeCheck",
    "DeadlineCheck",
    "treefinement_enabled",
    "register_flags",
]
