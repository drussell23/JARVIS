"""
Governed Pipeline Orchestrator
===============================

Central coordinator for the governed self-programming pipeline.  Ties
together the risk engine, candidate generator, approval provider, change
engine, and operation ledger into a single deterministic pipeline:

.. code-block:: text

    CLASSIFY -> ROUTE -> [CONTEXT_EXPANSION] -> [PLAN] -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE

The orchestrator owns **no domain logic** -- only phase transitions and
error handling.  Every code path ends in a terminal phase (COMPLETE,
CANCELLED, EXPIRED, or POSTMORTEM).

Key guarantees:
- All unhandled exceptions are caught and transition to POSTMORTEM
- Retries are bounded by ``OrchestratorConfig`` limits
- BLOCKED operations are short-circuited at CLASSIFY
- APPROVAL_REQUIRED operations pause at APPROVE and wait for human decision
- Ledger entries are recorded at every significant lifecycle event
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import os
import tempfile
import time
import dataclasses
from dataclasses import asdict as _dc_asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

from backend.core.ouroboros.governance.ascii_strict_gate import (
    AsciiStrictGate,
    build_retry_feedback as _ascii_gate_retry_feedback,
)
from backend.core.ouroboros.governance.test_runner import BlockedPathError
from backend.core.ouroboros.governance.context_expander import ContextExpander
from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangePhase,
    ChangeRequest,
    ChangeResult,
)
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
from backend.core.ouroboros.governance.learning_bridge import OperationOutcome
from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
    OpCostCapExceeded,
)
from backend.core.ouroboros.governance.forward_progress import (
    ForwardProgressConfig,
    ForwardProgressDetector,
    candidate_content_hash,
)
from backend.core.ouroboros.governance.productivity_detector import (
    ProductivityDetector,
    ProductivityDetectorConfig,
    productivity_content_hash,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskClassification,
    RiskTier,
)
from backend.core.ouroboros.governance.policy_engine import PolicyEngine, PolicyDecision
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy
from backend.core.ouroboros.governance.saga.cross_repo_verifier import CrossRepoVerifier
from backend.core.ouroboros.governance.saga.saga_types import RepoPatch, SagaTerminalState
# patch_benchmarker is intentionally NOT imported at module level — see
# `_run_benchmark` for the deferred import. This makes `patch_benchmarker`
# safely hot-reloadable via ModuleHotReloader: a module-level
# `from X import Y` would capture a stale class reference at orchestrator
# import time and never re-bind on reload.
from backend.core.ouroboros.integration import PerformanceRecord, TaskDifficulty

logger = logging.getLogger("Ouroboros.Orchestrator")

# Module-level buffer for LearningConsolidator periodic consolidation.
# Outcomes accumulate here; once the threshold is reached, consolidate()
# is called to generate new domain-level rules.
_CONSOLIDATION_BUFFER: list = []
_CONSOLIDATION_THRESHOLD: int = 10


# ---------------------------------------------------------------------------
# OrchestratorConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorConfig:
    """Frozen configuration for the governed pipeline orchestrator.

    Parameters
    ----------
    project_root:
        Root directory of the project being modified (jarvis repo).
    repo_registry:
        Optional multi-repo registry. When set, cross-repo saga applies
        resolve each repo's local_path from the registry instead of using
        project_root for all repos. Defaults to None (single-repo mode).
    generation_timeout_s:
        Maximum seconds for candidate generation (per attempt).
    validation_timeout_s:
        Maximum seconds for candidate validation (per attempt).
    approval_timeout_s:
        Maximum seconds to wait for human approval.
    max_generate_retries:
        Number of additional generation attempts after the first failure.
    max_validate_retries:
        Number of additional validation attempts after the first failure.
    """

    project_root: Path
    repo_registry: Optional["RepoRegistry"] = None  # Forward ref avoids circular import; resolved at type-check time
    generation_timeout_s: float = 180.0
    validation_timeout_s: float = 60.0
    approval_timeout_s: float = 600.0
    max_generate_retries: int = 1
    max_validate_retries: int = 2
    context_expansion_enabled: bool = True
    context_expansion_timeout_s: float = 30.0

    # Saga message bus (passive observability — created by GLS at startup)
    message_bus: Optional[Any] = None

    # Benchmarking
    benchmark_enabled: bool = True
    benchmark_timeout_s: float = 60.0

    # Model attribution
    model_attribution_enabled: bool = True
    model_attribution_lookback_n: int = 20
    model_attribution_min_sample_size: int = 3

    # Curriculum
    curriculum_enabled: bool = True
    curriculum_publish_interval_s: float = 3600.0
    curriculum_window_n: int = 50
    curriculum_top_k: int = 5
    curriculum_impact_weights: Dict[str, float] = field(default_factory=dict)

    # Reactor event polling
    reactor_event_poll_interval_s: float = 30.0

    # L2 self-repair engine (disabled by default)
    # Set by GovernedLoopService._build_components() when JARVIS_L2_ENABLED=true.
    repair_engine: Optional[Any] = None
    execution_graph_scheduler: Optional[Any] = None

    # Shadow harness — optional; set by GovernedLoopService when
    # JARVIS_SHADOW_HARNESS_ENABLED=true in .env
    shadow_harness: Optional[Any] = None

    def resolve_repo_roots(
        self,
        repo_scope: Tuple[str, ...],
        op_id: str,
    ) -> Dict[str, Path]:
        """Resolve per-repo filesystem roots from registry; fallback to project_root.

        Parameters
        ----------
        repo_scope:
            Tuple of repo names from OperationContext.
        op_id:
            Operation ID for structured warning on missing registry keys.

        Returns
        -------
        Dict mapping repo name -> absolute Path.
        Missing keys fall back to project_root with a warning (never raise).
        """
        roots: Dict[str, Path] = {}
        for repo in repo_scope:
            if self.repo_registry is not None:
                try:
                    roots[repo] = Path(self.repo_registry.get(repo).local_path)
                except (KeyError, AttributeError, TypeError):
                    # repo_registry may be a duck-typed substitute; catch all lookup failures
                    logger.warning(
                        "[OrchestratorConfig] repo=%s not in registry for op_id=%s; "
                        "falling back to project_root=%s",
                        repo, op_id, self.project_root,
                    )
                    roots[repo] = self.project_root
            else:
                roots[repo] = self.project_root
        return roots


# ---------------------------------------------------------------------------
# GovernedOrchestrator
# ---------------------------------------------------------------------------


class GovernedOrchestrator:
    """Central coordinator for the governed self-programming pipeline.

    Delegates to existing governance components (risk_engine, change_engine,
    ledger, canary via can_write).  Owns NO domain logic -- only phase
    transitions and error handling.

    Parameters
    ----------
    stack:
        GovernanceStack providing risk_engine, ledger, comm, change_engine,
        and the can_write() gate.
    generator:
        CandidateGenerator for code generation (has generate(context, deadline)).
    approval_provider:
        Optional ApprovalProvider for human-in-the-loop gate (has request(),
        await_decision()).
    config:
        Orchestrator configuration.
    """

    def __init__(
        self,
        stack: Any,
        generator: Any,
        approval_provider: Any,
        config: OrchestratorConfig,
        validation_runner: Any = None,  # LanguageRouter | duck-typed for testing
    ) -> None:
        self._stack = stack
        self._generator = generator
        self._approval_provider = approval_provider
        self._config = config
        self._validation_runner = validation_runner
        self._oracle_update_lock: asyncio.Lock = asyncio.Lock()
        self._reasoning_bridge: Optional[Any] = None  # set via set_reasoning_bridge()
        self._infra_applicator: Optional[Any] = None  # set via set_infra_applicator()
        self._reasoning_narrator: Optional[Any] = None  # set via set_reasoning_narrator()
        self._dialogue_store: Optional[Any] = None  # set via set_dialogue_store()
        self._pre_action_narrator: Optional[Any] = None  # set via set_pre_action_narrator()
        self._exploration_fleet: Optional[Any] = None  # set via set_exploration_fleet()
        self._critique_engine: Optional[Any] = None  # set via set_critique_engine()

        # ── Per-op cost governor ──
        # Enforces a dynamic cumulative cost ceiling per op, derived from
        # route + complexity. Prevents cost-runaway cascades (e.g. DW→Claude
        # fallback + 3 retries + 5 L2 iterations collectively spending >$2
        # while each individual call stays under its own per-provider cap).
        # Cap formula and factor table are fully env-var driven — see
        # cost_governor.py::CostGovernorConfig. Disable via
        # ``JARVIS_OP_COST_GOVERNOR_ENABLED=false``.
        self._cost_governor: CostGovernor = CostGovernor(CostGovernorConfig())

        # ── Forward-progress detector ──
        # Detects when the GENERATE retry loop is stuck producing the same
        # candidate repeatedly (content-hash identity). Trips after
        # ``JARVIS_FORWARD_PROGRESS_MAX_REPEATS`` consecutive identical
        # candidates and aborts the op via the phase-aware terminal picker.
        # Disable via ``JARVIS_FORWARD_PROGRESS_ENABLED=false``.
        self._forward_progress: ForwardProgressDetector = ForwardProgressDetector(
            ForwardProgressConfig(),
        )

        # ── Productivity-ratio detector (EC9) ──
        # Catches the silent-burn failure mode: model produces semantically
        # identical candidates with cosmetic differences (whitespace, import
        # order, docstring tweaks) that slip past EC8's byte-identical hash
        # while burning real money on each retry. EC9 normalizes each
        # candidate (AST dump for Python, canonical JSON, whitespace fallback
        # for everything else) and trips when cost accumulated since the last
        # *semantic* change crosses a USD threshold. Disable via
        # ``JARVIS_EC9_ENABLED=false``.
        self._productivity_detector: ProductivityDetector = ProductivityDetector(
            ProductivityDetectorConfig(),
        )

        # ── Session Intelligence: ephemeral lessons buffer ──
        # Accumulates compact lessons from completed/failed ops within this
        # session.  Injected into subsequent generation prompts so the model
        # avoids repeating mistakes and builds on successes.
        # Thread-safety: safe under asyncio single-threaded event loop.
        # If the orchestrator ever moves to multi-threaded execution,
        # wrap accesses in an asyncio.Lock.
        self._session_lessons: list = []  # List[Tuple[str, str]] — (lesson_type, lesson_text)
        _max = int(os.environ.get("JARVIS_SESSION_LESSONS_MAX", "20"))
        self._session_lessons_max: int = max(5, _max)

        # ── Session intelligence convergence metric ──
        # Tracks success rate before/after first lesson to detect poisoned lessons.
        self._ops_before_lesson: int = 0  # ops completed before first lesson recorded
        self._ops_before_lesson_success: int = 0
        self._ops_after_lesson: int = 0  # ops completed after first lesson recorded
        self._ops_after_lesson_success: int = 0
        self._convergence_check_interval: int = int(
            os.environ.get("JARVIS_LESSON_CONVERGENCE_CHECK_INTERVAL", "10")
        )

        # RSI Convergence Framework — lazy initialization
        self._rsi_score_function = None
        self._rsi_score_history = None
        self._rsi_convergence_tracker = None
        self._rsi_transition_tracker = None
        try:
            from backend.core.ouroboros.governance.composite_score import (
                CompositeScoreFunction, ScoreHistory,
            )
            self._rsi_score_function = CompositeScoreFunction()
            _rsi_dir = Path(os.environ.get(
                "JARVIS_SELF_EVOLUTION_DIR",
                str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
            ))
            self._rsi_score_history = ScoreHistory(persistence_dir=_rsi_dir)
        except Exception:
            logger.debug("RSI: CompositeScoreFunction not available", exc_info=True)

        try:
            from backend.core.ouroboros.governance.convergence_tracker import ConvergenceTracker
            self._rsi_convergence_tracker = ConvergenceTracker()
        except Exception:
            logger.debug("RSI: ConvergenceTracker not available", exc_info=True)

        try:
            from backend.core.ouroboros.governance.transition_tracker import TransitionProbabilityTracker
            self._rsi_transition_tracker = TransitionProbabilityTracker()
        except Exception:
            logger.debug("RSI: TransitionProbabilityTracker not available", exc_info=True)

        # ── Module hot-reloader (Manifesto §6 RSI loop closer) ──
        # When O+V successfully self-modifies a hot-reloadable governance
        # module (verify_gate, patch_benchmarker, semantic_triage,
        # plan_generator, strategic_direction), reload the new code in-process
        # so the very next op picks it up — no process restart required.
        # Disable via JARVIS_HOT_RELOAD_ENABLED=false. Quarantined modules
        # (orchestrator, providers, sensors) still require a restart, which
        # the harness handles via the restart_pending flag.
        self._hot_reloader: Optional[Any] = None
        if os.environ.get("JARVIS_HOT_RELOAD_ENABLED", "true").lower() != "false":
            try:
                from backend.core.ouroboros.governance.module_hot_reloader import (
                    ModuleHotReloader,
                )
                self._hot_reloader = ModuleHotReloader(
                    project_root=self._config.project_root,
                )
                logger.info(
                    "[Orchestrator] ModuleHotReloader armed (%d safe modules)",
                    len(self._hot_reloader.safe_modules),
                )
            except Exception:
                logger.debug(
                    "[Orchestrator] ModuleHotReloader unavailable",
                    exc_info=True,
                )

    def set_reasoning_bridge(self, bridge: Any) -> None:
        """Attach a ReasoningChainBridge for pre-CLASSIFY reasoning."""
        self._reasoning_bridge = bridge

    def set_infra_applicator(self, applicator: Any) -> None:
        """Attach an InfrastructureApplicator for deterministic post-APPLY hooks."""
        self._infra_applicator = applicator

    def set_reasoning_narrator(self, narrator: Any) -> None:
        """Attach a ReasoningNarrator for WHY-not-WHAT explanations."""
        self._reasoning_narrator = narrator

    def set_dialogue_store(self, store: Any) -> None:
        """Attach an OperationDialogueStore for reasoning journal recording."""
        self._dialogue_store = store

    def set_pre_action_narrator(self, narrator: Any) -> None:
        """Attach a PreActionNarrator for real-time WHAT-is-about-to-happen voice."""
        self._pre_action_narrator = narrator

    def set_exploration_fleet(self, fleet: Any) -> None:
        """Attach an ExplorationFleet for parallel codebase exploration."""
        self._exploration_fleet = fleet

    def set_critique_engine(self, engine: Any) -> None:
        """Attach a self-critique engine (Phase 3a).

        The engine runs after successful VERIFY + auto-commit and before
        the COMPLETE transition. Passing ``None`` detaches it. See
        ``self_critique.CritiqueEngine`` for the expected shape.
        """
        self._critique_engine = engine

    def _is_cancel_requested(self, op_id: str) -> bool:
        """Check if REPL /cancel was requested for this operation."""
        _gls = getattr(self._stack, "governed_loop_service", None)
        if _gls is not None and hasattr(_gls, "is_cancel_requested"):
            return _gls.is_cancel_requested(op_id)
        return False

    async def run(self, ctx: OperationContext) -> OperationContext:
        """Execute the full governed pipeline, returning the terminal context.

        Top-level try/except catches ALL unhandled exceptions and transitions
        to POSTMORTEM.  Every code path ends in a terminal phase (COMPLETE,
        CANCELLED, EXPIRED, or POSTMORTEM).

        Parameters
        ----------
        ctx:
            The initial OperationContext in CLASSIFY phase.

        Returns
        -------
        OperationContext
            The terminal context after pipeline completion or failure.
        """
        try:
            try:
                return await self._run_pipeline(ctx)
            except Exception as exc:
                logger.error(
                    "Unhandled exception in pipeline for %s: %s",
                    ctx.op_id,
                    exc,
                    exc_info=True,
                )
                # Try to advance to POSTMORTEM from current phase.
                # If we can't (e.g. already terminal), just return ctx.
                try:
                    ctx = ctx.advance(
                        OperationPhase.POSTMORTEM,
                        terminal_reason_code="unhandled_pipeline_exception",
                    )
                except ValueError:
                    # POSTMORTEM not legal from this phase — fall back to CANCELLED
                    # (legal from all non-terminal phases except VERIFY).
                    try:
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="unhandled_pipeline_exception",
                        )
                    except ValueError:
                        pass  # Already terminal — safe to return as-is
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"error": str(exc), "phase": ctx.phase.name},
                )
                return ctx
        finally:
            # Finalize the cost-governor entry no matter how the op ended.
            # This also logs the full summary (cap, cumulative, per-provider
            # breakdown) at DEBUG for postmortem analysis.
            try:
                _cost_final = self._cost_governor.finish(ctx.op_id)
                if _cost_final is not None:
                    logger.info(
                        "[Orchestrator] Cost summary op=%s phase=%s "
                        "spent=$%.4f / cap=$%.4f (%d calls)",
                        ctx.op_id,
                        ctx.phase.name,
                        _cost_final.get("cumulative_usd", 0.0),
                        _cost_final.get("cap_usd", 0.0),
                        _cost_final.get("call_count", 0),
                    )
            except Exception:
                logger.debug(
                    "[Orchestrator] CostGovernor.finish failed", exc_info=True,
                )
            # Finalize the forward-progress detector entry. Safe to call
            # whether or not the op actually observed anything.
            try:
                self._forward_progress.finish(ctx.op_id)
            except Exception:
                logger.debug(
                    "[Orchestrator] ForwardProgress.finish failed", exc_info=True,
                )
            # Finalize the productivity detector entry. Logs the summary
            # (cost_since_last_change, consecutive_stable, total_cost) at
            # DEBUG for postmortem productivity analysis.
            try:
                _pd_final = self._productivity_detector.finish(ctx.op_id)
                if _pd_final is not None:
                    logger.debug(
                        "[Orchestrator] Productivity summary op=%s "
                        "stable=%d burn=$%.4f total=$%.4f tripped=%s",
                        ctx.op_id,
                        _pd_final.get("consecutive_stable", 0),
                        _pd_final.get("cost_since_last_change_usd", 0.0),
                        _pd_final.get("total_cost_usd", 0.0),
                        _pd_final.get("tripped", False),
                    )
            except Exception:
                logger.debug(
                    "[Orchestrator] ProductivityDetector.finish failed",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Pipeline implementation
    # ------------------------------------------------------------------

    async def _run_pipeline(self, ctx: OperationContext) -> OperationContext:
        """Internal pipeline logic -- phases 1 through 8."""

        # ── Ouroboros Serpent: visual indicator that the pipeline is active ──
        _serpent = None
        try:
            from backend.core.ouroboros.governance.serpent_animation import get_serpent
            _serpent = get_serpent()
            await _serpent.start("CLASSIFY")
        except Exception:
            pass

        # ── JARVIS Tier 2: Emergency Protocol Check ──────────────────────
        # If emergency level is ORANGE or higher, block autonomous operations
        try:
            from backend.core.ouroboros.governance.emergency_protocols import (
                EmergencyProtocolEngine, AlertLevel,
            )
            _emergency = getattr(self._stack, "_emergency_engine", None)
            if _emergency is not None and not _emergency.can_proceed():
                state = _emergency.get_state()
                logger.warning(
                    "[Orchestrator] Emergency level %s — operation blocked (op=%s)",
                    state.level.name, ctx.op_id,
                )
                if _serpent:
                    await _serpent.stop(success=False)
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code=f"emergency_{state.level.name.lower()}",
                )
                return ctx
        except ImportError:
            pass
        except Exception:
            pass

        # ── JARVIS Tier 1: Operation Advisor ────────────────────────────
        # "Sir, I wouldn't recommend that."
        _advisory = None
        try:
            from backend.core.ouroboros.governance.operation_advisor import (
                OperationAdvisor, AdvisoryDecision,
            )
            _advisor = OperationAdvisor(self._config.project_root)
            _advisory = _advisor.advise(ctx.target_files, ctx.description, ctx.op_id)

            if _advisory.decision == AdvisoryDecision.BLOCK:
                logger.warning(
                    "[Orchestrator] Advisor BLOCKED operation: %s (op=%s)",
                    "; ".join(_advisory.reasons), ctx.op_id,
                )
                if _serpent:
                    await _serpent.stop(success=False)
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="advisor_blocked",
                )
                return ctx

            if _advisory.decision != AdvisoryDecision.RECOMMEND:
                # Inject advisory into context for generation awareness
                _adv_prompt = _advisor.format_for_prompt(_advisory)
                if _adv_prompt:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=_existing + "\n\n" + _adv_prompt,
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )

                # Voice the warning
                if _advisory.voice_message and self._reasoning_narrator is not None:
                    try:
                        self._reasoning_narrator.record_classify(
                            ctx.op_id, _advisory.decision.value,
                            _advisory.voice_message,
                        )
                    except Exception:
                        pass

                logger.info(
                    "[Orchestrator] Advisor: %s (risk=%.2f) — %s",
                    _advisory.decision.value, _advisory.risk_score,
                    _advisory.reasons[0] if _advisory.reasons else "no specific reason",
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Advisor failed", exc_info=True)

        # ---- Phase 1: CLASSIFY ----
        profile = self._build_profile(ctx)
        classification = self._stack.risk_engine.classify(profile)
        risk_tier = classification.tier

        # ---- Complexity + Persistence classification (Assimilation Gate) ----
        _complexity_result = None
        try:
            from backend.core.ouroboros.governance.complexity_classifier import (
                OperationComplexityClassifier,
            )
            _classifier = OperationComplexityClassifier(
                topology=getattr(self._stack, "topology", None),
                ledger=getattr(self._stack, "ledger", None),
            )
            _complexity_result = _classifier.classify(
                description=ctx.description,
                target_files=list(ctx.target_files),
            )
            # Stamp complexity on context for downstream routing decisions.
            # task_complexity is a declared field on OperationContext, so
            # object.__setattr__ values survive dataclasses.replace() in
            # advance() and all with_*() methods.
            object.__setattr__(ctx, "task_complexity", _complexity_result.complexity.value)

            logger.info(
                "[Orchestrator] \U0001f4ca Complexity: %s, Persistence: %s, auto_approve=%s, fast_path=%s [%s]",
                _complexity_result.complexity.value,
                _complexity_result.persistence.value,
                _complexity_result.auto_approve_eligible,
                _complexity_result.fast_path_eligible,
                ctx.op_id,
            )
        except Exception:
            logger.debug("[Orchestrator] ComplexityClassifier not available", exc_info=True)

        # ---- Consciousness regression detection (ProphecyEngine + MemoryEngine) ----
        _consciousness_bridge = getattr(self._stack, "consciousness_bridge", None)
        if _consciousness_bridge is None:
            # Check if GLS has the bridge (wired by Zone 6.12)
            _gls = getattr(self._stack, "governed_loop_service", None)
            if _gls is not None:
                _consciousness_bridge = getattr(_gls, "_consciousness_bridge", None)
        if _consciousness_bridge is not None:
            try:
                _regression = await _consciousness_bridge.assess_regression_risk(
                    list(ctx.target_files)
                )
                if _regression and _regression.get("risk_level") in ("high", "critical"):
                    logger.warning(
                        "[Orchestrator] Consciousness regression alert: %s risk for %s — %s [%s]",
                        _regression["risk_level"],
                        ctx.target_files,
                        _regression.get("reasoning", ""),
                        ctx.op_id,
                    )
            except Exception:
                logger.debug("[Orchestrator] Consciousness regression check failed", exc_info=True)

        # ---- Goal Memory injection (cross-session learning via ChromaDB) ----
        _goal_memory_bridge = None
        _gls_for_gmb = getattr(self._stack, "governed_loop_service", None)
        if _gls_for_gmb is not None:
            _goal_memory_bridge = getattr(_gls_for_gmb, "_goal_memory_bridge", None)
        if _goal_memory_bridge is not None:
            try:
                _goal_ctx = await _goal_memory_bridge.get_relevant_context(
                    description=ctx.description,
                    target_files=ctx.target_files,
                )
                if _goal_ctx:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=_existing + "\n\n" + _goal_ctx,
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )
            except Exception:
                logger.debug("[Orchestrator] Goal memory injection failed", exc_info=True)

        # ---- Strategic Direction injection (Manifesto + architecture docs) ----
        _strategic_svc = None
        if _gls_for_gmb is not None:
            _strategic_svc = getattr(_gls_for_gmb, "_strategic_direction", None)
        if _strategic_svc is not None and getattr(_strategic_svc, "is_loaded", False):
            try:
                _strat_prompt = _strategic_svc.format_for_prompt()
                if _strat_prompt:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id="manifesto-v4",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=_strat_prompt + "\n\n" + _existing,
                        strategic_memory_digest=(
                            ctx.strategic_memory_digest
                            or _strategic_svc.digest[:500]
                        ),
                    )
                    logger.debug(
                        "[Orchestrator] Strategic direction injected (%d principles)",
                        len(_strategic_svc.principles),
                    )
            except Exception:
                logger.debug("[Orchestrator] Strategic direction injection failed", exc_info=True)

        # ---- P2.4 + Week 2: Goal-directed context injection ----
        # Append the *most relevant* active user goals to the strategic
        # memory prompt so the generation model aligns its decisions with
        # current priorities. Scoped by target_files + description so a
        # noisy goal tracker doesn't hijack unrelated ops.
        #
        # Increment 3: after prompt injection, compute the full activity
        # entry set (direct matches + descendant credits + optional
        # sibling bumps) and append to the GoalActivityLedger. Every op
        # that reaches CLASSIFY writes at least one row so the session-end
        # drift aggregator sees it as "reached CLASSIFY", even when no
        # goal scored.
        try:
            from backend.core.ouroboros.governance.strategic_direction import (
                GoalActivityLedger,
                GoalTracker,
                get_active_session_id,
            )
            _goal_tracker = GoalTracker(self._config.project_root)
            _goal_prompt = _goal_tracker.format_for_prompt(
                target_files=list(ctx.target_files),
                description=ctx.description or "",
            )
            if _goal_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "goals-v1",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _goal_prompt if _existing else _goal_prompt,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.debug(
                    "[Orchestrator] Goal context injected (%d active / scoped)",
                    len(_goal_tracker.active_goals),
                )

            # Activity ledger append (Increment 3). Ledger-only — does
            # not feed intake priority math. Zero-match ops still get a
            # marker row so the drift denominator counts them.
            _session_id = get_active_session_id() or ""
            if _session_id:
                try:
                    _activity_entries = _goal_tracker.compute_activity_entries(
                        description=ctx.description or "",
                        target_files=list(ctx.target_files),
                    )
                    GoalActivityLedger(self._config.project_root).append(
                        session_id=_session_id,
                        op_id=ctx.op_id,
                        entries=_activity_entries,
                    )
                    logger.debug(
                        "[Orchestrator] GoalActivity ledger: wrote %d entries for op=%s",
                        len(_activity_entries) or 1,  # 1 marker row on zero-match
                        ctx.op_id,
                    )
                except Exception:
                    logger.debug(
                        "[Orchestrator] GoalActivity ledger append failed",
                        exc_info=True,
                    )
        except Exception:
            logger.debug("[Orchestrator] Goal injection skipped", exc_info=True)

        # ---- Task #195: User Preference Memory injection ----
        # Append typed user-preference memories (facts about the user,
        # feedback rules, forbidden paths, style choices) scoped by
        # relevance to the current op. Zero model inference — pure
        # deterministic scoring. Empty when no memory matches the op
        # shape, so silent on fresh repos.
        try:
            from backend.core.ouroboros.governance.user_preference_memory import (
                get_default_store,
            )
            _user_prefs = get_default_store(self._config.project_root)
            _pref_prompt = _user_prefs.format_for_prompt(
                target_files=list(ctx.target_files),
                description=ctx.description,
                risk_tier=str(getattr(ctx, "risk_tier", "") or ""),
            )
            if _pref_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "user-prefs-v1",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=(
                        _existing + "\n\n" + _pref_prompt if _existing else _pref_prompt
                    ),
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.debug(
                    "[Orchestrator] User preferences injected (%d chars)",
                    len(_pref_prompt),
                )
        except Exception:
            logger.debug("[Orchestrator] User preference injection skipped", exc_info=True)

        # ---- Policy engine check (declarative YAML rules) ----
        # Evaluated BEFORE the risk-engine BLOCKED short-circuit so that
        # explicit deny rules in policy files can override the risk engine.
        # Wrapped in hasattr + try/except so the pipeline is never broken
        # by a missing or misconfigured policy_engine attribute.
        if hasattr(self._stack, "policy_engine") and self._stack.policy_engine is not None:
            try:
                _policy_engine: PolicyEngine = self._stack.policy_engine
                for _tf in ctx.target_files:
                    _policy_decision = _policy_engine.classify(tool="edit", target=str(_tf))
                    if _policy_decision is PolicyDecision.BLOCKED:
                        logger.info(
                            "[Orchestrator] PolicyEngine BLOCKED op=%s target=%r",
                            ctx.op_id, _tf,
                        )
                        risk_tier = RiskTier.BLOCKED
                        break
            except Exception:
                logger.warning(
                    "[Orchestrator] PolicyEngine raised during CLASSIFY for op=%s; continuing",
                    ctx.op_id, exc_info=True,
                )

        if risk_tier is RiskTier.BLOCKED:
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                risk_tier=risk_tier,
                terminal_reason_code=classification.reason_code,
            )
            await self._record_ledger(
                ctx,
                OperationState.BLOCKED,
                {
                    "reason_code": classification.reason_code,
                    "risk_tier": risk_tier.name,
                },
            )
            return ctx

        # Announce operation start — VoiceNarrator fires here (INTENT type)
        try:
            await self._stack.comm.emit_intent(
                op_id=ctx.op_id,
                goal=ctx.description,
                target_files=list(ctx.target_files),
                risk_tier=risk_tier.name,
                blast_radius=len(ctx.target_files),
            )
        except Exception:
            logger.debug("emit_intent failed for op=%s", ctx.op_id, exc_info=True)

        # ---- Reasoning chain classification (optional, pre-routing) ----
        reasoning_result = None
        if self._reasoning_bridge and self._reasoning_bridge.is_active:
            try:
                reasoning_result = await self._reasoning_bridge.classify_with_reasoning(
                    command=ctx.description,
                    op_id=ctx.op_id,
                )
            except Exception:
                logger.debug("Reasoning chain bridge error", exc_info=True)

        # P3.1: Emit intent chain heartbeat — full reasoning chain for the
        # SerpentFlow display.  Deterministic: all data already computed.
        try:
            _chain_payload: Dict[str, Any] = {
                "phase": "intent_chain",
                "risk_tier": risk_tier.name,
                "complexity": (
                    _complexity_result.complexity.value
                    if _complexity_result is not None else ""
                ),
                "auto_approve": (
                    _complexity_result.auto_approve_eligible
                    if _complexity_result is not None else False
                ),
                "fast_path": (
                    _complexity_result.fast_path_eligible
                    if _complexity_result is not None else False
                ),
            }
            await self._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="intent_chain", progress_pct=10.0,
                **_chain_payload,
            )
        except Exception:
            pass  # Intent chain visibility is best-effort

        # Advance to ROUTE with risk_tier set (and optional reasoning result)
        if _serpent: _serpent.update_phase("ROUTE")
        ctx = ctx.advance(
            OperationPhase.ROUTE,
            risk_tier=risk_tier,
            reasoning_chain_result=reasoning_result,
        )

        # ── P0 Wiring: Start ReasoningNarrator + OperationDialogue ──────
        if self._reasoning_narrator is not None:
            try:
                self._reasoning_narrator.start_trace(ctx.op_id)
                self._reasoning_narrator.record_classify(
                    ctx.op_id,
                    risk_tier.value if hasattr(risk_tier, "value") else str(risk_tier),
                    f"files={list(ctx.target_files)[:3]}, "
                    f"complexity={getattr(_complexity_result, 'complexity', 'unknown')}",
                )
            except Exception:
                pass

        if self._dialogue_store is not None:
            try:
                from backend.core.ouroboros.governance.entropy_calculator import extract_domain_key
                _dk = extract_domain_key(ctx.target_files, ctx.description)
                self._dialogue_store.start_dialogue(
                    op_id=ctx.op_id,
                    domain_key=_dk,
                    description=ctx.description,
                    target_files=ctx.target_files,
                )
                _dialogue = self._dialogue_store.get_active(ctx.op_id)
                if _dialogue:
                    _dialogue.add_entry(
                        "CLASSIFY",
                        f"Risk={risk_tier}, complexity={getattr(_complexity_result, 'complexity', 'unknown')}",
                    )
            except Exception:
                pass

        # ---- Phase 2: ROUTE ----

        # Telemetry host-binding enforcement for remote routes (split-brain guard)
        _routing = getattr(ctx, "routing", None)
        if _routing is not None and str(getattr(_routing, "name", "")).upper() in ("GCP_PRIME", "REMOTE"):
            try:
                from backend.core.ouroboros.governance.telemetry_contextualizer import (
                    TelemetryContextualizer,
                )
                _tc = TelemetryContextualizer()
                _exec_host = str(getattr(_routing, "endpoint", "local"))
                _tel_host = str(getattr(ctx, "telemetry_host", _exec_host))
                await _tc.assert_host_binding(
                    execution_host=_exec_host,
                    telemetry_host=_tel_host,
                )
            except RuntimeError as _bind_err:
                logger.warning(
                    "[Orchestrator] Telemetry host-binding violation: %s [%s]",
                    _bind_err, ctx.op_id,
                )
            except Exception:
                logger.debug("[Orchestrator] TelemetryContextualizer not available", exc_info=True)

        # ── Urgency-aware provider routing (Manifesto §5 Tier 0) ──
        # Deterministic routing based on signal_urgency + signal_source +
        # task_complexity. Stamps provider_route on context for
        # CandidateGenerator dispatch.
        try:
            from backend.core.ouroboros.governance.urgency_router import (
                UrgencyRouter,
            )
            _urgency_router = UrgencyRouter()
            _provider_route, _route_reason = _urgency_router.classify(ctx)
            object.__setattr__(ctx, "provider_route", _provider_route.value)
            object.__setattr__(ctx, "provider_route_reason", _route_reason)
            logger.info(
                "[Orchestrator] \U0001f6e4\ufe0f  Route: %s (%s) [%s]",
                _provider_route.value, _route_reason, ctx.op_id,
            )
            # Emit route decision to CommProtocol for observability
            if hasattr(self._stack, "comm") and self._stack.comm is not None:
                try:
                    from backend.core.ouroboros.governance.urgency_router import (
                        UrgencyRouter as _UR,
                    )
                    await self._stack.comm.emit_decision(
                        op_id=ctx.op_id,
                        outcome=_provider_route.value,
                        reason_code=f"urgency_route:{_route_reason}",
                        details={
                            "route": _provider_route.value,
                            "route_description": _UR.describe_route(_provider_route),
                            "signal_urgency": getattr(ctx, "signal_urgency", ""),
                            "signal_source": getattr(ctx, "signal_source", ""),
                            "task_complexity": getattr(ctx, "task_complexity", ""),
                            "budget_profile": _UR.route_budget_profile(_provider_route),
                        },
                    )
                except Exception:
                    pass
        except Exception:
            logger.debug("[Orchestrator] UrgencyRouter not available", exc_info=True)

        # ── Start per-op cost governor ──
        # Called here (post-ROUTE) so the cap is derived from the actual
        # stamped route + task_complexity. If either field is empty the
        # governor uses safe "standard/light" defaults. Safe to call even
        # when governor is disabled — returns +inf cap.
        try:
            self._cost_governor.start(
                op_id=ctx.op_id,
                route=getattr(ctx, "provider_route", "") or "",
                complexity=getattr(ctx, "task_complexity", "") or "",
            )
        except Exception:
            logger.debug("[Orchestrator] CostGovernor.start failed", exc_info=True)

        if self._config.context_expansion_enabled:
            # ── PreActionNarrator: voice WHAT before CONTEXT_EXPANSION ──
            if self._pre_action_narrator is not None:
                try:
                    await self._pre_action_narrator.narrate_phase(
                        "CONTEXT_EXPANSION",
                        {"target_file": list(ctx.target_files)[0] if ctx.target_files else "unknown"},
                    )
                except Exception:
                    pass
            if _serpent: _serpent.update_phase("CONTEXT_EXPANSION")
            ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)

            # ---- Phase 2b: CONTEXT_EXPANSION ----
            try:
                expansion_deadline = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=self._config.context_expansion_timeout_s
                )
                from backend.core.ouroboros.governance.skill_registry import SkillRegistry as _SkillRegistry
                _skill_registry = _SkillRegistry(self._config.project_root)
                # DocFetcher: bounded external doc retrieval (P3 — Boundary Principle)
                _doc_fetcher = None
                try:
                    from backend.core.ouroboros.governance.doc_fetcher import DocFetcher
                    _doc_fetcher = DocFetcher()
                except ImportError:
                    pass

                # WebSearchCapability: structured search with epistemic allowlist
                _web_search = None
                try:
                    from backend.core.ouroboros.governance.web_search import WebSearchCapability
                    _ws = WebSearchCapability()
                    if _ws.is_available:
                        _web_search = _ws
                        logger.debug(
                            "[Orchestrator] WebSearchCapability available (backend=%s)",
                            _ws.backend_name,
                        )
                except ImportError:
                    pass

                # VisualCodeComprehension: screenshot-based analysis
                _visual = None
                try:
                    from backend.core.ouroboros.governance.visual_comprehension import (
                        VisualCodeComprehension,
                    )
                    _vc = VisualCodeComprehension()
                    if _vc.is_available:
                        _visual = _vc
                except ImportError:
                    pass

                # CodeExplorationTool: sandboxed hypothesis testing
                _explorer = None
                try:
                    from backend.core.ouroboros.governance.code_exploration import CodeExplorationTool
                    _explorer = CodeExplorationTool(str(self._config.project_root))
                except ImportError:
                    pass

                expander = ContextExpander(
                    generator=self._generator,
                    repo_root=self._config.project_root,
                    oracle=getattr(self._stack, "oracle", None),
                    skill_registry=_skill_registry,
                    doc_fetcher=_doc_fetcher,
                    web_search=_web_search,
                    visual_comprehension=_visual,
                    code_explorer=_explorer,
                    dialogue_store=self._dialogue_store,
                )
                ctx = await asyncio.wait_for(
                    expander.expand(ctx, expansion_deadline),
                    timeout=self._config.context_expansion_timeout_s,
                )

                # ExplorationFleet: parallel codebase exploration across Trinity repos
                if self._exploration_fleet is not None:
                    try:
                        _fleet_report = await asyncio.wait_for(
                            self._exploration_fleet.deploy(
                                goal=ctx.description,
                                max_agents=8,
                            ),
                            timeout=min(30.0, self._config.context_expansion_timeout_s / 2),
                        )
                        if _fleet_report.total_findings > 0:
                            _fleet_text = self._exploration_fleet.format_for_prompt(_fleet_report)
                            ctx = ctx.with_expanded_files(
                                ctx.expanded_files + (f"[Fleet:{_fleet_report.total_findings}]",)
                            )
                            logger.info(
                                "[Orchestrator] ExplorationFleet: %d agents, %d findings in %.1fs",
                                _fleet_report.agents_completed,
                                _fleet_report.total_findings,
                                _fleet_report.duration_s,
                            )
                    except Exception as _fleet_exc:
                        logger.debug("[Orchestrator] ExplorationFleet skipped: %s", _fleet_exc)

                # P2.1: Dependency-aware generation — inject Oracle graph summary
                _oracle_ref = getattr(self._stack, "oracle", None)
                if _oracle_ref is not None and ctx.target_files:
                    try:
                        _dep_summary = self._build_dependency_summary(
                            _oracle_ref, ctx.target_files,
                        )
                        if _dep_summary:
                            ctx = dataclasses.replace(ctx, dependency_summary=_dep_summary)
                            logger.info(
                                "[Orchestrator] Dependency summary injected (%d chars, %d files)",
                                len(_dep_summary), len(ctx.target_files),
                            )
                    except Exception as _dep_exc:
                        logger.debug("[Orchestrator] Dependency summary skipped: %s", _dep_exc)
            except Exception as exc:
                logger.warning(
                    "[Orchestrator] Context expansion failed for op=%s: %s; "
                    "continuing to GENERATE",
                    ctx.op_id, exc,
                )

            ctx = ctx.advance(OperationPhase.PLAN)
        else:
            # Expansion disabled: skip directly from ROUTE to PLAN
            ctx = ctx.advance(OperationPhase.PLAN)

        # ---- Phase 2c: PLAN — model-reasoned implementation planning ----
        # The model reasons about HOW to implement the change before writing
        # code. Planning failures are soft — the pipeline falls through to
        # GENERATE with an empty plan. Trivial ops skip planning entirely.
        if _serpent:
            _serpent.update_phase("PLAN")
        try:
            await self._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="plan", progress_pct=25.0,
            )
        except Exception:
            pass

        _plan_result: Optional[Any] = None
        try:
            from backend.core.ouroboros.governance.plan_generator import (
                PlanGenerator, PLAN_TIMEOUT_S,
            )
            _plan_gen = PlanGenerator(
                generator=self._generator,
                repo_root=self._config.project_root,
            )
            _plan_deadline = datetime.now(tz=timezone.utc) + timedelta(
                seconds=PLAN_TIMEOUT_S,
            )
            _plan_result = await asyncio.wait_for(
                _plan_gen.generate_plan(ctx, _plan_deadline),
                timeout=PLAN_TIMEOUT_S + 5.0,
            )

            if not _plan_result.skipped:
                # Store plan in context for injection into GENERATE prompt
                ctx = dataclasses.replace(
                    ctx,
                    implementation_plan=_plan_result.plan_json,
                    previous_hash=ctx.context_hash,
                )
                # Emit plan result for SerpentFlow rendering
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="plan", progress_pct=28.0,
                        plan_complexity=_plan_result.complexity,
                        plan_changes=len(_plan_result.ordered_changes),
                    )
                except Exception:
                    pass
                logger.info(
                    "[Orchestrator] PLAN complete for op=%s: complexity=%s, "
                    "%d ordered changes, %.1fs",
                    ctx.op_id, _plan_result.complexity,
                    len(_plan_result.ordered_changes),
                    _plan_result.planning_duration_s,
                )
            else:
                logger.debug(
                    "[Orchestrator] PLAN skipped for op=%s: %s",
                    ctx.op_id, _plan_result.skip_reason,
                )
        except ImportError:
            logger.debug("[Orchestrator] PlanGenerator not available, skipping PLAN phase")
        except Exception as exc:
            logger.warning(
                "[Orchestrator] PLAN phase failed for op=%s: %s; "
                "continuing to GENERATE without plan",
                ctx.op_id, exc,
            )

        # ---- Phase 2d: Plan Approval Hard Gate (Phase 1b) ----
        # For COMPLEX / ARCHITECTURAL ops, pause BEFORE burning generation
        # tokens and get human sign-off on the approach. Rejection aborts
        # the op; approval proceeds to GENERATE. Manifesto §6 (Iron Gate):
        # "every autonomous decision is visible" + cost protection.
        #
        # Env-gated, fully override-able for battle tests and CI:
        #   JARVIS_PLAN_APPROVAL_ENABLED         (default true)
        #   JARVIS_PLAN_APPROVAL_ROUTES          (default "complex")
        #   JARVIS_PLAN_APPROVAL_COMPLEXITIES    (default "complex,heavy_code,architectural")
        #   JARVIS_PLAN_APPROVAL_TIMEOUT_S       (default 600.0)
        #   JARVIS_PLAN_APPROVAL_EXPIRE_GRACE    (default false — strict)
        _plan_gate_enabled = os.environ.get(
            "JARVIS_PLAN_APPROVAL_ENABLED", "true"
        ).lower() not in ("false", "0", "no", "off")
        _plan_gate_applied = False
        if (
            _plan_gate_enabled
            and _plan_result is not None
            and not getattr(_plan_result, "skipped", True)
            and self._approval_provider is not None
            and hasattr(self._approval_provider, "request_plan")
        ):
            _gate_routes = {
                r.strip().lower()
                for r in os.environ.get(
                    "JARVIS_PLAN_APPROVAL_ROUTES", "complex"
                ).split(",")
                if r.strip()
            }
            _gate_complexities = {
                c.strip().lower()
                for c in os.environ.get(
                    "JARVIS_PLAN_APPROVAL_COMPLEXITIES",
                    "complex,heavy_code,architectural",
                ).split(",")
                if c.strip()
            }
            _route = (getattr(ctx, "provider_route", "") or "").lower()
            _task_cx = (getattr(ctx, "task_complexity", "") or "").lower()
            _plan_cx = (getattr(_plan_result, "complexity", "") or "").lower()
            # OR-predicate: gate trips if ANY of (provider_route,
            # task_complexity, plan_result.complexity) matches the filters.
            # plan_result.complexity takes precedence because the model
            # has just reasoned about the actual scope during PLAN phase.
            if (
                _route in _gate_routes
                or _task_cx in _gate_complexities
                or _plan_cx in _gate_complexities
            ):
                _plan_gate_applied = True
                _plan_gate_timeout = float(os.environ.get(
                    "JARVIS_PLAN_APPROVAL_TIMEOUT_S", "600.0"
                ))
                _expire_grace = os.environ.get(
                    "JARVIS_PLAN_APPROVAL_EXPIRE_GRACE", "false"
                ).lower() in ("true", "1", "yes", "on")

                # Render plan as markdown for human review. Fall back to
                # raw JSON if to_prompt_section() is unavailable.
                try:
                    _plan_markdown = _plan_result.to_prompt_section()
                except Exception:
                    _plan_markdown = _plan_result.plan_json or "(no plan)"

                logger.info(
                    "[Orchestrator] Plan Gate engaged for op=%s "
                    "(route=%r task_cx=%r plan_cx=%r) — awaiting human",
                    ctx.op_id, _route, _task_cx, _plan_cx,
                )
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="plan", progress_pct=30.0,
                        plan_gate_engaged=True,
                    )
                except Exception:
                    pass

                try:
                    _plan_req_id = await self._approval_provider.request_plan(
                        ctx, _plan_markdown,
                    )
                    _plan_decision: ApprovalResult = await (
                        self._approval_provider.await_decision(
                            _plan_req_id, _plan_gate_timeout,
                        )
                    )
                except Exception as _gate_exc:
                    # Gate infrastructure failure — log and continue without
                    # gating rather than blocking the pipeline forever.
                    logger.warning(
                        "[Orchestrator] Plan Gate infra failure for op=%s: %s; "
                        "continuing to GENERATE without approval",
                        ctx.op_id, _gate_exc,
                    )
                    _plan_decision = None  # type: ignore[assignment]

                if _plan_decision is not None:
                    if _plan_decision.status is ApprovalStatus.REJECTED:
                        _reject_reason = (
                            getattr(_plan_decision, "reason", "") or ""
                        )
                        logger.info(
                            "[Orchestrator] Plan REJECTED for op=%s: %s",
                            ctx.op_id, _reject_reason,
                        )
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="plan_rejected",
                        )
                        await self._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "plan_rejected",
                                "approver": _plan_decision.approver,
                                "rejection_reason": _reject_reason,
                                "plan_complexity": _plan_cx,
                            },
                        )
                        # Persist rejection so future similar plans learn from it.
                        if _reject_reason:
                            try:
                                from backend.core.ouroboros.governance.user_preference_memory import (
                                    get_default_store,
                                )
                                get_default_store().record_approval_rejection(
                                    op_id=ctx.op_id,
                                    description=f"[PLAN] {ctx.description}",
                                    target_files=list(ctx.target_files),
                                    reason=_reject_reason,
                                    approver=(
                                        getattr(_plan_decision, "approver", "human")
                                        or "human"
                                    ),
                                )
                            except Exception:
                                pass
                        # Session lesson for intra-session learning.
                        _files_short = ", ".join(
                            p.rsplit("/", 1)[-1] for p in ctx.target_files[:3]
                        )
                        self._session_lessons.append((
                            "code",
                            f"[PLAN REJECTED] {ctx.description[:60]} "
                            f"({_files_short}) — human rejected the approach: "
                            f"{_reject_reason[:80] or 'no reason given'}. "
                            f"Reconsider strategy before retry.",
                        ))
                        if len(self._session_lessons) > self._session_lessons_max:
                            self._session_lessons = (
                                self._session_lessons[-self._session_lessons_max:]
                            )
                        return ctx

                    if _plan_decision.status is ApprovalStatus.EXPIRED:
                        if _expire_grace:
                            logger.warning(
                                "[Orchestrator] Plan Gate expired for op=%s; "
                                "grace mode — continuing to GENERATE",
                                ctx.op_id,
                            )
                        else:
                            logger.info(
                                "[Orchestrator] Plan Gate EXPIRED for op=%s — "
                                "aborting (strict mode)",
                                ctx.op_id,
                            )
                            ctx = ctx.advance(
                                OperationPhase.EXPIRED,
                                terminal_reason_code="plan_approval_expired",
                            )
                            await self._record_ledger(
                                ctx,
                                OperationState.FAILED,
                                {"reason": "plan_approval_expired"},
                            )
                            return ctx

                    # APPROVED (or grace on EXPIRED) — continue to GENERATE
                    if _plan_decision.status is ApprovalStatus.APPROVED:
                        logger.info(
                            "[Orchestrator] Plan APPROVED for op=%s by %s",
                            ctx.op_id, _plan_decision.approver,
                        )
        elif _plan_gate_enabled and _plan_result is not None and not getattr(
            _plan_result, "skipped", True
        ):
            logger.debug(
                "[Orchestrator] Plan Gate skipped for op=%s: "
                "provider=%s has_request_plan=%s",
                ctx.op_id,
                type(self._approval_provider).__name__
                if self._approval_provider
                else "None",
                hasattr(self._approval_provider, "request_plan"),
            )

        ctx = ctx.advance(OperationPhase.GENERATE)

        # ── PreActionNarrator: voice WHAT before GENERATE ──
        if self._pre_action_narrator is not None:
            try:
                _provider_name = getattr(ctx, "routing_actual", None) or "unknown"
                await self._pre_action_narrator.narrate_phase(
                    "GENERATE",
                    {"provider": str(_provider_name), "thinking_mode": "standard"},
                )
            except Exception:
                pass

        # ── P2: Adaptive Learning — inject consolidated rules + success patterns ──
        try:
            from backend.core.ouroboros.governance.adaptive_learning import (
                LearningConsolidator, SuccessPatternStore,
            )
            from backend.core.ouroboros.governance.entropy_calculator import (
                extract_domain_key as _extract_dk,
            )
            _domain = _extract_dk(ctx.target_files, ctx.description)

            _consolidator = LearningConsolidator()
            _rules_context = _consolidator.format_rules_for_prompt(_domain)

            _success_store = SuccessPatternStore()
            _success_context = _success_store.format_for_prompt(_domain, ctx.target_files)

            if _rules_context or _success_context:
                _existing_mem = getattr(ctx, "strategic_memory_prompt", "") or ""
                _learning_block = ""
                if _rules_context:
                    _learning_block += f"\n\n{_rules_context}"
                if _success_context:
                    _learning_block += f"\n\n{_success_context}"
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing_mem + _learning_block,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Adaptive learning: injected %d rules + %d success "
                    "patterns for domain=%s (op=%s)",
                    len(_consolidator.get_rules_for_domain(_domain)),
                    len(_success_store.get_similar_successes(_domain, ctx.target_files)),
                    _domain, ctx.op_id,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Adaptive learning injection failed", exc_info=True)

        # ── P0: Test Coverage Enforcer (pre-GENERATE) ─────────────────────
        # If target files lack test coverage, inject instruction into the
        # generation context so the provider generates tests alongside code.
        try:
            from backend.core.ouroboros.governance.intelligence_hooks import (
                TestCoverageEnforcer,
            )
            _coverage_enforcer = TestCoverageEnforcer(self._config.project_root)
            _coverage_instruction = _coverage_enforcer.check_and_inject(
                ctx.target_files, ctx.description,
            )
            if _coverage_instruction:
                _existing_human = getattr(ctx, "human_instructions", "") or ""
                ctx = dataclasses.replace(
                    ctx,
                    human_instructions=_existing_human + _coverage_instruction,
                    previous_hash=ctx.context_hash,
                )
                logger.info(
                    "[Orchestrator] TestCoverageEnforcer: injected test generation "
                    "instruction for %d uncovered files (op=%s)",
                    _coverage_instruction.count("`"), ctx.op_id,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] TestCoverageEnforcer failed", exc_info=True)

        # ── JARVIS Tier 5: Cross-Domain Intelligence ──────────────────────
        try:
            from backend.core.ouroboros.governance.jarvis_intelligence import (
                UnifiedIntelligenceLayer,
            )
            _intel = UnifiedIntelligenceLayer(self._config.project_root)
            _syntheses = _intel.analyze_all_domains()
            _intel_prompt = _intel.format_for_prompt(_syntheses)
            if _intel_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _intel_prompt,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] JARVIS Tier 5: %d cross-domain syntheses injected",
                    len(_syntheses),
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Tier 5 injection failed", exc_info=True)

        # ── JARVIS Tier 6: Personality voice line ─────────────────────────
        _gls = getattr(self._stack, "governed_loop_service", None)
        if _gls is not None:
            _pe = getattr(_gls, "_personality_engine", None)
            if _pe is not None:
                try:
                    _chronic = getattr(_advisory, "chronic_entropy", 0.0) if _advisory else 0.0
                    _emerg = getattr(self._stack, "_emergency_engine", None)
                    _emerg_lvl = _emerg.current_level.value if _emerg else 0
                    _state = _pe.compute_state(
                        success_rate=_pe.success_rate,
                        chronic_entropy=_chronic,
                        emergency_level=_emerg_lvl,
                    )
                    if self._reasoning_narrator is not None:
                        _voice = _pe.get_voice_line(_state)
                        self._reasoning_narrator.record_classify(
                            ctx.op_id, f"personality:{_state.value}", _voice,
                        )
                except Exception:
                    pass

        # ── Advanced Repair: hierarchical localization + slow/fast thinking + doc-augmented ──
        try:
            from backend.core.ouroboros.governance.advanced_repair import (
                HierarchicalFaultLocalizer, SlowFastThinkingRouter, DocAugmentedRepair,
            )
            _apr_blocks: list = []

            # 1. Hierarchical fault localization (file → function → line)
            _localizer = HierarchicalFaultLocalizer(self._config.project_root)
            _error_msg = getattr(ctx, "error_pattern", "") or ctx.description
            _locations = _localizer.localize(ctx.target_files, _error_msg)
            _loc_prompt = _localizer.format_for_prompt(_locations)
            if _loc_prompt:
                _apr_blocks.append(_loc_prompt)

            # 2. Slow/fast thinking router
            _thinking = SlowFastThinkingRouter.route(
                ctx.description, ctx.target_files,
            )
            _think_prompt = SlowFastThinkingRouter.format_for_prompt(_thinking)
            if _think_prompt:
                _apr_blocks.append(_think_prompt)

            # 3. Documentation-augmented repair context
            _doc_repair = DocAugmentedRepair(self._config.project_root)
            _doc_context = _doc_repair.generate_docs_for_repair(ctx.target_files)
            if _doc_context:
                _apr_blocks.append(_doc_context)

            if _apr_blocks:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                _apr_combined = "\n\n".join(_apr_blocks)
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _apr_combined,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Advanced repair: %d blocks (localization=%d locs, "
                    "thinking=%s, docs=%d chars) for op=%s",
                    len(_apr_blocks), len(_locations), _thinking.depth,
                    len(_doc_context), ctx.op_id,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Advanced repair injection failed", exc_info=True)

        # ── Self-Evolution P0: Inject runtime prompt adaptations + negative constraints + code metrics ──
        try:
            from backend.core.ouroboros.governance.self_evolution import (
                RuntimePromptAdapter, NegativeConstraintStore,
                CodeMetricsAnalyzer, MultiVersionEvolutionTracker,
            )
            from backend.core.ouroboros.governance.entropy_calculator import extract_domain_key as _edk

            _se_domain = _edk(ctx.target_files, ctx.description)
            _se_blocks: List[str] = []

            # P0: Runtime prompt adaptation — learned instructions from outcomes
            _prompt_adapter = RuntimePromptAdapter()
            _adapted = _prompt_adapter.get_adapted_instructions(_se_domain)
            if _adapted:
                _se_blocks.append(_adapted)

            # P0: Negative constraints — "never do X" rules
            _neg_store = NegativeConstraintStore()
            _neg_prompt = _neg_store.format_for_prompt(_se_domain)
            if _neg_prompt:
                _se_blocks.append(_neg_prompt)

            # P1: Code metrics feedback — objective quality signals
            for _tf in ctx.target_files[:3]:
                _tf_path = self._config.project_root / _tf
                if _tf_path.is_dir() or not _tf_path.suffix:
                    continue  # Skip directories — only analyze files
                _metrics = CodeMetricsAnalyzer.analyze(_tf_path)
                if _metrics:
                    _mf = CodeMetricsAnalyzer.format_for_prompt(_metrics)
                    if _mf:
                        _se_blocks.append(_mf)

            if _se_blocks:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                _se_combined = "\n\n".join(_se_blocks)
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _se_combined,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Self-evolution: injected %d blocks for domain=%s",
                    len(_se_blocks), _se_domain,
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Self-evolution injection failed", exc_info=True)

        # ── Self-Evolution P2: Module-level function analysis + auto-documentation gaps ──
        try:
            from backend.core.ouroboros.governance.self_evolution import (
                ModuleLevelMutator, RepositoryAutoDocumentation,
            )
            _se2_blocks: List[str] = []

            # ModuleLevelMutator: show function-level breakdown of target files
            # so the generator can do surgical mutations instead of full rewrites
            for _tf in ctx.target_files[:3]:
                _tf_path = self._config.project_root / _tf
                if not _tf_path.is_file() or _tf_path.suffix != ".py":
                    continue
                _funcs = ModuleLevelMutator.list_functions(_tf_path)
                if _funcs:
                    _complex = [f for f in _funcs if f["complexity"] > 5]
                    if _complex:
                        _func_info = ", ".join(
                            f"{f['name']}(CC={f['complexity']}, L{f['start_line']}-{f['end_line']})"
                            for f in sorted(_complex, key=lambda x: x["complexity"], reverse=True)[:5]
                        )
                        _se2_blocks.append(
                            f"## Function-level analysis: {_tf}\n"
                            f"Complex functions (surgical mutation targets): {_func_info}\n"
                            f"Prefer modifying individual functions over full-file rewrites."
                        )

            # RepositoryAutoDocumentation: show doc gaps in target files
            _auto_doc = RepositoryAutoDocumentation()
            for _tf in ctx.target_files[:3]:
                _tf_path = self._config.project_root / _tf
                if _tf_path.is_file() and _tf_path.suffix == ".py":
                    _auto_doc.scan_file(_tf_path)
            _doc_prompt = _auto_doc.format_for_prompt(
                [str(self._config.project_root / tf) for tf in ctx.target_files[:3]]
            )
            if _doc_prompt:
                _se2_blocks.append(_doc_prompt)

            if _se2_blocks:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                _se2_combined = "\n\n".join(_se2_blocks)
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=getattr(ctx, "strategic_intent_id", "") or "",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=_existing + "\n\n" + _se2_combined,
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[Orchestrator] Self-evolution P2: injected %d blocks "
                    "(module analysis + doc gaps)",
                    len(_se2_blocks),
                )
        except ImportError:
            pass
        except Exception:
            logger.debug("[Orchestrator] Self-evolution P2 injection failed", exc_info=True)

        # ── Cooperative cancellation check (pre-GENERATE) ──
        if self._is_cancel_requested(ctx.op_id):
            ctx = ctx.advance(OperationPhase.CANCELLED, terminal_reason_code="user_cancelled")
            await self._record_ledger(ctx, OperationState.FAILED, {"reason": "user_cancelled"})
            return ctx

        if _serpent: _serpent.update_phase("GENERATE")
        # ---- Phase 3: GENERATE (with retry + episodic failure memory) ----
        generation: Optional[GenerationResult] = None
        generate_retries_remaining = self._config.max_generate_retries

        # Episodic failure memory — per-operation, injected into retries
        _episodic_memory = None
        try:
            from backend.core.ouroboros.governance.episodic_memory import EpisodicFailureMemory
            _episodic_memory = EpisodicFailureMemory(ctx.op_id)
        except ImportError:
            pass

        # ── Inject cumulative session lessons into context ──
        # Filter out infrastructure failures (timeouts, provider outages) to
        # avoid poisoning the model with environmentally-caused failures.
        if self._session_lessons:
            _code_lessons = [
                text for (ltype, text) in self._session_lessons
                if ltype == "code"
            ][-self._session_lessons_max:]
            if _code_lessons:
                _lessons_text = "\n".join(f"- {lesson}" for lesson in _code_lessons)
                ctx = dataclasses.replace(
                    ctx,
                    session_lessons=_lessons_text,
                )

        # ── Consciousness: inject fragile-file memory into first generation ──
        # Manifesto §4: "The organism possesses episodic memory and metacognition"
        if _consciousness_bridge is not None:
            try:
                _fragile_ctx = _consciousness_bridge.get_fragile_file_context(
                    ctx.target_files
                )
                if _fragile_ctx:
                    _existing_mem = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = dataclasses.replace(
                        ctx,
                        strategic_memory_prompt=(
                            f"{_existing_mem}\n\n{_fragile_ctx}" if _existing_mem else _fragile_ctx
                        ),
                    )
                    logger.info(
                        "[Orchestrator] Consciousness memory injected into GENERATE context "
                        "(%d chars) [%s]",
                        len(_fragile_ctx), ctx.op_id,
                    )
            except Exception:
                logger.debug("[Orchestrator] Consciousness injection failed", exc_info=True)

        # ── Stale-exploration guard: snapshot file hashes at GENERATE time ──
        _gen_hashes: list = []
        for _tf in ctx.target_files:
            _tf_path = self._config.project_root / _tf
            try:
                _tf_bytes = _tf_path.read_bytes()
                _gen_hashes.append((_tf, hashlib.sha256(_tf_bytes).hexdigest()))
            except (OSError, IOError):
                _gen_hashes.append((_tf, ""))  # new file — no hash
        if _gen_hashes:
            ctx = dataclasses.replace(ctx, generate_file_hashes=tuple(_gen_hashes))

        # Cumulative exploration credit across the GENERATE retry loop. When a
        # prior attempt satisfied the floor but failed downstream gates (ASCII,
        # dependency integrity, etc.), the retry feedback embeds the rejected
        # file content — re-reading via read_file is wasteful, so the credit
        # carries forward instead of forcing the model to spend tool rounds on
        # the same file twice (bt-2026-04-11-204228 / op-019d7e4c).
        _op_explore_credit = 0

        for attempt in range(1 + self._config.max_generate_retries):
            # ── Per-op cost cap check (Manifesto §5/§7) ──
            # If the cumulative spend across previous attempts has already
            # exceeded the dynamic cap, refuse to initiate another provider
            # call. Routes through the phase-aware terminal picker.
            if self._cost_governor.is_exceeded(ctx.op_id):
                _cost_summary = self._cost_governor.summary(ctx.op_id) or {}
                logger.warning(
                    "[Orchestrator] Per-op cost cap exceeded before attempt %d: "
                    "cumulative=$%.4f cap=$%.4f route=%s complexity=%s [%s]",
                    attempt + 1,
                    _cost_summary.get("cumulative_usd", 0.0),
                    _cost_summary.get("cap_usd", 0.0),
                    _cost_summary.get("route", "?"),
                    _cost_summary.get("complexity", "?"),
                    ctx.op_id,
                )
                _terminal = self._l2_escape_terminal(ctx.phase)
                ctx = ctx.advance(
                    _terminal,
                    terminal_reason_code="op_cost_cap_exceeded",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "op_cost_cap_exceeded",
                        "cost_summary": dict(_cost_summary),
                        "entry_phase": "GENERATE",
                    },
                )
                self._cost_governor.finish(ctx.op_id)
                return ctx

            try:
                # Heartbeat: GENERATE phase starting (Manifesto §7: Absolute Observability)
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate",
                        progress_pct=30.0 + attempt * 5.0,
                    )
                except Exception:
                    pass

                # Route-aware generation timeout (Manifesto §5):
                #   IMMEDIATE: 120s — fast reflex, but must accommodate Venom
                #              tool-round + full_content patch generation.
                #              Raised from 60s after bt-2026-04-11-085020
                #              diagnosed tool_round=yes calls needing 63s+ to
                #              stream 23KB+ full_content patches (first token
                #              at 1.1s, healthy ~365 bytes/s). The 60s cap was
                #              cutting legitimate streams off mid-output.
                #   STANDARD:  220s — DW primary then Claude fallback in
                #              series. Bumped from 120s after
                #              bt-2026-04-11-211131 diagnosed every standard
                #              op as `all_providers_exhausted`: DW eats 60-90s
                #              of the budget then Claude only has 30-40s left
                #              — too short for legitimate doc-gen / patch
                #              streams (which need 60-100s). 220s = ~90s DW
                #              cap + ~120s fallback hard cap + grace.
                #   COMPLEX:   240s — Claude planning + DW execution serial
                #   BACKGROUND/SPECULATIVE: 180s — no urgency
                _route = getattr(ctx, "provider_route", "") or "standard"
                _route_timeouts = {
                    "immediate": 120.0,
                    "standard": 220.0,
                    "complex": 240.0,
                    "background": 180.0,
                    "speculative": 180.0,
                }
                _gen_timeout = _route_timeouts.get(
                    _route, self._config.generation_timeout_s
                )
                deadline = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=_gen_timeout
                )
                # Emit streaming=start so SerpentFlow can render the
                # "synthesizing" header before tokens begin flowing.
                # Provider is unknown at this point (chosen during adaptive failback).
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate", progress_pct=31.0,
                        streaming="start", provider="",
                    )
                except Exception:
                    pass
                # Hard timeout — the deadline is advisory to the generator,
                # but asyncio.wait_for is the Iron Gate (Manifesto §6).
                generation = await asyncio.wait_for(
                    self._generator.generate(ctx, deadline),
                    timeout=_gen_timeout + 5.0,
                )
                # Charge the CostGovernor with the actual generation cost.
                # Non-positive costs (cache hits, fallback stubs) are a no-op.
                try:
                    _cost_this_call = float(getattr(generation, "cost_usd", 0.0) or 0.0)
                    _prov_name = getattr(generation, "provider_name", "") or ""
                    if _cost_this_call > 0.0:
                        self._cost_governor.charge(
                            ctx.op_id, _cost_this_call, _prov_name,
                        )
                except Exception:
                    logger.debug(
                        "[Orchestrator] CostGovernor.charge failed", exc_info=True,
                    )
                # Emit streaming=end to close the streaming block
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate", progress_pct=49.0,
                        streaming="end",
                    )
                except Exception:
                    pass

                # is_noop=True means the model signalled the change is already present.
                # Empty candidates is correct in this case — do not treat as a failure.
                if generation is not None and generation.is_noop:
                    break

                if generation is None or len(generation.candidates) == 0:
                    generation = None
                    raise RuntimeError("no_candidates_returned")

                # ── Forward-progress detector ──
                # Hash the first candidate's content and flag if the
                # retry loop is producing the same candidate repeatedly.
                # A trip means we're burning retries without any actual
                # change — escape the loop via the phase-aware terminal.
                try:
                    _fp_hash = candidate_content_hash(generation.candidates[0])
                    if _fp_hash and self._forward_progress.observe(
                        ctx.op_id, _fp_hash,
                    ):
                        _fp_summary = self._forward_progress.summary(ctx.op_id) or {}
                        logger.warning(
                            "[Orchestrator] Forward-progress trip: op=%s "
                            "stuck after %d repeats — escaping retry loop",
                            ctx.op_id,
                            _fp_summary.get("repeat_count", 0),
                        )
                        _terminal = self._l2_escape_terminal(ctx.phase)
                        ctx = ctx.advance(
                            _terminal,
                            terminal_reason_code="no_forward_progress",
                        )
                        await self._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "no_forward_progress",
                                "progress_summary": dict(_fp_summary),
                                "entry_phase": "GENERATE",
                            },
                        )
                        self._forward_progress.finish(ctx.op_id)
                        return ctx
                except Exception:
                    logger.debug(
                        "[Orchestrator] ForwardProgress.observe failed",
                        exc_info=True,
                    )

                # ── Productivity-ratio detector (EC9) ──
                # Complements EC8: EC8 catches byte-identical repetition;
                # EC9 catches *semantic* stagnation — candidates whose
                # normalized form (AST dump / canonical JSON / whitespace-
                # stripped) hasn't changed while the model keeps charging
                # us for retries. Trip = $ burned since last semantic
                # change exceeded the threshold AND we've seen enough
                # stable observations. Escape via phase-aware terminal.
                try:
                    _pd_hash = productivity_content_hash(
                        generation.candidates[0],
                        level=self._productivity_detector.level,
                    )
                    if _pd_hash and self._productivity_detector.observe(
                        ctx.op_id, _cost_this_call, _pd_hash,
                    ):
                        _pd_summary = self._productivity_detector.summary(ctx.op_id) or {}
                        logger.warning(
                            "[Orchestrator] Productivity stall: op=%s "
                            "burned=$%.4f stable=%d level=%s — escaping retry loop",
                            ctx.op_id,
                            _pd_summary.get("cost_since_last_change_usd", 0.0),
                            _pd_summary.get("consecutive_stable", 0),
                            _pd_summary.get("config", {}).get("normalization_level", "?"),
                        )
                        _terminal = self._l2_escape_terminal(ctx.phase)
                        ctx = ctx.advance(
                            _terminal,
                            terminal_reason_code="stalled_productivity",
                        )
                        await self._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "stalled_productivity",
                                "productivity_summary": dict(_pd_summary),
                                "entry_phase": "GENERATE",
                            },
                        )
                        self._productivity_detector.finish(ctx.op_id)
                        return ctx
                except Exception:
                    logger.debug(
                        "[Orchestrator] ProductivityDetector.observe failed",
                        exc_info=True,
                    )

                # ── Iron Gate: deterministic post-generation quality checks ──
                # Manifesto §6: agentic intelligence proposes, deterministic
                # code validates. These checks hard-fail BEFORE validation
                # adapters run, routing back through the GENERATE retry loop
                # with explicit error feedback so the model learns in-flight.
                #
                # Gate 1 — Exploration-first enforcement (no patch without
                # reading the codebase). Trivial ops bypass (small-surface
                # rewrites don't need the floor).
                #
                # Complexity-scaled threshold (bt-2026-04-11-090651 root cause):
                # simple ops (single target file, mechanical change) need only
                # 1 exploration call — one read_file IS reading the codebase.
                # moderate/complex ops keep the 2-call floor because they
                # touch multiple surfaces. Claude-sonnet-4-6 reliably refused
                # retry feedback on simple ops ("1/2 → 0/2") because the
                # exploration demand didn't match the task size; scaling by
                # complexity restores intent-alignment while preserving the
                # gate's purpose.
                _task_complexity = getattr(ctx, "task_complexity", "") or ""
                _EXPLORATION_TOOLS = frozenset({
                    "read_file", "search_code", "get_callers", "list_symbols",
                    "glob_files", "list_dir",
                })
                _env_min = os.environ.get("JARVIS_MIN_EXPLORATION_CALLS")
                if _env_min is not None:
                    _min_explore = int(_env_min)
                elif _task_complexity == "simple":
                    _min_explore = 1
                else:
                    _min_explore = 2
                _explore_gate_enabled = (
                    os.environ.get("JARVIS_EXPLORATION_GATE", "true").lower() == "true"
                    and _task_complexity != "trivial"
                )
                if _explore_gate_enabled:
                    _explore_count = sum(
                        1 for _rec in (generation.tool_execution_records or ())
                        if getattr(_rec, "tool_name", "") in _EXPLORATION_TOOLS
                    )
                    # Roll the per-attempt count into the per-op credit BEFORE
                    # comparing — a prior attempt that already satisfied the
                    # floor lets a no-tool retry pass (the rejected file is
                    # already in the retry-feedback prompt).
                    _op_explore_credit += _explore_count
                    if _op_explore_credit < _min_explore:
                        _explore_err = (
                            f"exploration_insufficient: {_op_explore_credit}/{_min_explore} "
                            f"exploration tool calls (expected >= {_min_explore}). "
                            f"You MUST call read_file/search_code/get_callers at least "
                            f"{_min_explore} times BEFORE proposing any patch. "
                            f"Use the tool loop to read the target file and grep for "
                            f"callers, then return your patch."
                        )
                        logger.warning(
                            "[Orchestrator] Iron Gate — exploration_insufficient: "
                            "%d/%d (attempt=%d cumulative) for op=%s",
                            _op_explore_credit, _min_explore, attempt + 1, ctx.op_id[:12],
                        )
                        generation = None
                        raise RuntimeError(_explore_err)

                # Gate 2 — ASCII/Unicode strictness (prevent rapidفuzz-class
                # typos where model emits non-ASCII code points in identifier
                # positions). Deterministic scan; O(n) on candidate size.
                # Delegates to AsciiStrictGate which:
                #   1) auto-repairs common punctuation drift (em-dash →
                #      hyphen, curly quotes → straight, ellipsis → ...,
                #      nbsp → space, zero-width strip) IN-PLACE on the
                #      candidate dict — healing the deterministic training-
                #      data artifact where Claude always inserts U+2014 at
                #      the same byte offset of requirements.txt.
                #   2) hard-rejects any residue (Unicode letters in
                #      identifier positions, unlisted symbols) per the
                #      original Iron Gate contract.
                _ascii_gate = AsciiStrictGate()
                if _ascii_gate.enabled:
                    for _cand in generation.candidates:
                        _ok, _ascii_err, _bad_list = _ascii_gate.check(_cand)
                        _repairs = _cand.get("_ascii_repair_count", 0) if isinstance(_cand, dict) else 0
                        if _repairs:
                            logger.info(
                                "[Orchestrator] Iron Gate — ascii_auto_repaired: "
                                "%d codepoint(s) healed file=%s op=%s",
                                _repairs,
                                _cand.get("file_path", "?") if isinstance(_cand, dict) else "?",
                                ctx.op_id[:12],
                            )
                        if not _ok:
                            _samples_str = ", ".join(
                                bc.format_sample() for bc in _bad_list
                            )
                            logger.warning(
                                "[Orchestrator] Iron Gate — ascii_corruption: "
                                "%d offender(s) [%s] op=%s",
                                len(_bad_list), _samples_str, ctx.op_id[:12],
                            )
                            # Stash the rejected content + offenders on the
                            # exception so the retry feedback builder can
                            # extract the specific offending lines and show
                            # them back to the model in context. Without
                            # this, the model only sees "U+0641 at L106:C6"
                            # which isn't enough to locate the bad identifier
                            # in a 200-line file.
                            _rejected_content = ""
                            if isinstance(_cand, dict):
                                _rejected_content = (
                                    _cand.get("full_content", "")
                                    or _cand.get("raw_content", "")
                                    or ""
                                )
                                if not _rejected_content and isinstance(_cand.get("files"), list):
                                    # Multi-file shape — grab the first file matching an offender
                                    _bad_path = _bad_list[0].file_path if _bad_list else ""
                                    for _entry in _cand["files"]:
                                        if isinstance(_entry, dict) and _entry.get("file_path") == _bad_path:
                                            _rejected_content = _entry.get("full_content", "") or ""
                                            break
                            generation = None
                            _ascii_exc = RuntimeError(_ascii_err or "ascii_corruption")
                            # Private attributes — read back in the retry feedback builder.
                            _ascii_exc._ascii_bad_codepoints = _bad_list  # type: ignore[attr-defined]
                            _ascii_exc._ascii_rejected_content = _rejected_content  # type: ignore[attr-defined]
                            raise _ascii_exc

                # Gate 3 — Dependency file integrity. Catches hallucinated
                # package-name renames/truncations in requirements.txt (and
                # future: package.json, Cargo.toml, etc.). Engineered in
                # response to bt-2026-04-10-184157, where Claude emitted a
                # requirements.txt patch renaming ``anthropic`` →
                # ``anthropichttp`` and ``rapidfuzz`` → ``rapidfu`` — two
                # pure-ASCII corruptions that slipped past every other gate.
                try:
                    from backend.core.ouroboros.governance.dependency_file_gate import (
                        check_candidate as _dep_check,
                    )
                except ImportError:
                    _dep_check = None  # type: ignore[assignment]
                if _dep_check is not None:
                    for _cand in generation.candidates:
                        _dep_result = _dep_check(_cand, self._config.project_root)
                        if _dep_result is None:
                            continue
                        _dep_reason, _dep_offenders = _dep_result
                        logger.warning(
                            "[Orchestrator] Iron Gate — dependency_file_integrity: "
                            "%d offender(s) [%s] op=%s",
                            len(_dep_offenders),
                            ", ".join(_dep_offenders[:5]),
                            ctx.op_id[:12],
                        )
                        # Extract the rejected content for retry feedback.
                        _rejected_content = ""
                        if isinstance(_cand, dict):
                            _rejected_content = _cand.get("full_content", "") or ""
                            if not _rejected_content and isinstance(_cand.get("files"), list):
                                for _entry in _cand["files"]:
                                    if not isinstance(_entry, dict):
                                        continue
                                    _ep = _entry.get("file_path", "") or ""
                                    from backend.core.ouroboros.governance.dependency_file_gate import is_dependency_file
                                    if is_dependency_file(_ep):
                                        _rejected_content = _entry.get("full_content", "") or ""
                                        break
                        generation = None
                        _dep_exc = RuntimeError(_dep_reason)
                        # Private attributes — retry feedback builder reads these.
                        _dep_exc._dep_file_offenders = _dep_offenders  # type: ignore[attr-defined]
                        _dep_exc._dep_file_rejected_content = _rejected_content  # type: ignore[attr-defined]
                        raise _dep_exc

                # Gate 4 — Docstring multi-line collapse detection. Catches
                # the regression where Claude rewrites a multi-line module
                # or function docstring as a single-line literal containing
                # ``\n`` escape sequences (bt-2026-04-11-211131,
                # headless_cli.py). Valid Python that breaks every reader.
                try:
                    from backend.core.ouroboros.governance.docstring_collapse_gate import (
                        check_candidate as _docstring_check,
                    )
                except ImportError:
                    _docstring_check = None  # type: ignore[assignment]
                if _docstring_check is not None:
                    for _cand in generation.candidates:
                        _ds_result = _docstring_check(_cand, self._config.project_root)
                        if _ds_result is None:
                            continue
                        _ds_reason, _ds_offenders = _ds_result
                        logger.warning(
                            "[Orchestrator] Iron Gate — docstring_collapse: "
                            "%d offender(s) [%s] op=%s",
                            len(_ds_offenders),
                            ", ".join(_ds_offenders[:5]),
                            ctx.op_id[:12],
                        )
                        _rejected_content = ""
                        if isinstance(_cand, dict):
                            _rejected_content = _cand.get("full_content", "") or ""
                            if not _rejected_content and isinstance(_cand.get("files"), list):
                                for _entry in _cand["files"]:
                                    if isinstance(_entry, dict) and (
                                        _entry.get("file_path", "") or ""
                                    ).endswith(".py"):
                                        _rejected_content = _entry.get("full_content", "") or ""
                                        break
                        generation = None
                        _ds_exc = RuntimeError(_ds_reason)
                        _ds_exc._docstring_collapse_offenders = _ds_offenders  # type: ignore[attr-defined]
                        _ds_exc._docstring_collapse_rejected_content = _rejected_content  # type: ignore[attr-defined]
                        raise _ds_exc

                # Heartbeat: generation succeeded with candidates
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="generate",
                        progress_pct=50.0,
                    )
                    # Also emit rich payload for BattleDiffTransport
                    _gen_msg = type(
                        "_Msg", (), {
                            "payload": {
                                "phase": "generate",
                                "candidates_count": len(generation.candidates),
                                "provider": generation.provider_name,
                                "model_id": getattr(generation, "model_id", ""),
                                "generation_duration_s": generation.generation_duration_s,
                                "tool_records": len(getattr(generation, "tool_execution_records", ()) or ()),
                                "total_input_tokens": getattr(generation, "total_input_tokens", 0),
                                "total_output_tokens": getattr(generation, "total_output_tokens", 0),
                                "cost_usd": getattr(generation, "cost_usd", 0.0),
                                # Include candidate file paths and preview for TUI display
                                "candidate_files": [
                                    getattr(c, "file_path", "") for c in generation.candidates[:3]
                                ],
                                "candidate_rationales": [
                                    (c.get("rationale", "") or "")[:80]
                                    for c in generation.candidates[:3]
                                ],
                                "candidate_preview": (
                                    getattr(generation.candidates[0], "raw_content", "")[:500]
                                    if generation.candidates else ""
                                ),
                            },
                            "op_id": ctx.op_id,
                            "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                        },
                    )()
                    for _t in getattr(self._stack.comm, "_transports", []):
                        try:
                            await _t.send(_gen_msg)
                        except Exception:
                            pass
                except Exception:
                    pass

                # Success -- record reasoning trace + dialogue
                if self._reasoning_narrator is not None:
                    try:
                        self._reasoning_narrator.record_generate(
                            ctx.op_id, generation.provider_name,
                            len(generation.candidates), generation.generation_duration_s,
                        )
                    except Exception:
                        pass
                if self._dialogue_store is not None:
                    try:
                        _d = self._dialogue_store.get_active(ctx.op_id)
                        if _d:
                            _d.add_entry(
                                "GENERATE",
                                f"{generation.provider_name} produced {len(generation.candidates)} "
                                f"candidates in {generation.generation_duration_s:.1f}s",
                            )
                    except Exception:
                        pass

                # Success -- break out of retry loop
                break

            except Exception as exc:
                _err_msg = str(exc)

                # ── BACKGROUND / SPECULATIVE route failures ──
                # These routes intentionally avoid Claude. Don't retry
                # with expensive providers — accept failure gracefully.
                _route = getattr(ctx, "provider_route", "")
                if _route == "speculative" and "speculative_deferred" in _err_msg:
                    # Speculative ops are fire-and-forget — not a failure.
                    logger.info(
                        "[Orchestrator] SPECULATIVE op deferred (DW background) [%s]",
                        ctx.op_id,
                    )
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="speculative_deferred",
                    )
                    await self._record_ledger(
                        ctx, OperationState.COMPLETED,
                        {"reason": "speculative_deferred", "route": "speculative"},
                    )
                    return ctx

                if _route == "background" and "background_dw_" in _err_msg:
                    # Background DW failure — don't cascade to Claude.
                    # Accept failure; sensor will re-detect if still relevant.
                    logger.info(
                        "[Orchestrator] BACKGROUND route: DW failed (%s), "
                        "accepting without Claude cascade [%s]",
                        _err_msg[:100], ctx.op_id,
                    )
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code=f"background_accepted:{_err_msg[:80]}",
                    )
                    await self._record_ledger(
                        ctx, OperationState.FAILED,
                        {"reason": "background_dw_failure", "error": _err_msg[:200],
                         "route": "background"},
                    )
                    return ctx

                logger.warning(
                    "Generation attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    1 + self._config.max_generate_retries,
                    ctx.op_id,
                    exc,
                )
                generate_retries_remaining -= 1
                if generate_retries_remaining < 0:
                    # ── IMMEDIATE → STANDARD demotion ──
                    # If IMMEDIATE exhausted Claude retries, demote to
                    # STANDARD (DW primary → Claude fallback) for one
                    # last attempt.  Direct call — don't rely on the
                    # exhausted for-loop range.
                    if _route == "immediate":
                        logger.info(
                            "[Orchestrator] IMMEDIATE exhausted — demoting "
                            "to STANDARD route for DW attempt [%s]",
                            ctx.op_id,
                        )
                        object.__setattr__(ctx, "provider_route", "standard")
                        object.__setattr__(
                            ctx, "provider_route_reason",
                            f"demotion:immediate_exhausted:{_err_msg[:60]}",
                        )
                        _route = "standard"  # update local for timeout calc
                        # Refresh the cost-governor cap for the new route so
                        # the demotion gets a proportional budget headroom.
                        try:
                            self._cost_governor.start(
                                op_id=ctx.op_id,
                                route="standard",
                                complexity=getattr(ctx, "task_complexity", "") or "",
                            )
                        except Exception:
                            pass
                        # Guard the demotion call itself: if cumulative spend
                        # already blew past the new cap, skip the demotion.
                        if self._cost_governor.is_exceeded(ctx.op_id):
                            logger.warning(
                                "[Orchestrator] Skipping STANDARD demotion — "
                                "cost cap already exceeded [%s]",
                                ctx.op_id,
                            )
                        else:
                            try:
                                _dem_deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=220.0)
                                generation = await asyncio.wait_for(
                                    self._generator.generate(ctx, _dem_deadline),
                                    timeout=225.0,
                                )
                                # Charge demotion call cost (may be zero).
                                try:
                                    _dem_cost = float(getattr(generation, "cost_usd", 0.0) or 0.0)
                                    _dem_prov = getattr(generation, "provider_name", "") or ""
                                    if _dem_cost > 0.0:
                                        self._cost_governor.charge(
                                            ctx.op_id, _dem_cost, _dem_prov,
                                        )
                                except Exception:
                                    pass
                                if generation is not None and len(generation.candidates) > 0:
                                    break  # success — continue pipeline
                                generation = None
                            except Exception as dem_exc:
                                logger.warning(
                                    "[Orchestrator] STANDARD demotion also failed: %s [%s]",
                                    dem_exc, ctx.op_id,
                                )

                    # All retries truly exhausted
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="generation_failed",
                    )
                    await self._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {"reason": "generation_failed", "error": str(exc)},
                    )
                    return ctx
                # P2: Dynamic Re-Planning — suggest alternative strategy on failure
                try:
                    from backend.core.ouroboros.governance.self_evolution import DynamicRePlanner
                    _attempt_num = self._config.max_generate_retries - generate_retries_remaining + 1
                    _fc = validation.failure_class or "" if 'validation' in dir() else ""
                    _em = validation.short_summary or "" if 'validation' in dir() else ""
                    _replan = DynamicRePlanner.suggest_replan(_fc, _em, _attempt_num)
                    if _replan:
                        _replan_text = DynamicRePlanner.format_for_prompt(_replan)
                        logger.info(
                            "[Orchestrator] Dynamic re-plan: %s (attempt %d)",
                            _replan.trigger[:50], _attempt_num,
                        )
                except Exception:
                    _replan_text = ""
                    pass

                # Retry: advance to GENERATE_RETRY with episodic memory context
                _retry_ctx_kwargs = {}

                # Inject direct error feedback so the model knows what went wrong
                _err_str = str(exc)

                # ── Iron Gate failures get targeted, in-flight instructions ──
                if _err_str.startswith("exploration_insufficient"):
                    _error_feedback = (
                        "## PREVIOUS GENERATION REJECTED — NO EXPLORATION\n\n"
                        f"{_err_str[:400]}\n\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        "- BEFORE writing any patch, call read_file on the target file(s).\n"
                        "- Call search_code or get_callers for any function/symbol you are\n"
                        "  about to modify so you understand its callers and tests.\n"
                        "- Only after you have at least 2 exploration tool calls in your\n"
                        "  tool_execution_records may you emit the final patch.\n"
                        "- Exploration is NOT optional. Patches without context corrupt code.\n"
                    )
                elif _err_str.startswith("ascii_corruption"):
                    # Extract the specific offending lines from the rejected
                    # candidate so the model sees its own bad code in context
                    # (not just "U+0641 at L106:C6"). The orchestrator stashed
                    # the full_content + BadCodepoint list on the exception
                    # just before raising, so we can reconstruct the exact
                    # lines that tripped the gate and show ASCII-only
                    # corrections alongside them.
                    _rejected = getattr(exc, "_ascii_rejected_content", "") or ""
                    _bad_cps = getattr(exc, "_ascii_bad_codepoints", None) or []
                    _offending_block = ""
                    if _rejected and _bad_cps:
                        _lines = _rejected.split("\n")
                        _seen_lines: set = set()
                        _line_samples = []
                        for _bc in _bad_cps[:5]:
                            _ln = getattr(_bc, "line", 0)
                            if _ln <= 0 or _ln in _seen_lines or _ln > len(_lines):
                                continue
                            _seen_lines.add(_ln)
                            _raw_line = _lines[_ln - 1]
                            # Build an ASCII-only "what-to-write-instead" hint
                            # by stripping every non-ASCII codepoint. For
                            # letters this produces a visible "hole" that
                            # shows where the model must make a deliberate
                            # spelling decision (e.g. rapidفuzz → rapiduzz,
                            # which makes the corruption obvious).
                            _stripped = "".join(
                                ch if ord(ch) < 128 else "·" for ch in _raw_line
                            )
                            _cp_hex = f"U+{getattr(_bc, 'codepoint', 0):04X}"
                            _char = getattr(_bc, "char", "?")
                            _line_samples.append(
                                f"  line {_ln} contains {_cp_hex} '{_char}':\n"
                                f"      WRONG: {_raw_line}\n"
                                f"      (·=non-ASCII): {_stripped}"
                            )
                        if _line_samples:
                            _offending_block = (
                                "\nSPECIFIC OFFENDING LINES FROM YOUR LAST OUTPUT:\n"
                                + "\n".join(_line_samples) + "\n"
                            )

                    _error_feedback = (
                        "## PREVIOUS GENERATION REJECTED — UNICODE CORRUPTION\n\n"
                        f"{_err_str[:400]}\n"
                        f"{_offending_block}\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        "- The lines above contain Unicode LETTERS that look like\n"
                        "  ASCII letters but aren't. These are HARD FAILURES — the\n"
                        "  Iron Gate auto-heals punctuation (em-dash, curly quotes,\n"
                        "  ellipsis, nbsp) but it will NEVER auto-heal letters\n"
                        "  because changing a letter changes the identity of a\n"
                        "  package, function, or variable.\n"
                        "- Re-emit the ENTIRE file using only 7-bit ASCII (0x20–0x7E)\n"
                        "  for every character. Every byte must satisfy ord(c) < 128.\n"
                        "- Common culprits in package manifests (requirements.txt,\n"
                        "  package.json, pyproject.toml, Pipfile):\n"
                        "    * U+0641 Arabic FEH 'ف' looks like ASCII 'f'\n"
                        "    * U+0430 Cyrillic 'а' looks like ASCII 'a'\n"
                        "    * U+0435 Cyrillic 'е' looks like ASCII 'e'\n"
                        "    * U+03BF Greek omicron 'ο' looks like ASCII 'o'\n"
                        "  If you're about to write 'rapidfuzz', type r-a-p-i-d-f-u-z-z\n"
                        "  using ONLY characters from the ASCII table. Do not rely on\n"
                        "  memory of what the package name 'looks like'.\n"
                        "- Sanity check: every single character in your output must\n"
                        "  be in the range 0x20–0x7E or \\n (0x0A). No exceptions.\n"
                    )
                elif _err_str.startswith("Dependency file rename/truncation suspected"):
                    # Gate 3 rejection — show the offender pairs and a clear
                    # rule: you are NOT allowed to rename/shorten an existing
                    # package name, only add new ones or bump versions.
                    _dep_offenders = getattr(exc, "_dep_file_offenders", None) or []
                    _dep_rejected = getattr(exc, "_dep_file_rejected_content", "") or ""
                    _offender_block = ""
                    if _dep_offenders:
                        _offender_lines = "\n".join(
                            f"  {i + 1}. {pair}" for i, pair in enumerate(_dep_offenders[:10])
                        )
                        _offender_block = (
                            "\nSUSPICIOUS RENAMES DETECTED:\n"
                            f"{_offender_lines}\n"
                        )
                    _error_feedback = (
                        "## PREVIOUS GENERATION REJECTED — DEPENDENCY FILE CORRUPTION\n\n"
                        f"{_err_str[:400]}\n"
                        f"{_offender_block}\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        "- You deleted existing package(s) and added a near-identical\n"
                        "  new name. This is almost always a typo or hallucination —\n"
                        "  real upgrades change only the VERSION, not the package name.\n"
                        "- If the goal is to UPGRADE a package: keep the name identical\n"
                        "  (e.g. `anthropic==0.75.0` → `anthropic==0.80.0`). NEVER change\n"
                        "  the letters of the package name.\n"
                        "- If you truly need to REPLACE a package with a different one,\n"
                        "  the new name must be clearly distinct (not a substring or\n"
                        "  truncation of the old name) AND the reason must be in the\n"
                        "  `rationale` field of your candidate.\n"
                        "- Common hallucination patterns to avoid:\n"
                        "    * truncation: `rapidfuzz` → `rapidfu` (WRONG)\n"
                        "    * suffix append: `anthropic` → `anthropichttp` (WRONG)\n"
                        "    * single-char typo: `requests` → `reqest` (WRONG)\n"
                        "- Before emitting, compare each package name against the\n"
                        "  source file character-by-character. Every name that was\n"
                        "  there must still be there with the exact same spelling.\n"
                    )
                else:
                    _error_feedback = (
                        "## PREVIOUS GENERATION FAILED\n\n"
                        f"Error: {_err_str[:300]}\n\n"
                        "INSTRUCTIONS FOR RETRY:\n"
                        "- Return schema_version '2b.1' with 'full_content' containing the COMPLETE file\n"
                        "- Do NOT return unified diffs or patches\n"
                        "- Ensure the JSON is valid (no trailing commas, no unquoted keys)\n"
                        "- full_content must be the entire file, not a summary or placeholder\n"
                    )
                _retry_ctx_kwargs["strategic_memory_prompt"] = _error_feedback

                # Record generation failure in episodic memory for downstream use
                if _episodic_memory is not None:
                    _gen_failure_class = "content"
                    if "exploration_insufficient" in _err_str:
                        _gen_failure_class = "exploration"
                    elif "ascii_corruption" in _err_str:
                        _gen_failure_class = "ascii"
                    elif _err_str.startswith("Dependency file rename/truncation"):
                        _gen_failure_class = "dep_file_rename"
                    elif "json_parse_error" in _err_str:
                        _gen_failure_class = "json_parse"
                    elif "diff_apply_failed" in _err_str:
                        _gen_failure_class = "diff_apply"
                    elif "schema_invalid" in _err_str:
                        _gen_failure_class = "schema"
                    try:
                        _episodic_memory.record(
                            file_path=list(ctx.target_files)[0] if ctx.target_files else "unknown",
                            attempt=attempt + 1,
                            failure_class=_gen_failure_class,
                            error_summary=_err_str[:500],
                            specific_errors=[_err_str[:200]],
                            line_numbers=[],
                        )
                    except Exception:
                        pass

                # Inject re-plan if available (appends to error feedback)
                if _replan_text:
                    _existing = _retry_ctx_kwargs.get("strategic_memory_prompt", "")
                    _retry_ctx_kwargs["strategic_memory_prompt"] = (
                        f"{_existing}\n\n{_replan_text}" if _existing else _replan_text
                    )

                if _episodic_memory is not None and _episodic_memory.has_failures():
                    _failure_context = _episodic_memory.format_for_prompt()
                    if _failure_context:
                        # Inject into strategic_memory_prompt so the generator sees it
                        _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                        _retry_ctx_kwargs["strategic_memory_prompt"] = (
                            f"{_existing}\n\n{_failure_context}" if _existing else _failure_context
                        )
                        logger.info(
                            "[Orchestrator] Injecting %d episodic failure(s) into retry context [%s]",
                            _episodic_memory.total_episodes, ctx.op_id,
                        )
                # Inject consciousness fragile-file memory into retry context
                if _consciousness_bridge is not None:
                    try:
                        _fragile_ctx = _consciousness_bridge.get_fragile_file_context(
                            ctx.target_files
                        )
                        if _fragile_ctx:
                            _existing_mem = _retry_ctx_kwargs.get("strategic_memory_prompt", "")
                            _retry_ctx_kwargs["strategic_memory_prompt"] = (
                                f"{_existing_mem}\n\n{_fragile_ctx}" if _existing_mem else _fragile_ctx
                            )
                    except Exception:
                        pass
                ctx = ctx.advance(OperationPhase.GENERATE_RETRY, **_retry_ctx_kwargs)

        assert generation is not None  # guaranteed by loop logic

        # L1: emit tool execution audit records to ledger stream.
        # This runs BEFORE the noop guard so that tool records are always
        # persisted regardless of whether the response was a noop.
        for _rec in generation.tool_execution_records:
            try:
                _entry = LedgerEntry(
                    op_id=ctx.op_id,
                    state=OperationState.SANDBOXING,
                    data={"kind": "tool_exec.v1", **_dc_asdict(_rec)},
                    entry_id=_rec.call_id,
                )
                await self._stack.ledger.append(_entry)
            except asyncio.CancelledError:
                raise
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "tool_exec ledger emit failed op=%s record=%s: %s",
                    ctx.op_id, getattr(_rec, "call_id", "?"), _exc,
                )  # ledger failure must never abort governance pipeline

        # Short-circuit: model signalled the change is already present
        if generation.is_noop:
            logger.info(
                "[Orchestrator] op=%s is_noop=True (provider=%s) — skipping APPLY",
                ctx.op_id,
                generation.provider_name,
            )
            ctx = ctx.advance(
                OperationPhase.COMPLETE,
                generation=generation,
                terminal_reason_code="noop",
            )
            await self._record_ledger(
                ctx,
                OperationState.APPLIED,
                {"reason": "noop", "provider": generation.provider_name},
            )
            return ctx

        # Store generation result in context
        if _serpent: _serpent.update_phase("VALIDATE")
        ctx = ctx.advance(OperationPhase.VALIDATE, generation=generation)

        # Heartbeat: VALIDATE phase starting (Manifesto §7)
        try:
            await self._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="validate", progress_pct=55.0,
            )
        except Exception:
            pass

        # ── PreActionNarrator: voice WHAT before VALIDATE ──
        if self._pre_action_narrator is not None:
            try:
                await self._pre_action_narrator.narrate_phase(
                    "VALIDATE", {"test_count": str(len(ctx.target_files))},
                )
            except Exception:
                pass

        # ---- Phase 4: VALIDATE ----

        # ── LSP Type Check (fast, incremental) ──
        _lsp_result = None
        try:
            from backend.core.ouroboros.governance.lsp_checker import LSPTypeChecker
            _lsp = LSPTypeChecker(project_root=self._config.project_root)
            if _lsp.detect_checker_sync():
                _changed = [str(self._config.project_root / f) for f in ctx.target_files]
                _lsp_result = await asyncio.get_event_loop().run_in_executor(
                    None, _lsp.check_incremental, _changed,
                )
                if not _lsp_result.passed:
                    logger.info(
                        "[Orchestrator] LSP found %d type errors in %s",
                        _lsp_result.error_count, list(ctx.target_files)[:3],
                    )
        except Exception:
            logger.debug("[Orchestrator] LSP check skipped", exc_info=True)

        # ── Exploration-first enforcement ──
        # Verify the model explored (read_file, search_code, get_callers)
        # before proposing writes.  Soft gate: warn + flag, don't reject.
        _EXPLORATION_TOOLS = frozenset({"read_file", "search_code", "get_callers"})
        _min_explore = int(os.environ.get("JARVIS_MIN_EXPLORATION_CALLS", "2"))
        _exploration_count = 0
        _exploration_first_ok = True
        if generation.tool_execution_records:
            for _rec in generation.tool_execution_records:
                _tname = getattr(_rec, "tool_name", "")
                if _tname in _EXPLORATION_TOOLS:
                    _exploration_count += 1
            if _exploration_count < _min_explore:
                _exploration_first_ok = False
                logger.warning(
                    "[Orchestrator] Exploration-first violation: %d/%d exploration calls "
                    "(expected >= %d) for op %s — candidate may lack codebase context",
                    _exploration_count, len(generation.tool_execution_records),
                    _min_explore, ctx.op_id[:12],
                )

        best_candidate: Optional[Dict[str, Any]] = None
        best_validation: Optional[ValidationResult] = None
        validate_retries_remaining = self._config.max_validate_retries

        for _ in range(1 + self._config.max_validate_retries):
            # Compute remaining budget from pipeline_deadline
            if ctx.pipeline_deadline is not None:
                remaining_s = (
                    ctx.pipeline_deadline - datetime.now(tz=timezone.utc)
                ).total_seconds()
            else:
                remaining_s = self._config.validation_timeout_s  # fallback

            if remaining_s <= 0.0:
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="validation_budget_exhausted",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "validation_budget_exhausted"},
                )
                return ctx

            # Try all candidates in parallel; pick first that passes
            async def _validate_one(cand: Dict[str, Any]) -> Tuple[Dict[str, Any], "ValidationResult", float]:
                _t0 = time.monotonic()
                _val = await self._run_validation(ctx, cand, remaining_s)
                return (cand, _val, time.monotonic() - _t0)

            _validation_tasks = [_validate_one(c) for c in generation.candidates]
            _validation_results = await asyncio.gather(*_validation_tasks, return_exceptions=True)

            # Process results in candidate order — preserves priority
            _early_return_ctx: Optional[OperationContext] = None
            for _vr in _validation_results:
                if isinstance(_vr, BaseException):
                    logger.debug("[Orchestrator] Candidate validation raised: %s", _vr)
                    continue
                candidate, validation, _validate_duration_s = _vr

                # Per-candidate ledger entry — always, pass or fail
                await self._record_ledger(ctx, OperationState.GATING, {
                    "event": "candidate_validated",
                    "candidate_id": candidate.get("candidate_id", "unknown"),
                    "candidate_hash": candidate.get("candidate_hash", ""),
                    "validation_outcome": "pass" if validation.passed else "fail",
                    "failure_class": validation.failure_class,
                    "duration_s": round(_validate_duration_s, 3),
                    "provider": generation.provider_name,
                    "model": getattr(generation, "model_id", ""),
                    "exploration_first_ok": _exploration_first_ok,
                    "exploration_count": _exploration_count,
                })

                # Heartbeat: validation result for TUI (Manifesto §7)
                try:
                    _val_msg = type("_Msg", (), {
                        "payload": {
                            "phase": "validate",
                            "test_passed": validation.passed,
                            "test_count": getattr(validation, "test_count", 0),
                            "test_failures": getattr(validation, "failure_count", 0),
                            "failure_class": validation.failure_class or "",
                            "validation_output": str(getattr(validation, "output_preview", ""))[:300],
                        },
                        "op_id": ctx.op_id,
                        "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                    })()
                    for _t in getattr(self._stack.comm, "_transports", []):
                        try:
                            await _t.send(_val_msg)
                        except Exception:
                            pass
                except Exception:
                    pass

                # Emit gate event for duplication blocks
                if validation.failure_class == "duplication":
                    try:
                        await self._stack.comm.emit_decision(
                            op_id=ctx.op_id,
                            outcome="blocked",
                            reason_code="duplication",
                            target_files=list(ctx.target_files),
                        )
                    except Exception:
                        pass

                if validation.passed and best_candidate is None:
                    best_candidate = candidate
                    best_validation = validation
                    continue  # still record ledger for remaining, but winner is chosen

                # Infra failure: non-retryable — escalate immediately
                if validation.failure_class == "infra" and _early_return_ctx is None:
                    ctx = ctx.advance(
                        OperationPhase.POSTMORTEM,
                        validation=validation,
                        terminal_reason_code="validation_infra_failure",
                    )
                    await self._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {
                            "reason": "validation_infra_failure",
                            "failure_class": "infra",
                            "adapter_names_run": list(validation.adapter_names_run),
                            "validation_duration_s": validation.validation_duration_s,
                            "short_summary": validation.short_summary,
                        },
                    )
                    _early_return_ctx = ctx

                # Budget failure: non-retryable
                if validation.failure_class == "budget" and _early_return_ctx is None:
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        validation=validation,
                        terminal_reason_code="validation_budget_exhausted",
                    )
                    await self._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {"reason": "validation_budget_exhausted"},
                    )
                    _early_return_ctx = ctx

                if not validation.passed:
                    # test/build failure: track for ledger; try next candidate
                    best_validation = validation

                    # ---- Record failure in episodic memory + build structured critique ----
                    if _episodic_memory is not None and validation.failure_class in ("test", "build"):
                        try:
                            from backend.core.ouroboros.governance.structured_critique import CritiqueBuilder
                            critique_report = CritiqueBuilder.from_validation_output(
                                file_path=candidate.get("file_path", "unknown"),
                                failure_class=validation.failure_class or "test",
                                error_text=validation.error or "",
                                test_output=validation.short_summary or "",
                            )
                            _episodic_memory.record(
                                file_path=candidate.get("file_path", "unknown"),
                                attempt=self._config.max_validate_retries - validate_retries_remaining + 1,
                                failure_class=validation.failure_class or "test",
                                error_summary=critique_report.summary,
                                specific_errors=[c.what_failed for c in critique_report.critiques],
                                line_numbers=[c.line_number for c in critique_report.critiques if c.line_number],
                            )
                            logger.info(
                                "[Orchestrator] Episodic memory recorded: %s — %s [%s]",
                                candidate.get("file_path", "?"),
                                critique_report.summary,
                                ctx.op_id,
                            )
                        except Exception:
                            logger.debug("[Orchestrator] Episodic/critique recording failed", exc_info=True)

            # If a non-retryable failure was found and no candidate passed, return immediately
            if _early_return_ctx is not None and best_candidate is None:
                return _early_return_ctx

            if best_candidate is not None:
                break  # at least one candidate passed

            # All candidates failed this attempt
            # Short-circuit: if no tests were discovered, retrying is pointless —
            # the same candidates will produce the same 0-test result every time.
            if best_validation is not None and getattr(best_validation, "test_count", -1) == 0:
                logger.info(
                    "[Orchestrator] Skipping retries — no tests discovered for op=%s",
                    ctx.op_id,
                )
                validate_retries_remaining = -1  # fall through to L2 / cancel

            validate_retries_remaining -= 1
            if validate_retries_remaining < 0:
                # ── L2 self-repair dispatch ───────────────────────────────────
                if self._config.repair_engine is not None and best_validation is not None:
                    _pl_deadline = ctx.pipeline_deadline or (
                        datetime.now(timezone.utc) + timedelta(seconds=self._config.generation_timeout_s)
                    )
                    directive = await self._l2_hook(ctx, best_validation, _pl_deadline)
                    if directive[0] == "break":
                        best_candidate, best_validation = directive[1], directive[2]
                        break  # fall through to GATE
                    elif directive[0] in ("cancel", "fatal"):
                        return directive[1]  # ctx was advanced inside _l2_hook
                # ── end L2 dispatch ───────────────────────────────────────────

                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="no_candidate_valid",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason_code": "no_candidate_valid",
                        "candidates_tried": [
                            c.get("candidate_id", "?") for c in generation.candidates
                        ],
                        "failure_class": best_validation.failure_class if best_validation else "test",
                        "adapter_names_run": list(best_validation.adapter_names_run) if best_validation else [],
                        "validation_duration_s": best_validation.validation_duration_s if best_validation else 0.0,
                        "short_summary": best_validation.short_summary if best_validation else "",
                    },
                )
                return ctx

            # ── Micro-Fix: try InteractiveRepair before expensive VALIDATE_RETRY ──
            if self._pre_action_narrator is not None:
                try:
                    await self._pre_action_narrator.narrate_phase(
                        "MICRO_FIX", {"target": list(ctx.target_files)[:1]},
                    )
                except Exception:
                    pass
            try:
                from backend.core.ouroboros.governance.interactive_repair import InteractiveRepairLoop
                _repair = InteractiveRepairLoop(
                    provider=self._generator,
                    project_root=self._config.project_root,
                )
                _repair_target = list(ctx.target_files)[0] if ctx.target_files else None
                if _repair_target:
                    _repair_abs = self._config.project_root / _repair_target
                    if _repair_abs.is_file():
                        _repair_content = _repair_abs.read_text(errors="replace")
                        _test_argv = ["python3", "-m", "pytest", "-x", "-q"]
                        _repair_result = await asyncio.wait_for(
                            _repair.repair(
                                file_path=str(_repair_target),
                                file_content=_repair_content,
                                test_argv=_test_argv,
                                op_id=ctx.op_id,
                            ),
                            timeout=90.0,
                        )
                        if _repair_result.fixed:
                            logger.info(
                                "[Orchestrator] Micro-fix succeeded in %d iterations for op=%s",
                                _repair_result.iterations_used, ctx.op_id,
                            )
                            # Skip full regeneration — advance to GATE
                            ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as _repair_exc:
                logger.debug("[Orchestrator] Micro-fix failed: %s", _repair_exc)

            # Retry: advance to VALIDATE_RETRY with episodic memory context
            _vr_kwargs = {}
            if _episodic_memory is not None and _episodic_memory.has_failures():
                _vr_context = _episodic_memory.format_for_prompt()
                if _vr_context:
                    _existing_vr = getattr(ctx, "strategic_memory_prompt", "") or ""
                    _vr_kwargs["strategic_memory_prompt"] = (
                        f"{_existing_vr}\n\n{_vr_context}" if _existing_vr else _vr_context
                    )
            ctx = ctx.advance(OperationPhase.VALIDATE_RETRY, **_vr_kwargs)

        assert best_candidate is not None  # guaranteed by loop logic
        assert best_validation is not None

        # Source-drift check: file must not have changed since generation
        drift_hash = self._check_source_drift(best_candidate, self._config.project_root)
        if drift_hash is not None:
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                terminal_reason_code="source_drift_detected",
            )
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason_code": "source_drift_detected",
                "file_path": best_candidate.get("file_path"),
                "expected_source_hash": best_candidate.get("source_hash"),
                "actual_source_hash": drift_hash,
            })
            return ctx

        # Winner traceability ledger entry
        await self._record_ledger(ctx, OperationState.GATING, {
            "event": "validation_complete",
            "winning_candidate_id": best_candidate.get("candidate_id"),
            "winning_candidate_hash": best_candidate.get("candidate_hash"),
            "winning_file_path": best_candidate.get("file_path"),
            "source_hash": best_candidate.get("source_hash"),
            "source_path": best_candidate.get("source_path"),
            "provider": generation.provider_name,
            "model": getattr(generation, "model_id", ""),
            "total_candidates_tried": len(generation.candidates),
        })

        # ── Shadow harness check (soft advisory — never hard-blocks GATE) ──────
        # Evaluates candidate structural integrity before GATE. Uses AST comparison
        # between the candidate's proposed content and itself (firewall-only path).
        # If the harness is disqualified, logs a warning — GATE still proceeds.
        if self._config.shadow_harness is not None and best_candidate is not None:
            import time as _sh_time
            from backend.core.ouroboros.governance.shadow_harness import (
                OutputComparator,
                SideEffectFirewall,
                CompareMode,
            )
            from backend.core.ouroboros.governance.op_context import ShadowResult
            _sh_start = _sh_time.monotonic()
            _violations: list = []
            _confidence = 0.0
            try:
                _content = (
                    best_candidate.get("full_content")
                    or best_candidate.get("unified_diff")
                    or ""
                )
                with SideEffectFirewall():
                    _confidence = OutputComparator().compare(
                        _content, _content, CompareMode.AST
                    )
            except Exception as _sh_exc:
                _violations.append(str(_sh_exc))
                _confidence = 0.0
            _sh_dur = _sh_time.monotonic() - _sh_start
            self._config.shadow_harness.record_run(_confidence)
            _shadow_result = ShadowResult(
                confidence=_confidence,
                comparison_mode="ast",
                violations=tuple(_violations),
                shadow_duration_s=_sh_dur,
                production_match=(_confidence >= 0.7),
                disqualified=self._config.shadow_harness.is_disqualified,
            )
            ctx = ctx.with_shadow_result(_shadow_result)
            if self._config.shadow_harness.is_disqualified:
                logger.warning(
                    "[Orchestrator] ShadowHarness disqualified for op=%s "
                    "(confidence=%.2f, violations=%d) — proceeding to GATE with advisory",
                    ctx.op_id,
                    _confidence,
                    len(_violations),
                )

        # ── Entropy measurement (Pillar 4: Synthetic Soul) ──────────────────
        # Compute CompositeEntropySignal from acute (this generation) +
        # chronic (historical domain) signals. Pure deterministic math.
        try:
            from backend.core.ouroboros.governance.entropy_calculator import (
                compute_acute_signal,
                compute_chronic_signal,
                compute_systemic_entropy,
                build_cognitive_inefficiency_event,
                extract_domain_key,
                EntropyQuadrant,
            )

            # Acute signal: from validation + shadow + retry data
            _shadow_conf = 1.0
            if ctx.shadow is not None:
                _shadow_conf = getattr(ctx.shadow, "confidence", 1.0)

            _critique_errors = 0
            _critique_warnings = 0
            _critique_infos = 0
            if _episodic_memory is not None:
                try:
                    for ep in getattr(_episodic_memory, "_episodes", []):
                        _critique_errors += getattr(ep, "error_count", 0)
                        _critique_warnings += getattr(ep, "warning_count", 0)
                        _critique_infos += getattr(ep, "info_count", 0)
                except Exception:
                    pass

            _acute = compute_acute_signal(
                validation_passed=best_validation.passed,
                critique_errors=_critique_errors,
                critique_warnings=_critique_warnings,
                critique_infos=_critique_infos,
                shadow_confidence=_shadow_conf,
                retries_used=(self._config.max_generate_retries - generate_retries_remaining),
                max_retries=self._config.max_generate_retries,
            )

            # Chronic signal: from LearningBridge history
            _domain_key = extract_domain_key(ctx.target_files, ctx.description)
            _chronic_outcomes: list = []
            if hasattr(self._stack, "learning_bridge") and self._stack.learning_bridge is not None:
                try:
                    _history = await self._stack.learning_bridge.get_domain_history(
                        _domain_key
                    )
                    _chronic_outcomes = _history if _history else []
                except Exception:
                    pass  # No history available — chronic signal stays neutral

            _chronic = compute_chronic_signal(_domain_key, _chronic_outcomes)

            # Fuse into systemic entropy
            _composite = compute_systemic_entropy(_acute, _chronic)

            # Log for observability (Pillar 7)
            logger.info(
                "[Orchestrator] Entropy: acute=%.3f chronic=%.3f systemic=%.3f "
                "quadrant=%s trigger=%s domain=%s (op=%s)",
                _acute.normalized_score, _chronic.normalized_score,
                _composite.systemic_score, _composite.quadrant.value,
                _composite.should_trigger, _domain_key, ctx.op_id,
            )

            # Record in ledger
            await self._record_ledger(ctx, OperationState.GATING, {
                "event": "entropy_measured",
                "acute_score": round(_acute.normalized_score, 4),
                "chronic_score": round(_chronic.normalized_score, 4),
                "systemic_score": round(_composite.systemic_score, 4),
                "quadrant": _composite.quadrant.value,
                "domain_key": _domain_key,
                "should_trigger": _composite.should_trigger,
            })

            # Act on quadrant
            if _composite.quadrant == EntropyQuadrant.IMMEDIATE_TRIGGER:
                # Emit CognitiveInefficiencyEvent to GapSignalBus
                _event = build_cognitive_inefficiency_event(ctx.op_id, _composite)
                try:
                    from backend.neural_mesh.synthesis.gap_signal_bus import (
                        GapSignalBus, CapabilityGapEvent,
                    )
                    _bus = GapSignalBus.get_instance()
                    if _bus is not None:
                        _gap_event = CapabilityGapEvent(
                            goal=ctx.description or "capability gap detected via entropy",
                            task_type=_domain_key,
                            target_app="ouroboros",
                            source="entropy_calculator",
                            resolution_mode="synthesis",
                        )
                        _bus.emit(_gap_event)
                        logger.warning(
                            "[Orchestrator] IMMEDIATE_TRIGGER: CognitiveInefficiencyEvent "
                            "emitted for domain=%s systemic=%.3f (op=%s)",
                            _domain_key, _composite.systemic_score, ctx.op_id,
                        )
                except Exception:
                    logger.debug("[Orchestrator] GapSignalBus emit failed", exc_info=True)

            elif _composite.quadrant == EntropyQuadrant.FALSE_CONFIDENCE:
                # Force sandbox validation even though validation passed
                logger.warning(
                    "[Orchestrator] FALSE_CONFIDENCE: domain=%s has high chronic "
                    "failure rate (%.3f) despite passing validation. "
                    "Recommend sandbox re-verification. (op=%s)",
                    _domain_key, _chronic.failure_rate, ctx.op_id,
                )

        except ImportError:
            pass  # entropy_calculator not available — degrade gracefully
        except Exception:
            logger.debug("[Orchestrator] Entropy computation failed", exc_info=True)

        # Store compact validation result in context; full output is in ledger
        ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)

        # Heartbeat: GATE phase (Manifesto §7)
        try:
            await self._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="gate", progress_pct=75.0,
            )
        except Exception:
            pass

        if _serpent: _serpent.update_phase("GATE")
        # ---- Phase 5: GATE ----
        allowed, reason = self._stack.can_write(
            {"files": list(ctx.target_files)}
        )
        if not allowed:
            logger.warning(
                "[Orchestrator] GATE BLOCKED: can_write=%s for op=%s files=%s",
                reason, ctx.op_id, list(ctx.target_files)[:3],
            )
            ctx = ctx.advance(
                OperationPhase.CANCELLED,
                terminal_reason_code=f"gate_blocked:{reason}",
            )
            await self._record_ledger(
                ctx,
                OperationState.BLOCKED,
                {"reason": f"gate_blocked:{reason}"},
            )
            return ctx

        # ---- Security Review (LLM-as-a-Judge) before APPROVE gate ----
        try:
            from backend.core.ouroboros.governance.security_reviewer import SecurityReviewer, SecurityVerdict
            # Only wire SecurityReviewer with a genuine PrimeClient — the
            # former fallback passed CandidateGenerator / provider objects
            # whose generate(context, deadline) signature crashes SecurityReviewer
            # (TypeError: generate() got an unexpected keyword argument 'prompt').
            # See orchestrator battle test bt-2026-04-10-184157 postmortem.
            _sec_client = getattr(self._stack, "prime_client", None)
            _sec_reviewer = SecurityReviewer(prime_client=_sec_client)
            if _sec_reviewer.is_enabled and best_candidate is not None:
                _sec_result = await _sec_reviewer.review(
                    candidate=best_candidate,
                    target_files=list(ctx.target_files),
                    description=ctx.description,
                )
                if _sec_result.verdict == SecurityVerdict.BLOCK:
                    logger.warning(
                        "[Orchestrator] Security review BLOCKED: %s [%s]",
                        _sec_result.summary, ctx.op_id,
                    )
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="security_review_blocked",
                    )
                    await self._record_ledger(
                        ctx, OperationState.BLOCKED,
                        {"reason": "security_review_blocked", "summary": _sec_result.summary},
                    )
                    return ctx
                elif _sec_result.verdict == SecurityVerdict.WARN:
                    logger.info(
                        "[Orchestrator] Security review WARN: %s [%s]",
                        _sec_result.summary, ctx.op_id,
                    )
                    # Emit proactive alert for security warnings (Manifesto §7)
                    try:
                        _warn_msg = type("_Msg", (), {
                            "payload": {
                                "proactive_alert": True,
                                "alert_title": "Security Review Warning",
                                "alert_body": _sec_result.summary or "Potential security concern detected.",
                                "alert_severity": "warning",
                                "alert_source": "SecurityReviewer",
                            },
                            "op_id": ctx.op_id,
                            "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                        })()
                        for _t in getattr(self._stack.comm, "_transports", []):
                            try:
                                await _t.send(_warn_msg)
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception:
            logger.debug("[Orchestrator] SecurityReviewer not available", exc_info=True)

        # ---- Diff-Aware Similarity Gate (Sub-project C) ----
        if best_candidate is not None:
            try:
                from backend.core.ouroboros.governance.similarity_gate import check_similarity
                _src_content = ""
                if ctx.target_files:
                    _src_path = self._config.project_root / ctx.target_files[0]
                    if _src_path.exists():
                        _src_content = _src_path.read_text(encoding="utf-8", errors="replace")
                # Extract candidate content as a string. best_candidate is a dict with
                # either top-level `full_content` (legacy single-file) or a `files` list
                # (multi-file). Passing the raw dict would crash similarity_gate with
                # AttributeError: 'dict' object has no attribute 'splitlines'.
                _cand_content = ""
                if isinstance(best_candidate, dict):
                    _cand_content = best_candidate.get("full_content", "") or ""
                    if not _cand_content and isinstance(best_candidate.get("files"), list):
                        _target0 = ctx.target_files[0] if ctx.target_files else None
                        for _entry in best_candidate["files"]:
                            if not isinstance(_entry, dict):
                                continue
                            if _target0 is None or _entry.get("file_path") == _target0:
                                _cand_content = _entry.get("full_content", "") or ""
                                if _cand_content:
                                    break
                if _src_content and _cand_content:
                    _sim_reason = check_similarity(_cand_content, _src_content)
                    if _sim_reason is not None:
                        logger.info(
                            "[Orchestrator] GATE similarity escalation: %s [%s]",
                            _sim_reason, ctx.op_id,
                        )
                        if risk_tier is not RiskTier.APPROVAL_REQUIRED:
                            risk_tier = RiskTier.APPROVAL_REQUIRED
                        # Emit gate event for VoiceNarrator
                        try:
                            await self._stack.comm.emit_decision(
                                op_id=ctx.op_id,
                                outcome="escalated",
                                reason_code="similarity_escalation",
                                target_files=list(ctx.target_files),
                            )
                        except Exception:
                            pass
            except Exception:
                logger.debug("[Orchestrator] Similarity gate skipped", exc_info=True)

        # Autonomy tier gate: frozen at submit() to prevent TrustGraduator race.
        # "observe" → force APPROVAL_REQUIRED regardless of risk_tier.
        _frozen_tier = getattr(ctx, "frozen_autonomy_tier", "governed")
        if _frozen_tier == "observe" and risk_tier is not RiskTier.APPROVAL_REQUIRED:
            risk_tier = RiskTier.APPROVAL_REQUIRED
            logger.info(
                "[Orchestrator] GATE: frozen_tier=observe → APPROVAL_REQUIRED; op=%s",
                ctx.op_id,
            )

        # ---- Risk floor override (REPL /risk command) ----
        # JARVIS_RISK_CEILING env var sets the minimum risk tier floor.
        # E.g. /risk notify_apply → everything is at least NOTIFY_APPLY.
        _risk_floor_str = os.environ.get("JARVIS_RISK_CEILING", "")
        if _risk_floor_str:
            _floor_map = {
                "SAFE_AUTO": RiskTier.SAFE_AUTO,
                "NOTIFY_APPLY": RiskTier.NOTIFY_APPLY,
                "APPROVAL_REQUIRED": RiskTier.APPROVAL_REQUIRED,
            }
            _floor = _floor_map.get(_risk_floor_str.upper())
            if _floor is not None and risk_tier.value < _floor.value:
                logger.info(
                    "[Orchestrator] GATE: risk floor %s → escalating %s to %s; op=%s",
                    _risk_floor_str, risk_tier.name, _floor.name, ctx.op_id,
                )
                risk_tier = _floor

        # ---- Phase 5b: NOTIFY_APPLY (Yellow — auto-apply with prominent CLI notice + diff preview) ----
        if risk_tier is RiskTier.NOTIFY_APPLY:
            _reason = getattr(ctx, "risk_reason_code", "notify_apply")
            logger.info(
                "[Orchestrator] GATE: NOTIFY_APPLY (Yellow) — auto-applying with notice; op=%s reason=%s",
                ctx.op_id, _reason,
            )
            try:
                await self._stack.comm.emit_decision(
                    op_id=ctx.op_id,
                    outcome="notify_apply",
                    reason_code=_reason,
                    target_files=list(ctx.target_files),
                )
            except Exception:
                pass

            # Render diff preview in CLI before auto-apply
            _notify_delay_s = float(os.environ.get("JARVIS_NOTIFY_APPLY_DELAY_S", "5"))
            if best_candidate is not None and _notify_delay_s > 0:
                _diff_preview = (
                    best_candidate.get("unified_diff")
                    or best_candidate.get("full_content", "")
                )
                if _diff_preview:
                    # Emit diff via heartbeat so SerpentFlow renders it
                    try:
                        for _t in getattr(self._stack.comm, "_transports", []):
                            try:
                                _preview_msg = type("_Msg", (), {
                                    "payload": {
                                        "phase": "notify_apply_diff",
                                        "diff_preview": str(_diff_preview)[:4000],
                                        "delay_s": _notify_delay_s,
                                        "target_files": list(ctx.target_files),
                                    },
                                    "op_id": ctx.op_id,
                                    "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                                })()
                                await _t.send(_preview_msg)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # Delay window — /reject during this window triggers cancellation
                    logger.info(
                        "[Orchestrator] NOTIFY_APPLY diff preview shown, waiting %.0fs for /reject",
                        _notify_delay_s,
                    )
                    await asyncio.sleep(_notify_delay_s)
                    # Check if user cancelled during the window
                    if self._is_cancel_requested(ctx.op_id):
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="user_rejected_notify_apply",
                        )
                        await self._record_ledger(
                            ctx, OperationState.FAILED,
                            {"reason": "user_rejected_notify_apply"},
                        )
                        return ctx

        # ---- Phase 6: APPROVE (conditional) ----
        if risk_tier is RiskTier.APPROVAL_REQUIRED:
            # New: async PR review path. Opt-in via JARVIS_ORANGE_PR_ENABLED.
            # When enabled, we file a GitHub PR on a review branch instead of
            # blocking the loop. On any failure, we fall back to the existing
            # CLI approval provider path.
            try:
                from backend.core.ouroboros.governance.orange_pr_reviewer import (
                    OrangePRReviewer,
                    is_orange_pr_enabled,
                )
                _orange_pr_on = is_orange_pr_enabled()
            except Exception:
                _orange_pr_on = False

            if _orange_pr_on:
                try:
                    _files_for_pr = self._iter_candidate_files(best_candidate)
                    _reviewer = OrangePRReviewer(self._config.project_root)
                    _pr_result = await _reviewer.create_review_pr(
                        op_id=ctx.op_id,
                        description=ctx.description,
                        files=_files_for_pr,
                        evidence={
                            "risk_tier": risk_tier.name,
                            "target_files": list(ctx.target_files),
                            "file_count": len(_files_for_pr),
                        },
                        risk_tier_name=risk_tier.name,
                    )
                except Exception:
                    logger.exception(
                        "[Orchestrator] Orange PR reviewer raised for op=%s; "
                        "falling back to CLI approval",
                        ctx.op_id,
                    )
                    _pr_result = None

                if _pr_result is not None:
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="pending_pr_review",
                    )
                    await self._record_ledger(
                        ctx,
                        OperationState.GATING,
                        {
                            "event": "orange_pr_created",
                            "pr_url": _pr_result.url,
                            "branch": _pr_result.branch,
                            "base_branch": _pr_result.base_branch,
                            "risk_tier": risk_tier.name,
                        },
                    )
                    logger.info(
                        "[Orchestrator] op=%s handed off to async PR review: %s",
                        ctx.op_id, _pr_result.url,
                    )
                    return ctx
                # Fall through to the CLI approval path on PR creation failure.
                logger.warning(
                    "[Orchestrator] op=%s Orange PR creation failed; "
                    "using CLI approval fallback",
                    ctx.op_id,
                )

            if self._approval_provider is None:
                # No approval provider available -> CANCELLED
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="approval_required_but_no_provider",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "approval_required_but_no_provider"},
                )
                return ctx

            ctx = ctx.advance(OperationPhase.APPROVE)
            await self._record_ledger(
                ctx,
                OperationState.GATING,
                {"waiting_approval": True, "risk_tier": risk_tier.name},
            )

            # Notify via comm channel (TUI + voice will receive this)
            try:
                await self._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id,
                    phase="approve",
                    progress_pct=0.0,
                )
            except Exception:
                logger.debug(
                    "Comm heartbeat failed for op=%s", ctx.op_id, exc_info=True
                )

            request_id = await self._approval_provider.request(ctx)
            decision: ApprovalResult = await self._approval_provider.await_decision(
                request_id, self._config.approval_timeout_s
            )

            if decision.status is ApprovalStatus.EXPIRED:
                ctx = ctx.advance(
                    OperationPhase.EXPIRED,
                    terminal_reason_code="approval_expired",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "approval_expired"},
                )
                return ctx

            if decision.status is ApprovalStatus.REJECTED:
                _reject_reason = getattr(decision, "reason", "") or ""
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="approval_rejected",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "approval_rejected",
                        "approver": decision.approver,
                        "rejection_reason": _reject_reason,
                    },
                )

                # P2.2: Capture rejection as a session lesson so the model
                # learns what the human doesn't want within this session.
                _files_short = ", ".join(
                    p.rsplit("/", 1)[-1] for p in ctx.target_files[:3]
                )
                _reason_tag = _reject_reason[:80] if _reject_reason else "no reason given"
                self._session_lessons.append((
                    "code",
                    f"[REJECTED] {ctx.description[:60]} ({_files_short}) "
                    f"— human rejected: {_reason_tag}. "
                    f"Avoid this approach in future operations.",
                ))
                if len(self._session_lessons) > self._session_lessons_max:
                    self._session_lessons = self._session_lessons[-self._session_lessons_max:]

                # P2.2: Feed rejection into NegativeConstraintStore for
                # cross-session learning (prompt adaptation on similar ops).
                if _reject_reason:
                    try:
                        from backend.core.ouroboros.governance.self_evolution import (
                            NegativeConstraintStore,
                        )
                        from backend.core.ouroboros.governance.entropy_calculator import (
                            extract_domain_key as _rej_edk,
                        )
                        _rej_domain = _rej_edk(ctx.target_files, ctx.description)
                        _ns = NegativeConstraintStore()
                        _ns.add_constraint(
                            _rej_domain,
                            f"Human rejected: {_reject_reason[:120]}",
                            f"Op {ctx.op_id} on {_files_short} was rejected at Iron Gate",
                            source_op_id=ctx.op_id,
                            severity="hard",
                        )
                    except Exception:
                        pass  # Constraint recording is best-effort

                # Task #195: Persist rejection to UserPreferenceStore as a
                # typed FEEDBACK memory. NegativeConstraintStore is domain-
                # keyed (cross-session prompt adaptation); the user-pref
                # memory is human-readable and surfaces in the User
                # Preferences prompt section on any similarly-shaped op.
                if _reject_reason:
                    try:
                        from backend.core.ouroboros.governance.user_preference_memory import (
                            get_default_store,
                        )
                        get_default_store().record_approval_rejection(
                            op_id=ctx.op_id,
                            description=ctx.description,
                            target_files=list(ctx.target_files),
                            reason=_reject_reason,
                            approver=getattr(decision, "approver", "human") or "human",
                        )
                    except Exception:
                        pass  # Postmortem persistence is best-effort

                return ctx

            # APPROVED -- continue to APPLY

        # ── PreActionNarrator: voice WHAT before APPLY ──
        if self._pre_action_narrator is not None:
            try:
                _tf = list(ctx.target_files)[0] if ctx.target_files else "unknown"
                await self._pre_action_narrator.narrate_phase("APPLY", {"target_file": _tf})
            except Exception:
                pass

        # ── Cooperative cancellation check (pre-APPLY) ──
        if self._is_cancel_requested(ctx.op_id):
            ctx = ctx.advance(OperationPhase.CANCELLED, terminal_reason_code="user_cancelled")
            await self._record_ledger(ctx, OperationState.FAILED, {"reason": "user_cancelled"})
            return ctx

        # ---- Phase 7: APPLY ----
        ctx = ctx.advance(OperationPhase.APPLY)

        # ── Pre-APPLY git checkpoint (Manifesto §6: Iron Gate) ──
        _checkpoint = None
        _ckpt_mgr = None
        try:
            from backend.core.ouroboros.governance.workspace_checkpoint import WorkspaceCheckpointManager
            _ckpt_mgr = WorkspaceCheckpointManager(self._config.project_root)
            _checkpoint = await _ckpt_mgr.create_checkpoint(
                ctx.op_id, f"pre-apply: {ctx.description[:80]}"
            )
        except Exception:
            logger.debug("[Orchestrator] Pre-APPLY checkpoint skipped", exc_info=True)

        # Heartbeat: APPLY phase starting (Manifesto §7)
        try:
            _apply_target = list(ctx.target_files)[0] if ctx.target_files else ""
            await self._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="APPLY", progress_pct=80.0,
                target_file=_apply_target,
            )
        except Exception:
            pass

        # Deploy gate: canary preflight before applying changes
        try:
            from backend.core.ouroboros.governance.deploy_gate import DeployGate
            _canary = getattr(self._stack, "canary_controller", None)
            if _canary is not None:
                _gate = DeployGate(canary=_canary)
                _preflight = _gate.preflight(
                    service=ctx.primary_repo,
                    target_files=list(ctx.target_files),
                )
                if not _preflight.passed:
                    logger.warning(
                        "[Orchestrator] DeployGate preflight FAILED: %s [%s]",
                        _preflight.reason, ctx.op_id,
                    )
                    # Don't block — log warning. Gate is advisory until graduation gate passes.
        except Exception:
            logger.debug("[Orchestrator] DeployGate not available", exc_info=True)

        # Cross-repo saga path
        if ctx.cross_repo:
            if "execution_graph" in best_candidate:
                ctx, best_candidate = await self._materialize_execution_graph_candidate(
                    ctx,
                    best_candidate,
                )
            return await self._execute_saga_apply(ctx, best_candidate)

        # ── Stale-exploration guard: check hashes before APPLY ──
        # If a target file was modified by a concurrent operation since GENERATE,
        # the candidate is stale.  Log a warning (soft gate) — the apply proceeds
        # but the ledger records the staleness for future convergence analysis.
        _stale_files: list = []
        if ctx.generate_file_hashes:
            for _ghf, _ghash in ctx.generate_file_hashes:
                if not _ghash:
                    continue  # new file at GENERATE time, skip
                _ghf_path = self._config.project_root / _ghf
                try:
                    _now_hash = hashlib.sha256(_ghf_path.read_bytes()).hexdigest()
                except (OSError, IOError):
                    continue  # file deleted — different problem
                if _now_hash != _ghash:
                    _stale_files.append(_ghf)
            if _stale_files:
                logger.warning(
                    "[Orchestrator] Stale-exploration: %d file(s) changed between GENERATE and APPLY: %s [%s]",
                    len(_stale_files), _stale_files[:3], ctx.op_id[:12],
                )
                await self._record_ledger(ctx, OperationState.APPLYING, {
                    "event": "stale_exploration_detected",
                    "stale_files": _stale_files,
                })

        # ── LiveWorkSensor: don't stomp on human-active files ──
        # If the human is actively editing a target file, defer the autonomous
        # apply. Green/Yellow tiers abort with `human_active`; Orange tier
        # (APPROVAL_REQUIRED) proceeds because the human already approved.
        try:
            from backend.core.ouroboros.governance.live_work_sensor import (
                LiveWorkSensor,
                is_enabled as _lws_enabled,
            )
            if _lws_enabled() and ctx.risk_tier is not RiskTier.APPROVAL_REQUIRED:
                _lws = LiveWorkSensor(self._config.project_root)
                _active_hit: Optional[Tuple[str, str]] = None
                _scan_targets: set[str] = set(ctx.target_files)
                for _cf, _ in self._iter_candidate_files(best_candidate):
                    if _cf:
                        _scan_targets.add(_cf)
                for _tf in sorted(_scan_targets):
                    _is_active, _reason = _lws.is_human_active(str(_tf))
                    if _is_active:
                        _active_hit = (str(_tf), _reason or "human active")
                        break
                if _active_hit is not None:
                    _hit_file, _hit_reason = _active_hit
                    logger.warning(
                        "[Orchestrator] LiveWorkSensor: human is active on %s (%s) — deferring APPLY [%s]",
                        _hit_file, _hit_reason, ctx.op_id[:12],
                    )
                    await self._record_ledger(ctx, OperationState.FAILED, {
                        "reason": "human_active_on_target",
                        "file": _hit_file,
                        "signal": _hit_reason,
                    })
                    ctx = ctx.advance(
                        OperationPhase.POSTMORTEM,
                        terminal_reason_code="human_active_on_target",
                    )
                    await self._publish_outcome(ctx, OperationState.FAILED, "human_active_on_target")
                    return ctx
        except Exception:
            logger.debug("[Orchestrator] LiveWorkSensor check skipped", exc_info=True)

        # Capture pre-apply snapshots for complexity baseline + multi-file rollback.
        # Include ctx.target_files AND every file the candidate proposes — for a
        # multi-file candidate the secondary files may not be in ctx.target_files
        # and we need their pre-state to restore them if any file in the batch
        # fails its apply.
        snapshots: Dict[str, str] = {}
        _snapshot_targets: set[str] = {str(f) for f in ctx.target_files}
        for _cf, _ in self._iter_candidate_files(best_candidate):
            if _cf:
                _snapshot_targets.add(_cf)
        for f in _snapshot_targets:
            fpath = Path(f) if Path(f).is_absolute() else self._config.project_root / f
            if fpath.exists():
                try:
                    snapshots[str(f)] = fpath.read_text(errors="replace")
                except OSError:
                    pass
        if snapshots:
            ctx = ctx.with_pre_apply_snapshots(snapshots)

        # Multi-file candidates go through a batch apply helper with
        # all-or-nothing rollback semantics. Single-file candidates still
        # use the legacy single ChangeRequest path (zero change for them).
        _candidate_files = self._iter_candidate_files(best_candidate)
        if len(_candidate_files) > 1:
            _t_apply = time.monotonic()
            try:
                change_result = await self._apply_multi_file_candidate(
                    ctx, best_candidate, _candidate_files, snapshots,
                )
            except Exception as exc:
                logger.error(
                    "Multi-file change engine raised for %s: %s", ctx.op_id, exc
                )
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="change_engine_error",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "change_engine_error", "error": str(exc), "multi_file": True},
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply)
                await self._publish_outcome(ctx, OperationState.FAILED, "change_engine_error")
                return ctx
            # Single-file fall-through path (change_result is already set).
            change_request = None  # type: ignore[assignment]
        else:
            change_request = self._build_change_request(ctx, best_candidate)
            _t_apply = time.monotonic()
            try:
                change_result = await self._stack.change_engine.execute(change_request)
            except Exception as exc:
                logger.error(
                    "Change engine raised for %s: %s", ctx.op_id, exc
                )
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="change_engine_error",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {"reason": "change_engine_error", "error": str(exc)},
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply)
                await self._publish_outcome(ctx, OperationState.FAILED, "change_engine_error")
                return ctx

        if not change_result.success:
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="change_engine_failed",
                rollback_occurred=change_result.rolled_back,
            )
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {
                    "reason": "change_engine_failed",
                    "rolled_back": change_result.rolled_back,
                },
            )
            self._record_canary_for_ctx(
                ctx, False, time.monotonic() - _t_apply,
                rolled_back=change_result.rolled_back,
            )
            await self._publish_outcome(ctx, OperationState.FAILED, "change_engine_failed")
            return ctx

        # ---- Phase 7.5: INFRASTRUCTURE (deterministic post-APPLY hook) ----
        # Boundary Principle: the agentic layer wrote the file (e.g., requirements.txt).
        # This hook executes the KNOWN consequence (pip install). No inference.
        if self._infra_applicator is not None and self._infra_applicator.is_enabled:
            infra_results = await self._infra_applicator.execute_post_apply(
                modified_files=ctx.target_files,
                op_id=ctx.op_id,
            )
            if infra_results and not self._infra_applicator.all_succeeded(infra_results):
                # Infrastructure operation failed — the file change is correct
                # but the environment didn't accept it. Roll back the file change
                # and mark FAILED so Ouroboros can retry with corrected deps.
                _failed = [r for r in infra_results if not r.success]
                logger.error(
                    "[Orchestrator] Infrastructure hook failed for %s: %s",
                    ctx.op_id,
                    "; ".join(f"{r.file_trigger}: exit={r.exit_code}" for r in _failed),
                )
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="infrastructure_failed",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "infrastructure_failed",
                        "infra_results": [
                            {
                                "file": r.file_trigger,
                                "command": r.command,
                                "exit_code": r.exit_code,
                                "stderr": r.stderr_tail[:500],
                            }
                            for r in _failed
                        ],
                    },
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply)
                await self._publish_outcome(ctx, OperationState.FAILED, "infrastructure_failed")
                return ctx

            # Log successful infra operations for observability
            for r in infra_results:
                logger.info(
                    "[Orchestrator] Infrastructure: %s completed in %.1fs (op=%s)",
                    r.file_trigger, r.duration_s, ctx.op_id,
                )

        if _serpent: _serpent.update_phase("APPLY")
        # ---- Phase 8: VERIFY ----
        if _serpent: _serpent.update_phase("VERIFY")
        ctx = ctx.advance(OperationPhase.VERIFY)

        # Heartbeat: VERIFY phase starting (Manifesto §7)
        try:
            await self._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="verify", progress_pct=92.0,
            )
        except Exception:
            pass

        await self._record_ledger(
            ctx,
            OperationState.APPLIED,
            {"op_id": ctx.op_id},
        )

        # ---- Phase 8a: Scoped post-apply test run ----
        # Run tests scoped to the files that were just modified.  This catches
        # regressions *before* the broader benchmark gate and can route failures
        # into L2 repair instead of immediate rollback.
        _verify_test_passed = True
        _verify_test_total = 0
        _verify_test_failures = 0
        _verify_failed_names: Tuple[str, ...] = ()

        if self._validation_runner is not None and ctx.target_files:
            _changed = tuple(
                self._config.project_root / f for f in ctx.target_files
            )
            _files_str = ", ".join(str(f) for f in list(ctx.target_files)[:3])

            # Heartbeat: scoped verify starting (drives ⏺ Verify block in CLI)
            try:
                await self._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id, phase="verify",
                    verify_test_starting=True,
                    verify_target_files=list(ctx.target_files),
                )
            except Exception:
                pass

            _verify_budget_s = min(
                60.0,
                float(os.environ.get("JARVIS_VERIFY_TIMEOUT_S", "60")),
            )
            try:
                _multi = await asyncio.wait_for(
                    self._validation_runner.run(
                        changed_files=_changed,
                        sandbox_dir=None,
                        timeout_budget_s=_verify_budget_s,
                        op_id=ctx.op_id,
                    ),
                    timeout=_verify_budget_s + 5.0,
                )
                _verify_test_passed = _multi.passed
                for _ar in _multi.adapter_results:
                    _verify_test_total += _ar.test_result.total
                    _verify_test_failures += _ar.test_result.failed
                    _verify_failed_names += _ar.test_result.failed_tests
                # 0/0 → N/A, not failure. When no test adapter has any tests
                # for the changed files (deps-only changes, docs, configs),
                # treat verify as a no-op rather than routing to L2 repair.
                # Manifesto §6: only real signals trigger neuroplasticity.
                if _verify_test_total == 0 and _verify_test_failures == 0:
                    _verify_test_passed = True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("[Orchestrator] Verify scoped test timed out [%s]", ctx.op_id)
                _verify_test_passed = False
                _verify_test_failures = 1
            except BlockedPathError:
                pass  # security gate — skip scoped verify, let benchmark handle
            except Exception as exc:
                logger.debug("[Orchestrator] Verify scoped test error: %s", exc)

            # Heartbeat: scoped verify result (drives ⏺ Verify result in CLI)
            try:
                await self._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id, phase="verify",
                    verify_test_passed=_verify_test_passed,
                    verify_test_total=_verify_test_total,
                    verify_test_failures=_verify_test_failures,
                    verify_target_files=list(ctx.target_files),
                )
            except Exception:
                pass

            # On failure: attempt L2 repair before rollback
            if not _verify_test_passed and self._config.repair_engine is not None:
                logger.info(
                    "[Orchestrator] VERIFY test failed (%d/%d) — routing to L2 repair [%s]",
                    _verify_test_failures, _verify_test_total, ctx.op_id,
                )
                _pl_deadline = ctx.pipeline_deadline or (
                    datetime.now(timezone.utc) + timedelta(seconds=60)
                )
                # Build a synthetic ValidationResult for L2
                _synth_val = ValidationResult(
                    passed=False,
                    best_candidate=best_candidate,
                    validation_duration_s=0.0,
                    error=f"post-apply verify: {_verify_test_failures}/{_verify_test_total} failing",
                    failure_class="test",
                    short_summary=f"verify: {', '.join(_verify_failed_names[:3])}",
                    adapter_names_run=(),
                )
                try:
                    directive = await self._l2_hook(ctx, _synth_val, _pl_deadline)
                    if directive[0] == "break":
                        # L2 converged — apply the repair candidate to real files,
                        # then mark verify as passed.  Without this step, the L2
                        # candidate is validated in sandbox but never written to disk.
                        _l2_candidate = directive[1]
                        _l2_change = self._build_change_request(ctx, _l2_candidate)
                        try:
                            _l2_result = await self._stack.change_engine.execute(_l2_change)
                            if _l2_result.success:
                                _verify_test_passed = True
                                _verify_test_failures = 0
                                logger.info(
                                    "[Orchestrator] L2 repair applied in VERIFY phase [%s]",
                                    ctx.op_id,
                                )
                            else:
                                logger.warning(
                                    "[Orchestrator] L2 repair candidate failed to apply [%s]",
                                    ctx.op_id,
                                )
                        except Exception as _apply_exc:
                            logger.debug("[Orchestrator] L2 repair apply error: %s", _apply_exc)
                    elif directive[0] in ("cancel", "fatal"):
                        # L2 decided to escape. _l2_hook has already advanced
                        # ctx to the phase-appropriate terminal (POSTMORTEM
                        # from VERIFY per _l2_escape_terminal) and recorded a
                        # ledger entry. Capture the terminal ctx and return
                        # immediately — continuing VERIFY logic (benchmark,
                        # verify gate, rollback) on a terminal ctx would
                        # violate the FSM and produce spurious transitions.
                        ctx = directive[1]
                        logger.info(
                            "[Orchestrator] L2 escaped VERIFY phase — "
                            "op ctx advanced to %s [%s]",
                            ctx.phase.name, ctx.op_id,
                        )
                        return ctx
                except Exception as _l2_exc:
                    # Log the failure as a one-liner instead of a full traceback;
                    # the exception path is handled inside _l2_hook which already
                    # advances ctx to POSTMORTEM.
                    logger.debug(
                        "[Orchestrator] L2 repair in VERIFY failed: %s: %s",
                        type(_l2_exc).__name__, _l2_exc,
                    )

        ctx = await self._run_benchmark(ctx, [])

        # ---- Verify Gate: enforce regression thresholds (Sub-project C) ----
        _verify_error = None
        try:
            from backend.core.ouroboros.governance.verify_gate import (
                enforce_verify_thresholds,
                rollback_files,
            )
            _br = getattr(ctx, "benchmark_result", None)
            if _br is not None:
                _baseline_cov = None
                _snapshots = getattr(ctx, "pre_apply_snapshots", {})
                if isinstance(_snapshots, dict):
                    _baseline_cov = _snapshots.get("_coverage_baseline")
                _verify_error = enforce_verify_thresholds(_br, baseline_coverage=_baseline_cov)
        except Exception as exc:
            logger.debug("[Orchestrator] Verify gate skipped: %s", exc)

        # Combine scoped-test failure with benchmark regression
        if _verify_error is None and not _verify_test_passed:
            _verify_error = f"scoped verify: {_verify_test_failures}/{_verify_test_total} tests failing"

        if _verify_error is not None:
            logger.warning(
                "[Orchestrator] VERIFY regression gate fired: %s [%s]",
                _verify_error, ctx.op_id,
            )
            # Emit gate event for VoiceNarrator
            try:
                await self._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause=f"verify_regression: {_verify_error}",
                    failed_phase="VERIFY",
                    target_files=list(ctx.target_files),
                )
            except Exception:
                pass
            # Rollback files
            try:
                _snapshots = getattr(ctx, "pre_apply_snapshots", {})
                if _snapshots:
                    rollback_files(
                        pre_apply_snapshots=_snapshots,
                        target_files=list(ctx.target_files),
                        repo_root=self._config.project_root,
                    )
            except Exception as exc:
                logger.error("[Orchestrator] Verify rollback failed: %s", exc)

            # Git checkpoint restore as safety net (Manifesto §6: Iron Gate)
            if _checkpoint is not None and _ckpt_mgr is not None:
                try:
                    await _ckpt_mgr.restore_checkpoint(_checkpoint.checkpoint_id)
                    logger.info(
                        "[Orchestrator] Git checkpoint restored: %s [%s]",
                        _checkpoint.checkpoint_id, ctx.op_id,
                    )
                except Exception:
                    logger.debug("[Orchestrator] Checkpoint restore failed", exc_info=True)

            if _serpent: _serpent.update_phase("POSTMORTEM")
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="verify_regression",
                rollback_occurred=True,
            )
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": "verify_regression", "detail": _verify_error, "rollback_occurred": True},
            )
            self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply, rolled_back=True)
            await self._publish_outcome(ctx, OperationState.FAILED, "verify_regression")
            return ctx

        # ---- Phase 8b: Auto-commit (Gap #6 — autonomy loop closer) ----
        # After successful APPLY+VERIFY, commit with structured O+V signature.
        # Commit failures are non-fatal — the change is already applied on disk.
        _committed_hash: Optional[str] = None  # captured for Phase 3a critique below
        try:
            from backend.core.ouroboros.governance.auto_committer import AutoCommitter
            _committer = AutoCommitter(repo_root=self._config.project_root)
            _gen = ctx.generation
            _provider = getattr(_gen, "provider_name", "") if _gen else ""
            _cost = 0.0
            if _gen:
                _in_tok = getattr(_gen, "total_input_tokens", 0) or 0
                _out_tok = getattr(_gen, "total_output_tokens", 0) or 0
                _cost = (_in_tok * 0.0000001 + _out_tok * 0.0000004)  # rough estimate
            _commit_result = await asyncio.wait_for(
                _committer.commit(
                    op_id=ctx.op_id,
                    description=ctx.description,
                    target_files=ctx.target_files,
                    risk_tier=ctx.risk_tier,
                    provider_name=_provider,
                    generation_cost=_cost,
                ),
                timeout=30.0,
            )
            if _commit_result.committed:
                _committed_hash = _commit_result.commit_hash
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id, phase="commit",
                        progress_pct=98.0,
                        commit_hash=_commit_result.commit_hash,
                        commit_pushed=_commit_result.pushed,
                        commit_branch=_commit_result.push_branch,
                    )
                except Exception:
                    pass
                logger.info(
                    "[Orchestrator] Auto-committed %s for op=%s",
                    _commit_result.commit_hash, ctx.op_id,
                )
            elif _commit_result.skipped_reason:
                logger.debug(
                    "[Orchestrator] Auto-commit skipped: %s",
                    _commit_result.skipped_reason,
                )
        except ImportError:
            logger.debug("[Orchestrator] AutoCommitter not available")
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Auto-commit failed for op=%s: %s; "
                "change is applied but not committed",
                ctx.op_id, exc,
            )

        # ---- Phase 8b2: In-process hot-reload (Manifesto §6 RSI loop closer) ----
        # If this op modified one of our hot-reloadable governance modules,
        # reload it now so the next op uses the freshly-fixed code without
        # a process restart. Quarantined modules trigger a restart_pending
        # flag that the harness honors after the current op completes.
        # Fault-isolated — never raises, never alters terminal state.
        if self._hot_reloader is not None:
            try:
                _hr_batch = self._hot_reloader.reload_for_op(
                    op_id=ctx.op_id,
                    target_files=ctx.target_files,
                )
                if _hr_batch.overall_status == "success":
                    _reloaded_names = [
                        o.module_name.rsplit(".", 1)[-1]
                        for o in _hr_batch.outcomes
                        if o.status == "reloaded"
                    ]
                    logger.info(
                        "[Orchestrator] Hot-reloaded %d module(s) for op=%s: %s",
                        len(_reloaded_names), ctx.op_id, _reloaded_names,
                    )
                    try:
                        await self._stack.comm.emit_heartbeat(
                            op_id=ctx.op_id, phase="hot_reload",
                            progress_pct=99.0,
                            reloaded_modules=_reloaded_names,
                            reload_count=self._hot_reloader.reload_count,
                        )
                    except Exception:
                        pass
                elif _hr_batch.overall_status in ("reload_failed", "preflight_failed"):
                    logger.warning(
                        "[Orchestrator] Hot-reload failed for op=%s: %s; "
                        "restart will be queued",
                        ctx.op_id, _hr_batch.restart_reason,
                    )
                elif _hr_batch.restart_required:
                    logger.info(
                        "[Orchestrator] Hot-reload deferred to restart for op=%s: %s",
                        ctx.op_id, _hr_batch.restart_reason,
                    )
            except Exception as exc:
                logger.warning(
                    "[Orchestrator] Hot-reload hook raised for op=%s: %s",
                    ctx.op_id, exc,
                )

        # ---- Phase 8c: Self-critique (Phase 3a — post-VERIFY quality signal) ----
        # Runs cheap DW critique over the applied diff against the original
        # goal. Poor ratings (≤2) persist as FEEDBACK memories for future
        # ops; excellent ratings (=5) reinforce file reputation. Fully
        # non-blocking — every failure mode is swallowed.
        if self._critique_engine is not None:
            try:
                _test_summary = "(no test summary captured)"
                _vr = ctx.validation
                if _vr is not None:
                    _passed = getattr(_vr, "tests_passed", 0) or 0
                    _total = getattr(_vr, "tests_total", 0) or 0
                    if _total:
                        _test_summary = f"{_passed}/{_total} tests passed"
                    elif _passed:
                        _test_summary = f"{_passed} tests passed"
                _critique_result = await asyncio.wait_for(
                    self._critique_engine.critique_op(
                        op_id=ctx.op_id,
                        description=ctx.description,
                        target_files=ctx.target_files,
                        risk_tier=ctx.risk_tier,
                        commit_hash=_committed_hash,
                        test_summary=_test_summary,
                    ),
                    timeout=float(os.environ.get("JARVIS_CRITIQUE_TIMEOUT_S", "30")) + 5.0,
                )
                try:
                    await self._stack.comm.emit_heartbeat(
                        op_id=ctx.op_id,
                        phase="critique",
                        progress_pct=99.0,
                        critique_rating=int(getattr(_critique_result, "rating", 0)),
                        critique_matches_goal=bool(
                            getattr(_critique_result, "matches_goal", True)
                        ),
                        critique_rationale=str(
                            getattr(_critique_result, "rationale", "")
                        )[:200],
                        critique_provider=str(
                            getattr(_critique_result, "provider_name", "")
                        ),
                        critique_parse_ok=bool(
                            getattr(_critique_result, "parse_ok", True)
                        ),
                    )
                except Exception:
                    pass
                # Session lesson: record poor critiques intra-session so
                # retries this session avoid repeating the pattern.
                if (
                    getattr(_critique_result, "parse_ok", False)
                    and getattr(_critique_result, "is_poor", False)
                ):
                    _files_short = ", ".join(
                        p.rsplit("/", 1)[-1] for p in ctx.target_files[:3]
                    )
                    self._session_lessons.append((
                        "code",
                        f"[CRITIQUE POOR {getattr(_critique_result, 'rating', '?')}/5] "
                        f"{ctx.description[:60]} ({_files_short}): "
                        f"{str(getattr(_critique_result, 'rationale', ''))[:120]}",
                    ))
                    if len(self._session_lessons) > self._session_lessons_max:
                        self._session_lessons = (
                            self._session_lessons[-self._session_lessons_max:]
                        )
            except asyncio.TimeoutError:
                logger.info(
                    "[Orchestrator] Self-critique timed out for op=%s — "
                    "non-blocking, continuing to COMPLETE",
                    ctx.op_id,
                )
            except Exception as exc:
                logger.debug(
                    "[Orchestrator] Self-critique failed for op=%s: %s",
                    ctx.op_id, exc,
                )

        if _serpent: _serpent.update_phase("COMPLETE")
        ctx = ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="complete")

        # Heartbeat: COMPLETE (Manifesto §7)
        try:
            await self._stack.comm.emit_heartbeat(
                op_id=ctx.op_id, phase="complete", progress_pct=100.0,
            )
        except Exception:
            pass

        self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
        await self._publish_outcome(ctx, OperationState.APPLIED)
        await self._persist_performance_record(ctx)
        applied_files = [Path(p).resolve() for p in ctx.target_files]
        await self._oracle_incremental_update(applied_files)

        # ── P0 Wiring: Complete ReasoningNarrator + OperationDialogue ────
        if self._reasoning_narrator is not None:
            try:
                self._reasoning_narrator.record_outcome(ctx.op_id, True, "Applied successfully")
                await self._reasoning_narrator.narrate_completion(ctx.op_id)
            except Exception:
                pass
        if self._dialogue_store is not None:
            try:
                _d = self._dialogue_store.get_active(ctx.op_id)
                if _d:
                    _d.add_entry("COMPLETE", "Applied successfully")
                self._dialogue_store.complete_dialogue(ctx.op_id, "success")
            except Exception:
                pass

        # ── RSI Convergence: compute composite score ──────────────────
        if self._rsi_score_function is not None:
            try:
                _score = self._rsi_score_function.compute(
                    op_id=ctx.op_id,
                    test_pass_rate_before=getattr(ctx, "test_pass_rate_before", 0.0),
                    test_pass_rate_after=1.0 if getattr(ctx, "validation_passed", False) else 0.0,
                    coverage_before=getattr(ctx, "coverage_before", 0.0),
                    coverage_after=getattr(ctx, "coverage_after", 0.0),
                    complexity_before=getattr(ctx, "complexity_before", 0.0),
                    complexity_after=getattr(ctx, "complexity_after", 0.0),
                    lint_before=getattr(ctx, "lint_before", 0),
                    lint_after=getattr(ctx, "lint_after", 0),
                    blast_radius_total=getattr(ctx, "blast_radius_total", 0),
                )
                logger.info("[RSI Score] op=%s composite=%.4f", ctx.op_id, _score.composite)
            except Exception:
                logger.debug("RSI score computation failed", exc_info=True)

        # ── Ouroboros Serpent: stop animation ──
        if _serpent:
            try:
                await _serpent.stop(success=True)
            except Exception:
                pass

        return ctx

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_canary_for_ctx(
        self,
        ctx: OperationContext,
        success: bool,
        latency_s: float,
        rolled_back: bool = False,
    ) -> None:
        """Record canary telemetry for every file in ctx.target_files."""
        for f in ctx.target_files:
            self._stack.canary.record_operation(
                file_path=str(f),
                success=success,
                latency_s=latency_s,
                rolled_back=rolled_back,
            )

    async def _publish_outcome(
        self,
        ctx: OperationContext,
        final_state: OperationState,
        error_pattern: Optional[str] = None,
    ) -> None:
        """Publish operation outcome to LearningBridge + SuccessPatternStore.

        Fault-isolated — never raises. Records both failures (LearningBridge)
        and successes (SuccessPatternStore) for the adaptive learning loop.
        """
        if self._stack.learning_bridge is None:
            return
        try:
            outcome = OperationOutcome(
                op_id=ctx.op_id,
                goal=ctx.description,
                target_files=list(ctx.target_files),
                final_state=final_state,
                error_pattern=error_pattern,
            )
            await self._stack.learning_bridge.publish(outcome)
        except Exception:
            logger.exception(
                "[Orchestrator] LearningBridge.publish failed for op %s; outcome not recorded",
                ctx.op_id,
            )

        # P2: Record success patterns for positive feedback loop
        if final_state in (OperationState.APPLIED,):
            try:
                from backend.core.ouroboros.governance.adaptive_learning import (
                    SuccessPatternStore,
                )
                from backend.core.ouroboros.governance.entropy_calculator import (
                    extract_domain_key as _extract_dk,
                )
                _domain = _extract_dk(ctx.target_files, ctx.description)
                _provider = ""
                if ctx.generation is not None:
                    _provider = ctx.generation.provider_name
                _store = SuccessPatternStore()
                _store.record_success(
                    domain_key=_domain,
                    description=ctx.description,
                    target_files=ctx.target_files,
                    provider=_provider,
                    approach_summary=f"Succeeded via {_provider} on {len(ctx.target_files)} files",
                )
                logger.debug(
                    "[Orchestrator] Success pattern recorded: domain=%s provider=%s (op=%s)",
                    _domain, _provider, ctx.op_id,
                )
            except Exception:
                pass  # Positive feedback is best-effort — never block

        # P2.3: Provider performance tracking — model-selection learning.
        # Records (provider, complexity, success, duration) so future routing
        # can prefer the provider that succeeds at this complexity class.
        try:
            from backend.core.ouroboros.governance.adaptive_learning import (
                ProviderPerformanceTracker,
            )
            _provider = ""
            _gen_duration = 0.0
            if ctx.generation is not None:
                _provider = ctx.generation.provider_name
                _gen_duration = ctx.generation.generation_duration_s
            if _provider:
                _complexity = getattr(ctx, "task_complexity", "unknown") or "unknown"
                _is_success = final_state in (OperationState.APPLIED,)
                _tracker = ProviderPerformanceTracker()
                _tracker.record(
                    provider=_provider,
                    complexity=_complexity,
                    success=_is_success,
                    generation_s=_gen_duration,
                )
                _tracker.persist()
                logger.debug(
                    "[Orchestrator] Provider performance: %s/%s/%s (%.1fs)",
                    _provider, _complexity,
                    "OK" if _is_success else "FAIL", _gen_duration,
                )
        except Exception:
            pass  # Provider tracking is best-effort

        # Self-evolution feedback: record outcome for prompt adaptation +
        # negative constraints + evolution tracking
        try:
            from backend.core.ouroboros.governance.self_evolution import (
                RuntimePromptAdapter, NegativeConstraintStore,
                MultiVersionEvolutionTracker,
            )
            from backend.core.ouroboros.governance.entropy_calculator import (
                extract_domain_key as _se_edk,
            )
            _se_domain = _se_edk(ctx.target_files, ctx.description)
            _is_success = final_state in (OperationState.APPLIED,)

            # P0: Record for runtime prompt adaptation
            _pa = RuntimePromptAdapter()
            _pa.record_outcome(
                _se_domain, ctx.op_id, _is_success,
                failure_class=error_pattern or "",
            )

            # P0: Add negative constraint on failure
            if not _is_success and error_pattern:
                _ns = NegativeConstraintStore()
                _ns.add_constraint(
                    _se_domain,
                    f'Avoid pattern that caused "{error_pattern}"',
                    f"Operation {ctx.op_id} failed: {error_pattern}",
                    source_op_id=ctx.op_id,
                    severity="soft",
                )

            # P2: Multi-version evolution tracking
            _evt = MultiVersionEvolutionTracker()
            _evt.record_operation(_is_success, len(ctx.target_files))

            # P2: LearningConsolidator — periodic consolidation of outcomes into rules
            # Accumulates outcomes and consolidates when enough data is available.
            try:
                from backend.core.ouroboros.governance.adaptive_learning import (
                    LearningConsolidator,
                )
                _lc = LearningConsolidator()
                _provider_name = ""
                if ctx.generation is not None:
                    _provider_name = ctx.generation.provider_name
                _outcome_dict = {
                    "domain_key": _se_domain,
                    "success": _is_success,
                    "error_pattern": error_pattern or "",
                    "provider": _provider_name,
                    "target_files": list(ctx.target_files),
                }
                # Buffer outcome in a module-level accumulator; consolidate
                # when the buffer reaches threshold (10 outcomes).
                _CONSOLIDATION_BUFFER.append(_outcome_dict)
                if len(_CONSOLIDATION_BUFFER) >= _CONSOLIDATION_THRESHOLD:
                    _new_rules = _lc.consolidate(list(_CONSOLIDATION_BUFFER))
                    _CONSOLIDATION_BUFFER.clear()
                    if _new_rules:
                        logger.info(
                            "[Orchestrator] LearningConsolidator: %d new rules from %d outcomes",
                            len(_new_rules), _CONSOLIDATION_THRESHOLD,
                        )
            except Exception:
                pass  # Consolidation is best-effort

        except Exception:
            pass  # Self-evolution feedback is best-effort

        # JARVIS Tier 6: Record operation in PersonalityEngine
        _gls = getattr(self._stack, "governed_loop_service", None)
        if _gls is not None:
            _pe = getattr(_gls, "_personality_engine", None)
            if _pe is not None:
                try:
                    _pe.record_operation(_is_success)
                except Exception:
                    pass

            # JARVIS Tier 2: Record alert in EmergencyEngine on failure
            if not _is_success:
                _ee = getattr(_gls, "_emergency_engine", None)
                if _ee is not None:
                    try:
                        from backend.core.ouroboros.governance.emergency_protocols import AlertType
                        _ee.record_alert(
                            AlertType.GENERATION_FAILURE,
                            f"Operation {ctx.op_id} failed: {error_pattern or 'unknown'}",
                            ctx.op_id,
                        )
                    except Exception:
                        pass

        # ── RSI Convergence: check convergence state ──────────────────
        if self._rsi_score_history is not None and self._rsi_convergence_tracker is not None:
            try:
                composites = self._rsi_score_history.get_composite_values()
                if len(composites) >= 5:
                    _report = self._rsi_convergence_tracker.analyze(composites)
                    logger.info(
                        "[RSI Convergence] state=%s slope=%.4f r2_log=%.2f recommendation=%s",
                        _report.state.value, _report.slope,
                        _report.r_squared_log, _report.recommendation,
                    )
            except Exception:
                logger.debug("RSI convergence check failed", exc_info=True)

        # ── RSI Convergence: record technique outcomes ────────────────
        if self._rsi_transition_tracker is not None:
            try:
                from backend.core.ouroboros.governance.transition_tracker import TechniqueOutcome
                _techniques = getattr(ctx, "techniques_applied", [])
                _domain = getattr(ctx, "domain", "unknown")
                _complexity = getattr(ctx, "task_complexity", "unknown")
                _composite = getattr(ctx, "composite_score", 0.5)
                for _tech in _techniques:
                    self._rsi_transition_tracker.record(TechniqueOutcome(
                        technique=_tech, domain=_domain, complexity=_complexity,
                        success=(final_state.value in ("applied", "complete")),
                        composite_score=_composite, op_id=ctx.op_id,
                    ))
            except Exception:
                logger.debug("RSI transition tracking failed", exc_info=True)

        # ── Session Intelligence: record ephemeral lesson ──────────────
        # Each lesson is a (type, text) tuple.  Type is "code" or "infra".
        # Infrastructure failures (timeouts, provider outages) are excluded
        # from generation prompts to avoid poisoning the model with
        # environmentally-caused failures that don't reflect code quality.
        _INFRA_PATTERNS = frozenset({
            "timeout", "connection_error", "budget", "all_providers_exhausted",
            "pypi_timeout", "change_engine_error", "infrastructure_failed",
            "deadline_exceeded", "provider_unavailable", "rate_limited",
        })
        try:
            _files_short = ", ".join(str(f).split("/")[-1] for f in list(ctx.target_files)[:2])
            _err = error_pattern or ""
            _is_infra = any(p in _err.lower() for p in _INFRA_PATTERNS)
            _lesson_type = "infra" if _is_infra else "code"
            if final_state in (OperationState.APPLIED,):
                _lesson_text = f"[OK] {ctx.description[:80]} ({_files_short})"
            else:
                # P1.3: Causal post-mortem — deterministic analysis of what
                # went wrong and what the model should do differently next time.
                _causal = self._causal_postmortem(_err, ctx)
                _lesson_text = (
                    f"[FAIL:{_err or 'unknown'}] {ctx.description[:60]} "
                    f"({_files_short}) — {_causal}"
                )
            self._session_lessons.append((_lesson_type, _lesson_text))
            # Cap to prevent unbounded growth
            if len(self._session_lessons) > self._session_lessons_max:
                self._session_lessons = self._session_lessons[-self._session_lessons_max:]

            # ── Convergence metric: track success rate before/after first lesson ──
            _has_lessons = len(self._session_lessons) > 1  # >1 = lessons exist from prior ops
            if _has_lessons:
                self._ops_after_lesson += 1
                if _is_success:
                    self._ops_after_lesson_success += 1
                # Periodic check: if post-lesson success rate is worse, clear lessons
                if (self._ops_after_lesson > 0
                        and self._ops_after_lesson % self._convergence_check_interval == 0):
                    _pre_rate = (
                        self._ops_before_lesson_success / max(1, self._ops_before_lesson)
                    )
                    _post_rate = (
                        self._ops_after_lesson_success / max(1, self._ops_after_lesson)
                    )
                    if _post_rate < _pre_rate and self._ops_before_lesson >= 3:
                        logger.warning(
                            "[Orchestrator] Session intelligence convergence NEGATIVE: "
                            "pre-lesson %.0f%% (%d/%d) > post-lesson %.0f%% (%d/%d) — clearing lesson buffer",
                            _pre_rate * 100, self._ops_before_lesson_success, self._ops_before_lesson,
                            _post_rate * 100, self._ops_after_lesson_success, self._ops_after_lesson,
                        )
                        self._session_lessons.clear()
                        # Reset counters so the metric starts fresh
                        self._ops_before_lesson = self._ops_after_lesson
                        self._ops_before_lesson_success = self._ops_after_lesson_success
                        self._ops_after_lesson = 0
                        self._ops_after_lesson_success = 0
                    else:
                        logger.info(
                            "[Orchestrator] Session intelligence convergence OK: "
                            "pre-lesson %.0f%% post-lesson %.0f%%",
                            _pre_rate * 100, _post_rate * 100,
                        )
            else:
                self._ops_before_lesson += 1
                if _is_success:
                    self._ops_before_lesson_success += 1
        except Exception:
            pass  # Session lessons are best-effort

    @staticmethod
    def _build_dependency_summary(
        oracle: Any,
        target_files: Sequence[str],
    ) -> str:
        """Build a ~200-token dependency summary from the Oracle graph.

        Queries direct dependents, transitive importers, and blast radius
        for each target file.  The summary is injected into the generation
        prompt so the model avoids breaking downstream consumers.

        Returns empty string if the Oracle is unavailable or target files
        have no dependents.
        """
        if oracle is None or not target_files:
            return ""

        lines: list = []
        seen_files: set = set()

        for raw_path in target_files[:3]:  # Cap at 3 files to stay within budget
            try:
                ctx_info = oracle.get_context_for_improvement(raw_path, max_depth=2)
            except Exception:
                continue

            if not ctx_info.get("found"):
                continue

            risk = ctx_info.get("risk_assessment", {})
            dependents = ctx_info.get("dependents", [])
            related = ctx_info.get("related_files", [])

            if not dependents and not related:
                continue

            # Direct dependents (files that import/call this target)
            dep_paths = []
            for d in dependents[:8]:
                fp = d.get("file_path", "") if isinstance(d, dict) else getattr(d, "file_path", "")
                if fp and fp not in seen_files:
                    dep_paths.append(fp)
                    seen_files.add(fp)

            risk_level = risk.get("risk_level", "low")
            total_affected = risk.get("total_affected", 0)

            file_line = f"**{raw_path}** — risk={risk_level}, {total_affected} affected"
            if dep_paths:
                file_line += f"\n  Dependents: {', '.join(dep_paths[:6])}"
                if len(dep_paths) > 6:
                    file_line += f" (+{len(dep_paths) - 6} more)"
            lines.append(file_line)

        if not lines:
            return ""

        return (
            "## Dependency Impact (from Oracle graph)\n\n"
            "These files import/call your targets. Ensure changes are "
            "backward-compatible or update dependents too.\n\n"
            + "\n".join(lines)
        )

    @staticmethod
    def _causal_postmortem(error_pattern: str, ctx: "OperationContext") -> str:
        """Deterministic causal analysis of a failed operation.

        Maps failure reason codes to actionable lessons the model can use
        in subsequent generations.  No LLM call — pure pattern matching.
        Returns a short (<100 word) causal sentence.
        """
        _err = (error_pattern or "").lower()
        _n_files = len(ctx.target_files)

        # Generation failures
        if "generation_failed" in _err:
            return (
                "All generation attempts failed. The prompt may be too large or "
                "the task too ambiguous. Try: narrower scope, fewer target files, "
                "or split into smaller operations."
            )
        if "tool_loop_max_iterations" in _err:
            return (
                "Model exhausted the tool loop without producing a patch. "
                "It may be over-exploring. Try: more specific task description."
            )
        if "tool_loop_budget_exceeded" in _err:
            return (
                "Accumulated tool context exceeded the prompt budget. "
                "Too many large file reads. Use targeted line ranges instead."
            )

        # Validation failures
        if "no_candidate_valid" in _err:
            return (
                "Generated code failed validation (tests or type checks). "
                "Read the test file first and ensure the patch matches expected behavior."
            )
        if "source_drift" in _err:
            return (
                "Target file changed between generation and application. "
                "Another operation may have modified the same file. "
                "Re-read before patching."
            )
        if "schema_invalid" in _err or "validate_diff" in _err:
            return (
                "Generated output didn't match the expected JSON schema. "
                "Ensure the response contains a valid diff block with correct format."
            )

        # Apply failures
        if "change_engine" in _err:
            return (
                "Patch application failed. The diff likely targets lines that "
                "no longer exist. Use read_file to verify the exact current content "
                "before generating the diff."
            )
        if "stale_diff" in _err:
            return (
                "The diff references content that doesn't match the current file. "
                "Always read_file immediately before generating a patch."
            )

        # Verify failures
        if "verify_regression" in _err:
            return (
                "Post-apply tests regressed. The change broke existing behavior. "
                "Check dependents with search_code/get_callers before modifying "
                "shared functions."
            )

        # Security / gate failures
        if "security_review_blocked" in _err:
            return (
                "Security review blocked the change. Avoid patterns like: "
                "hardcoded secrets, command injection, unsafe deserialization."
            )
        if "gate_blocked" in _err:
            return "File write permission denied. Check file lock state."

        # Budget / provider failures (infra — less actionable but still logged)
        if "budget" in _err or "exhausted" in _err:
            return "Provider budget exhausted. Operation was too expensive."
        if "timeout" in _err or "deadline" in _err:
            return (
                "Operation timed out. Consider: simpler task scope, fewer files, "
                "or check if the provider is under load."
            )

        # Fallback
        if _n_files > 3:
            return (
                f"Failed on {_n_files}-file operation. Multi-file changes are "
                "harder — consider splitting into single-file operations."
            )
        return "Unknown failure. Read target files and check dependents before retrying."

    async def _run_benchmark(
        self,
        ctx: OperationContext,
        applied_files: Sequence[Path],
    ) -> OperationContext:
        """Run PatchBenchmarker. Fault-isolated — never raises, never alters terminal state."""
        if not self._config.benchmark_enabled:
            return ctx
        try:
            # Deferred import: re-binds on every call so ModuleHotReloader
            # changes to patch_benchmarker.py take effect on the next op
            # without a process restart.
            from backend.core.ouroboros.governance.patch_benchmarker import (
                PatchBenchmarker,
            )
            benchmarker = PatchBenchmarker(
                project_root=self._config.project_root,
                timeout_s=self._config.benchmark_timeout_s,
                pre_apply_snapshots=getattr(ctx, "pre_apply_snapshots", {}),
            )
            result = await asyncio.wait_for(
                benchmarker.benchmark(ctx),
                timeout=self._config.benchmark_timeout_s,
            )
            return ctx.with_benchmark_result(result)
        except asyncio.CancelledError:
            logger.debug(
                "[Orchestrator] Benchmark cancelled for op=%s; continuing without metrics",
                ctx.op_id,
            )
            return ctx
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Benchmark failed for op=%s: %s; continuing without metrics",
                ctx.op_id, exc,
            )
            return ctx

    async def _persist_performance_record(self, ctx: OperationContext) -> None:
        """Write PerformanceRecord to persistence. Fault-isolated — never raises."""
        if self._stack.performance_persistence is None:
            return
        try:
            br = getattr(ctx, "benchmark_result", None)
            record = PerformanceRecord(
                model_id=getattr(ctx, "model_id", None) or "unknown",
                task_type=br.task_type if br else "code_improvement",
                difficulty=getattr(ctx, "difficulty", TaskDifficulty.MODERATE),
                success=ctx.phase == OperationPhase.COMPLETE,
                latency_ms=getattr(ctx, "elapsed_ms", 0.0),
                iterations_used=getattr(ctx, "iterations_used", 1),
                code_quality_score=br.quality_score if br else 0.0,
                op_id=ctx.op_id,
                patch_hash=br.patch_hash if br else "",
                pass_rate=br.pass_rate if br else 0.0,
                lint_violations=br.lint_violations if br else 0,
                coverage_pct=br.coverage_pct if br else 0.0,
                complexity_delta=br.complexity_delta if br else 0.0,
            )
            await self._stack.performance_persistence.save_record(record)
        except Exception as exc:
            logger.warning(
                "[Orchestrator] PerformanceRecord persist failed for op=%s: %s",
                ctx.op_id, exc,
            )

    async def _oracle_incremental_update(
        self,
        applied_files: Sequence[Path],
    ) -> None:
        """Notify Oracle of changed files after successful COMPLETE. Fault-isolated — never raises."""
        oracle = getattr(self._stack, "oracle", None)
        if oracle is None:
            return
        try:
            async with self._oracle_update_lock:
                # P1-6: shielded_wait_for — oracle index is a must-complete write.
                # Cancellation leaves the index partially stale; shielding lets the
                # update finish in the background while we surface TimeoutError.
                from backend.core.async_safety import shielded_wait_for as _shielded_wf
                await _shielded_wf(
                    oracle.incremental_update(applied_files),
                    timeout=30.0,
                    name="oracle.incremental_update",
                )
        except asyncio.TimeoutError:
            logger.warning(
                "[Orchestrator] Oracle incremental_update timed out (>30s); "
                "update continues in background"
            )
        except asyncio.CancelledError:
            pass  # swallow — oracle update is non-blocking; don't abort COMPLETE
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Oracle incremental_update failed: %s", exc
            )

    def _build_profile(self, ctx: OperationContext) -> OperationProfile:
        """Build an OperationProfile from the context's target files.

        Uses conservative defaults for blast radius and security surface
        detection since the orchestrator doesn't have deep code analysis.
        Real implementations would enrich this via blast-radius adapters.
        """
        target_paths = [Path(f) for f in ctx.target_files]

        # Conservative heuristics for profile fields
        touches_supervisor = any(
            "supervisor" in str(p).lower() for p in target_paths
        )
        touches_security = any(
            any(kw in str(p).lower() for kw in ("auth", "secret", "cred", "token", "encrypt"))
            for p in target_paths
        )
        is_core = any(
            any(kw in str(p).lower() for kw in ("router", "controller", "engine", "orchestrator"))
            for p in target_paths
        )

        return OperationProfile(
            files_affected=target_paths,
            change_type=ChangeType.MODIFY,
            blast_radius=len(target_paths),
            crosses_repo_boundary=False,
            touches_security_surface=touches_security,
            touches_supervisor=touches_supervisor,
            test_scope_confidence=0.8,
            is_dependency_change=False,
            is_core_orchestration_path=is_core,
        )

    @staticmethod
    def _ast_preflight(content: str) -> Optional[str]:
        """Return a short error string if content fails ast.parse, else None.

        Parameters
        ----------
        content:
            Python source code to parse.

        Returns
        -------
        Optional[str]
            ``None`` if the content parses cleanly, or a human-readable error
            string (e.g. ``"SyntaxError: invalid syntax (<unknown>, line 1)"``).
        """
        try:
            ast.parse(content)
            return None
        except SyntaxError as exc:
            return f"SyntaxError: {exc}"

    @staticmethod
    def _check_source_drift(
        candidate: Dict[str, Any],
        project_root: Path,
    ) -> Optional[str]:
        """Return None if source unchanged; return current hash if drift detected.

        Compares candidate["source_hash"] (hash at generation time) against the
        current file content hash.  Returns None if no source_hash recorded
        (skip check) or file not found (let APPLY handle).

        Parameters
        ----------
        candidate:
            Candidate dict containing ``source_hash`` (hash at generation time)
            and ``file_path`` (relative path from project root).
        project_root:
            Root directory of the project being modified.

        Returns
        -------
        Optional[str]
            ``None`` if no drift (source unchanged or check skipped), or the
            current file's SHA-256 hex digest if drift was detected.
        """
        import hashlib as _hl
        source_hash = candidate.get("source_hash", "")
        if not source_hash:
            return None  # nothing to compare — skip
        file_path = project_root / candidate.get("file_path", "")
        try:
            current_content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None  # file not found — let APPLY handle
        current_hash = _hl.sha256(current_content.encode()).hexdigest()
        return current_hash if current_hash != source_hash else None

    async def _materialize_execution_graph_candidate(
        self,
        ctx: OperationContext,
        candidate: Dict[str, Any],
    ) -> Tuple[OperationContext, Dict[str, Any]]:
        """Execute an L3 execution graph and convert it into saga-ready patches."""
        graph = candidate.get("execution_graph")
        if graph is None:
            return ctx, candidate

        scheduler = self._config.execution_graph_scheduler
        if scheduler is None:
            raise RuntimeError("execution_graph_scheduler_unavailable")

        ctx = ctx.with_execution_graph_metadata(
            execution_graph_id=graph.graph_id,
            execution_plan_digest=graph.plan_digest,
            subagent_count=len(graph.units),
            parallelism_budget=graph.concurrency_limit,
            causal_trace_id=graph.causal_trace_id,
        )

        submitted = await scheduler.submit(graph)
        if not submitted and not scheduler.has_graph(graph.graph_id):
            raise RuntimeError(f"execution_graph_submit_rejected:{graph.graph_id}")

        if ctx.pipeline_deadline is not None:
            timeout_s = max(
                0.1,
                (ctx.pipeline_deadline - datetime.now(tz=timezone.utc)).total_seconds(),
            )
        else:
            timeout_s = max(sum(unit.timeout_s for unit in graph.units), 1.0)

        state = await scheduler.wait_for_graph(graph.graph_id, timeout_s=timeout_s)
        if state.phase.value != "completed":
            raise RuntimeError(
                f"execution_graph_terminal:{state.phase.value}:{state.last_error or 'unknown'}"
            )

        updated = dict(candidate)
        updated["patches"] = scheduler.get_merged_patches(graph.graph_id)
        return ctx, updated

    # Phases where code has already been written to disk. An L2 escape from
    # any of these is a *regression* (disk state diverged from baseline) and
    # must be recorded as POSTMORTEM so the forensic path runs. Escapes from
    # earlier phases have touched no files and can safely be CANCELLED
    # (graceful abort). This set is the single source of truth — any new
    # post-apply phase added to the FSM should be added here once.
    _POST_APPLY_PHASES: frozenset = frozenset({
        OperationPhase.APPLY,
        OperationPhase.VERIFY,
    })

    @classmethod
    def _l2_escape_terminal(cls, current_phase: OperationPhase) -> OperationPhase:
        """Return the appropriate terminal phase for an L2 escape.

        Principle: once code has touched disk (APPLY/VERIFY), an escape is a
        regression requiring forensics → POSTMORTEM. Before that, the op
        hasn't altered any files, so a graceful abort is a user-level
        cancellation → CANCELLED.

        Parameters
        ----------
        current_phase:
            The phase the ctx is in when L2 is invoked.

        Returns
        -------
        OperationPhase
            Either ``POSTMORTEM`` (post-apply) or ``CANCELLED`` (pre-apply).
        """
        if current_phase in cls._POST_APPLY_PHASES:
            return OperationPhase.POSTMORTEM
        return OperationPhase.CANCELLED

    async def _l2_hook(
        self,
        ctx: "OperationContext",
        best_validation: "ValidationResult",
        deadline: datetime,
    ) -> tuple:
        """Run the L2 repair engine; return a directive tuple to the caller.

        Returns:
            ("break", candidate, canonical_val)  → L2 converged; caller breaks to GATE
            ("cancel", ctx)                      → L2 stopped or canonical validate failed; ctx is advanced to the phase-appropriate terminal
            ("fatal", ctx)                       → non-CancelledError exception; ctx is advanced to POSTMORTEM
        Raises:
            asyncio.CancelledError — if engine.run() was cancelled (terminal recorded first)

        The terminal phase chosen for ``cancel``/``fatal`` respects the
        current ctx phase via :meth:`_l2_escape_terminal`:
          • From VALIDATE/VALIDATE_RETRY (pre-apply) → CANCELLED
          • From APPLY/VERIFY (post-apply) → POSTMORTEM
        ``fatal`` classification always routes to POSTMORTEM regardless of
        phase — an engine-level exception is always a forensic event.
        """
        # Snapshot the entry phase up front — we use it for every terminal
        # selection below, even if ctx is later reassigned.
        _entry_phase = ctx.phase
        _escape_terminal = self._l2_escape_terminal(_entry_phase)

        try:
            l2_result = await self._config.repair_engine.run(ctx, best_validation, deadline)
        except asyncio.CancelledError:
            # asyncio cancellation is a forensic event — always POSTMORTEM.
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="l2_cancelled",
            )
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": "l2_cancelled", "entry_phase": _entry_phase.name},
            )
            raise
        except Exception as exc:
            # Engine-level exceptions are always POSTMORTEM (forensic path).
            logger.error("[Orchestrator] L2 engine error: %s", exc, exc_info=True)
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code=f"l2_fatal:{type(exc).__name__}",
            )
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": f"l2_fatal:{type(exc).__name__}", "entry_phase": _entry_phase.name},
            )
            return ("fatal", ctx)

        if l2_result.terminal == "L2_CONVERGED" and l2_result.candidate is not None:
            _remaining_s = (deadline - datetime.now(timezone.utc)).total_seconds()
            canonical_val = await self._run_validation(ctx, l2_result.candidate, _remaining_s)
            if canonical_val.passed:
                await self._record_ledger(ctx, OperationState.SANDBOXING, {
                    "event": "l2_converged",
                    "iterations": len(l2_result.iterations),
                    **l2_result.summary,
                })
                return ("break", l2_result.candidate, canonical_val)
            else:
                # Phase-aware escape: post-apply → POSTMORTEM, pre-apply → CANCELLED.
                ctx = ctx.advance(
                    _escape_terminal,
                    terminal_reason_code="l2_canonical_validate_failed",
                )
                await self._record_ledger(ctx, OperationState.FAILED, {
                    "reason": "l2_canonical_validate_failed",
                    "entry_phase": _entry_phase.name,
                    "terminal": _escape_terminal.name,
                    **l2_result.summary,
                })
                return ("cancel", ctx)

        elif l2_result.terminal == "L2_STOPPED":
            # Phase-aware escape: post-apply → POSTMORTEM, pre-apply → CANCELLED.
            ctx = ctx.advance(
                _escape_terminal,
                terminal_reason_code="l2_stopped",
            )
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason": "l2_stopped",
                "entry_phase": _entry_phase.name,
                "terminal": _escape_terminal.name,
                "stop_reason": l2_result.stop_reason,
                **l2_result.summary,
            })
            return ("cancel", ctx)

        else:  # L2_CONVERGED with no candidate (shouldn't happen in practice)
            # No candidate is an engine invariant violation → POSTMORTEM.
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="l2_no_candidate",
            )
            await self._record_ledger(ctx, OperationState.FAILED, {
                "reason": "l2_no_candidate",
                "entry_phase": _entry_phase.name,
                **l2_result.summary,
            })
            return ("fatal", ctx)

    @staticmethod
    def _iter_candidate_files(
        candidate: Dict[str, Any],
    ) -> list[Tuple[str, str]]:
        """Return every (file_path, full_content) pair this candidate proposes.

        Multi-file support (Manifesto §6 — coordinated architectural changes):
        when a candidate has a ``files`` list, each entry represents one file
        to apply atomically with the others. Otherwise the primary
        ``file_path`` / ``full_content`` pair is the only one.

        The feature is gated by ``JARVIS_MULTI_FILE_GEN_ENABLED`` (default
        ``true``). When disabled, any ``files`` list is ignored and only the
        primary file is returned — the pipeline behaves exactly as before.

        Ordering:
          • Single-file candidates yield ``[(file_path, full_content)]``.
          • Multi-file candidates yield the entries in ``files`` in order,
            so the first entry is the primary / authoritative file and
            subsequent entries are its coordinated siblings.

        Returns
        -------
        list[tuple[str, str]]
            Non-empty list of ``(file_path, full_content)`` pairs. At minimum,
            contains the primary file.
        """
        primary_path = candidate.get("file_path", "") or ""
        primary_content = candidate.get("full_content", "") or ""

        multi_enabled = (
            os.environ.get("JARVIS_MULTI_FILE_GEN_ENABLED", "true").lower()
            not in ("false", "0", "no", "off")
        )
        files_field = candidate.get("files") if multi_enabled else None
        if isinstance(files_field, list) and files_field:
            pairs: list[Tuple[str, str]] = []
            seen: set[str] = set()
            for entry in files_field:
                if not isinstance(entry, dict):
                    continue
                fp = str(entry.get("file_path", "") or "")
                fc = entry.get("full_content", "") or ""
                if not fp or not isinstance(fc, str):
                    continue
                # De-duplicate — if the primary appears in the list, we
                # don't want to process it twice.
                if fp in seen:
                    continue
                seen.add(fp)
                pairs.append((fp, fc))
            if pairs:
                return pairs

        # Fallback: single-file candidate (legacy path).
        return [(primary_path, primary_content)]

    @staticmethod
    def _validate_config_file_format(
        file_path_str: str, content: str,
    ) -> Optional[str]:
        """Deterministic pre-APPLY format check for common config files.

        Manifesto §6 Iron Gate: deterministic perimeter around agentic
        generation. When the model emits requirements.txt, package.json,
        or similar, a single typo or Unicode corruption would otherwise
        only surface at APPLY (pip install, npm install, etc.). This
        check catches malformed configs BEFORE the change reaches disk.

        Returns ``None`` if the file looks well-formed, or a human-readable
        error string if it does not. Unknown file extensions pass through.

        Parameters
        ----------
        file_path_str : str
            Path or basename of the target file (used for extension dispatch).
        content : str
            Full proposed content.
        """
        if not isinstance(content, str):
            return "config_format: content is not a string"

        _name = Path(file_path_str).name.lower()
        _suffix = Path(file_path_str).suffix.lower()

        # requirements.txt family
        if _name.startswith("requirements") and _suffix == ".txt":
            for _lineno, _raw in enumerate(content.splitlines(), start=1):
                _line = _raw.strip()
                if not _line or _line.startswith("#"):
                    continue
                # Strip inline comments
                if " #" in _line:
                    _line = _line.split(" #", 1)[0].strip()
                # Skip directives (-r, -e, --index-url, etc.)
                if _line.startswith("-"):
                    continue
                # Skip URLs and VCS refs
                if "://" in _line or _line.startswith(("git+", "hg+", "bzr+", "svn+")):
                    continue
                # First token is the distribution name — must start with an
                # ASCII letter/digit and contain only PEP 503 normalizable
                # chars (letters, digits, dash, underscore, dot).
                _first = _line.split(";", 1)[0]  # drop environment marker
                # Split on any version/extras separator
                _pkg_name = ""
                for _ch in _first:
                    if _ch.isalnum() or _ch in "-_.":
                        _pkg_name += _ch
                    else:
                        break
                if not _pkg_name:
                    return (
                        f"requirements.txt line {_lineno}: could not parse "
                        f"package name from {_raw[:60]!r}"
                    )
                # Check for non-ASCII codepoints anywhere in the line (the
                # rapidفuzz class of typo). The global ASCII gate also
                # catches this earlier, but belt-and-suspenders is cheap.
                for _ch in _raw:
                    if ord(_ch) > 127:
                        return (
                            f"requirements.txt line {_lineno}: non-ASCII "
                            f"codepoint U+{ord(_ch):04X} — likely typo "
                            f"in package name {_raw[:60]!r}"
                        )
            return None

        # JSON family
        if _suffix in (".json",) or _name in (
            "package.json", "tsconfig.json", "composer.json",
        ):
            import json as _json
            try:
                _json.loads(content)
            except _json.JSONDecodeError as exc:
                return (
                    f"{_name}: invalid JSON at line {exc.lineno} "
                    f"col {exc.colno}: {exc.msg[:120]}"
                )
            return None

        # YAML (only if PyYAML is available; otherwise pass through)
        if _suffix in (".yml", ".yaml"):
            try:
                import yaml as _yaml  # type: ignore  # noqa: PLC0415
                try:
                    _yaml.safe_load(content)
                except _yaml.YAMLError as exc:  # type: ignore[attr-defined]
                    return f"{_name}: invalid YAML: {str(exc)[:180]}"
            except ImportError:
                pass  # yaml not installed — skip check
            return None

        # Unknown extension — pass through (no gate)
        return None

    async def _run_validation(
        self,
        ctx: OperationContext,
        candidate: Dict[str, Any],
        remaining_s: float,
    ) -> ValidationResult:
        """Run the full validation pipeline for a single candidate.

        Steps:
          1. AST preflight (fast, no subprocess)
          2. Budget guard (remaining_s <= 0 → budget failure)
          3. Write candidate to temp sandbox dir
          4. validation_runner.run() with op_id continuity
          5. Map MultiAdapterResult → compact ValidationResult

        The full adapter stdout/stderr is recorded in the ledger separately;
        ValidationResult holds only a ≤300-char summary.

        Parameters
        ----------
        ctx:
            Current operation context (used for op_id tracing).
        candidate:
            Candidate dict with ``file`` and ``content`` keys.
        remaining_s:
            Remaining pipeline budget in seconds.

        Returns
        -------
        ValidationResult
            Compact, immutable result suitable for embedding in the context.
        """
        if "execution_graph" in candidate:
            return ValidationResult(
                passed=True,
                best_candidate=candidate,
                validation_duration_s=0.0,
                error=None,
                failure_class=None,
                short_summary="execution graph accepted for L3 scheduling",
                adapter_names_run=(),
            )

        content = candidate.get("full_content", "")
        target_file_str = candidate.get(
            "file_path",
            str(ctx.target_files[0]) if ctx.target_files else "unknown.py",
        )

        # ── Multi-file expansion ────────────────────────────────────────
        # If the candidate has a `files` list (Manifesto §6 coordinated
        # edits), iterate the AST + duplication + config-format gates
        # over every file, not just the primary. The primary remains the
        # anchor for the single-file legacy runner path.
        _all_files = self._iter_candidate_files(candidate)
        _is_multi_file = len(_all_files) > 1

        # Step 1: AST preflight — fast gate, no subprocess (Python files only).
        # Runs on EVERY file in a multi-file candidate, short-circuiting on the
        # first failure so the retry feedback names the offending file.
        for _fp, _fc in _all_files:
            if not _fp.endswith(".py"):
                continue
            _syntax_error = self._ast_preflight(_fc)
            if _syntax_error:
                _scoped = (
                    f"{_fp}: {_syntax_error}" if _is_multi_file else _syntax_error
                )
                return ValidationResult(
                    passed=False,
                    best_candidate=None,
                    validation_duration_s=0.0,
                    error=_scoped,
                    failure_class="build",
                    short_summary=_scoped[:300],
                    adapter_names_run=(),
                )

        # Step 1b: Duplication guard — check for structural duplication (Python only).
        # Runs on each Python file in a multi-file candidate; every file must be
        # clean for the batch to pass.
        for _fp, _fc in _all_files:
            if not _fp.endswith(".py"):
                continue
            try:
                from backend.core.ouroboros.governance.duplication_checker import check_duplication
                _source_content = ""
                _src_path = Path(_fp)
                if not _src_path.is_absolute():
                    _src_path = self._config.project_root / _src_path
                if _src_path.exists():
                    _source_content = _src_path.read_text(encoding="utf-8", errors="replace")
                if _source_content:
                    _dup_error = check_duplication(_fc, _source_content, _fp)
                    if _dup_error is not None:
                        _scoped = (
                            f"{_fp}: {_dup_error}" if _is_multi_file else _dup_error
                        )
                        return ValidationResult(
                            passed=False,
                            best_candidate=None,
                            validation_duration_s=0.0,
                            error=_scoped,
                            failure_class="duplication",
                            short_summary=_scoped[:300],
                            adapter_names_run=(),
                        )
            except Exception as exc:
                logger.debug("[Orchestrator] Duplication check skipped for %s: %s", _fp, exc)

        # Non-code files (docs, configs, etc.) need no test/syntax runner,
        # but structured config files get a format sanity check so that
        # generation-quality failures (malformed deps, bad JSON, etc.) are
        # caught at VALIDATE instead of blowing up post-APPLY. This is the
        # pre-APPLY deterministic gate described in Manifesto §6.
        _RUNNABLE_EXTENSIONS = {".py", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp"}

        # Config-format gate runs on every non-runnable file. In a multi-file
        # candidate we must catch a bad requirements.txt even when the primary
        # is a .py — otherwise the pip install would still fail post-APPLY.
        for _fp, _fc in _all_files:
            if Path(_fp).suffix in _RUNNABLE_EXTENSIONS:
                continue
            _cfg_err = self._validate_config_file_format(_fp, _fc)
            if _cfg_err is not None:
                logger.warning(
                    "[Orchestrator] config-format gate rejected %s: %s",
                    _fp, _cfg_err[:160],
                )
                return ValidationResult(
                    passed=False,
                    best_candidate=None,
                    validation_duration_s=0.0,
                    error=_cfg_err,
                    failure_class="build",
                    short_summary=f"config-format: {_cfg_err[:240]}",
                    adapter_names_run=(),
                )

        # If NO file in the candidate is code (.py/.cpp/etc.), there's
        # nothing for the runner to execute — pass through after the
        # format gates above. For mixed candidates (some code + some
        # config) we still run the runner on the code files below.
        if not any(Path(_fp).suffix in _RUNNABLE_EXTENSIONS for _fp, _ in _all_files):
            return ValidationResult(
                passed=True,
                best_candidate=candidate,
                validation_duration_s=0.0,
                error=None,
                failure_class=None,
                short_summary="validation skipped: non-code file (format-checked)",
                adapter_names_run=(),
            )

        # When no runner is configured, skip test execution (dry-run / test mode)
        if self._validation_runner is None:
            return ValidationResult(
                passed=True,
                best_candidate=candidate,
                validation_duration_s=0.0,
                error=None,
                failure_class=None,
                short_summary="validation skipped: no runner configured",
                adapter_names_run=(),
            )

        # Step 2: Budget guard
        if remaining_s <= 0.0:
            return ValidationResult(
                passed=False,
                best_candidate=None,
                validation_duration_s=0.0,
                error="pipeline budget exhausted before validation",
                failure_class="budget",
                short_summary="Budget exhausted",
                adapter_names_run=(),
            )

        # Step 3: Write to temp sandbox
        # For a multi-file candidate we write every file preserving its
        # relative path under the sandbox root, so cross-file imports can
        # resolve during the runner's syntax / test pass. Only code files
        # become `changed_files` for the runner — configs are already
        # validated by the format gate above and don't need execution.
        multi = None
        t0 = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="ouroboros_validate_") as sandbox_str:
            sandbox = Path(sandbox_str)
            runner_changed: list[Path] = []
            for _fp, _fc in _all_files:
                _rel = Path(_fp)
                if _rel.is_absolute():
                    # Collapse absolute paths into just the basename to keep
                    # everything under the sandbox root. This preserves the
                    # legacy single-file behaviour for absolute inputs.
                    _sandbox_file = sandbox / _rel.name
                else:
                    _sandbox_file = sandbox / _rel
                _sandbox_file.parent.mkdir(parents=True, exist_ok=True)
                _sandbox_file.write_text(_fc, encoding="utf-8")
                if _sandbox_file.suffix in _RUNNABLE_EXTENSIONS:
                    runner_changed.append(_sandbox_file)

            # Safety net: if nothing qualifies for the runner (shouldn't
            # happen — the all-non-code early return would have fired),
            # fall back to the primary file path anchor.
            if not runner_changed:
                _primary_rel = Path(target_file_str)
                _primary_file = sandbox / (_primary_rel.name if _primary_rel.is_absolute() else _primary_rel)
                runner_changed = [_primary_file]

            # Step 4: Run LanguageRouter (or any duck-typed runner)
            try:
                multi = await self._validation_runner.run(
                    changed_files=tuple(runner_changed),
                    sandbox_dir=sandbox,
                    timeout_budget_s=remaining_s,
                    op_id=ctx.op_id,
                )
            except BlockedPathError as exc:
                # Security gate rejection → failure_class="security" → CANCELLED (not POSTMORTEM)
                return ValidationResult(
                    passed=False,
                    best_candidate=None,
                    validation_duration_s=time.monotonic() - t0,
                    error=str(exc),
                    failure_class="security",
                    short_summary=f"BlockedPathError: {str(exc)[:280]}",
                    adapter_names_run=(),
                )
            except Exception as exc:
                return ValidationResult(
                    passed=False,
                    best_candidate=None,
                    validation_duration_s=time.monotonic() - t0,
                    error=str(exc),
                    failure_class="infra",
                    short_summary=f"runner exception: {str(exc)[:200]}",
                    adapter_names_run=(),
                )

        # Step 5: Map to compact ValidationResult (sandbox dir is now cleaned up)
        assert multi is not None
        duration = time.monotonic() - t0
        adapter_names = tuple(r.adapter for r in multi.adapter_results)
        summary_parts = []
        for r in multi.adapter_results:
            tail = (r.test_result.stdout or "")[-150:] if r.test_result else ""
            summary_parts.append(f"[{r.adapter}:{'PASS' if r.passed else 'FAIL'}] {tail}")
        short_summary = " | ".join(summary_parts)[:300]

        return ValidationResult(
            passed=multi.passed,
            best_candidate=candidate if multi.passed else None,
            validation_duration_s=duration,
            error=None if multi.passed else f"validation failed: {multi.failure_class}",
            failure_class=None if multi.passed else multi.failure_class,
            short_summary=short_summary,
            adapter_names_run=adapter_names,
        )

    def _build_change_request(
        self, ctx: OperationContext, candidate: Dict[str, Any]
    ) -> ChangeRequest:
        """Build a ChangeRequest from the context and best candidate.

        Parameters
        ----------
        ctx:
            The current operation context.
        candidate:
            The validated candidate dict with ``file`` and ``content`` keys.
        """
        target_file = Path(
            candidate.get("file_path", str(ctx.target_files[0] if ctx.target_files else "unknown.py"))
        )
        proposed_content = candidate.get("full_content", "")

        profile = self._build_profile(ctx)

        return ChangeRequest(
            goal=ctx.description,
            target_file=target_file,
            proposed_content=proposed_content,
            profile=profile,
            op_id=ctx.op_id,
        )

    async def _apply_multi_file_candidate(
        self,
        ctx: OperationContext,
        candidate: Dict[str, Any],
        files: list[Tuple[str, str]],
        snapshots: Dict[str, str],
    ) -> ChangeResult:
        """Apply a multi-file candidate atomically.

        Manifesto §6 boundary rule: the agentic layer emits the coordinated
        edits, the deterministic layer applies them. We iterate through the
        files, running each through the existing single-file ChangeEngine
        pipeline (which keeps every per-file gate: risk classification,
        governance lock, verify hook, rollback). If any file fails, every
        previously-applied file in the batch is restored from its pre-apply
        snapshot so the on-disk state matches the pre-batch state.

        This helper explicitly does NOT re-implement the 8-phase pipeline —
        it composes the existing engine so the pipeline's guarantees still
        hold for each file, and adds batch-level rollback on top.

        Parameters
        ----------
        ctx:
            Current operation context.
        candidate:
            The validated best candidate, must contain a non-empty ``files`` list.
        files:
            The ``(file_path, full_content)`` pairs produced by
            ``_iter_candidate_files``. At least one entry (guaranteed by caller).
        snapshots:
            Pre-apply snapshots keyed by file path, used to restore files
            if the batch fails partway through.

        Returns
        -------
        ChangeResult
            A single aggregated result. ``success=True`` only when every file
            applied cleanly. ``rolled_back`` reflects whether any restoration
            was attempted on failure.
        """
        profile = self._build_profile(ctx)
        applied: list[Tuple[str, Path]] = []   # (rel_path, abs_path) of successfully applied files
        last_phase_reached: ChangePhase = ChangePhase.PLAN
        last_risk_tier: Optional[RiskTier] = None
        last_error: Optional[str] = None

        for idx, (fp, fc) in enumerate(files):
            # Build an absolute target path anchored at the project root.
            _rel = Path(fp)
            _abs = _rel if _rel.is_absolute() else (self._config.project_root / _rel)

            _per_file_request = ChangeRequest(
                goal=f"{ctx.description} [multi-file {idx + 1}/{len(files)}: {fp}]",
                target_file=_abs,
                proposed_content=fc,
                profile=profile,
                op_id=f"{ctx.op_id}::{idx:02d}",
            )

            try:
                per_result = await self._stack.change_engine.execute(_per_file_request)
            except Exception as exc:
                logger.error(
                    "[Orchestrator] Multi-file apply: file %d/%d (%s) raised: %s",
                    idx + 1, len(files), fp, exc,
                )
                per_result = ChangeResult(
                    op_id=_per_file_request.op_id or ctx.op_id,
                    success=False,
                    phase_reached=last_phase_reached,
                    rolled_back=False,
                    error=f"change_engine_raise: {exc}",
                )

            last_phase_reached = per_result.phase_reached
            if per_result.risk_tier is not None:
                last_risk_tier = per_result.risk_tier

            if per_result.success:
                applied.append((fp, _abs))
                continue

            # ── Failure — roll back every previously-applied file ──
            last_error = (
                f"multi_file_apply failed on {fp} "
                f"(file {idx + 1}/{len(files)}): {per_result.error or 'unknown'}"
            )
            logger.error("[Orchestrator] %s", last_error)
            rolled_back_any = False
            for done_fp, done_abs in applied:
                if done_fp in snapshots:
                    try:
                        done_abs.parent.mkdir(parents=True, exist_ok=True)
                        done_abs.write_text(snapshots[done_fp], encoding="utf-8")
                        rolled_back_any = True
                        logger.info(
                            "[Orchestrator] Multi-file rollback: restored %s", done_fp,
                        )
                    except OSError as _restore_exc:
                        logger.error(
                            "[Orchestrator] Multi-file rollback FAILED for %s: %s",
                            done_fp, _restore_exc,
                        )
                else:
                    # No snapshot = file was new in this batch; unlink to undo creation.
                    try:
                        if done_abs.exists():
                            done_abs.unlink()
                            rolled_back_any = True
                            logger.info(
                                "[Orchestrator] Multi-file rollback: removed new file %s", done_fp,
                            )
                    except OSError as _unlink_exc:
                        logger.error(
                            "[Orchestrator] Multi-file rollback unlink FAILED for %s: %s",
                            done_fp, _unlink_exc,
                        )

            await self._record_ledger(ctx, OperationState.APPLYING, {
                "event": "multi_file_rollback",
                "failed_file": fp,
                "failed_index": idx,
                "total_files": len(files),
                "rolled_back_count": len(applied),
                "rolled_back_any": rolled_back_any,
            })

            return ChangeResult(
                op_id=ctx.op_id,
                success=False,
                phase_reached=last_phase_reached,
                risk_tier=last_risk_tier,
                rolled_back=rolled_back_any or per_result.rolled_back,
                error=last_error,
            )

        # All files applied cleanly — return aggregated success.
        await self._record_ledger(ctx, OperationState.APPLYING, {
            "event": "multi_file_apply_complete",
            "file_count": len(files),
            "files": [fp for fp, _ in files],
        })
        return ChangeResult(
            op_id=ctx.op_id,
            success=True,
            phase_reached=last_phase_reached,
            risk_tier=last_risk_tier,
            rolled_back=False,
            error=None,
        )

    async def _execute_saga_apply(
        self,
        ctx: OperationContext,
        best_candidate: dict,
    ) -> OperationContext:
        """Execute multi-repo saga apply + three-tier verify.

        Selected when ctx.cross_repo is True. Single-repo path is unchanged.
        """
        # Build patch_map from best_candidate["patches"] or fall back to empty per-repo patches
        patch_map: Dict[str, RepoPatch] = {}
        if best_candidate and "patches" in best_candidate:
            patch_map = best_candidate["patches"]
        else:
            for repo in ctx.repo_scope:
                patch_map[repo] = RepoPatch(repo=repo, files=())

        # Resolve per-repo filesystem roots from registry (fallback to project_root)
        repo_roots = self._config.resolve_repo_roots(
            repo_scope=ctx.repo_scope,
            op_id=ctx.op_id,
        )

        strategy = SagaApplyStrategy(
            repo_roots=repo_roots,
            ledger=self._stack.ledger,
            message_bus=getattr(self._config, "message_bus", None),
            branch_isolation=os.environ.get(
                "JARVIS_SAGA_BRANCH_ISOLATION", "false"
            ).lower() in ("1", "true", "yes"),
            keep_failed_saga_branches=os.environ.get(
                "JARVIS_SAGA_KEEP_FORENSICS_BRANCHES", "true"
            ).lower() in ("1", "true", "yes"),
        )
        _t_saga = time.monotonic()
        apply_result = await strategy.execute(ctx, patch_map)

        if apply_result.terminal_state == SagaTerminalState.SAGA_ABORTED:
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code=apply_result.reason_code,
            )
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id},
            )
            self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
            await self._publish_outcome(ctx, OperationState.FAILED, apply_result.reason_code)
            return ctx

        if apply_result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED:
            verifier = CrossRepoVerifier(
                repo_roots=repo_roots,
            )
            verify_result = await verifier.verify(
                repo_scope=ctx.repo_scope,
                patch_map=patch_map,
                dependency_edges=ctx.dependency_edges,
            )

            if not verify_result.passed:
                comp_ok = await strategy.compensate_after_verify_failure(
                    saga_result=apply_result,
                    patch_map=patch_map,
                    op_id=ctx.op_id,
                    reason_code=verify_result.reason_code,
                )
                # Emit SAGA_FAILED to bus if available
                _bus = getattr(strategy, "_bus", None)
                if _bus is not None:
                    try:
                        from backend.core.ouroboros.governance.autonomy.saga_messages import (
                            SagaMessage, SagaMessageType, MessagePriority,
                        )
                        _bus.send(SagaMessage(
                            message_type=SagaMessageType.SAGA_FAILED,
                            saga_id=apply_result.saga_id,
                            correlation_id=apply_result.saga_id,
                            priority=MessagePriority.HIGH,
                            payload={
                                "schema_version": "1.0",
                                "op_id": ctx.op_id,
                                "saga_id": apply_result.saga_id,
                                "reason_code": "verify_failed",
                                "failed_phase": "VERIFY",
                            },
                        ))
                    except Exception:
                        pass
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code=verify_result.reason_code,
                    rollback_occurred=comp_ok,
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": verify_result.reason_code,
                        "saga_id": apply_result.saga_id,
                        "compensated": comp_ok,
                    },
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
                await self._publish_outcome(ctx, OperationState.FAILED, verify_result.reason_code)
                return ctx

            # B+ mode: promote ephemeral branches before declaring success
            promote_state, promoted_shas = await strategy.promote_all(
                apply_order=list(ctx.repo_scope),
                saga_id=apply_result.saga_id,
                op_id=ctx.op_id,
            )

            if promote_state == SagaTerminalState.SAGA_PARTIAL_PROMOTE:
                try:
                    await self._stack.comm.emit_postmortem(
                        op_id=ctx.op_id,
                        root_cause="saga_partial_promote",
                        failed_phase="PROMOTE",
                        next_safe_action="human_intervention_required",
                    )
                except Exception:
                    pass
                try:
                    await self._stack.controller.pause(scope="cross_repo_saga")
                except TypeError:
                    await self._stack.controller.pause()
                except Exception:
                    logger.exception(
                        "[Orchestrator] controller.pause() failed for partial promote %s",
                        ctx.op_id,
                    )
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="saga_partial_promote",
                )
                await self._record_ledger(
                    ctx, OperationState.FAILED,
                    {"reason": "saga_partial_promote", "saga_id": apply_result.saga_id, "promoted_repos": promoted_shas},
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
                await self._publish_outcome(ctx, OperationState.FAILED, "saga_partial_promote")
                return ctx

            # SAGA_SUCCEEDED
            ctx = ctx.advance(OperationPhase.VERIFY)
            await self._record_ledger(
                ctx,
                OperationState.APPLIED,
                {"saga_id": apply_result.saga_id},
            )
            ctx = await self._run_benchmark(ctx, [])
            ctx = ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="complete")
            self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_saga)
            await self._publish_outcome(ctx, OperationState.APPLIED)
            await self._persist_performance_record(ctx)
            try:
                saga_applied: Sequence[Path] = [
                    (Path(self._config.repo_registry.get(repo).local_path) / rel_path).resolve()
                    for repo, patch in patch_map.items()
                    for rel_path, _ in patch.new_content
                ] if self._config.repo_registry is not None else []
            except Exception:
                saga_applied = []
            await self._oracle_incremental_update(saga_applied)
            return ctx

        if apply_result.terminal_state == SagaTerminalState.SAGA_STUCK:
            # Compensation failed: data may be inconsistent — emit postmortem
            try:
                await self._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause="saga_stuck",
                    failed_phase="APPLY",
                    next_safe_action="human_intervention_required",
                )
            except Exception:
                pass
            # Halt intake: dirty state requires human review before next op
            try:
                await self._stack.controller.pause()
            except Exception:
                logger.exception(
                    "[Orchestrator] controller.pause() failed for stuck saga %s; "
                    "manual pause may be required",
                    ctx.op_id,
                )
            else:
                logger.warning(
                    "[Orchestrator] Safe pause triggered after SAGA_STUCK on %s",
                    ctx.op_id,
                )
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="saga_stuck",
            )
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id},
            )
            self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
            await self._publish_outcome(ctx, OperationState.FAILED, "saga_stuck")
            return ctx

        # SAGA_ROLLED_BACK: clean rollback — change not applied, system is clean
        # Advance to CANCELLED so the returned context is terminal and explicit.
        ctx = ctx.advance(
            OperationPhase.CANCELLED,
            terminal_reason_code=apply_result.reason_code,
            rollback_occurred=True,
        )
        await self._record_ledger(
            ctx,
            OperationState.FAILED,
            {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id, "rolled_back": True},
        )
        self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga, rolled_back=True)
        await self._publish_outcome(ctx, OperationState.FAILED, apply_result.reason_code)
        return ctx

    async def _record_ledger(
        self,
        ctx: OperationContext,
        state: OperationState,
        data: Dict[str, Any],
    ) -> None:
        """Append a ledger entry, logging errors without raising.

        Awaits the ledger append inline so that entries are committed
        before the pipeline continues.  Errors are logged but never
        propagate -- ledger failures must not crash the pipeline.
        """
        entry = LedgerEntry(
            op_id=ctx.op_id,
            state=state,
            data=data,
        )
        try:
            await self._stack.ledger.append(entry)
        except Exception as exc:
            logger.error(
                "Ledger append failed: op_id=%s state=%s error=%s",
                entry.op_id,
                entry.state.value,
                exc,
            )


# Alias so tests can import `Orchestrator` as well as `GovernedOrchestrator`
Orchestrator = GovernedOrchestrator
