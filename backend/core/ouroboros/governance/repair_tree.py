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
# RepairTreeRunner — Phase 0 skeleton (execution lives in Phase 1)
# ===========================================================================


class RepairTreeRunner:
    """BFS / BEAM_K tree-search repair orchestrator.

    Phase 0 ships the constructor signature + public method stubs.
    Phase 1 wires the actual layer-dispatch loop composing
    ``parallel_dispatch.build_execution_graph`` +
    ``worktree_manager.WorktreeManager`` + the existing
    ``repair_engine`` generation path.

    The runner is intentionally separate from ``RepairEngine`` —
    ``RepairEngine`` keeps its byte-identical LINEAR FSM. The strategy
    gate added in Phase 1 routes ``BFS``/``BEAM_K`` to this class
    while ``LINEAR`` continues through ``RepairEngine._run_inner``
    unchanged (master-flag-FALSE rollback path).
    """

    def __init__(
        self,
        budget: TreefinementBudget,
        *,
        repair_budget: Any = None,
        worktree_manager: Any = None,
        clock: Any = None,
    ) -> None:
        """Construct a runner.

        Parameters
        ----------
        budget : TreefinementBudget
            Tree-only knobs (strategy, K, beam width, etc.).
        repair_budget : RepairBudget, optional
            Shared validation envelope (max_total_validation_runs,
            timebox_s). Phase 1 runner consults this; Phase 0 stores
            it for symmetry. Injection-friendly for tests.
        worktree_manager : WorktreeManager, optional
            COW git-worktree provider per branch. Phase 1 wires the
            real ``worktree_manager.WorktreeManager``; Phase 0 accepts
            the dependency for testability.
        clock : Callable[[], float], optional
            Monotonic time source. Defaults to ``time.monotonic``.
            Tests inject deterministic clocks.
        """
        self.budget = budget
        self._repair_budget = repair_budget
        self._worktree_manager = worktree_manager
        self._clock = clock or time.monotonic

    async def run_tree(
        self, *_args: Any, **_kwargs: Any,
    ) -> RepairTreeResult:
        """BFS layer dispatch — Phase 1.

        Phase 0 raises ``NotImplementedError`` to make accidental
        wiring loud. The master flag stays default-FALSE through
        Phases 0-4 specifically so this method is unreachable in
        production until Phase 5 hardening + Phase 6 PRD update land.
        """
        del _args, _kwargs  # Phase 1 will define the real signature
        raise NotImplementedError(
            "RepairTreeRunner.run_tree is Phase 1; Phase 0 ships the "
            "substrate skeleton only. Master flag "
            f"({MASTER_FLAG_ENV_VAR}) must remain default-FALSE until "
            "Phases 1+2+3 land and Phase 5 hardening passes."
        )


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
    "treefinement_enabled",
    "register_flags",
]
