"""L2 Iterative Self-Repair Loop Engine

Provides configuration, runtime budget tracking, and FSM-driven repair orchestration
for Ouroboros governance operations that fail validation.

The repair loop implements:
- Multi-iteration classification and fix generation
- Test-driven repair with failure class tracking
- Adaptive timeout and cost budgeting
- Flaky test detection and confirmation
- Progress tracking and early termination

This module is structured as:
1. **RepairBudget** - Immutable configuration loaded from environment
2. **L2State / L2Event** - FSM state and event enumerations
3. **RepairIterationRecord** - Per-iteration ledger payload
4. **RepairResult** - Terminal outcome returned to the orchestrator
5. **RepairEngine** - FSM executor and repair orchestration
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RepairBudget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairBudget:
    """Immutable repair loop resource and iteration budget.

    Loaded from environment variables at system startup. All fields are frozen
    and cannot be mutated after construction.

    Parameters
    ----------
    enabled : bool
        Whether L2 iterative repair is enabled. Set via ``JARVIS_L2_ENABLED``
        (default: ``True``). L2 closes the self-repair loop on validation
        failure — Manifesto §6 threshold-triggered neuroplasticity.
    max_iterations : int
        Maximum repair iterations before hard stop. Set via ``JARVIS_L2_MAX_ITERS``
        (default: ``5``).
    timebox_s : float
        Total wall-clock time budget for entire repair loop in seconds.
        Set via ``JARVIS_L2_TIMEBOX_S`` (default: ``120.0``).
    min_deadline_remaining_s : float
        Minimum remaining deadline before stopping repair. If operation deadline
        is less than this value, repair halts. Set via ``JARVIS_L2_MIN_DEADLINE_S``
        (default: ``10.0``).
    per_iteration_test_timeout_s : float
        Test execution timeout per iteration in seconds. Set via ``JARVIS_L2_ITER_TEST_TIMEOUT_S``
        (default: ``60.0``).
    max_diff_lines : int
        Maximum diff lines per candidate. Set via ``JARVIS_L2_MAX_DIFF_LINES``
        (default: ``150``).
    max_files_changed : int
        Maximum files changed per candidate. Set via ``JARVIS_L2_MAX_FILES_CHANGED``
        (default: ``3``).
    max_total_validation_runs : int
        Maximum total validation/test runs across all iterations.
        Set via ``JARVIS_L2_MAX_VALIDATION_RUNS`` (default: ``8``).
    no_progress_streak_kill : int
        Kill repair after N consecutive failures with no progress.
        Set via ``JARVIS_L2_NO_PROGRESS_KILL`` (default: ``2``).
    max_class_retries : Dict[str, int]
        Max retries per failure class. Keys: ``"syntax"``, ``"test"``, ``"flake"``, ``"env"``.
        Set via ``JARVIS_L2_CLASS_RETRIES_JSON`` (default: ``{"syntax":2,"test":3,"flake":2,"env":1}``).
    flake_confirm_reruns : int
        How many times to rerun a passing test to confirm it's not flaky.
        Set via ``JARVIS_L2_FLAKE_RERUNS`` (default: ``1``).
    """

    enabled: bool = True
    max_iterations: int = 5
    timebox_s: float = 120.0
    min_deadline_remaining_s: float = 10.0
    per_iteration_test_timeout_s: float = 60.0
    max_diff_lines: int = 150
    max_files_changed: int = 3
    max_total_validation_runs: int = 8
    no_progress_streak_kill: int = 2
    # Slice 5A — L2 provider isolation: bound each iteration's provider
    # generate call so a single 118s Claude stream cannot eat the whole
    # pipeline budget and starve all remaining iters. Default 45s gives
    # the provider enough headroom for a reasoned patch while leaving
    # budget for 2-3 more iters under a 120s timebox.
    per_iter_provider_timeout_s: float = 45.0
    # Stop the engine if N consecutive iters timeout on the provider
    # (avoids burning the full timebox on a wedged provider chain).
    max_consecutive_provider_timeouts: int = 2
    max_class_retries: Dict[str, int] = dataclasses.field(
        default_factory=lambda: {"syntax": 2, "test": 3, "flake": 2, "env": 1}
    )
    flake_confirm_reruns: int = 1

    @classmethod
    def from_env(cls) -> RepairBudget:
        """Load RepairBudget configuration from environment variables.

        All environment variables are optional. Missing variables fall back to
        defaults. For ``JARVIS_L2_CLASS_RETRIES_JSON``, parse errors log a
        warning and use the default dict.

        Returns
        -------
        RepairBudget
            Frozen budget instance with values read from environment.
        """
        # Boolean parsing: L2 defaults to enabled (Manifesto §6 — the
        # self-repair loop is load-bearing for the Ouroboros cycle).
        # Accept explicit "false" / "0" / "no" to opt out.
        enabled_str = os.environ.get("JARVIS_L2_ENABLED", "true").lower()
        enabled = enabled_str not in ("false", "0", "no", "off")

        # Integer parsing
        max_iterations = int(os.environ.get("JARVIS_L2_MAX_ITERS", "5"))
        max_diff_lines = int(os.environ.get("JARVIS_L2_MAX_DIFF_LINES", "150"))
        max_files_changed = int(os.environ.get("JARVIS_L2_MAX_FILES_CHANGED", "3"))
        max_total_validation_runs = int(os.environ.get("JARVIS_L2_MAX_VALIDATION_RUNS", "8"))
        no_progress_streak_kill = int(os.environ.get("JARVIS_L2_NO_PROGRESS_KILL", "2"))
        flake_confirm_reruns = int(os.environ.get("JARVIS_L2_FLAKE_RERUNS", "1"))

        # Float parsing
        timebox_s = float(os.environ.get("JARVIS_L2_TIMEBOX_S", "120.0"))
        min_deadline_remaining_s = float(os.environ.get("JARVIS_L2_MIN_DEADLINE_S", "10.0"))
        per_iteration_test_timeout_s = float(os.environ.get("JARVIS_L2_ITER_TEST_TIMEOUT_S", "60.0"))
        # Slice 5A — L2 provider isolation knobs
        per_iter_provider_timeout_s = float(
            os.environ.get("JARVIS_L2_PER_ITER_PROVIDER_TIMEOUT_S", "45.0"),
        )
        max_consecutive_provider_timeouts = int(
            os.environ.get("JARVIS_L2_MAX_CONSECUTIVE_PROVIDER_TIMEOUTS", "2"),
        )

        # JSON parsing with fallback to default
        max_class_retries_json = os.environ.get("JARVIS_L2_CLASS_RETRIES_JSON")
        if max_class_retries_json:
            try:
                max_class_retries = json.loads(max_class_retries_json)
            except (json.JSONDecodeError, ValueError) as e:
                _logger.warning(
                    "Failed to parse JARVIS_L2_CLASS_RETRIES_JSON: %s, using defaults",
                    e,
                )
                max_class_retries = cls.__dataclass_fields__["max_class_retries"].default_factory()
        else:
            max_class_retries = cls.__dataclass_fields__["max_class_retries"].default_factory()

        return cls(
            enabled=enabled,
            max_iterations=max_iterations,
            timebox_s=timebox_s,
            min_deadline_remaining_s=min_deadline_remaining_s,
            per_iteration_test_timeout_s=per_iteration_test_timeout_s,
            max_diff_lines=max_diff_lines,
            max_files_changed=max_files_changed,
            max_total_validation_runs=max_total_validation_runs,
            no_progress_streak_kill=no_progress_streak_kill,
            max_class_retries=max_class_retries,
            flake_confirm_reruns=flake_confirm_reruns,
            per_iter_provider_timeout_s=per_iter_provider_timeout_s,
            max_consecutive_provider_timeouts=max_consecutive_provider_timeouts,
        )


# ---------------------------------------------------------------------------
# L2 FSM enumerations
# ---------------------------------------------------------------------------


class L2State(str, enum.Enum):
    L2_INIT = "L2_INIT"
    L2_PREPARE_BASELINE = "L2_PREPARE_BASELINE"
    L2_GENERATE_PATCH = "L2_GENERATE_PATCH"
    L2_MATERIALIZE_CANDIDATE = "L2_MATERIALIZE_CANDIDATE"
    L2_RUN_VALIDATION = "L2_RUN_VALIDATION"
    L2_CLASSIFY_FAILURE = "L2_CLASSIFY_FAILURE"
    L2_EVALUATE_PROGRESS = "L2_EVALUATE_PROGRESS"
    L2_DECIDE_RETRY = "L2_DECIDE_RETRY"
    L2_BUILD_REPAIR_PROMPT = "L2_BUILD_REPAIR_PROMPT"
    L2_CONVERGED = "L2_CONVERGED"
    L2_STOPPED = "L2_STOPPED"
    L2_ABORTED = "L2_ABORTED"


class L2Event(str, enum.Enum):
    EV_START = "EV_START"
    EV_PATCH_GENERATED = "EV_PATCH_GENERATED"
    EV_PATCH_INVALID = "EV_PATCH_INVALID"
    EV_VALIDATION_PASS = "EV_VALIDATION_PASS"
    EV_VALIDATION_FAIL = "EV_VALIDATION_FAIL"
    EV_FAILURE_CLASSIFIED_SYNTAX = "EV_FAILURE_CLASSIFIED_SYNTAX"
    EV_FAILURE_CLASSIFIED_TEST = "EV_FAILURE_CLASSIFIED_TEST"
    EV_FAILURE_CLASSIFIED_ENV = "EV_FAILURE_CLASSIFIED_ENV"
    EV_FAILURE_CLASSIFIED_FLAKE = "EV_FAILURE_CLASSIFIED_FLAKE"
    EV_PROGRESS = "EV_PROGRESS"
    EV_NO_PROGRESS = "EV_NO_PROGRESS"
    EV_OSCILLATION_DETECTED = "EV_OSCILLATION_DETECTED"
    EV_BUDGET_EXHAUSTED = "EV_BUDGET_EXHAUSTED"
    EV_NON_RETRYABLE_ENV = "EV_NON_RETRYABLE_ENV"
    EV_RETRY_ALLOWED = "EV_RETRY_ALLOWED"
    EV_RETRY_DENIED = "EV_RETRY_DENIED"
    EV_CANCEL = "EV_CANCEL"
    EV_FATAL_INFRA = "EV_FATAL_INFRA"


# ---------------------------------------------------------------------------
# RepairIterationRecord and RepairResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairIterationRecord:
    """Ledger payload for one repair iteration. schema_version: repair.iter.v1"""

    schema_version: str = "repair.iter.v1"
    op_id: str = ""
    iteration: int = 0
    repair_state: str = ""
    failure_class: str = ""
    failure_signature_hash: str = ""
    patch_signature_hash: str = ""
    diff_lines: int = 0
    files_changed: int = 0
    validation_duration_s: float = 0.0
    outcome: str = ""    # "progress"|"no_progress"|"converged"|"stopped"|"aborted"
    stop_reason: Optional[str] = None
    model_id: str = ""
    provider_name: str = ""


@dataclass(frozen=True)
class RepairResult:
    """Terminal outcome returned by RepairEngine.run() to the orchestrator.

    Note: ``terminal`` is always ``"L2_CONVERGED"`` or ``"L2_STOPPED"``.
    ``"L2_ABORTED"`` is never returned — ``asyncio.CancelledError`` is
    re-raised directly so the orchestrator can handle POSTMORTEM itself.
    Non-CancelledError infra errors are returned as ``"L2_STOPPED"`` with a
    structured ``stop_reason`` (e.g. ``"sandbox_infra_error:OSError"``).
    """

    terminal: str                        # "L2_CONVERGED"|"L2_STOPPED"
    candidate: Optional[Dict[str, Any]]  # converged candidate dict, or None
    stop_reason: Optional[str]           # set when terminal=="L2_STOPPED"
    summary: Dict[str, Any]              # key metrics for ledger payload
    iterations: Tuple[RepairIterationRecord, ...]


@dataclass(frozen=True)
class CandidateGenerationResult:
    """Output of :meth:`RepairEngine._generate_repair_candidate`.

    Phase A (Treefinement Production Wiring v3.4): single-source
    primitive extracted from the inline GENERATE block in
    ``_run_inner``. Composed by BOTH the legacy LINEAR FSM AND the
    Phase C ``ProductionBranchGenerator`` (which uses the
    ``hypothesis_seed`` parameter for cross-branch layer-N+1 context).

    NEVER raises into callers — provider exceptions are quarantined
    into ``stop_reason`` fields. ``asyncio.CancelledError`` is the
    sole exception that propagates (orchestrator-handled POSTMORTEM
    contract).

    Field semantics
    ---------------
    ``candidate``: ``None`` on any failure; ``dict`` on success.
    Callers MUST check ``candidate is None`` before consuming
    ``stop_reason``.

    ``model_id`` / ``provider_name``: ``None`` (sentinel) when the
    provider response had no such attribute OR when the call failed.
    Callers should treat ``None`` as "no value supplied; preserve
    previous value" — this preserves the byte-equivalent semantics
    of the original ``getattr(gen_result, "model_id", previous)``
    fallback pattern in ``_run_inner``.

    ``stop_reason``: ``None`` on success; structured failure code
    on failure. Examples: ``"generate_error:RuntimeError"``,
    ``"empty_candidates"``.
    """

    candidate: Optional[Dict[str, Any]]
    model_id: Optional[str]
    provider_name: Optional[str]
    stop_reason: Optional[str]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _count_diff_lines(diff: str) -> int:
    """Count changed lines in a unified diff (+ and - lines, excluding +++ / ---)."""
    return sum(
        1 for ln in diff.splitlines()
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    )


def _count_diff_files(diff: str) -> int:
    """Count distinct files changed in a unified diff.

    Counts ``+++ b/<path>`` headers; falls back to 1 for single-file
    diffs that lack file headers (schema 2b.1-diff normal case).
    """
    paths = {ln[6:].strip() for ln in diff.splitlines() if ln.startswith("+++ b/")}
    return len(paths) if paths else 1


def _patch_sig(diff: str) -> str:
    """SHA-256 hex digest of a unified diff (delegates to failure_classifier)."""
    from backend.core.ouroboros.governance.failure_classifier import patch_signature_hash
    return patch_signature_hash(diff)


# ---------------------------------------------------------------------------
# RepairEngine
# ---------------------------------------------------------------------------


class RepairEngine:
    """L2 iterative self-repair loop executor.

    Drives the bounded generate→sandbox-run→classify→revise loop.
    Called by the orchestrator after VALIDATE exhaustion when L2 is enabled.

    Parameters
    ----------
    budget:
        Frozen resource limits loaded from env (RepairBudget.from_env()).
    prime_provider:
        Provider with async generate(ctx, deadline, repair_context=None).
    repo_root:
        Path to the repository root (passed to sandbox_factory).
    sandbox_factory:
        Callable(repo_root, test_timeout_s) → async context manager.
        Defaults to RepairSandbox when None.
    ledger:
        Optional OperationLedger for audit trail.
    """

    def __init__(
        self,
        budget: RepairBudget,
        prime_provider: Any,
        repo_root: Any,
        sandbox_factory: Any = None,
        ledger: Any = None,
        context_bridge: Any = None,
    ) -> None:
        self._budget = budget
        self._prime = prime_provider
        self._repo_root = repo_root
        self._ledger = ledger
        # Repair Context Bridge (Slice 2): graph-derived dependency-cone steer.
        # Injectable for tests; lazily built on first use otherwise. Only consulted
        # when JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED is on (else inert / cone=None).
        self._context_bridge = context_bridge
        if sandbox_factory is None:
            from backend.core.ouroboros.governance.repair_sandbox import RepairSandbox
            self._sandbox_factory = RepairSandbox
        else:
            self._sandbox_factory = sandbox_factory
        self._classifier = _lazy_classifier()
        # Live iteration accounting — read by the operator status line
        # (``StatusLineBuilder``) so the glanceable TUI can render
        # ``Phase: L2 Repair 2/8`` while ``run()`` is in progress.
        # Zeroes outside an active ``run()`` call. Write-only inside
        # the run loop; readers should treat as advisory (no lock).
        self._current_iteration: int = 0
        self._max_iterations_live: int = 0

    # ------------------------------------------------------------------
    # Public live-iteration accessors (read by operator status line)
    # ------------------------------------------------------------------
    @property
    def current_iteration(self) -> int:
        """0 when not running; 1..N during an active repair pass."""
        return self._current_iteration

    @property
    def max_iterations_live(self) -> int:
        """``budget.max_iterations`` while ``run()`` is in flight; else 0."""
        return self._max_iterations_live

    @property
    def is_running(self) -> bool:
        """True iff ``run()`` is currently inside its loop."""
        return self._max_iterations_live > 0

    async def run(
        self,
        ctx: Any,
        _best_validation: Any,
        pipeline_deadline: datetime,
    ) -> RepairResult:
        """Execute the L2 repair loop. See :meth:`_run_inner` for behavior.

        This outer wrapper exists solely to guarantee that the live
        iteration counters exposed via :attr:`current_iteration` /
        :attr:`max_iterations_live` / :attr:`is_running` reset to zero
        no matter which of ``run()``'s many return paths (or exceptions)
        fires. The operator status line reads these counters to render
        ``Phase: L2 Repair 2/8`` — stale values after a completed run
        would cause the status line to claim "L2 still running" until
        the next op overwrites them.

        Phase 5 strategy gate
        ---------------------
        Before delegating to the legacy LINEAR FSM, this method consults
        the Treefinement strategy gate
        (:meth:`_maybe_run_treefinement`). When master flag is FALSE
        (default) OR strategy is LINEAR OR no production tree-runner
        factory is registered (Phase 5 default), the gate returns
        ``None`` and we fall through to ``_run_inner`` byte-identically.
        Tree mode requires Phase 6+ to register a production factory.
        """
        try:
            # Phase 5 strategy gate — Treefinement integration point.
            # Position MUST be BEFORE _run_inner so the gate can preempt
            # the legacy FSM. AST-pinned in
            # tests/governance/test_repair_tree_hardening.py.
            tree_result = await self._maybe_run_treefinement(
                ctx, _best_validation, pipeline_deadline,
            )
            if tree_result is not None:
                return tree_result
            return await self._run_inner(
                ctx, _best_validation, pipeline_deadline,
            )
        finally:
            self._current_iteration = 0
            self._max_iterations_live = 0

    async def _maybe_run_treefinement(
        self,
        ctx: Any,
        _best_validation: Any,
        pipeline_deadline: datetime,
    ) -> Optional[RepairResult]:
        """Phase 5 strategy gate.

        Returns a ``RepairResult`` when tree mode handled the op;
        ``None`` when caller should fall through to the legacy
        ``_run_inner``. NEVER raises into the caller.

        Gate decision table (all must be true for tree path):

          1. ``treefinement_enabled()`` — master flag (§33.1 default-FALSE)
          2. ``budget.branching_strategy != LINEAR`` — operator chose
             ``bfs`` or ``beam_k`` via env
          3. ``get_production_tree_runner_factory() is not None`` —
             Phase 6+ registered a production factory

        When ANY of those is false, returns ``None`` (legacy path).
        Tree path failure ALSO returns ``None`` (degraded fallback) —
        only ``asyncio.CancelledError`` propagates.
        """
        try:
            from backend.core.ouroboros.governance.repair_tree import (
                BranchingStrategy,
                TreefinementBudget,
                get_production_tree_runner_factory,
                treefinement_enabled,
            )
        except ImportError:
            return None

        try:
            if not treefinement_enabled():
                return None
            budget = TreefinementBudget.from_env()
            if budget.branching_strategy == BranchingStrategy.LINEAR:
                return None
            factory = get_production_tree_runner_factory()
            if factory is None:
                # Phase E — attempt lazy boot registration. NEVER
                # raises; returns False on any failure. The lazy
                # import is intentional (avoids hard dep on
                # repair_tree_production for non-tree-mode callers).
                try:
                    from backend.core.ouroboros.governance.repair_tree_production import (  # noqa: E501
                        register_production_factory_at_boot,
                    )
                    register_production_factory_at_boot()
                    factory = get_production_tree_runner_factory()
                except ImportError:
                    # repair_tree_production not available → factory
                    # stays None → fall through to LINEAR.
                    factory = None
                except Exception:  # noqa: BLE001 — defensive
                    _logger.debug(
                        "[RepairEngine] lazy production registration "
                        "raised; falling back to LINEAR",
                        exc_info=True,
                    )
                    factory = None

            if factory is None:
                _logger.info(
                    "[RepairEngine] tree mode requested via "
                    "JARVIS_L2_BRANCHING_STRATEGY=%s but no production "
                    "runner factory registered + lazy boot "
                    "registration failed; falling back to LINEAR "
                    "_run_inner",
                    budget.branching_strategy.value,
                )
                return None
            # Phase 6+ runner construction + execution. Phase 5 ships
            # only the gate skeleton — the conversion logic lives with
            # the production wiring that registers the factory.
            return await self._invoke_tree_factory(
                factory=factory,
                budget=budget,
                ctx=ctx,
                pipeline_deadline=pipeline_deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — gate is fail-open
            _logger.warning(
                "[RepairEngine] treefinement gate raised; "
                "falling back to LINEAR _run_inner",
                exc_info=True,
            )
            return None

    async def _invoke_tree_factory(
        self,
        *,
        factory: Any,
        budget: Any,
        ctx: Any,
        pipeline_deadline: datetime,
    ) -> Optional[RepairResult]:
        """Phase D — invoke production factory + adapt tree result.

        Composes the canonical Phase D factory contract:

          1. ``factory(*, budget, ctx, repair_engine, pipeline_deadline,
             posture=None) -> Callable[[], Awaitable[RepairTreeResult]]``
          2. ``await invocation()`` → ``RepairTreeResult``
          3. ``tree_result_to_repair_result(tree_result, op_id=...)
             -> RepairResult``

        Returns ``None`` when ANY stage fails (factory construction
        / tree invocation / result adaptation) so the gate falls
        through to legacy ``_run_inner`` byte-identically.
        Only ``asyncio.CancelledError`` propagates (orchestrator
        POSTMORTEM contract).
        """
        # Stage 1 — construct invocation closure
        try:
            invocation = factory(
                budget=budget,
                ctx=ctx,
                repair_engine=self,
                pipeline_deadline=pipeline_deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail-open per gate contract
            _logger.warning(
                "[RepairEngine] production factory raised during "
                "construction; falling back to LINEAR _run_inner",
                exc_info=True,
            )
            return None

        # Stage 2 — run the tree
        try:
            tree_result = await invocation()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _logger.warning(
                "[RepairEngine] production tree invocation raised; "
                "falling back to LINEAR _run_inner",
                exc_info=True,
            )
            return None

        # Stage 3 — adapt RepairTreeResult → RepairResult
        try:
            from backend.core.ouroboros.governance.repair_tree_production import (  # noqa: E501
                tree_result_to_repair_result,
            )
        except ImportError:
            _logger.warning(
                "[RepairEngine] repair_tree_production unavailable "
                "for adapter import; falling back to LINEAR",
            )
            return None
        op_id = getattr(ctx, "op_id", "") or ""
        try:
            return tree_result_to_repair_result(
                tree_result, op_id=op_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _logger.warning(
                "[RepairEngine] tree-result adapter raised; "
                "falling back to LINEAR _run_inner",
                exc_info=True,
            )
            return None

    async def _run_inner(
        self,
        ctx: Any,
        _best_validation: Any,
        pipeline_deadline: datetime,
    ) -> RepairResult:
        """Execute the L2 repair loop.

        Parameters
        ----------
        ctx:
            OperationContext from the orchestrator.
        _best_validation:
            The best ValidationResult that failed (reserved for future use;
            first iteration uses ctx.generation.candidates[0] directly).
        pipeline_deadline:
            UTC datetime after which no new iteration should start.

        Returns
        -------
        RepairResult
            terminal == "L2_CONVERGED" if a passing candidate was found,
            terminal == "L2_STOPPED" otherwise.

        Raises
        ------
        asyncio.CancelledError
            Propagated immediately; never swallowed.
        """
        budget = self._budget
        iteration = 0
        # Expose live iteration accounting for the operator status line.
        # ``max_iterations_live`` is non-zero iff the run loop is active;
        # the status line uses that as the ``is_running`` signal.
        self._current_iteration = 0
        self._max_iterations_live = budget.max_iterations
        repair_context = None
        seen_pairs: Set[Tuple[str, str]] = set()
        class_retry_counts: Dict[str, int] = {}
        no_progress_streak = 0
        prev_failing_count: Optional[int] = None
        prev_failure_class: Optional[str] = None
        total_validation_runs = 0
        # Slice 5A — track consecutive provider iter timeouts so a wedged
        # provider chain hard-stops only after N back-to-back timeouts
        # (default 2) instead of starving the whole timebox on iter 1.
        consecutive_provider_timeouts = 0
        t_start = time.monotonic()
        records: list = []
        model_id: str = getattr(ctx.generation, "model_id", "")
        provider_name: str = getattr(ctx.generation, "provider_name", "")

        def _stopped(reason: str) -> RepairResult:
            rec = RepairIterationRecord(
                op_id=ctx.op_id,
                iteration=iteration,
                repair_state=L2State.L2_STOPPED.value,
                outcome="stopped",
                stop_reason=reason,
            )
            self._emit_record(ctx.op_id, rec)
            # Note: `rec` is NOT appended to `records` because _stopped() always
            # returns immediately. The sentinel is included via `records + [rec]`
            # so that RepairResult.iterations captures it without mutating `records`.
            return RepairResult(
                terminal="L2_STOPPED",
                candidate=None,
                stop_reason=reason,
                summary={"iterations": iteration, "total_validation_runs": total_validation_runs},
                iterations=tuple(records + [rec]),
            )

        while True:
            # ----------------------------------------------------------------
            # Kill conditions (checked BEFORE every iteration)
            # ----------------------------------------------------------------
            now = datetime.now(timezone.utc)
            elapsed = time.monotonic() - t_start
            remaining_s = (pipeline_deadline - now).total_seconds()

            if remaining_s < budget.min_deadline_remaining_s:
                return _stopped("deadline_budget_exhausted")
            if elapsed > budget.timebox_s:
                return _stopped("timebox_exhausted")
            if iteration >= budget.max_iterations:
                return _stopped("max_iterations_exhausted")
            if total_validation_runs >= budget.max_total_validation_runs:
                return _stopped("max_validation_runs_exhausted")

            iteration += 1
            self._current_iteration = iteration  # live counter for status line
            _logger.info(
                "\U0001f527 [L2 Repair] Iteration %d/%d starting (%.0fs elapsed, %.0fs remaining)",
                iteration, budget.max_iterations, elapsed, remaining_s,
            )

            # ----------------------------------------------------------------
            # GENERATE — composed via _generate_repair_candidate (Phase A
            # extraction). The primitive is single-source: same call path
            # used by the production BranchGenerator for tree-search branches
            # (Phase C) so cross-branch and LINEAR generations stay byte-
            # equivalent in their provider invocation.
            # ----------------------------------------------------------------
            if repair_context is not None:
                gen_outcome = await self._generate_repair_candidate(
                    ctx, pipeline_deadline,
                    repair_context=repair_context,
                    hypothesis_seed=None,  # LINEAR FSM does not seed
                )
                if gen_outcome.candidate is None:
                    # ──────────────────────────────────────────────────
                    # Slice 5A — graceful continue on provider iter
                    # timeout. The pre-5A behavior hard-stopped the
                    # engine on ANY generate_error stop_reason; the
                    # bt-2026-05-25-095834 cascade proved that a single
                    # provider timeout (Claude stream cap) terminated
                    # the entire L2 loop and chained into the orchestrator
                    # ForegroundCooldown. Now: if the stop_reason was
                    # specifically a provider iter timeout (the only
                    # source of `provider_iter_timeout:` stop_reason
                    # under Slice 5A), CONTINUE to next iter until N
                    # consecutive timeouts (max_consecutive_provider_
                    # timeouts, default 2). All other generate_error
                    # shapes preserve byte-equivalent hard-stop behavior.
                    # ──────────────────────────────────────────────────
                    _is_provider_timeout = (
                        gen_outcome.stop_reason is not None
                        and gen_outcome.stop_reason.startswith(
                            "provider_iter_timeout:",
                        )
                    )
                    if _is_provider_timeout:
                        consecutive_provider_timeouts += 1
                        if (
                            consecutive_provider_timeouts
                            >= budget.max_consecutive_provider_timeouts
                        ):
                            _logger.warning(
                                "[L2 Repair] hard-stop: %d consecutive "
                                "provider iter timeouts >= cap %d",
                                consecutive_provider_timeouts,
                                budget.max_consecutive_provider_timeouts,
                            )
                            return _stopped(
                                "consecutive_provider_timeouts_exhausted:"
                                f"{consecutive_provider_timeouts}",
                            )
                        # Soft-skip this iter; the next loop entry will
                        # re-check kill conditions (remaining_s, timebox,
                        # etc.) before retrying GENERATE.
                        continue
                    return _stopped(
                        gen_outcome.stop_reason
                        or "generate_error:unknown",
                    )
                # Reset the counter on any successful provider call.
                consecutive_provider_timeouts = 0
                current_candidate = gen_outcome.candidate
                # Preserve byte-equivalent getattr-with-fallback semantic
                # — None (sentinel) means "provider response lacked the
                # attribute"; keep previous value. Non-None values
                # (including empty string) overwrite, matching the
                # pre-Phase-A getattr behavior exactly.
                if gen_outcome.model_id is not None:
                    model_id = gen_outcome.model_id
                if gen_outcome.provider_name is not None:
                    provider_name = gen_outcome.provider_name
            else:
                # First iteration: use candidate from failed L1 generation
                current_candidate = dict(ctx.generation.candidates[0])

            # ----------------------------------------------------------------
            # Candidate shape detection (Bug B fix)
            # ----------------------------------------------------------------
            # Providers are ``force_full_content=True``, so candidates ship
            # with ``full_content`` rather than ``unified_diff``. The old L2
            # path read ``unified_diff`` unconditionally, got an empty string,
            # synthesized a headers-only patch, and BSD ``patch`` exited 2
            # with "I can't seem to find a patch in there anywhere." in
            # stdout — never surfaced in the error. Branch explicitly:
            #   * real diff (contains @@ hunk header)  → apply_patch
            #   * full_content present                  → apply_full_content
            #   * neither usable                        → fail fast
            diff = current_candidate.get("unified_diff", "") or ""
            full_content = current_candidate.get("full_content", "") or ""
            _has_real_diff = "@@" in diff and ("+" in diff or "-" in diff)
            _has_full_content = bool(full_content)

            if _has_real_diff:
                if _count_diff_lines(diff) > budget.max_diff_lines:
                    return _stopped("diff_expansion_rejected")
                if _count_diff_files(diff) > budget.max_files_changed:
                    return _stopped("diff_files_rejected")
            elif not _has_full_content:
                return _stopped("candidate_unusable:no_diff_or_full_content")

            # ----------------------------------------------------------------
            # RUN in sandbox
            # ----------------------------------------------------------------
            total_validation_runs += 1
            file_path = current_candidate.get("file_path", "")
            sandbox_content = ""
            _patch_failed = False
            try:
                async with self._sandbox_factory(
                    self._repo_root, budget.per_iteration_test_timeout_s
                ) as sb:
                    try:
                        if _has_real_diff:
                            await sb.apply_patch(diff, file_path)
                        else:
                            # full_content path: write the candidate verbatim.
                            # NOTE: L2 is single-file; ``files: [...]`` multi-
                            # file candidates are not yet handled inside the
                            # sandbox apply path.
                            await sb.apply_full_content(full_content, file_path)
                    except RuntimeError as apply_exc:
                        # Apply failure — the diff is malformed, doesn't match
                        # the file, or full_content write failed. Candidate
                        # quality issue, not infra. Fail this iteration so L2
                        # can retry with a new candidate.
                        _logger.info(
                            "[L2 Repair] Iteration %d: apply failed (%s): %s",
                            iteration,
                            "diff" if _has_real_diff else "full_content",
                            apply_exc,
                        )
                        _patch_failed = True
                    if not _patch_failed:
                        target = sb.sandbox_root / file_path if file_path else None
                        if target is not None and hasattr(target, "exists") and target.exists():
                            try:
                                sandbox_content = target.read_text(encoding="utf-8", errors="replace")
                            except Exception:
                                sandbox_content = ""
                    # Scope pytest to the single target file when we know it.
                    # The previous empty-target path forced pytest to discover
                    # and run the ENTIRE repo test suite on every L2 iteration,
                    # which consumed ~90-100s of the 120s timebox and left L2
                    # with only 1 usable iteration per op. For the common case
                    # where ``file_path`` IS the failing test module (test_failure
                    # source) scoping to it runs in ~1s instead.
                    #
                    # Known limitation: for ops whose target is a source file
                    # (e.g. fix ``src/foo.py`` from a failure in
                    # ``tests/test_foo.py``), scoping to ``file_path`` alone
                    # misses the real failing test. The right fix is to thread
                    # an explicit test path / node id through via ``ctx`` or
                    # ``best_validation`` metadata — tracked as a follow-up.
                    if _patch_failed:
                        svr = None
                    else:
                        test_targets: Tuple[str, ...] = (file_path,) if file_path else ()
                        svr = await sb.run_tests(
                            test_targets, budget.per_iteration_test_timeout_s
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _stopped(f"sandbox_infra_error:{type(exc).__name__}")

            # Patch application failure → treat as failed test (try next iteration)
            if _patch_failed or svr is None:
                from backend.core.ouroboros.governance.repair_sandbox import SandboxValidationResult
                svr = SandboxValidationResult(
                    passed=False,
                    stdout="patch application failed",
                    stderr="",
                    returncode=-1,
                    duration_s=0.0,
                )

            # ----------------------------------------------------------------
            # CONVERGED?
            # ----------------------------------------------------------------
            _test_status = "\u2705 PASSED" if svr.passed else f"\u274c FAILED ({getattr(svr, 'failure_class', 'unknown')})"
            _logger.info(
                "\U0001f527 [L2 Repair] Iteration %d/%d tests: %s",
                iteration, budget.max_iterations, _test_status,
            )
            # For telemetry, record *content* line count on the full_content
            # path so dashboards don't read diff_lines=0 as "nothing changed".
            _metric_lines = (
                _count_diff_lines(diff)
                if _has_real_diff
                else len(full_content.splitlines())
            )
            _metric_files = _count_diff_files(diff) if _has_real_diff else 1

            if svr.passed:
                rec = RepairIterationRecord(
                    op_id=ctx.op_id,
                    iteration=iteration,
                    repair_state=L2State.L2_CONVERGED.value,
                    outcome="converged",
                    diff_lines=_metric_lines,
                    files_changed=_metric_files,
                    validation_duration_s=svr.duration_s,
                    model_id=model_id,
                    provider_name=provider_name,
                )
                self._emit_record(ctx.op_id, rec)
                records.append(rec)
                _logger.info(
                    "\U0001f527 [L2 Repair] \u2705 CONVERGED after %d iteration(s)! All tests pass.",
                    iteration,
                )
                return RepairResult(
                    terminal="L2_CONVERGED",
                    candidate=current_candidate,
                    stop_reason=None,
                    summary={
                        "iterations": iteration,
                        "total_validation_runs": total_validation_runs,
                    },
                    iterations=tuple(records),
                )

            # ----------------------------------------------------------------
            # CLASSIFY FAILURE
            # ----------------------------------------------------------------
            classification = self._classifier.classify(svr)
            # ── Slice 4A — L2-local hard-stop subtype narrowing ──
            # Closes the bt-2026-05-25-091657 L2-after-1-iter trap:
            # the failure_classifier flags ``missing_dependency`` and
            # ``interpreter_mismatch`` as non_retryable for ALL
            # consumers — sensible for general callers (the operator's
            # environment IS broken, no LLM can pip-install). But in
            # L2 repair context the model just edited imports/code in
            # the worktree, so ``ModuleNotFoundError`` is almost
            # always a CODE issue the next iteration can fix. The
            # global classifier semantic is preserved (other consumers
            # still see is_non_retryable=True); L2 narrows its OWN
            # hard-stop set to subtypes that no patch could resolve:
            # ``permission_denied`` and ``port_conflict`` (truly
            # environmental — wrong umask, OS-level binding, etc.).
            # ``missing_dependency`` and ``interpreter_mismatch`` fall
            # through to the normal per-class retry path so L2 uses
            # its iteration budget to fight through them.
            _L2_HARD_STOP_ENV_SUBTYPES = frozenset({
                "permission_denied", "port_conflict",
            })
            if (
                classification.is_non_retryable
                and classification.env_subtype in _L2_HARD_STOP_ENV_SUBTYPES
            ):
                return _stopped(f"non_retryable_env:{classification.env_subtype}")

            fail_class = classification.failure_class.value
            fail_sig = classification.failure_signature_hash
            # Sign the applied content — diff when present, full_content
            # otherwise — so oscillation detection can tell distinct
            # full_content candidates apart (empty-string patch_sig would
            # collapse every full_content iter onto the same hash).
            patch_sig = _patch_sig(diff if _has_real_diff else full_content)

            # ----------------------------------------------------------------
            # EVALUATE PROGRESS
            # ----------------------------------------------------------------
            # Two of three progress conditions from the design doc are checked:
            #   1. Fewer failing tests than previous iteration.
            #   2. Failure severity improved (syntax/env → test).
            # Condition 3 (sig-hash set narrowing + diff_lines decrease) is
            # deferred to v1.1 per the implementation plan.
            current_failing_count = len(classification.failing_test_ids)
            is_progress = (
                prev_failing_count is None
                or current_failing_count < prev_failing_count
                or (
                    fail_class == "test"
                    and prev_failure_class is not None
                    and prev_failure_class in ("syntax", "env")
                )
            )
            prev_failing_count = current_failing_count
            prev_failure_class = fail_class

            # ----------------------------------------------------------------
            # OSCILLATION check
            # ----------------------------------------------------------------
            pair = (fail_sig, patch_sig)
            if pair in seen_pairs:
                return _stopped("oscillation_detected")
            seen_pairs.add(pair)

            # ----------------------------------------------------------------
            # NO-PROGRESS streak
            # ----------------------------------------------------------------
            if is_progress:
                no_progress_streak = 0
            else:
                no_progress_streak += 1
                if no_progress_streak >= budget.no_progress_streak_kill:
                    return _stopped("no_progress_streak")

            # ----------------------------------------------------------------
            # PER-CLASS retry cap
            # ----------------------------------------------------------------
            class_retry_counts[fail_class] = class_retry_counts.get(fail_class, 0) + 1
            if class_retry_counts[fail_class] > budget.max_class_retries.get(fail_class, 1):
                return _stopped(f"class_retries_exhausted:{fail_class}")

            # ----------------------------------------------------------------
            # BUILD REPAIR PROMPT (set repair_context for next iteration)
            # ----------------------------------------------------------------
            from backend.core.ouroboros.governance.op_context import RepairContext
            # Repair Context Bridge (Slice 2): graph-derived dependency-cone steer
            # for the NEXT iteration's GENERATE. Gated + fail-soft → None when off,
            # leaving the repair prompt byte-identical to pre-bridge behavior.
            _dependency_cone = await self._build_dependency_cone(
                ctx, file_path, classification.failing_test_ids,
            )
            repair_context = RepairContext(
                iteration=iteration,
                max_iterations=budget.max_iterations,
                failure_class=fail_class,
                failure_signature_hash=fail_sig,
                failing_tests=classification.failing_test_ids,
                failure_summary=(svr.stdout + svr.stderr)[:300],
                current_candidate_content=sandbox_content,
                current_candidate_file_path=file_path,
                dependency_cone=_dependency_cone,
            )

            outcome = "progress" if is_progress else "no_progress"
            rec = RepairIterationRecord(
                op_id=ctx.op_id,
                iteration=iteration,
                repair_state=L2State.L2_BUILD_REPAIR_PROMPT.value,
                failure_class=fail_class,
                failure_signature_hash=fail_sig,
                patch_signature_hash=patch_sig,
                diff_lines=_metric_lines,
                files_changed=_metric_files,
                validation_duration_s=svr.duration_s,
                outcome=outcome,
                model_id=model_id,
                provider_name=provider_name,
            )
            self._emit_record(ctx.op_id, rec)
            records.append(rec)

    async def _build_dependency_cone(
        self,
        ctx: Any,
        file_path: str,
        failing_tests: Tuple[str, ...],
    ) -> Optional[str]:
        """Repair Context Bridge (Slice 2) — build + render the graph dependency cone.

        Gated by ``JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED`` (the bridge's ``build`` self-gates and
        returns ``None`` when off). Lazily instantiates the bridge, sources the fault coordinates
        adaptively (Slice 1 ``fault_node_keys`` from ``ctx.intake_evidence_json`` → file → tests),
        and offloads the lazy-graph cone build to a worker thread. Fail-soft: any error → ``None``,
        leaving the repair prompt byte-identical to pre-bridge behavior."""
        try:
            if self._context_bridge is None:
                from backend.core.ouroboros.governance.repair_context_bridge import (
                    RepairContextBridge,
                )
                self._context_bridge = RepairContextBridge()
            evidence_json = getattr(ctx, "intake_evidence_json", "") or ""
            cone = await self._context_bridge.build(
                evidence_json=evidence_json,
                target_file=file_path,
                failing_tests=tuple(failing_tests or ()),
            )
            if cone is None:
                return None
            clause = self._context_bridge.render_clause(cone)
            return clause or None
        except Exception as exc:  # noqa: BLE001 — cone is advisory; never break L2
            _logger.debug("[RepairBridge] dependency cone unavailable (non-fatal): %s", exc)
            return None

    async def _generate_repair_candidate(
        self,
        ctx: Any,
        pipeline_deadline: datetime,
        *,
        repair_context: Any,
        hypothesis_seed: Optional[str] = None,
    ) -> CandidateGenerationResult:
        """Generate a single repair candidate via the prime provider.

        Phase A extraction (Treefinement Production Wiring v3.4) of the
        inline GENERATE block from :meth:`_run_inner`. Single-source
        primitive composed by:

          * The legacy LINEAR FSM (``_run_inner``) — passes
            ``hypothesis_seed=None`` preserving byte-equivalent
            pre-Phase-A behavior.
          * The Phase C ``ProductionBranchGenerator`` — passes a
            ``hypothesis_seed`` carrying the parent branch's
            ``fix_hypothesis`` so layer-N+1 GENERATE knows which
            strategy survived. This is the substrate hook for
            cross-branch context threading; the actual prompt
            enrichment lives in the generator (which composes
            ``maybe_inject_sibling_outcomes`` from Phase 3).

        Contract
        --------
        * NEVER raises into callers EXCEPT ``asyncio.CancelledError``
          (which propagates per the orchestrator-handled POSTMORTEM
          contract — same as ``_run_inner`` discipline).
        * Provider exceptions quarantine to ``CandidateGenerationResult
          (candidate=None, stop_reason="generate_error:<TypeName>")``.
        * Empty-candidates response quarantines to ``stop_reason=
          "empty_candidates"`` with provider attribution preserved.
        * Returns sentinel ``model_id=None`` / ``provider_name=None``
          when the provider response lacks those attributes — callers
          implement getattr-with-fallback by checking ``is not None``
          before overwriting (matches the original ``getattr(gen_result,
          "model_id", model_id)`` semantic).

        Parameters
        ----------
        ctx : Any
            OperationContext from the orchestrator.
        pipeline_deadline : datetime
            UTC deadline; passed through to provider.
        repair_context : Any
            Required — the caller's prior-failure context. The
            first-iteration ``ctx.generation.candidates[0]`` reuse
            path stays inline in ``_run_inner`` and does NOT call
            this primitive (no provider invocation needed).
        hypothesis_seed : str, optional
            Phase C composition hook. Phase A passes ``None``;
            ``ProductionBranchGenerator`` will pass the parent
            branch's ``fix_hypothesis``. Reserved for future provider-
            shape extension — the current ``self._prime.generate``
            signature accepts only ``ctx`` + ``pipeline_deadline`` +
            ``repair_context``, so this parameter is captured for
            telemetry-only at the substrate level until a provider
            shape change exposes seed threading natively.
        """
        # The hypothesis_seed parameter is the Phase C composition hook
        # — captured here as a deliberate Phase A no-op so the call-site
        # contract is stable. Phase C will thread it through provider
        # extensions OR via the prompt-injection layer; either path
        # composes the same _generate_repair_candidate signature.
        del hypothesis_seed  # Phase A: explicitly unused; Phase C owns

        # ──────────────────────────────────────────────────────────────
        # Slice 5A — L2 provider isolation (bt-2026-05-25-095834 root)
        #
        # The naked ``await self._prime.generate(...)`` saw the FULL
        # pipeline_deadline (e.g. 118s for Claude streaming) and any
        # single iter could exhaust the entire L2 timebox. The cascade
        # from bt-2026-05-25-095834: L2 iter 1 timeout → iter 2 generate
        # eats remaining budget → engine bails → orchestrator
        # ForegroundCooldown → 16 ops piled up cancelled.
        #
        # Fix: wrap with asyncio.wait_for bounded by the L2-local
        # per_iter_provider_timeout_s (default 45s). The deadline arg
        # passed to provider stays unchanged so server-side cap still
        # honors pipeline_deadline as upper bound; the wait_for is a
        # CLIENT-side narrower bound. Effective bound is the smaller of
        # (per_iter_provider_timeout_s, remaining_pipeline_seconds) so
        # the deadline contract is never violated.
        #
        # On asyncio.TimeoutError we emit a structured stop_reason
        # ("provider_iter_timeout:<s>") which the L2 loop classifies
        # as a SOFT iter failure (continues to next iter via the new
        # _consecutive_provider_timeouts counter) instead of the
        # pre-Slice-5A behavior where any generate_error hard-stopped
        # the engine. Stops only after N consecutive timeouts (budget
        # field max_consecutive_provider_timeouts, default 2).
        # ──────────────────────────────────────────────────────────────
        _per_iter_bound = self._budget.per_iter_provider_timeout_s
        _now = datetime.now(timezone.utc)
        _remaining_pipeline_s = (pipeline_deadline - _now).total_seconds()
        # Honor pipeline_deadline as hard upper bound — never wait_for
        # longer than the operation's overall remaining budget.
        _effective_timeout_s = max(
            1.0, min(_per_iter_bound, _remaining_pipeline_s),
        )
        try:
            gen_result = await asyncio.wait_for(
                self._prime.generate(
                    ctx, pipeline_deadline, repair_context=repair_context,
                ),
                timeout=_effective_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            _logger.warning(
                "[L2 Repair] provider iter timeout: effective=%.1fs "
                "per_iter_bound=%.1fs remaining_pipeline=%.1fs — "
                "continuing to next iter (Slice 5A graceful continue)",
                _effective_timeout_s, _per_iter_bound, _remaining_pipeline_s,
            )
            return CandidateGenerationResult(
                candidate=None,
                model_id=None,
                provider_name=None,
                stop_reason=f"provider_iter_timeout:{_effective_timeout_s:.1f}s",
            )
        except Exception as exc:  # noqa: BLE001 — Protocol contract
            # ──────────────────────────────────────────────────────────
            # Slice 7 — total observability for L2 generation failures
            # (bt-2026-05-25-203830 root: Slice 6.1 proved both L2
            # dispatches threw IDENTICAL `generate_error:TypeError` on
            # iter 2's _prime.generate call. The exception class was
            # logged but the actual TypeError message, file, line, and
            # stack frames were swallowed silently by this handler —
            # operators had no way to see WHAT was actually broken in
            # the provider chain. Manifesto §8 violation.)
            #
            # Fix: log full traceback at ERROR level BEFORE returning
            # the quarantined CandidateGenerationResult. ``logger.
            # exception`` captures exc_info automatically; we include
            # the op_id (when available on ctx) plus the exception
            # CLASS + MESSAGE in the structured message so grep can
            # attribute the failure to a specific op.
            #
            # The contract is preserved verbatim: still returns
            # CandidateGenerationResult(candidate=None,
            # stop_reason=f"generate_error:{TypeName}"). Slice 5A's
            # provider_iter_timeout path stays intact. Slice 6/6.1's
            # l2_retry classification still consumes the stop_reason
            # identically. Pure diagnostic addition — zero behavior
            # change to the FSM.
            # ──────────────────────────────────────────────────────────
            _op_id_for_log = getattr(ctx, "op_id", "<unknown>")
            _logger.error(
                "[L2 Repair] _generate_repair_candidate raised %s: %s "
                "(op=%s) — quarantining as generate_error stop_reason; "
                "full traceback follows",
                type(exc).__name__,
                str(exc) or "(no message)",
                _op_id_for_log,
                exc_info=True,
            )
            return CandidateGenerationResult(
                candidate=None,
                model_id=None,
                provider_name=None,
                stop_reason=f"generate_error:{type(exc).__name__}",
            )
        if not gen_result.candidates:
            return CandidateGenerationResult(
                candidate=None,
                model_id=None,
                provider_name=None,
                stop_reason="empty_candidates",
            )
        # Use sentinel ``None`` when attribute missing so callers can
        # distinguish "no value supplied" from "explicit empty string".
        # Matches the byte-equivalent pre-Phase-A getattr behavior.
        return CandidateGenerationResult(
            candidate=dict(gen_result.candidates[0]),
            model_id=getattr(gen_result, "model_id", None),
            provider_name=getattr(gen_result, "provider_name", None),
            stop_reason=None,
        )

    def _emit_record(self, op_id: str, record: RepairIterationRecord) -> None:
        """Append a RepairIterationRecord to the ledger (if wired).

        ``OperationLedger.append`` is async, so we schedule it as a
        fire-and-forget task on the running event loop.  Called from both
        sync inner functions and the async ``run()`` body.
        """
        if self._ledger is None:
            return
        try:
            import asyncio as _asyncio
            from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
            entry = LedgerEntry(
                op_id=op_id,
                state=OperationState.SANDBOXING,
                data={"kind": "repair.iter.v1", **dataclasses.asdict(record)},
                entry_id=f"{op_id}:l2:iter:{record.iteration}",
            )
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(self._ledger.append(entry))
            except RuntimeError:
                # No running loop — silently drop (non-critical telemetry)
                pass
        except Exception:
            _logger.debug("repair_engine: failed to emit ledger record", exc_info=True)


# ---------------------------------------------------------------------------
# Lazy import helper (avoids circular import at module load time)
# ---------------------------------------------------------------------------


def _lazy_classifier():
    """Import and return a FailureClassifier instance."""
    from backend.core.ouroboros.governance.failure_classifier import FailureClassifier
    return FailureClassifier()
