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
import sys
import tempfile
import time
import dataclasses
from dataclasses import asdict as _dc_asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

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

# Grace period added to route-based _gen_timeout for the outer wait_for
# Iron Gate.  The generator may internally refresh the fallback budget to
# _FALLBACK_MIN_GUARANTEED_S (90s) even when the parent deadline is nearly
# exhausted — 5s was too tight and caused 129s Claude streams to be cut
# by the 125s outer gate (bt-2026-04-12-061609).  15s accommodates Tier 0
# overhead + asyncio cancellation propagation delay on streaming responses.
_OUTER_GATE_GRACE_S = float(os.environ.get("JARVIS_OUTER_GATE_GRACE_S", "15"))
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _phase_runner_complete_extracted() -> bool:
    """Slice 1 of Wave 2 (5) — COMPLETE phase extraction gate.

    Reads ``JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED``, **default
    ``true`` as of 2026-04-22 graduation (3 clean soak sessions
    bt-2026-04-22-183425 / -185203 / -190730 + Slice 1 parity
    22/22 byte-identical vs inline). Explicit ``=false`` remains a
    runtime kill switch that reverts to the inline block.**

    When ``true``, ``_run_pipeline`` delegates the COMPLETE block at
    line ~7073 to
    :class:`backend.core.ouroboros.governance.phase_runners.complete_runner.COMPLETERunner`.
    When ``false``, the inline block runs unchanged. Parity tests
    (tests/governance/phase_runner/test_complete_runner_parity.py)
    pin byte-identical observable output across both paths.

    Graduation ledger: ``memory/project_wave2_graduation_matrix.md``.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_route_extracted() -> bool:
    """Slice 3 of Wave 2 (5) — ROUTE phase extraction gate.

    **Default ``true`` as of 2026-04-22 atomic #3 graduation** (3 clean
    soak sessions bt-2026-04-22-214630 / -220234 / -222322, each with
    zero runner-attributed frames + zero shutdown race + 40 total
    ROUTE+CTX+PLAN delegation markers). Flipped together with
    ``_phase_runner_context_expansion_extracted`` and
    ``_phase_runner_plan_extracted`` since the combined gate
    ``_phase_runner_slice3_fully_extracted`` requires all three.
    Explicit ``=false`` on this helper alone remains a per-phase
    kill switch — operator can sever ROUTE without affecting CTX/PLAN.

    Graduation ledger: ``memory/project_wave2_graduation_matrix.md``.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_context_expansion_extracted() -> bool:
    """Slice 3 of Wave 2 (5) — CONTEXT_EXPANSION phase extraction gate.

    **Default ``true`` as of 2026-04-22** (atomic #3 graduation with
    ROUTE + PLAN; see ``_phase_runner_route_extracted`` docstring for
    soak evidence). Explicit ``=false`` kill switch remains.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_CONTEXT_EXPANSION_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_plan_extracted() -> bool:
    """Slice 3 of Wave 2 (5) — PLAN phase extraction gate.

    **Default ``true`` as of 2026-04-22** (atomic #3 graduation with
    ROUTE + CTX; see ``_phase_runner_route_extracted`` docstring).
    Explicit ``=false`` kill switch remains.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_PLAN_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_generate_extracted() -> bool:
    """Slice 5a/5b of Wave 2 (5) — GENERATE phase extraction gate.

    **Default ``true`` as of 2026-04-23 graduation** (3 clean sessions
    under post-Ticket-A1/B/C guards: bt-2026-04-23-062014 (14 markers)
    + bt-2026-04-23-203517 S2′ (12 markers, session_outcome=complete)
    + bt-2026-04-23-210943 S3 (13 markers, session_outcome=complete);
    all three idle_timeout stop, 0 runner-attributed frames, 0 JARVIS
    shutdown race, 0 POSTMORTEMs, 39 total [PhaseRunnerDelegate] GENERATE
    markers). Iron Gate live lines NOT observed across the cadence
    because Anthropic transport weather (canonical signature:
    anthropic/_base_client.py:1637 request → httpx/_transports/default.py:101
    map_httpcore_exceptions) prevented candidates from forming; §6
    depth is attested by the Slice 5a+5b parity oracle (36/36 tests
    green on HEAD 68954cc62d — 12 FSM-edge parity + 24 Iron Gate
    suite across Exploration-first / Exploration Ledger / ASCII strict
    / Dependency integrity / Multi-file coverage / Retry feedback).
    reachability_source=partial_live+parity under the path (B)
    contract documented in project_wave2_graduation_matrix.md.
    Explicit ``=false`` remains a runtime kill switch reverting to the
    ~1,611-line inline GENERATE block. A post-flip confirmation session
    is required per operator directive to capture Iron Gate telemetry
    if/when the transport weather clears — failure to observe Iron
    Gate lines post-flip does NOT auto-rollback unless runner-attributed
    regression or parity breaks.

    When ``true``, delegates the ~1,611-line GENERATE block (prelude +
    retry loop + CandidateGenerator dispatch + cost cap + forward-progress
    detector + productivity detector + Iron Gate suite + retry feedback)
    to :class:`GENERATERunner`. Cross-phase artifacts (``generation``,
    ``_episodic_memory``) threaded via ``PhaseResult.artifacts`` for
    VALIDATE to consume. Slice delivery: 5a = spine parity, 5b = Iron
    Gate suite parity depth (same runner module + flag).
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_slice4b_extracted() -> bool:
    """Slice 4b of Wave 2 (5) — APPROVE + APPLY + VERIFY combined gate.

    **Default ``true`` as of 2026-04-23 graduation** (harness-class 4-session
    cadence bt-2026-04-23-033530 / -040327 / -043017 / -045653 — each 0 PM /
    0 runner-attributed frames / 0 shutdown race; reachability observed in
    4/4 via `[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner`
    markers on live RuntimeHealthSensor IMMEDIATE ops walking
    CLASSIFY → ROUTE+CTX+PLAN → VALIDATE → GATE → SLICE4B with APPLY
    HEARTBEAT @ 80% on `requirements.txt`; reachability_source=opportunistic
    per operator-accepted bar "real op hit the runner under flag-on, not
    that our backlog seed won a race"). Explicit ``=false`` remains a
    runtime kill switch reverting to the ~1150-line inline APPROVE+APPLY+VERIFY
    block.

    When ``true``, delegates the ~1150-line APPROVE + APPLY (with 7.5
    INFRA) + VERIFY (with 8a scoped tests, 8b auto-commit, 8b2 hot-reload,
    8c self-critique, 8d visual VERIFY) block to :class:`Slice4bRunner`.
    Mirror of the Slice 3 combined-gate approach: the three phases are
    deeply interleaved (APPROVE tail runs on every path; APPLY consumes
    APPROVE locals; VERIFY consumes APPLY locals) so per-phase flags
    would require 6-way artifact threading. Per-phase decomposition
    arrives with Slice 6 dispatcher cutover. ``t_apply`` is threaded
    via ``PhaseResult.artifacts["t_apply"]`` for COMPLETERunner's
    canary latency calculation.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_gate_extracted() -> bool:
    """Slice 4a.2 of Wave 2 (5) — GATE phase extraction gate.

    **Default ``true`` as of 2026-04-23 graduation** (3 clean soak
    sessions bt-2026-04-23-005127 / -010733 / -012329, each 0 PM /
    $0 / 0 runner-attributed frames / 0 shutdown race; reachability
    observed in 2/3 sessions via ``[PhaseRunnerDelegate] GATE`` +
    ``[SemanticGuard]`` lines — S3 terminated upstream of GATE per
    downstream-of-VALIDATE reachability profile). Explicit ``=false``
    remains a runtime kill switch reverting to the 600-line inline
    GATE block.

    When ``true``, delegates the 600-line GATE block (can_write +
    SecurityReviewer + SimilarityGate + frozen_tier + risk ceiling +
    SemanticGuardian + REVIEW shadow + MutationGate + MIN_RISK_TIER
    floor + 5a green preview + 5b NOTIFY_APPLY yellow) to GATERunner.
    The ``risk_tier`` local mutates at up to 6 sites in GATE and is
    threaded back via ``PhaseResult.artifacts["risk_tier"]``.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_GATE_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_validate_extracted() -> bool:
    """Slice 4a.1 of Wave 2 (5) — VALIDATE phase extraction gate.

    **Default ``true`` as of 2026-04-22 graduation** (3 clean soak
    sessions bt-2026-04-22-230147 / -232323 / -235808, each 0 PM /
    $0 / 0 runner-attributed frames / 0 shutdown race; reachability
    observed in 2/3 sessions via 2 ``[PhaseRunnerDelegate] VALIDATE``
    delegation markers + 6 ``[ValidateRetryFSM]`` FSM transition lines).
    Explicit ``=false`` remains a runtime kill switch reverting to
    the 762-line inline VALIDATE block.

    When ``true``, delegates the 762-line VALIDATE block (nested retry
    FSM + L2 dispatch + source-drift + shadow harness + entropy +
    read-only short-circuit) to VALIDATERunner. Parity tests at
    ``tests/governance/phase_runner/test_validate_runner_parity.py``
    pin observable output across both paths. The ``best_candidate``
    local leaks downstream to GATE (37 refs) and is threaded via
    ``PhaseResult.artifacts``.

    Graduation ledger: ``memory/project_wave2_graduation_matrix.md``.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _phase_runner_slice3_fully_extracted() -> bool:
    """All three Slice 3 flags set — routes ROUTE+CTX+PLAN through runners.

    The three phases are currently interleaved in the inline pipeline
    (ROUTE body → conditional CTX → PLAN body); wiring each flag
    independently would require splitting the interleaving. For now,
    the dispatcher demands ALL THREE flags before using runners.
    Per-phase flags remain visible for env-var discoverability and
    future per-phase independence once Slice 6 (dispatcher cutover)
    decouples them entirely.
    """
    return (
        _phase_runner_route_extracted()
        and _phase_runner_context_expansion_extracted()
        and _phase_runner_plan_extracted()
    )


def _phase_runner_classify_extracted() -> bool:
    """Slice 2 of Wave 2 (5) — CLASSIFY phase extraction gate.

    Reads ``JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED``, **default
    ``true`` as of 2026-04-22 graduation (3 clean soak sessions
    bt-2026-04-22-200312 / -202123 / -203723 with 38 total
    ``[PhaseRunnerDelegate] CLASSIFY → runner`` reachability markers
    + Slice 2 parity 22/22 byte-identical vs inline).** Explicit
    ``=false`` remains a runtime kill switch that reverts to the
    inline block.

    When ``true``, ``_run_pipeline`` delegates the 760-line CLASSIFY
    block (emergency check + advisor + risk classification + 8 prompt
    injections + advance to ROUTE + narrator/dialogue start +
    ClassifyClarify) to
    :class:`backend.core.ouroboros.governance.phase_runners.classify_runner.CLASSIFYRunner`.
    The ``_advisory`` + ``_consciousness_bridge`` locals leak
    downstream (Tier 6 personality voice + VERIFY L2 retry fragile-
    file injection) and are threaded back through
    ``PhaseResult.artifacts`` to preserve the data flow.

    When ``false``, the inline block runs unchanged. Parity tests
    (tests/governance/phase_runner/test_classify_runner_parity.py)
    pin observable output across both paths.

    Graduation ledger: ``memory/project_wave2_graduation_matrix.md``.
    """
    return (
        os.environ.get("JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED", "true")
        .strip().lower() in _TRUTHY
    )


def _inject_last_session_summary_impl(
    project_root: Path,
    ctx: OperationContext,
) -> OperationContext:
    """Inject rendered LastSessionSummary into ``ctx.strategic_memory_prompt``.

    Extracted from ``_run_pipeline`` for testability. Zero behavioral
    change vs. the inline block: reads LSS with ``get_default_summary``,
    appends the rendered dense one-liner(s) to the existing strategic
    memory prompt via ``with_strategic_memory_context``, emits the §8
    observability contract INFO line on success, DEBUG when disabled,
    and swallows any injection failure (returns ``ctx`` unchanged).

    Authority invariant unchanged: this path touches ONLY the prompt
    surface the model reads at CONTEXT_EXPANSION — zero authority over
    Iron Gate, UrgencyRouter, risk tier, policy engine, FORBIDDEN_PATH,
    ToolExecutor protected-path checks, or approval gating.
    """
    try:
        from backend.core.ouroboros.governance.last_session_summary import (
            get_default_summary,
        )
        _lss = get_default_summary(project_root)
        _lss_enabled, _lss_n, _lss_sid, _lss_chars, _lss_hash8 = (
            _lss.inject_metrics()
        )
        if _lss_enabled:
            _lss_prompt = _lss.format_for_prompt()
            if _lss_prompt:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "last-session-v1",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=(
                        _existing + "\n\n" + _lss_prompt
                        if _existing else _lss_prompt
                    ),
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
            logger.info(
                "[LastSessionSummary] op=%s enabled=true n_sessions=%d "
                "latest_session_id=%s chars_out=%d "
                "inject_site=context_expansion hash8=%s source=summary_json",
                ctx.op_id, _lss_n, _lss_sid, _lss_chars, _lss_hash8,
            )
        else:
            logger.debug(
                "[LastSessionSummary] op=%s enabled=false "
                "inject_site=context_expansion",
                ctx.op_id,
            )
    except Exception:
        logger.debug(
            "[Orchestrator] LastSessionSummary injection skipped",
            exc_info=True,
        )
    return ctx


class _PreloadedExplorationRecord:
    """Synthetic exploration record for files the lean prompt builder inlined.

    ``ExplorationLedger.from_records`` duck-types on ``tool_name`` /
    ``arguments_hash`` / ``output_bytes`` / ``status``. When the lean prompt
    builder inlines a target file region directly into the generation
    prompt, the model has effectively "read" that file — we synthesize a
    fake ``read_file`` record so the ledger grants comprehension credit
    matching the legacy counter's ``_preloaded_credit`` behavior.

    Keeping this class in ``orchestrator.py`` preserves
    ``exploration_engine``'s pure-module contract (no orchestrator-side
    concepts leak into it). The ``preloaded:`` prefix on
    ``arguments_hash`` guarantees stable dedup per normalized path and
    no collision with a real ``read_file`` tool call.
    """

    __slots__ = ("tool_name", "arguments_hash", "output_bytes", "status")

    def __init__(self, path: str) -> None:
        self.tool_name = "read_file"
        self.arguments_hash = f"preloaded:{path}"
        self.output_bytes = 0
        self.status = "success"


def _inject_postmortem_recall_impl(
    ctx: OperationContext,
) -> OperationContext:
    """Inject prior-op POSTMORTEM lessons into ``ctx.strategic_memory_prompt``.

    Extracted from ``_run_pipeline`` for testability (mirrors the
    ``_inject_last_session_summary_impl`` pattern). Zero behavioral change
    vs. the inline block: looks up POSTMORTEMs from prior sessions whose
    op_signature is similar to the current op's signature (file paths +
    descriptive intent) and injects up to ``top_k`` lessons into the prompt
    as the "## Lessons from prior similar ops" section.

    Authority invariant per PRD §12.2: read-only, best-effort, never blocks
    the FSM. Master flag default-off (``JARVIS_POSTMORTEM_RECALL_ENABLED``).
    When off this is byte-for-byte pre-P0 behavior. ``PostmortemRecallService``
    itself returns ``[]`` cleanly on any failure path; this wrapper additionally
    swallows any exception and emits a DEBUG breadcrumb.

    Closes the rooted "system has perfect memory and zero recall" gap from
    PRD §4.2 Shallow #2 — P0 of PRD Phase 1.
    """
    try:
        from backend.core.ouroboros.governance.postmortem_recall import (
            get_default_service as _get_pm_recall,
            render_recall_section as _render_pm_recall,
        )
        _pm_svc = _get_pm_recall()
        if _pm_svc is None:
            # Master flag off: emit observability breadcrumb so live-cadence
            # graduation can distinguish "helper ran with master off" from
            # "helper never ran". Mirrors LSS / ConversationBridge / SemanticIndex
            # disabled-state breadcrumbs (uniform CONTEXT_EXPANSION audit).
            logger.debug(
                "[PostmortemRecall] op=%s enabled=false "
                "inject_site=context_expansion",
                ctx.op_id,
            )
        else:
            _pm_target_files = ", ".join(
                sorted((ctx.target_files or ()))[:5]
            )
            _pm_op_signature = (
                f"description={(ctx.description or '')[:200]} | "
                f"files={_pm_target_files}"
            )
            _pm_matches = _pm_svc.recall_for_op(_pm_op_signature)
            _pm_section = _render_pm_recall(_pm_matches)
            if _pm_section:
                _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                ctx = ctx.with_strategic_memory_context(
                    strategic_intent_id=ctx.strategic_intent_id or "pm-recall-p0",
                    strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                    strategic_memory_prompt=(
                        _existing + "\n\n" + _pm_section
                        if _existing else _pm_section
                    ),
                    strategic_memory_digest=ctx.strategic_memory_digest,
                )
                logger.info(
                    "[PostmortemRecall] op=%s enabled=true matched=%d "
                    "inject_site=context_expansion (P0 — PRD Phase 1)",
                    ctx.op_id, len(_pm_matches),
                )
            else:
                logger.debug(
                    "[PostmortemRecall] op=%s enabled=true matched=0 "
                    "inject_site=context_expansion",
                    ctx.op_id,
                )
    except Exception:
        logger.debug(
            "[Orchestrator] PostmortemRecall injection skipped",
            exc_info=True,
        )
    return ctx


def _reflect_cognitive_metrics_post_apply_impl(
    ctx: OperationContext,
    applied_files: Sequence[Any],
) -> None:
    """Phase 4 P3 follow-on — vindication call site at APPLY-success.

    Best-effort observability: when ``JARVIS_COGNITIVE_METRICS_ENABLED``
    is on AND the singleton is wired (set by orchestrator.__init__) AND
    a pre-apply snapshot was captured at CONTEXT_EXPANSION (via
    ``score_pre_apply``), calls ``CognitiveMetricsService.auto_reflect_post_apply``
    which computes before/after deltas and persists a vindication
    ``CognitiveMetricRecord`` to the JSONL ledger.

    Authority invariant per PRD §12.2: read-only, never blocks the FSM.
    Any exception (oracle down, ledger write failed) emits a DEBUG
    breadcrumb and returns silently. Vindication score is NOT consumed
    by Iron Gate / risk_tier / approve gating in this slice — advisory
    signal only, recorded for future Phase 4 work to consume.
    """
    try:
        from backend.core.ouroboros.governance.cognitive_metrics import (
            get_default_service as _get_cm_svc,
            is_enabled as _cm_enabled,
        )
        if not _cm_enabled():
            return
        svc = _get_cm_svc()
        if svc is None:
            return
        # applied_files is a Sequence[Path] from the orchestrator call
        # site; normalize to List[str] for the service API.
        target_strs = [str(p) for p in (applied_files or ())]
        if not target_strs:
            return
        svc.auto_reflect_post_apply(
            op_id=ctx.op_id,
            target_files=target_strs,
        )
    except Exception:
        logger.debug(
            "[Orchestrator] CognitiveMetrics post-apply reflection skipped",
            exc_info=True,
        )


def _score_cognitive_metrics_pre_apply_impl(
    ctx: OperationContext,
) -> None:
    """Phase 4 P3 — pre-APPLY oracle pre-score for the current op.

    Best-effort observability: when ``JARVIS_COGNITIVE_METRICS_ENABLED``
    is on AND the singleton is wired (set by orchestrator.__init__) AND
    the candidate ``ctx.target_files`` is non-empty, calls the wrapped
    ``OraclePreScorer`` and persists a ``CognitiveMetricRecord`` to the
    JSONL ledger. The pre-score is NOT consumed by Iron Gate / risk-tier
    / approve gating in this slice — it's an advisory signal only.
    Future slices can weight downstream decisions on it.

    Authority invariant per PRD §12.2: read-only, never blocks the FSM.
    Any exception (oracle down, ledger write failed, complexity probe
    raised) emits a DEBUG breadcrumb and returns silently.
    """
    try:
        from backend.core.ouroboros.governance.cognitive_metrics import (
            get_default_service as _get_cm_svc,
            is_enabled as _cm_enabled,
        )
        if not _cm_enabled():
            return
        svc = _get_cm_svc()
        if svc is None:
            return
        target_files = list(ctx.target_files or ())
        if not target_files:
            return
        # The OraclePreScorer accepts max_complexity + has_tests as
        # optional probes; we pass the conservative defaults so the
        # signal is computable on every well-formed ctx. Future slices
        # can wire real complexity probes.
        svc.score_pre_apply(
            op_id=ctx.op_id,
            target_files=target_files,
            max_complexity=0,
            has_tests=True,
        )
    except Exception:
        logger.debug(
            "[Orchestrator] CognitiveMetrics pre-score skipped",
            exc_info=True,
        )


def _plan_review_required() -> bool:
    """Return True when the session requires pre-execution plan review."""
    return (
        os.environ.get("JARVIS_SHOW_PLAN_BEFORE_EXECUTE", "").strip().lower()
        in _TRUTHY
    )


def _human_is_watching() -> bool:
    """Detect whether a human is likely watching the terminal.

    Returns ``True`` when any of:
    - ``sys.stdout`` is attached to an interactive TTY.
    - ``JARVIS_DIFF_PREVIEW_ALL`` env var is set to a truthy value.
      (Explicit flag for CI / headless modes where TTY is absent but the
      human is tailing logs.)

    Used to decide whether SAFE_AUTO (Green) operations should show a
    diff preview before auto-applying.
    """
    explicit = os.environ.get("JARVIS_DIFF_PREVIEW_ALL", "").lower() in (
        "true", "1", "yes",
    )
    if explicit:
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


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
        Env-tunable via ``JARVIS_MAX_VALIDATE_RETRIES`` (default ``2``).

        Set to ``0`` to bypass retries and dispatch failures straight to
        L2 Repair on the first critique. The original justification was
        latency: for a complex multi-file op, each validation pass costs
        ~7 minutes, so 2 retries consume ~21 minutes before L2 can
        dispatch — exceeding a typical 20-minute idle budget.

        Session U (``bt-2026-04-15-215858``, FSM-instrumented) revealed
        a stronger second justification: re-validation is **non-
        deterministic across iterations**. Same candidate, same test
        targets — iter=0 returned ``failure_class='test'`` (the real LSP
        defect that L2 should repair) but iter=1 returned
        ``failure_class='infra'`` (a sandbox/pytest transient). The
        ``'infra'`` class is non-retryable by design: it triggers the
        early-return branch at ``_early_return_ctx`` and advances ctx
        straight to POSTMORTEM, killing the op on a flake instead of
        giving L2 a chance to repair the legitimate critique iter=0
        identified. Setting ``max_validate_retries=0`` takes the loop
        out of this race entirely — iter=0 runs once, the
        ``validate_retries_remaining`` counter decrements to ``-1``, and
        the L2 dispatch branch at ``validate_retries_remaining < 0``
        fires on the real ``'test'`` critique.

        Battle-test override: ``JARVIS_MAX_VALIDATE_RETRIES=0``.
    """

    project_root: Path
    repo_registry: Optional["RepoRegistry"] = None  # Forward ref avoids circular import; resolved at type-check time
    generation_timeout_s: float = 180.0
    validation_timeout_s: float = 60.0
    approval_timeout_s: float = 600.0
    max_generate_retries: int = 1
    max_validate_retries: int = field(
        default_factory=lambda: int(
            os.environ.get("JARVIS_MAX_VALIDATE_RETRIES", "2")
        )
    )
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

        # Phase B REVIEW subagent — harness-attached post-construction via
        # set_subagent_orchestrator() so the constructor signature stays
        # stable. None until governed_loop_service wires it.
        self._subagent_orchestrator: Any = None

        # ── Phase 1 Step 3C: reload-hostile state hoisted to _governance_state ──
        # Every field that would otherwise get re-allocated on
        # ``importlib.reload(orchestrator)`` now lives on an
        # :class:`OrchestratorState` dataclass in the quarantined
        # ``_governance_state`` module. When
        # ``JARVIS_UNQUARANTINE_ORCHESTRATOR=true``, the state is a
        # process-wide singleton — the second-generation orchestrator
        # instance rebinds into the already-populated state without
        # losing the oracle update lock, cost governor, forward-
        # progress detector, session lessons, RSI trackers, hot-reload
        # subscription, or any of the seven harness-attached refs.
        # When the flag is false (default during rollout), each call
        # mints a fresh state via ``OrchestratorState.fresh(...)`` so
        # behavior is bit-for-bit identical to the pre-hoist code.
        from backend.core.ouroboros.governance._governance_state import (
            OrchestratorState,
            get_orchestrator_state,
            unquarantine_orchestrator_enabled,
        )

        if unquarantine_orchestrator_enabled():
            self._state = get_orchestrator_state(
                project_root=self._config.project_root,
            )
            logger.info(
                "[Orchestrator] Unquarantined state path engaged — "
                "reload-hostile roots sourced from process-wide "
                "OrchestratorState singleton",
            )
        else:
            self._state = OrchestratorState.fresh(
                project_root=self._config.project_root,
            )

        # Bind-once aliases for container-stable roots. These fields are
        # never reassigned after __init__ — the dataclass attribute
        # identity stays put, so an instance-level alias is safe and
        # minimizes call-site churn. Compare with the property/setter
        # pattern below, which is required for *rebindable* fields.
        self._oracle_update_lock: asyncio.Lock = self._state.oracle_update_lock
        self._cost_governor: CostGovernor = self._state.cost_governor
        # Register the cost_governor as the process-wide default so
        # pure helper modules (PLAN-EXPLOIT, etc.) can look it up
        # without taking it as a parameter through every call site.
        # Best-effort — never fails orchestrator construction.
        try:
            from backend.core.ouroboros.governance.cost_governor import (
                set_default_cost_governor as _set_default_cg,
            )
            _set_default_cg(self._cost_governor)
        except Exception:  # noqa: BLE001
            pass

        # Phase 4 P3 (2026-04-26) — un-strand the OraclePreScorer +
        # VindicationReflector via the CognitiveMetricsService wrapper.
        # Wires the singleton with the live Oracle off the stack so that
        # both the orchestrator helpers and the /cognitive REPL surface
        # share one service per process. Best-effort: any failure means
        # cognitive metrics are observed-only-via-repl rather than
        # auto-scored — never breaks orchestrator construction.
        try:
            from backend.core.ouroboros.governance.cognitive_metrics import (
                CognitiveMetricsService as _CMSvc,
                is_enabled as _cm_enabled,
                set_default_service as _set_default_cm,
            )
            if _cm_enabled():
                _oracle_for_cm = getattr(self._stack, "oracle", None)
                if _oracle_for_cm is not None:
                    _set_default_cm(_CMSvc(
                        oracle=_oracle_for_cm,
                        project_root=self._config.project_root,
                    ))
        except Exception:  # noqa: BLE001
            pass

        self._forward_progress: ForwardProgressDetector = self._state.forward_progress
        self._productivity_detector: ProductivityDetector = (
            self._state.productivity_detector
        )
        # Counter dataclass alias — see class-level note on why the
        # candidate_generator pattern (bind once, mutate attributes on
        # the stable dataclass) is safer than property/setter for int
        # read-modify-write patterns like ``x += 1``.
        self._counters = self._state.counters

        # Config-only ints. Not reload-hostile because they're derived
        # from env vars and re-read on construction — the post-reload
        # instance gets the same value without indirection.
        _max = int(os.environ.get("JARVIS_SESSION_LESSONS_MAX", "20"))
        self._session_lessons_max: int = max(5, _max)
        self._convergence_check_interval: int = int(
            os.environ.get("JARVIS_LESSON_CONVERGENCE_CHECK_INTERVAL", "10")
        )

        # Log whichever trackers are live on the bound state. Preserves
        # the legacy debug-level visibility without re-running the
        # optional-module try/except chain (that happens once inside
        # ``OrchestratorState.fresh()``).
        if self._state.rsi_score_function is None:
            logger.debug("RSI: CompositeScoreFunction not available")
        if self._state.rsi_convergence_tracker is None:
            logger.debug("RSI: ConvergenceTracker not available")
        if self._state.rsi_transition_tracker is None:
            logger.debug("RSI: TransitionProbabilityTracker not available")
        _hr = self._state.hot_reloader
        if _hr is not None:
            logger.info(
                "[Orchestrator] ModuleHotReloader armed (%d safe modules)",
                len(_hr.safe_modules),
            )

    # ─────────────────────────────────────────────────────────────────
    # Phase 1 Step 3C: property/setter pairs for rebindable state
    # ─────────────────────────────────────────────────────────────────
    #
    # Every field below is either slice-rebound (``xs = xs[-CAP:]``)
    # or set to ``None`` at construction and later reassigned via a
    # harness ``set_*()`` method. Both patterns would plant a *real*
    # instance attribute that shadows any plain descriptor on the
    # class, so the rebind would silently drift away from the
    # :class:`OrchestratorState` singleton on the next reload.
    #
    # The property/setter pair fixes this by routing every read *and*
    # write through ``self._state.<field>``. The instance never grows
    # an attribute that could shadow the class descriptor, so the
    # post-reload instance sees the already-populated state.
    #
    # In-place mutations (``session_lessons.append(x)``,
    # ``session_lessons.clear()``) are alias-safe — they operate on
    # the list identity held inside ``self._state``, not on a local
    # copy — so the existing call sites keep working unchanged.

    @property
    def _subagent_scheduler(self) -> Any:
        """Alias to ``_config.execution_graph_scheduler``.

        Added 2026-04-24 (S7 finding) to close the W3(6) Slice 4 wiring gap:
        ``phase_dispatcher.py`` reads ``orchestrator._subagent_scheduler``
        when deciding whether to run the post-GENERATE enforce-mode
        ``dispatch_fanout`` path; orchestrator stores the same handle as
        ``_config.execution_graph_scheduler`` (passed in via
        ``OrchestratorConfig`` from ``governed_loop_service``). Pre-fix
        ``getattr`` returned ``None`` and the enforce path always logged
        ``enforce_fanout skipped: orchestrator has no _subagent_scheduler
        reference``. The alias keeps the dispatcher's call shape stable
        while making the field reachable.
        """
        return self._config.execution_graph_scheduler

    @property
    def _cancel_token_registry(self) -> Any:
        """Forward to GovernedLoopService's :class:`CancelTokenRegistry`.

        W3(7) Slice 2 — gives the dispatcher a single attribute lookup to
        find the per-session registry. The registry lives on GLS (created
        in __init__); the orchestrator surfaces it via ``self._stack``.
        Returns ``None`` for unit-test orchestrators constructed without
        a stack — runners must handle ``pctx.cancel_token is None``
        cleanly (no race wrap, behavior identical to pre-W3(7)).
        """
        _gls = getattr(self._stack, "governed_loop_service", None)
        if _gls is None:
            return None
        return getattr(_gls, "_cancel_token_registry", None)

    @property
    def _session_lessons(self) -> list:
        return self._state.session_lessons

    @_session_lessons.setter
    def _session_lessons(self, value: list) -> None:
        self._state.session_lessons = value

    @property
    def _ops_before_lesson(self) -> int:
        return self._state.counters.ops_before_lesson

    @_ops_before_lesson.setter
    def _ops_before_lesson(self, value: int) -> None:
        self._state.counters.ops_before_lesson = value

    @property
    def _ops_before_lesson_success(self) -> int:
        return self._state.counters.ops_before_lesson_success

    @_ops_before_lesson_success.setter
    def _ops_before_lesson_success(self, value: int) -> None:
        self._state.counters.ops_before_lesson_success = value

    @property
    def _ops_after_lesson(self) -> int:
        return self._state.counters.ops_after_lesson

    @_ops_after_lesson.setter
    def _ops_after_lesson(self, value: int) -> None:
        self._state.counters.ops_after_lesson = value

    @property
    def _ops_after_lesson_success(self) -> int:
        return self._state.counters.ops_after_lesson_success

    @_ops_after_lesson_success.setter
    def _ops_after_lesson_success(self, value: int) -> None:
        self._state.counters.ops_after_lesson_success = value

    @property
    def _rsi_score_function(self) -> Optional[Any]:
        return self._state.rsi_score_function

    @_rsi_score_function.setter
    def _rsi_score_function(self, value: Optional[Any]) -> None:
        self._state.rsi_score_function = value

    @property
    def _rsi_score_history(self) -> Optional[Any]:
        return self._state.rsi_score_history

    @_rsi_score_history.setter
    def _rsi_score_history(self, value: Optional[Any]) -> None:
        self._state.rsi_score_history = value

    @property
    def _rsi_convergence_tracker(self) -> Optional[Any]:
        return self._state.rsi_convergence_tracker

    @_rsi_convergence_tracker.setter
    def _rsi_convergence_tracker(self, value: Optional[Any]) -> None:
        self._state.rsi_convergence_tracker = value

    @property
    def _rsi_transition_tracker(self) -> Optional[Any]:
        return self._state.rsi_transition_tracker

    @_rsi_transition_tracker.setter
    def _rsi_transition_tracker(self, value: Optional[Any]) -> None:
        self._state.rsi_transition_tracker = value

    @property
    def _hot_reloader(self) -> Optional[Any]:
        return self._state.hot_reloader

    @_hot_reloader.setter
    def _hot_reloader(self, value: Optional[Any]) -> None:
        self._state.hot_reloader = value

    # § 4 attached refs — all seven flow through ``self._state``.
    # Harness ``set_*()`` methods below assign through these setters,
    # so rebinding the orchestrator class does not require re-running
    # the harness wiring pass.

    @property
    def _reasoning_bridge(self) -> Optional[Any]:
        return self._state.reasoning_bridge

    @_reasoning_bridge.setter
    def _reasoning_bridge(self, value: Optional[Any]) -> None:
        self._state.reasoning_bridge = value

    @property
    def _infra_applicator(self) -> Optional[Any]:
        return self._state.infra_applicator

    @_infra_applicator.setter
    def _infra_applicator(self, value: Optional[Any]) -> None:
        self._state.infra_applicator = value

    @property
    def _reasoning_narrator(self) -> Optional[Any]:
        return self._state.reasoning_narrator

    @_reasoning_narrator.setter
    def _reasoning_narrator(self, value: Optional[Any]) -> None:
        self._state.reasoning_narrator = value

    @property
    def _dialogue_store(self) -> Optional[Any]:
        return self._state.dialogue_store

    @_dialogue_store.setter
    def _dialogue_store(self, value: Optional[Any]) -> None:
        self._state.dialogue_store = value

    @property
    def _pre_action_narrator(self) -> Optional[Any]:
        return self._state.pre_action_narrator

    @_pre_action_narrator.setter
    def _pre_action_narrator(self, value: Optional[Any]) -> None:
        self._state.pre_action_narrator = value

    @property
    def _exploration_fleet(self) -> Optional[Any]:
        return self._state.exploration_fleet

    @_exploration_fleet.setter
    def _exploration_fleet(self, value: Optional[Any]) -> None:
        self._state.exploration_fleet = value

    @property
    def _critique_engine(self) -> Optional[Any]:
        return self._state.critique_engine

    @_critique_engine.setter
    def _critique_engine(self, value: Optional[Any]) -> None:
        self._state.critique_engine = value

    def set_reasoning_bridge(self, bridge: Any) -> None:
        """Attach a ReasoningChainBridge for pre-CLASSIFY reasoning.

        Writes through the :attr:`_reasoning_bridge` setter, which
        routes into ``self._state.reasoning_bridge``. When the
        orchestrator class reloads, the new instance inherits the
        already-populated state and the harness does *not* need to
        re-run this setter.
        """
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

    def set_subagent_orchestrator(self, orch: Any) -> None:
        """Attach the Phase B ``SubagentOrchestrator`` for REVIEW shadow dispatch.

        The orchestrator is the single spawn point for ephemeral REVIEW
        subagents (see ``subagent_orchestrator.py:dispatch_review``). Passing
        ``None`` detaches it — the post-VALIDATE shadow hook then no-ops.
        """
        self._subagent_orchestrator = orch

    async def _run_review_shadow(self, ctx: Any, best_candidate: Any) -> None:
        """Phase B — post-VALIDATE REVIEW subagent in OBSERVER MODE.

        Gated by ``JARVIS_REVIEW_SUBAGENT_SHADOW`` (default **``true``**,
        graduated 2026-04-20). When on, dispatches a REVIEW subagent per
        candidate file and emits the verdict to telemetry. **The FSM
        proceeds to GATE regardless of verdict** — no risk-tier change,
        no retry routing, no state mutation. The contract stays
        observer-only even post-graduation; promoting REVIEW into
        authority-carrying gate logic is a separate slice with its own
        graduation arc.

        Graduation evidence (2026-04-20):
          * 28-test regression spine green (test_review_subagent.py +
            test_review_subagent_correlation.py).
          * Session 1 live FSM integration: observer hook fired
            post-VALIDATE at 25ms latency, FSM continued without
            interruption, aggregate telemetry format proven stable.
          * Session 2 Path B synthetic reject-proof: aggregate=REJECT
            emitted correctly for a poisoned candidate carrying the
            credential_shape_introduced pattern, with findings[0]
            identifying the triggering pattern precisely. Surfaced and
            fixed a latent case-mismatch bug in the aggregation logic
            (pinned by two new regression tests).
          * Upstream intelligence (Claude sonnet-4-6) refused to generate
            the credential-shape poison on its own — a complementary
            safety layer; REVIEW is the net for cases Claude's RLHF
            guardrails don't catch, not a redundant check.

        Must not raise under any condition — the observer contract forbids
        the shadow from breaking the main generation loop.
        """
        if best_candidate is None:
            return
        if self._subagent_orchestrator is None:
            return
        if os.environ.get(
            "JARVIS_REVIEW_SUBAGENT_SHADOW", "true"
        ).lower() not in ("true", "1"):
            return

        try:
            _files = best_candidate.get("files") if isinstance(
                best_candidate.get("files"), list,
            ) else None
            _iter = (
                [
                    (entry.get("file_path", ""), entry.get("full_content", ""))
                    for entry in _files
                    if isinstance(entry, dict)
                ]
                if _files
                else [(
                    best_candidate.get("file_path", ""),
                    best_candidate.get("full_content", ""),
                )]
            )

            _t0 = time.monotonic()
            _verdicts: list = []
            for _path, _new in _iter:
                if not _path or not isinstance(_new, str):
                    continue
                _old = ""
                try:
                    _abs = (
                        self._config.project_root / _path
                        if not Path(_path).is_absolute()
                        else Path(_path)
                    )
                    if _abs.is_file():
                        _old = _abs.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    _old = ""

                _result = await self._subagent_orchestrator.dispatch_review(
                    parent_ctx=ctx,
                    file_path=_path,
                    pre_apply_content=_old,
                    candidate_content=_new,
                    generation_intent=getattr(ctx, "description", "") or "(no description)",
                    timeout_s=30.0,
                )

                _verdict = "unknown"
                _score = 0.0
                for _k, _v in (_result.type_payload or ()):
                    if _k == "verdict":
                        _verdict = str(_v)
                    elif _k == "semantic_integrity_score":
                        try:
                            _score = float(_v)
                        except (TypeError, ValueError):
                            pass
                _status = (
                    _result.status.value
                    if hasattr(_result.status, "value")
                    else str(_result.status)
                )
                _verdicts.append((_path, _verdict, _score, _status))

            _duration_ms = int((time.monotonic() - _t0) * 1000)

            # Aggregate: worst-of across files. REJECT dominates,
            # APPROVE_WITH_RESERVATIONS dominates APPROVE.
            #
            # Verdict comparison uses the string constants from
            # subagent_contracts (values: "reject", "approve_with_reservations",
            # "approve") — NOT uppercase literals. A prior uppercase comparison
            # silently reclassified every REJECT as APPROVE in the aggregate
            # telemetry (caught 2026-04-20 via synthetic reject-proof harness).
            # The _aggregate output string stays uppercase for stable log
            # parsing ("aggregate=REJECT"); only the input comparison is
            # lowercase-matched against the verdict values on the wire.
            from backend.core.ouroboros.governance.subagent_contracts import (
                REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS,
                REVIEW_VERDICT_REJECT,
            )
            _counts = {"approved": 0, "reservations": 0, "rejected": 0, "failed": 0}
            _aggregate = "APPROVE" if _verdicts else "NO_FILES"
            for _p, _v, _s, _st in _verdicts:
                if _st != "completed":
                    _counts["failed"] += 1
                    continue
                if _v == REVIEW_VERDICT_REJECT:
                    _counts["rejected"] += 1
                    _aggregate = "REJECT"
                elif _v == REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS:
                    _counts["reservations"] += 1
                    if _aggregate != "REJECT":
                        _aggregate = "APPROVE_WITH_RESERVATIONS"
                else:
                    _counts["approved"] += 1

            # Stable structured line — key=value so a simple split("=") parser
            # can build rollup counters (aggregate-verdict distribution,
            # approve/reject rates, per-session verdict sanity) across the
            # graduation arc. Matches the [SemanticGuard] log convention.
            logger.info(
                "[REVIEW-SHADOW] op=%s aggregate=%s files_reviewed=%d "
                "approved=%d reservations=%d rejected=%d failed=%d "
                "duration_ms=%d (observer — FSM proceeds regardless)",
                getattr(ctx, "op_id", "?"),
                _aggregate,
                len(_verdicts),
                _counts["approved"],
                _counts["reservations"],
                _counts["rejected"],
                _counts["failed"],
                _duration_ms,
            )
        except Exception:
            # Observer contract: shadow must never break the FSM.
            logger.debug(
                "[Orchestrator] REVIEW shadow dispatch skipped",
                exc_info=True,
            )

    async def _run_plan_shadow(self, ctx: Any) -> Any:
        """Phase B PLAN-shadow — AgenticPlanSubagent dispatch running
        alongside the legacy ``PlanGenerator`` as an observer.

        Gated by ``JARVIS_PLAN_SUBAGENT_SHADOW`` (default **``true``**,
        graduated 2026-04-20). When on, this hook:
          * Dispatches the PLAN subagent with ctx.target_files + description
          * Receives an execution_graph 2d.1-shaped payload back
          * Stashes the payload into ``ctx.execution_graph`` **without
            touching ``ctx.implementation_plan``** (the legacy flat-list
            plan remains the authoritative input to GENERATE; the DAG is
            observer-only this slice)
          * Emits a stable ``[PLAN-SHADOW]`` telemetry line so the legacy
            flat list and the subagent DAG can be compared across ops

        Single-file ops and ops with no target files skip — there is no
        DAG to build. Dispatch failures bump DEBUG logs only; the FSM
        proceeds regardless. Returns the (possibly updated) context so
        the caller can chain ``ctx = await self._run_plan_shadow(ctx)``.
        """
        if self._subagent_orchestrator is None:
            return ctx
        if os.environ.get(
            "JARVIS_PLAN_SUBAGENT_SHADOW", "true",
        ).lower() not in ("true", "1"):
            return ctx

        target_files = tuple(
            t for t in (getattr(ctx, "target_files", ()) or ()) if t
        )
        if len(target_files) < 2:
            # Single-file or zero-file op → no DAG to build.
            return ctx

        try:
            _t0 = time.monotonic()
            _description = (
                getattr(ctx, "description", "")
                or getattr(ctx, "goal", "")
                or "(no description)"
            )
            _primary_repo = getattr(ctx, "primary_repo", "jarvis") or "jarvis"
            _risk_tier = str(getattr(ctx, "risk_tier", "") or "")

            _result = await self._subagent_orchestrator.dispatch_plan(
                parent_ctx=ctx,
                op_description=str(_description),
                target_files=target_files,
                primary_repo=str(_primary_repo),
                risk_tier=_risk_tier,
                timeout_s=30.0,
            )

            # Extract the stable metrics from type_payload. Any missing
            # key falls back to a neutral default — the shadow contract
            # guarantees no raise.
            _payload = dict(_result.type_payload or ())
            _unit_count = int(_payload.get("unit_count", 0) or 0)
            _edge_count = int(_payload.get("edge_count", 0) or 0)
            _root_count = int(_payload.get("root_count", 0) or 0)
            _parallel = _payload.get("parallel_branches", ()) or ()
            _parallel_pairs = len(_parallel)
            _validation_valid = bool(_payload.get("validation_valid", False))
            _validation_errors = _payload.get("validation_errors", ()) or ()
            _execution_graph = _payload.get("execution_graph")
            _graph_id = ""
            if _execution_graph:
                # execution_graph is a tuple-of-tuple; find ("graph_id", X).
                for _k, _v in _execution_graph:
                    if _k == "graph_id":
                        _graph_id = str(_v)
                        break

            # Stash the DAG on ctx WITHOUT overwriting implementation_plan.
            # Uses dataclasses.replace so the immutable-by-convention ctx
            # is respected; if the field doesn't exist (older ctx shape),
            # fall through silently.
            if _execution_graph is not None:
                try:
                    ctx = dataclasses.replace(
                        ctx, execution_graph=_execution_graph,
                    )
                except (TypeError, ValueError):
                    # Older ctx without execution_graph field — log and
                    # continue; the shadow telemetry still fires.
                    logger.debug(
                        "[Orchestrator] PLAN-shadow could not stash "
                        "execution_graph on ctx — field missing",
                    )

            _duration_ms = int((time.monotonic() - _t0) * 1000)
            _status = (
                _result.status.value
                if hasattr(_result.status, "value")
                else str(_result.status)
            )

            logger.info(
                "[PLAN-SHADOW] op=%s status=%s dag_units=%d edges=%d "
                "roots=%d parallel_pairs=%d validation_valid=%s "
                "graph_id=%s duration_ms=%d "
                "(observer — FSM proceeds regardless)",
                getattr(ctx, "op_id", "?"),
                _status,
                _unit_count,
                _edge_count,
                _root_count,
                _parallel_pairs,
                _validation_valid,
                _graph_id or "<none>",
                _duration_ms,
            )

            # If the DAG itself was invalid, surface at INFO so the
            # graduation-arc telemetry captures validator failures
            # alongside the shadow dispatch. Still observer-only — no
            # raise, no FSM mutation.
            if not _validation_valid and _validation_errors:
                logger.info(
                    "[PLAN-SHADOW] op=%s validation_errors=%s",
                    getattr(ctx, "op_id", "?"),
                    list(_validation_errors)[:5],
                )
        except Exception:
            # Observer contract: shadow must never break the FSM.
            logger.debug(
                "[Orchestrator] PLAN shadow dispatch skipped",
                exc_info=True,
            )

        return ctx

    def _is_cancel_requested(self, op_id: str) -> bool:
        """Check if REPL /cancel was requested for this operation."""
        _gls = getattr(self._stack, "governed_loop_service", None)
        if _gls is not None and hasattr(_gls, "is_cancel_requested"):
            return _gls.is_cancel_requested(op_id)
        return False

    def _add_session_lesson(
        self,
        lesson_type: str,
        lesson_text: str,
        op_id: str = "",
    ) -> None:
        """Append a lesson, cap the buffer, and emit a heartbeat.

        Centralises the 4+ scattered ``_session_lessons.append((...))``
        + ``if len(...) > max: rebind`` blocks and adds the SerpentFlow
        heartbeat so the operator sees "📖 applying N lessons".

        Parameters
        ----------
        lesson_type:
            ``"code"`` or ``"infra"``.
        lesson_text:
            Human-readable lesson text (will be truncated to ~200 chars
            by the heartbeat for transport safety).
        op_id:
            Originating operation — passed to SerpentFlow for block scoping.
        """
        self._session_lessons.append((lesson_type, lesson_text))
        if len(self._session_lessons) > self._session_lessons_max:
            self._session_lessons = self._session_lessons[-self._session_lessons_max:]

        # Emit heartbeat to SerpentFlow / transports
        try:
            _payload = {
                "phase": "session_lessons",
                "lesson_count": len(self._session_lessons),
                "latest_lesson": lesson_text[:200],
                "lessons": list(self._session_lessons),
            }
            for _t in getattr(self._stack.comm, "_transports", []):
                try:
                    _msg = type("_Msg", (), {
                        "payload": _payload,
                        "op_id": op_id,
                        "msg_type": type("_T", (), {"value": "HEARTBEAT"})(),
                    })()
                    # Transport.send() is async but we are in sync context here;
                    # schedule it without awaiting (fire-and-forget for non-critical UX).
                    import asyncio as _aio
                    try:
                        _loop = _aio.get_running_loop()
                        _loop.create_task(_t.send(_msg))
                    except RuntimeError:
                        pass  # No running loop — skip heartbeat
                except Exception:
                    pass
        except Exception:
            pass  # Heartbeat is non-critical UX

    async def _emit_route_cost_heartbeat(
        self,
        ctx: OperationContext,
        *,
        cost_usd: float,
        provider: str,
        route: str,
        cost_event: str,
    ) -> None:
        """Emit route-aware cost telemetry for dashboard transports."""
        delta = float(cost_usd or 0.0)
        if delta <= 0.0:
            return
        comm = getattr(self._stack, "comm", None)
        if comm is None:
            return
        try:
            await comm.emit_heartbeat(
                op_id=ctx.op_id,
                phase="cost",
                progress_pct=0.0,
                route=route or "unknown",
                provider=provider or "",
                cost_usd=delta,
                cost_event=cost_event,
                task_complexity=getattr(ctx, "task_complexity", "") or "",
            )
        except Exception:
            logger.debug(
                "[Orchestrator] Route cost heartbeat failed", exc_info=True,
            )

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
        # Phase 9.5 Part B — Phase 8 producer wiring (op-level).
        # Each call NEVER raises and gates on its own substrate master
        # flag (default false). Cost is microseconds when off — the
        # imports are lazy inside the producer module.
        _phase8_op_t0 = time.monotonic()
        _phase8_terminal_ctx = ctx
        try:
            from backend.core.ouroboros.governance.observability.phase8_producers import (
                check_flag_changes_and_publish as _phase8_flag_scan,
            )
            _phase8_flag_scan()
        except Exception:
            logger.debug(
                "[Phase8Wiring] op-start flag scan failed", exc_info=True,
            )
        try:
            try:
                _phase8_terminal_ctx = await self._run_pipeline(ctx)
                return _phase8_terminal_ctx
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
                _phase8_terminal_ctx = ctx
                return ctx
        finally:
            # Phase 9.5 Part B — terminal-phase Phase 8 producer hooks.
            # NEVER raises. Records (a) op-level latency for the
            # terminal phase, (b) one decision-trace row tagged
            # OP_TERMINAL with the final phase + reason. Substrate
            # master flags (default false) gate the writes; calls are
            # microseconds when off.
            try:
                from backend.core.ouroboros.governance.observability.phase8_producers import (
                    record_decision as _phase8_record_decision,
                    record_phase_latency as _phase8_record_latency,
                )
                _phase8_elapsed_s = max(
                    0.0, time.monotonic() - _phase8_op_t0,
                )
                _phase8_final_ctx = _phase8_terminal_ctx
                _phase8_final_phase_name = (
                    _phase8_final_ctx.phase.name
                    if hasattr(_phase8_final_ctx, "phase") else "UNKNOWN"
                )
                _phase8_record_latency(
                    "OP_TERMINAL", _phase8_elapsed_s,
                )
                _phase8_record_decision(
                    op_id=getattr(_phase8_final_ctx, "op_id", ""),
                    phase="OP_TERMINAL",
                    decision=_phase8_final_phase_name,
                    factors={
                        "terminal_reason": (
                            getattr(
                                _phase8_final_ctx,
                                "terminal_reason_code", "",
                            ) or ""
                        ),
                        "elapsed_s": round(_phase8_elapsed_s, 3),
                    },
                    rationale="op terminal",
                )
            except Exception:
                logger.debug(
                    "[Phase8Wiring] terminal hooks failed", exc_info=True,
                )
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
            # Close the per-op TaskBoard registry entry (Gap #5 Slice 3).
            # Idempotent + safe on ops that never touched a task tool
            # (returns False cleanly). Single canonical shutdown hook
            # per the Gap #5 Slice 2 authorization. Authority-free —
            # just a scratchpad cleanup.
            try:
                from backend.core.ouroboros.governance.task_tool import (
                    close_task_board,
                )
                close_task_board(
                    ctx.op_id,
                    reason="op terminal phase=" + ctx.phase.name,
                )
            except Exception:
                logger.debug(
                    "[Orchestrator] TaskBoard close failed",
                    exc_info=True,
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

        # Wave 2 (5) Slice 6a — Dispatcher short-circuit.
        # When JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED=true, the phase
        # dispatcher runs every phase through the PhaseRunnerRegistry;
        # the legacy inline blocks below are never reached. When off
        # (default), fall through to the legacy path unchanged.
        from backend.core.ouroboros.governance.phase_dispatcher import (
            dispatcher_enabled as _dispatcher_enabled,
        )
        if _dispatcher_enabled():
            from backend.core.ouroboros.governance.phase_dispatcher import (
                dispatch_pipeline as _dispatch_pipeline,
            )
            logger.info("[PhaseRunnerDelegate] DISPATCHER → pipeline op=%s", ctx.op_id[:16])
            return await _dispatch_pipeline(self, _serpent, ctx)

        # Wave 2 (5) Slice 2 - CLASSIFYRunner delegation gate.
        # Flag JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED (default false) routes
        # the 760-line CLASSIFY block through the extracted PhaseRunner.
        # Parity tests pin identical observable output across both paths.
        # _advisory is the only local that leaks downstream (line ~2779
        # Tier 6 personality voice line reads .chronic_entropy) - we
        # thread it through result.artifacts to preserve the data flow.
        if _phase_runner_classify_extracted():
            from backend.core.ouroboros.governance.phase_runners.classify_runner import (
                CLASSIFYRunner,
            )
            logger.info("[PhaseRunnerDelegate] CLASSIFY → runner op=%s", ctx.op_id[:16])
            _classify_runner = CLASSIFYRunner(self, _serpent)
            _classify_result = await _classify_runner.run(ctx)
            # Rebind CLASSIFY locals that downstream phases read:
            #  - _advisory at ~line 2819 (Tier 6 personality voice)
            #  - _consciousness_bridge at ~line 3030 and ~line 4513
            #    (fragile-file memory injection, both initial + L2 retry)
            _advisory = _classify_result.artifacts.get("advisory")
            _consciousness_bridge = _classify_result.artifacts.get(
                "consciousness_bridge",
            )
            if _classify_result.next_phase is None:
                return _classify_result.next_ctx
            ctx = _classify_result.next_ctx
            # `risk_tier` is carried as a function-scoped local across
            # phases (reassigned at ~5498, 5515, 5538, 5628, 5731, 5737,
            # 5809). advance(ROUTE, risk_tier=...) stamped it on ctx,
            # so we rebind from there to keep both paths identical.
            risk_tier = ctx.risk_tier
        else:
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
                    OperationAdvisor, AdvisoryDecision, infer_read_only_intent,
                )
                # Stamp read-only intent onto the hash-chained context BEFORE
                # advising. The Advisor's bypass of blast_radius + test_coverage
                # is mathematically safe only because ctx.is_read_only is
                # enforced downstream by tool_executor (mutating tools refused)
                # and the orchestrator's APPLY short-circuit.
                if not ctx.is_read_only:
                    _inferred_ro = infer_read_only_intent(ctx.description)
                    if _inferred_ro:
                        ctx = ctx.with_read_only_intent(True)
                        logger.info(
                            "[Orchestrator] Read-only intent inferred op=%s "
                            "— Advisor blast/coverage bypass active; tool_executor "
                            "will refuse mutations; APPLY phase will short-circuit",
                            ctx.op_id,
                        )
                _advisor = OperationAdvisor(self._config.project_root)
                _advisory = _advisor.advise(
                    ctx.target_files, ctx.description, ctx.op_id,
                    is_read_only=ctx.is_read_only,
                )

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

            # ---- ConversationBridge (v0.1): TUI dialogue as untrusted soft bias ----
            # Injects the user's recent TUI turns BETWEEN the trusted manifesto
            # block (above) and the trusted goals + user-preferences blocks
            # (below). Untrusted-in-the-middle ordering preserves attention-
            # mechanism dominance for FORBIDDEN_PATH / style prefs (which come
            # last) while still surfacing conversational intent to the model.
            #
            # Authority invariant (plan v0.1 §9): this block has zero authority
            # over Iron Gate, UrgencyRouter, risk tier, policy engine,
            # FORBIDDEN_PATH, tool protected-path checks, or approval gating.
            # Consumed ONLY by StrategicDirection at this injection site.
            try:
                from backend.core.ouroboros.governance.conversation_bridge import (
                    get_default_bridge,
                )
                _bridge = get_default_bridge()
                (
                    _bridge_enabled,
                    _n_turns,
                    _n_user,
                    _n_assistant,
                    _n_postmortem,
                    _chars_in,
                    _redacted,
                    _hash8,
                ) = _bridge.inject_metrics()
                if _bridge_enabled:
                    _conv_prompt = _bridge.format_for_prompt()
                    if _conv_prompt:
                        _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                        ctx = ctx.with_strategic_memory_context(
                            strategic_intent_id=ctx.strategic_intent_id or "conv-bridge-v1",
                            strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                            strategic_memory_prompt=(
                                _existing + "\n\n" + _conv_prompt
                                if _existing else _conv_prompt
                            ),
                            strategic_memory_digest=ctx.strategic_memory_digest,
                        )
                    # §8 one-line observability contract (v1.1 source breakdown).
                    # Logged whether or not there were turns to inject —
                    # operators need to see that the wiring fired.
                    logger.info(
                        "[ConversationBridge] op=%s enabled=true n_turns=%d "
                        "n_user=%d n_assistant=%d n_postmortem=%d chars_in=%d "
                        "inject_site=context_expansion redacted=%s hash8=%s",
                        ctx.op_id, _n_turns, _n_user, _n_assistant, _n_postmortem,
                        _chars_in, _redacted, _hash8,
                    )
                else:
                    # §8 §7-tweak: DEBUG line at inject site when master switch
                    # is off so "is wiring live?" is answerable without content.
                    logger.debug(
                        "[ConversationBridge] op=%s enabled=false "
                        "inject_site=context_expansion",
                        ctx.op_id,
                    )
            except Exception:
                logger.debug(
                    "[Orchestrator] ConversationBridge injection skipped",
                    exc_info=True,
                )

            # ---- P0 PostmortemRecall (PRD Phase 1): prior-op lessons ----
            # Helper extraction mirrors LSS pattern (testability per PRD §11
            # Layer 3 reachability supplement, W3(6) precedent). Body lives at
            # module scope as `_inject_postmortem_recall_impl`. ConversationBridge
            # → PostmortemRecall → SemanticIndex ordering preserved.
            ctx = _inject_postmortem_recall_impl(ctx)

            # ---- Phase 4 P3 Cognitive Metrics: Oracle pre-score ----
            # Best-effort observability — calls OraclePreScorer via the
            # CognitiveMetricsService singleton wired at orchestrator boot.
            # Persists a CognitiveMetricRecord to the JSONL ledger when
            # JARVIS_COGNITIVE_METRICS_ENABLED is on. Advisory only —
            # the existing Iron Gate / risk_tier_floor stack remains
            # authoritative. Helper body at module scope as
            # `_score_cognitive_metrics_pre_apply_impl`.
            _score_cognitive_metrics_pre_apply_impl(ctx)

            # ---- SemanticIndex v0.1: recency-weighted focus + closures ----
            # Soft semantic prior drawn from the recency-weighted centroid
            # over recent commits + active goals + recent conversation.
            # Injected BETWEEN the ConversationBridge block (above) and the
            # Goals block (below) so the ordering reads top-to-bottom as:
            # Strategic → Bridge (untrusted dialogue) → Semantic (untrusted
            # prior) → Goals (trusted) → UserPreferences (highest trust).
            #
            # Authority invariant: this block has **zero** authority over
            # Iron Gate, UrgencyRouter, risk tier, policy engine, FORBIDDEN_PATH,
            # or approval gating. It affects ONLY the prompt surface the model
            # reads at CONTEXT_EXPANSION — §4 (data sovereignty, local
            # embedder) + §8 (hashes + counts, no raw vectors in logs).
            try:
                from backend.core.ouroboros.governance.semantic_index import (
                    get_default_index,
                )
                _semi = get_default_index(self._config.project_root)
                # Q3 Slice 3 — non-blocking build trigger so CLASSIFY
                # never stalls on subprocess+embed. format_prompt_sections
                # operates against the currently-loaded centroid (empty
                # on cold start → returns None, callers handle that).
                _semi.build_async()
                _semi_prompt = _semi.format_prompt_sections()
                if _semi_prompt:
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=ctx.strategic_intent_id or "semantic-v1",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=(
                            _existing + "\n\n" + _semi_prompt
                            if _existing else _semi_prompt
                        ),
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )
                    _semi_stats = _semi.stats()
                    logger.info(
                        "[SemanticIndex] op=%s corpus_n=%d centroid_hash8=%s "
                        "inject_site=context_expansion prompt_chars=%d",
                        ctx.op_id, _semi_stats.corpus_n,
                        _semi_stats.centroid_hash8, len(_semi_prompt),
                    )
                else:
                    logger.debug(
                        "[SemanticIndex] op=%s no prompt section (disabled or empty)",
                        ctx.op_id,
                    )
            except Exception:
                logger.debug(
                    "[Orchestrator] SemanticIndex injection skipped",
                    exc_info=True,
                )

            # ---- TaskBoard advisory prompt injection (Gap #5 Slice 3) ----
            #
            # Read-only + authority-free. We do NOT lazily create a board
            # here — only render when the model has already touched a task
            # tool during this op (i.e. a board exists in the registry).
            # Avoids injecting an empty "Current tasks" section on every
            # op. Per authorization: NEVER gates Iron Gate / policy /
            # approval (Manifesto §1 + §6). Tier -1 sanitation inside
            # TaskBoard.render_prompt_section() handles model content
            # safety — we don't fight the sanitizer here.
            try:
                from backend.core.ouroboros.governance.task_tool import (
                    _BOARDS,
                )
                _tb = _BOARDS.get(ctx.op_id)
                if _tb is not None:
                    _tb_prompt = _tb.render_prompt_section()
                    if _tb_prompt:
                        _tb_existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                        ctx = ctx.with_strategic_memory_context(
                            strategic_intent_id=(
                                ctx.strategic_intent_id or "task-board-v1"
                            ),
                            strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                            strategic_memory_prompt=(
                                _tb_existing + "\n\n" + _tb_prompt
                                if _tb_existing else _tb_prompt
                            ),
                            strategic_memory_digest=ctx.strategic_memory_digest,
                        )
                        logger.info(
                            "[TaskBoard] op=%s inject_site=context_expansion "
                            "prompt_chars=%d",
                            ctx.op_id, len(_tb_prompt),
                        )
            except Exception:
                logger.debug(
                    "[Orchestrator] TaskBoard injection skipped", exc_info=True,
                )

            # ---- TDD directive (Feature 1 V1 — prompt contract, NOT red-green) ----
            #
            # When the intent envelope carries evidence["tdd_mode"]=True,
            # prepend a prompt directive instructing the model to emit
            # tests + impl together (test file first in files: [...]).
            # Honest scope: this is a prompt contract, not a red-green
            # proof. True test-first orchestration (run tests → confirm
            # fail → generate impl → run tests → confirm pass) is a
            # separate multi-commit project scoped for V1.1. The V1
            # module ships the declarative layer so ops can be marked
            # TDD now; V1.1 flips the flag from "prompt hint" to
            # "pipeline sub-phase trigger" without client-side changes.
            try:
                from backend.core.ouroboros.governance.tdd_directive import (
                    is_tdd_op,
                    tdd_prompt_directive,
                )
                if is_tdd_op(ctx):
                    _tdd_text = tdd_prompt_directive()
                    _existing = getattr(ctx, "strategic_memory_prompt", "") or ""
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=ctx.strategic_intent_id or "tdd-v1",
                        strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
                        strategic_memory_prompt=(
                            _existing + "\n\n" + _tdd_text
                            if _existing else _tdd_text
                        ),
                        strategic_memory_digest=ctx.strategic_memory_digest,
                    )
                    logger.info(
                        "[TDDDirective] op=%s tdd_mode=true directive_chars=%d "
                        "scope=prompt_contract_not_red_green",
                        ctx.op_id, len(_tdd_text),
                    )
            except Exception:
                logger.debug(
                    "[Orchestrator] TDD directive injection skipped",
                    exc_info=True,
                )

            # ---- Goal inference — hypothesized direction from multi-signal cross-corr ----
            #
            # Closes the "read the room" gap: watch commits, REPL inputs,
            # memory, completed ops, file hotspots, and declared goals;
            # synthesize ranked hypotheses about where the operator is
            # headed. Injected as a clearly-labeled "Inferred Direction
            # (hypotheses — not declared goals)" section so the model
            # weights it BELOW explicit goals. Default OFF, fail-closed.
            #
            # Authority invariant: hypotheses inform prompt surface only.
            # They NEVER affect risk tier, route, guardian findings, gate
            # verdicts, or approval. Operator accepts/rejects via /infer.
            try:
                from backend.core.ouroboros.governance.goal_inference import (
                    GoalInferenceEngine,
                    get_default_engine,
                    inference_enabled,
                    render_prompt_section,
                )
                if inference_enabled():
                    _engine = get_default_engine(self._config.project_root)
                    if _engine is None:
                        _engine = GoalInferenceEngine(
                            repo_root=self._config.project_root,
                        )
                    _inf_result = _engine.build()
                    _inf_text = render_prompt_section(_inf_result)
                    if _inf_text:
                        _existing = getattr(
                            ctx, "strategic_memory_prompt", "",
                        ) or ""
                        ctx = ctx.with_strategic_memory_context(
                            strategic_intent_id=(
                                ctx.strategic_intent_id or "goal-inference-v1"
                            ),
                            strategic_memory_fact_ids=(
                                ctx.strategic_memory_fact_ids
                            ),
                            strategic_memory_prompt=(
                                _existing + "\n\n" + _inf_text
                                if _existing else _inf_text
                            ),
                            strategic_memory_digest=(
                                ctx.strategic_memory_digest
                            ),
                        )
                        logger.info(
                            "[GoalInference] op=%s injected hypotheses=%d "
                            "top_conf=%.2f chars=%d",
                            ctx.op_id,
                            min(
                                len(_inf_result.inferred),
                                # top_k applied inside render
                                5,
                            ),
                            (_inf_result.inferred[0].confidence
                             if _inf_result.inferred else 0.0),
                            len(_inf_text),
                        )
            except Exception:
                logger.debug(
                    "[Orchestrator] Goal inference injection skipped",
                    exc_info=True,
                )

            # ---- LastSessionSummary v0.1: session-to-session episodic continuity ----
            # Read-only structured summary of past session(s), rendered as
            # a dense untrusted block. Injected between SemanticIndex (above)
            # and Goals (below) so the untrusted stack stays contiguous:
            # Strategic → Bridge → Semantic → LastSession → Goals → UserPrefs.
            # Helper extracted for integration-test coverage of the composed
            # CONTEXT_EXPANSION prompt (see test_last_session_summary_composition).
            ctx = _inject_last_session_summary_impl(self._config.project_root, ctx)

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

            # ---- ClassifyClarify: one operator question at the CLASSIFY→ROUTE boundary ----
            #
            # Closes the "intake description is ambiguous" gap. Narrow
            # ambiguity heuristic (short desc + no target files, or generic
            # target list, or no goal-keyword match). On trigger, ask the
            # operator ONE concise question with a bounded timeout. The
            # answer enriches ctx.description + evidence only — it has NO
            # authority over risk classification, routing law, SemanticGuardian
            # findings, or any deterministic engine input (Manifesto §1
            # Boundary Principle).
            #
            # Default OFF (JARVIS_CLASSIFY_CLARIFY_ENABLED=0). Opt-in means
            # no session is interrupted until the operator explicitly
            # enables the feature + the heuristic actually fires.
            try:
                from backend.core.ouroboros.governance.classify_clarify import (
                    ask_operator as _clarify_ask,
                    merge_into_context as _clarify_merge,
                    clarify_enabled as _clarify_enabled,
                )
                if _clarify_enabled():
                    # Extract goal keywords from the active GoalTracker so
                    # the heuristic can check "no goal keyword match".
                    _goal_keywords: tuple = ()
                    try:
                        from backend.core.ouroboros.governance.strategic_direction import (
                            GoalTracker,
                        )
                        _kws: list = []
                        for _g in GoalTracker(
                            self._config.project_root,
                        ).active_goals:
                            _kws.extend(getattr(_g, "keywords", ()) or ())
                        _goal_keywords = tuple(_kws)
                    except Exception:
                        _goal_keywords = ()
                    _clarify_response = await _clarify_ask(
                        op_id=ctx.op_id,
                        description=ctx.description or "",
                        target_files=tuple(ctx.target_files or ()),
                        goal_keywords=_goal_keywords,
                    )
                    if _clarify_response.outcome == "answered":
                        # Merge the sanitized answer into the description.
                        # The risk classifier has ALREADY run above — we do
                        # not re-classify. The clarification only affects
                        # downstream prompt content (description + evidence).
                        _new_desc, _patch = _clarify_merge(
                            original_description=ctx.description or "",
                            response=_clarify_response,
                        )
                        try:
                            import dataclasses as _dc
                            ctx = _dc.replace(ctx, description=_new_desc)
                        except Exception:
                            logger.debug(
                                "[Orchestrator] ClassifyClarify ctx merge skipped",
                                exc_info=True,
                            )
            except Exception:
                logger.debug(
                    "[Orchestrator] ClassifyClarify skipped",
                    exc_info=True,
                )

        # Wave 2 (5) Slice 3 - ROUTE+CTX+PLAN PhaseRunner delegation gate.
        # All three flags (JARVIS_PHASE_RUNNER_{ROUTE,CONTEXT_EXPANSION,PLAN}_EXTRACTED)
        # must be set to engage the runner chain. This all-or-nothing
        # gate simplifies wiring while the three phases remain
        # interleaved (ROUTE body -> conditional CTX -> PLAN body) in the
        # inline pipeline. Per-phase independence arrives with Slice 6
        # (dispatcher cutover).
        if _phase_runner_slice3_fully_extracted():
            from backend.core.ouroboros.governance.phase_runners import (
                ContextExpansionRunner,
                PLANRunner,
                ROUTERunner,
            )
            logger.info("[PhaseRunnerDelegate] ROUTE+CTX+PLAN → runners op=%s", ctx.op_id[:16])
            # ROUTE: runs the routing body + either advance(CTX) or advance(PLAN)
            _route_result = await ROUTERunner(self, _serpent).run(ctx)
            ctx = _route_result.next_ctx
            # CTX: runs only if ROUTERunner advanced to CONTEXT_EXPANSION
            if _route_result.next_phase is OperationPhase.CONTEXT_EXPANSION:
                _ctx_result = await ContextExpansionRunner(self, _serpent).run(ctx)
                ctx = _ctx_result.next_ctx
            # PLAN: advisory artifact comes from CLASSIFY's result — carried
            # via the _advisory local established by the CLASSIFY hook.
            _plan_result = await PLANRunner(self, _serpent, advisory=_advisory).run(ctx)
            if _plan_result.next_phase is None:
                # Terminal exit from PLAN (plan_rejected, plan_expired, etc.)
                return _plan_result.next_ctx
            ctx = _plan_result.next_ctx
        else:
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
                            route=_provider_route.value,
                            route_reason=_route_reason,
                            budget_profile=_UR.route_budget_profile(_provider_route),
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
                    is_read_only=bool(getattr(ctx, "is_read_only", False)),
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
            _plan_review_required_now = _plan_review_required()
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

            # Phase B PLAN-shadow (Slice 1b) — observer-only DAG dispatch.
            # Runs AFTER the legacy PlanGenerator regardless of whether the
            # legacy plan succeeded or skipped. Gated by
            # JARVIS_PLAN_SUBAGENT_SHADOW (default false). The shadow never
            # raises and never blocks the FSM; its only side-effect is
            # setting ctx.execution_graph + emitting [PLAN-SHADOW] telemetry.
            try:
                ctx = await self._run_plan_shadow(ctx)
            except Exception:
                # Defense in depth — the hook itself is exception-safe but
                # an awaitable propagation through asyncio.wait_for etc.
                # could surface edge-case cancellations. Never propagate.
                logger.debug(
                    "[Orchestrator] PLAN-shadow wrapper swallowed exception",
                    exc_info=True,
                )

            if _plan_review_required_now and (
                _plan_result is None or getattr(_plan_result, "skipped", True)
            ):
                _skip_reason = getattr(_plan_result, "skip_reason", "") or "plan_not_available"
                logger.info(
                    "[Orchestrator] Plan review required for op=%s but no plan is "
                    "available: %s",
                    ctx.op_id,
                    _skip_reason,
                )
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="plan_required_unavailable",
                )
                await self._record_ledger(
                    ctx,
                    OperationState.FAILED,
                    {
                        "reason": "plan_required_unavailable",
                        "detail": _skip_reason,
                    },
                )
                return ctx

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
            _plan_gate_enabled = _plan_review_required_now or (
                os.environ.get("JARVIS_PLAN_APPROVAL_ENABLED", "true").lower()
                not in ("false", "0", "no", "off")
            )
            _plan_gate_applied = False
            if (
                _plan_gate_enabled
                and _plan_result is not None
                and not getattr(_plan_result, "skipped", True)
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
                # Problem #7 Slice 2: plan-mode force-review override.
                # When JARVIS_PLAN_APPROVAL_MODE=true (or ctx opt-in)
                # the operator has explicitly asked to halt EVERY op
                # for review, regardless of the complexity heuristic.
                # Late import keeps plan_approval optional — if the
                # module is unavailable for any reason, plan mode is
                # treated as off. Never raises.
                _plan_mode_force = False
                try:
                    from backend.core.ouroboros.governance.plan_approval import (
                        should_force_plan_review as _should_force_plan_review,
                    )
                    _plan_mode_force = _should_force_plan_review(ctx)
                except Exception:  # noqa: BLE001 — optional dep
                    _plan_mode_force = False
                _should_gate = (
                    _plan_review_required_now
                    or _plan_mode_force
                    or _route in _gate_routes
                    or _task_cx in _gate_complexities
                    or _plan_cx in _gate_complexities
                )
                _provider_supports_plan = (
                    self._approval_provider is not None
                    and hasattr(self._approval_provider, "request_plan")
                )
                if _should_gate and not _provider_supports_plan:
                    logger_msg = (
                        "[Orchestrator] Plan review required for op=%s but no "
                        "plan approval provider is available"
                        if _plan_review_required_now
                        else "[Orchestrator] Plan Gate skipped for op=%s: "
                        "provider=%s has_request_plan=%s"
                    )
                    if _plan_review_required_now:
                        logger.info(logger_msg, ctx.op_id)
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="plan_review_unavailable",
                        )
                        await self._record_ledger(
                            ctx,
                            OperationState.FAILED,
                            {
                                "reason": "plan_review_unavailable",
                                "detail": "approval_provider_missing",
                            },
                        )
                        return ctx
                    logger.debug(
                        logger_msg,
                        ctx.op_id,
                        type(self._approval_provider).__name__
                        if self._approval_provider
                        else "None",
                        hasattr(self._approval_provider, "request_plan"),
                    )
                elif _should_gate:
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

                    # Problem #7 Slice 2: shadow-register this plan with
                    # the PlanApprovalController so REPL (/plan pending,
                    # /plan show) and IDE observability (/observability/
                    # plans, SSE plan_* events) surface it. The primary
                    # approval authority stays with self._approval_provider;
                    # this is a read-only mirror for operator visibility.
                    # Best-effort: any failure (module unavailable,
                    # duplicate op_id, etc.) silently no-ops — the actual
                    # approval path is unaffected.
                    _plan_mirror_registered = False
                    try:
                        from backend.core.ouroboros.governance.plan_approval import (
                            get_default_controller as _get_pa_controller,
                        )
                        _pa_controller = _get_pa_controller()
                        if _pa_controller.snapshot(ctx.op_id) is None:
                            _pa_controller.request_approval(
                                ctx.op_id,
                                {
                                    "markdown": _plan_markdown,
                                    "description": getattr(ctx, "description", ""),
                                    "target_files": list(
                                        getattr(ctx, "target_files", []) or [],
                                    ),
                                    "approach": getattr(
                                        _plan_result, "approach", "",
                                    ) or "",
                                    "complexity": getattr(
                                        _plan_result, "complexity", "",
                                    ) or "",
                                    "ordered_changes": list(
                                        getattr(
                                            _plan_result, "ordered_changes", [],
                                        ) or [],
                                    ),
                                    "risk_factors": list(
                                        getattr(
                                            _plan_result, "risk_factors", [],
                                        ) or [],
                                    ),
                                    "test_strategy": getattr(
                                        _plan_result, "test_strategy", "",
                                    ) or "",
                                },
                                timeout_s=_plan_gate_timeout,
                            )
                            _plan_mirror_registered = True
                    except Exception:  # noqa: BLE001 — best-effort mirror
                        logger.debug(
                            "[Orchestrator] PlanApproval mirror register "
                            "best-effort failed for op=%s", ctx.op_id,
                            exc_info=True,
                        )

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
                        if _plan_review_required_now:
                            logger.info(
                                "[Orchestrator] Plan review required for op=%s but "
                                "the plan gate failed: %s",
                                ctx.op_id,
                                _gate_exc,
                            )
                            ctx = ctx.advance(
                                OperationPhase.CANCELLED,
                                terminal_reason_code="plan_review_unavailable",
                            )
                            await self._record_ledger(
                                ctx,
                                OperationState.FAILED,
                                {
                                    "reason": "plan_review_unavailable",
                                    "detail": str(_gate_exc)[:200],
                                },
                            )
                            return ctx
                        # Gate infrastructure failure — log and continue without
                        # gating rather than blocking the pipeline forever.
                        logger.warning(
                            "[Orchestrator] Plan Gate infra failure for op=%s: %s; "
                            "continuing to GENERATE without approval",
                            ctx.op_id, _gate_exc,
                        )
                        _plan_decision = None  # type: ignore[assignment]

                    if _plan_decision is not None:
                        # Problem #7 Slice 2: mirror the decision onto
                        # the PlanApprovalController shadow so REPL /
                        # IDE views see the terminal transition. Best-
                        # effort; never raises.
                        if _plan_mirror_registered:
                            try:
                                from backend.core.ouroboros.governance.plan_approval import (
                                    get_default_controller as _get_pa_ctrl,
                                    PlanApprovalStateError as _PAStateError,
                                )
                                _pa_mirror_ctrl = _get_pa_ctrl()
                                _mirror_approver = (
                                    getattr(_plan_decision, "approver", None)
                                    or "orchestrator"
                                )
                                try:
                                    if _plan_decision.status is ApprovalStatus.APPROVED:
                                        _pa_mirror_ctrl.approve(
                                            ctx.op_id, reviewer=_mirror_approver,
                                        )
                                    elif _plan_decision.status is ApprovalStatus.REJECTED:
                                        _pa_mirror_ctrl.reject(
                                            ctx.op_id,
                                            reason=getattr(
                                                _plan_decision, "reason", "",
                                            ) or "",
                                            reviewer=_mirror_approver,
                                        )
                                    # EXPIRED path: the controller's own
                                    # timeout already auto-rejects; no
                                    # additional mirror call needed.
                                except _PAStateError:
                                    # Already terminal — the controller's
                                    # timeout_task may have expired the
                                    # shadow first. Harmless; skip.
                                    pass
                            except Exception:  # noqa: BLE001 — best-effort
                                logger.debug(
                                    "[Orchestrator] PlanApproval mirror terminal "
                                    "propagation best-effort failed for op=%s",
                                    ctx.op_id, exc_info=True,
                                )
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
                            self._add_session_lesson(
                                "code",
                                f"[PLAN REJECTED] {ctx.description[:60]} "
                                f"({_files_short}) — human rejected the approach: "
                                f"{_reject_reason[:80] or 'no reason given'}. "
                                f"Reconsider strategy before retry.",
                                op_id=ctx.op_id,
                            )
                            return ctx

                        if _plan_decision.status is ApprovalStatus.EXPIRED:
                            if _expire_grace and not _plan_review_required_now:
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
            ctx = ctx.advance(OperationPhase.GENERATE)

            # ── Option C: DW topology early-detection circuit breaker ──
            # Pre-GENERATE check: if route=BACKGROUND AND topology says
            # skip_and_queue AND op is NOT read-only, the op is
            # structurally doomed (CandidateGenerator will raise
            # background_dw_blocked_by_topology when invoked). Skip
            # the GENERATE phase entirely and go straight to the same
            # graceful-accept path the late-detection branch already
            # uses (CANCELLED + FAILED ledger). Outcome is byte-
            # identical to today's late-detection path; the difference
            # is "[CircuitBreaker] pre-GENERATE skip" log instead of
            # "BACKGROUND route: DW failed... accepting" after a
            # generation hot-path entry.
            #
            # Master flag JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED
            # (default false). When off, this block is a no-op and
            # the late-detection path runs exactly as before.
            try:
                from backend.core.ouroboros.governance.dw_topology_circuit_breaker import (  # noqa: E501
                    is_circuit_breaker_enabled as _cb_enabled,
                    ledger_reason_label as _cb_ledger_label,
                    should_circuit_break as _cb_should_break,
                    terminal_reason_code as _cb_terminal_code,
                )
                if _cb_enabled():
                    _cb_break, _cb_reason = _cb_should_break(
                        provider_route=getattr(
                            ctx, "provider_route", "",
                        ) or "",
                        is_read_only=bool(
                            getattr(ctx, "is_read_only", False),
                        ),
                    )
                    if _cb_break:
                        logger.info(
                            "[CircuitBreaker] pre-GENERATE skip: "
                            "route=%s reason=%s op=%s",
                            getattr(ctx, "provider_route", "?"),
                            _cb_reason[:120],
                            (ctx.op_id or "?")[:16],
                        )
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code=(
                                _cb_terminal_code(_cb_reason)
                            ),
                        )
                        await self._record_ledger(
                            ctx, OperationState.FAILED,
                            {
                                "reason": _cb_ledger_label(_cb_reason),
                                "topology_reason": _cb_reason[:200],
                                "route": getattr(
                                    ctx, "provider_route", "",
                                ),
                                "circuit_breaker_fired": True,
                            },
                        )
                        return ctx
            except Exception:  # noqa: BLE001 — never let circuit
                # breaker crash GENERATE entry. The late-detection
                # path remains the authoritative behavior.
                logger.debug(
                    "[CircuitBreaker] consultation raised — falling "
                    "through to late-detection path",
                    exc_info=True,
                )

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

        # Wave 2 (5) Slice 5a/5b - GENERATERunner delegation gate.
        # Flag JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED (default false) routes
        # the 1611-line GENERATE block through the extracted PhaseRunner.
        # Cross-phase artifacts (generation, _episodic_memory) threaded
        # via artifacts for VALIDATE consumption.
        if _phase_runner_generate_extracted():
            from backend.core.ouroboros.governance.phase_runners.generate_runner import (
                GENERATERunner,
            )
            logger.info("[PhaseRunnerDelegate] GENERATE → runner op=%s", ctx.op_id[:16])
            _generate_runner = GENERATERunner(self, _serpent, _consciousness_bridge)
            _generate_result = await _generate_runner.run(ctx)
            generation = _generate_result.artifacts.get("generation")
            _episodic_memory = _generate_result.artifacts.get("episodic_memory")
            # generate_retries_remaining is consumed by VALIDATE's entropy
            # computation (orchestrator.py ~5402 retries_used=...).
            generate_retries_remaining = _generate_result.artifacts.get(
                "generate_retries_remaining",
                self._config.max_generate_retries,
            )
            if _generate_result.next_phase is None:
                # Terminal exit (cost cap / no_forward_progress / stalled /
                # l2 escape / iron gate failure / etc.)
                return _generate_result.next_ctx
            ctx = _generate_result.next_ctx
        else:
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
            # Ledger-path counterpart to _op_explore_credit (#103).
            # When JARVIS_EXPLORATION_LEDGER_ENABLED is true the Iron Gate consults
            # ExplorationLedger.from_records(_op_explore_records) instead of the
            # int counter. Records accumulate across retries so the ledger sees
            # the union of every tool call the model has made for this op, then
            # dedup-by-(tool, arguments_hash) happens inside diversity_score().
            _op_explore_records: List[Any] = []

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
                    # Per-route generation window — env-tunable so battle-test
                    # harnesses (e.g. live_fire_exploration_gate.py) can widen
                    # the architectural COMPLEX window without patching code.
                    # Defaults preserve the 2026-04-12 calibration.
                    _route_timeouts = {
                        "immediate": float(os.environ.get(
                            "JARVIS_GEN_TIMEOUT_IMMEDIATE_S", "120")),
                        "standard": float(os.environ.get(
                            "JARVIS_GEN_TIMEOUT_STANDARD_S", "220")),
                        "complex": float(os.environ.get(
                            "JARVIS_GEN_TIMEOUT_COMPLEX_S", "240")),
                        "background": float(os.environ.get(
                            "JARVIS_GEN_TIMEOUT_BACKGROUND_S", "180")),
                        "speculative": float(os.environ.get(
                            "JARVIS_GEN_TIMEOUT_SPECULATIVE_S", "180")),
                    }
                    _gen_timeout = _route_timeouts.get(
                        _route, self._config.generation_timeout_s
                    )
                    # Read-only BG/SPEC subagent fan-out override (Session 6,
                    # Derek 2026-04-17). The outer asyncio.wait_for at line
                    # below enforces this timeout absolutely — when the op
                    # is read-only and routed BG/SPEC, three parallel
                    # subagents can consume MAX_PARALLEL_SCOPES *
                    # PRIMARY_PROVIDER_TIMEOUT_S seconds of wall-clock before
                    # the parent Claude begins synthesis. 180s is the
                    # Session-5/6 killer. The cap-extension in candidate_
                    # generator._call_fallback is necessary but insufficient
                    # — this outer gate must also widen.
                    if (
                        bool(getattr(ctx, "is_read_only", False))
                        and _route in ("background", "speculative")
                    ):
                        try:
                            from backend.core.ouroboros.governance.subagent_contracts import (
                                MAX_PARALLEL_SCOPES,
                                PRIMARY_PROVIDER_TIMEOUT_S,
                            )
                            _fanout_budget_s = (
                                MAX_PARALLEL_SCOPES * PRIMARY_PROVIDER_TIMEOUT_S
                            )
                        except Exception:
                            _fanout_budget_s = 3 * 90  # Phase 1 Defaults
                        # Default 180s matches candidate_generator
                        # _BG_READONLY_SYNTHESIS_RESERVE_S — the two must
                        # stay aligned so the inner fallback cap and the
                        # outer orchestrator wait_for use the same reserve
                        # assumption. Session 12 empirically sized this.
                        _synthesis_reserve_s = float(os.environ.get(
                            "JARVIS_GEN_TIMEOUT_READONLY_SYNTHESIS_RESERVE_S",
                            "180",
                        ))
                        _gen_timeout_readonly = _gen_timeout + _fanout_budget_s + _synthesis_reserve_s
                        # Allow operator override via dedicated env var.
                        _gen_timeout_readonly = float(os.environ.get(
                            "JARVIS_GEN_TIMEOUT_BACKGROUND_READONLY_S",
                            str(_gen_timeout_readonly),
                        ))
                        logger.info(
                            "[Orchestrator] Read-only %s route: extending "
                            "gen_timeout %.0fs → %.0fs (fanout_budget=%.0fs, "
                            "synthesis_reserve=%.0fs) op=%s",
                            _route, _gen_timeout, _gen_timeout_readonly,
                            _fanout_budget_s, _synthesis_reserve_s, ctx.op_id,
                        )
                        _gen_timeout = _gen_timeout_readonly
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
                    # Operator-visible token streaming (UX Priority 2 — closes
                    # the "spinner for 2 minutes" gap). Gated on (1) the
                    # JARVIS_UI_STREAMING_ENABLED env flag (checked inside the
                    # renderer), and (2) the route: only IMMEDIATE / STANDARD /
                    # COMPLEX are operator-visible. BACKGROUND and SPECULATIVE
                    # skip — no operator is watching, and streaming serialization
                    # would waste CPU that should go to inference.
                    _stream_renderer = None
                    if _route not in ("background", "speculative"):
                        try:
                            from backend.core.ouroboros.battle_test.stream_renderer import (
                                get_stream_renderer,
                            )
                            _stream_renderer = get_stream_renderer()
                            if _stream_renderer is not None:
                                # Provider name is unknown at this point
                                # (adaptive failback chooses mid-generate).
                                # Pass empty string; the renderer's INFO line
                                # will show provider="" rather than mislabeling
                                # with task_complexity.
                                _stream_renderer.start(
                                    op_id=ctx.op_id,
                                    provider="",
                                )
                        except Exception:
                            logger.debug(
                                "[Orchestrator] stream renderer start failed",
                                exc_info=True,
                            )
                            _stream_renderer = None
                    # Hard timeout — the deadline is advisory to the generator,
                    # but asyncio.wait_for is the Iron Gate (Manifesto §6).
                    try:
                        # Phase B parallel-edge exploitation (Manifesto §2 + §3).
                        # Attempt DAG-driven fan-out first; on ANY fallback
                        # condition (flag off, no DAG, invalid DAG, edges>0,
                        # single-unit, BG route, read-only, per-unit error /
                        # timeout / noop) returns None — legacy single-stream
                        # path runs byte-identically below.
                        _parallel_gen = None
                        try:
                            from backend.core.ouroboros.governance.plan_exploit import (
                                try_parallel_generate,
                            )
                            _parallel_gen = await try_parallel_generate(
                                ctx,
                                deadline,
                                _gen_timeout,
                                self._generator,
                                outer_grace_s=_OUTER_GATE_GRACE_S,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # Observer contract: the exploit hook must NEVER
                            # break the FSM. Any unexpected failure routes
                            # straight to the legacy path.
                            logger.debug(
                                "[Orchestrator] plan_exploit fan-out raised — "
                                "falling back to legacy generate",
                                exc_info=True,
                            )
                            _parallel_gen = None

                        if _parallel_gen is not None:
                            generation = _parallel_gen
                        else:
                            generation = await asyncio.wait_for(
                                self._generator.generate(ctx, deadline),
                                timeout=_gen_timeout + _OUTER_GATE_GRACE_S,
                            )
                    finally:
                        # End the stream regardless of success / failure so the
                        # Live widget closes and the observability INFO line
                        # emits TTFT + TPS even when generation times out.
                        if _stream_renderer is not None:
                            try:
                                _stream_renderer.end()
                            except Exception:
                                logger.debug(
                                    "[Orchestrator] stream renderer end failed",
                                    exc_info=True,
                                )
                    # Charge the CostGovernor with the actual generation cost.
                    # Non-positive costs (cache hits, fallback stubs) are a no-op.
                    try:
                        _cost_this_call = float(getattr(generation, "cost_usd", 0.0) or 0.0)
                        _prov_name = getattr(generation, "provider_name", "") or ""
                        if _cost_this_call > 0.0:
                            # Slice 2 of Per-Phase Cost Drill-Down arc:
                            # tag charge with current phase so the operator
                            # can answer "why did this op cost $X" per-phase.
                            _phase_tag = getattr(
                                getattr(ctx, "phase", None), "name", "",
                            ) or ""
                            self._cost_governor.charge(
                                ctx.op_id, _cost_this_call, _prov_name,
                                phase=_phase_tag,
                            )
                            await self._emit_route_cost_heartbeat(
                                ctx,
                                cost_usd=_cost_this_call,
                                provider=_prov_name,
                                route=getattr(ctx, "provider_route", "") or "standard",
                                cost_event="generation_attempt",
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
                        # Preloaded-prompt credit: when the lean prompt builder
                        # inlines target regions directly into the generation
                        # prompt, the model has already "seen" those files without
                        # needing a read_file tool call — semantically equivalent
                        # exploration. Gives DW BACKGROUND route (no tool loop)
                        # and simple/trivial ops a fair path through the gate.
                        _preloaded_credit = len(
                            getattr(generation, "prompt_preloaded_files", ()) or ()
                        )
                        # Roll the per-attempt count into the per-op credit BEFORE
                        # comparing — a prior attempt that already satisfied the
                        # floor lets a no-tool retry pass (the rejected file is
                        # already in the retry-feedback prompt).
                        _op_explore_credit += _explore_count + _preloaded_credit

                        # Accumulate ledger records across retry attempts (#103).
                        # Cumulative semantics mirror _op_explore_credit — the
                        # ledger sees every tool call the model has made for this
                        # op, then dedup-by-(tool, arguments_hash) happens inside
                        # diversity_score(). Preloaded files become synthetic
                        # read_file records so the ledger grants comprehension
                        # credit matching the legacy counter's preload behavior.
                        _op_explore_records.extend(
                            generation.tool_execution_records or ()
                        )
                        for _pf in (
                            getattr(generation, "prompt_preloaded_files", ()) or ()
                        ):
                            _op_explore_records.append(
                                _PreloadedExplorationRecord(str(_pf))
                            )

                        from backend.core.ouroboros.governance.exploration_engine import (  # noqa: E501
                            ExplorationFloors,
                            ExplorationInsufficientError,
                            ExplorationLedger,
                            evaluate_exploration,
                            is_ledger_enabled,
                        )

                        if is_ledger_enabled():
                            # ── DECISION path (#103) ──
                            # Ledger is authoritative. Legacy int-counter gate is
                            # skipped entirely. Emit ``(decision)`` log tag — kept
                            # distinct from ``(shadow)`` so ops can grep either
                            # mode without ambiguity.
                            try:
                                _ledger = ExplorationLedger.from_records(
                                    _op_explore_records
                                )
                                _floors = ExplorationFloors.from_env_with_adapted(_task_complexity)
                                _verdict = evaluate_exploration(_ledger, _floors)
                            except Exception:
                                # If the ledger itself blows up, fall through to
                                # the legacy counter gate so we never leave the op
                                # ungated. Log once so the failure is visible.
                                logger.exception(
                                    "[Orchestrator] ExplorationLedger(decision) "
                                    "evaluation failed — falling back to counter"
                                )
                                _verdict = None
                            if _verdict is not None:
                                _covered_names = sorted(
                                    c.value for c in _verdict.categories_covered
                                )
                                logger.info(
                                    "[Orchestrator] ExplorationLedger(decision) "
                                    "op=%s complexity=%s score=%.2f min_score=%.2f "
                                    "unique=%d categories=%s would_pass=%s",
                                    ctx.op_id[:12],
                                    _task_complexity or "unknown",
                                    _verdict.score,
                                    _floors.min_score,
                                    _ledger.unique_call_count(),
                                    ",".join(_covered_names) or "-",
                                    _verdict.sufficient,
                                )
                                if _verdict.insufficient:
                                    _missing = sorted(
                                        c.value for c in _verdict.missing_categories
                                    )
                                    _decision_msg = (
                                        f"exploration_insufficient: "
                                        f"score={_verdict.score:.1f}/"
                                        f"{_floors.min_score:.1f} "
                                        f"categories={len(_verdict.categories_covered)}/"
                                        f"{_floors.min_categories} "
                                        f"missing={','.join(_missing) or '-'}"
                                    )
                                    logger.warning(
                                        "[Orchestrator] Iron Gate — "
                                        "ExplorationLedger(decision) insufficient "
                                        "op=%s %s (attempt=%d)",
                                        ctx.op_id[:12],
                                        _decision_msg,
                                        attempt + 1,
                                    )
                                    generation = None
                                    raise ExplorationInsufficientError(
                                        _decision_msg,
                                        verdict=_verdict,
                                        floors=_floors,
                                    )
                                # Ledger PASSED — skip legacy counter gate
                                # entirely. Jump to the ASCII gate below.
                            else:
                                # Ledger eval crashed → fall through to legacy gate
                                pass

                        # ── LEGACY path (flag off) or ledger-eval fallback ──
                        # Shadow log + int-counter gate. Shadow log is suppressed
                        # when enforcement is on (the decision log above covers
                        # that path) so operators don't see duplicate lines.
                        if not is_ledger_enabled():
                            _shadow_on = (
                                os.environ.get(
                                    "JARVIS_EXPLORATION_SHADOW_LOG", "",
                                ).strip().lower() in _TRUTHY
                            )
                            if _shadow_on:
                                try:
                                    _sledger = ExplorationLedger.from_records(
                                        _op_explore_records
                                    )
                                    _sfloors = ExplorationFloors.from_env_with_adapted(
                                        _task_complexity
                                    )
                                    _sverdict = evaluate_exploration(
                                        _sledger, _sfloors
                                    )
                                    _scovered = sorted(
                                        c.value for c in _sverdict.categories_covered
                                    )
                                    logger.info(
                                        "[Orchestrator] ExplorationLedger(shadow) "
                                        "op=%s complexity=%s legacy_credit=%d "
                                        "score=%.2f min_score=%.2f unique=%d "
                                        "categories=%s would_pass=%s",
                                        ctx.op_id[:12],
                                        _task_complexity or "unknown",
                                        _op_explore_credit,
                                        _sverdict.score,
                                        _sfloors.min_score,
                                        _sledger.unique_call_count(),
                                        ",".join(_scovered) or "-",
                                        _sverdict.sufficient,
                                    )
                                except Exception:
                                    logger.debug(
                                        "[Orchestrator] ExplorationLedger shadow "
                                        "log error",
                                        exc_info=True,
                                    )

                        if (
                            not is_ledger_enabled()
                            and _op_explore_credit < _min_explore
                        ):
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
                                "%d/%d (attempt=%d cumulative, preloaded=%d) for op=%s",
                                _op_explore_credit, _min_explore, attempt + 1,
                                _preloaded_credit, ctx.op_id[:12],
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

                    # Gate 5 — Multi-file coverage. Session O (bt-2026-04-15-
                    # 175547) closed the full governed APPLY arc but only 1
                    # of 4 target files landed on disk because the winning
                    # candidate returned legacy {file_path, full_content}
                    # instead of {files: [...]}, so _apply_multi_file_candidate
                    # was never invoked. This gate rejects any multi-target op
                    # whose candidate fails to cover every path in
                    # context.target_files via a populated files: [...] list.
                    # The retry-feedback builder names the missing paths and
                    # reiterates the multi-file contract. Master switch:
                    # JARVIS_MULTI_FILE_ENFORCEMENT (default true).
                    try:
                        from backend.core.ouroboros.governance.multi_file_coverage_gate import (
                            check_candidate as _mf_check,
                        )
                    except ImportError:
                        _mf_check = None  # type: ignore[assignment]
                    if _mf_check is not None:
                        for _cand in generation.candidates:
                            _mf_result = _mf_check(
                                _cand,
                                ctx.target_files,
                                self._config.project_root,
                            )
                            if _mf_result is None:
                                continue
                            _mf_reason, _mf_missing = _mf_result
                            logger.warning(
                                "[Orchestrator] Iron Gate — multi_file_coverage: "
                                "missing %d/%d [%s] op=%s",
                                len(_mf_missing),
                                len(ctx.target_files),
                                ", ".join(_mf_missing[:5]),
                                ctx.op_id[:12],
                            )
                            generation = None
                            _mf_exc = RuntimeError(_mf_reason)
                            # Private attributes — retry feedback builder reads these.
                            _mf_exc._mf_missing_paths = _mf_missing  # type: ignore[attr-defined]
                            _mf_exc._mf_target_files = tuple(ctx.target_files)  # type: ignore[attr-defined]
                            raise _mf_exc

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
                    _route = getattr(ctx, "provider_route", "")

                    # ── Partial shadow log (widened) ──
                    # Fire the ExplorationLedger shadow pass for EVERY
                    # generation failure, regardless of route/cause. The
                    # original BG-DW-only branch missed failure modes like
                    # doubleword_schema_invalid, all_providers_exhausted,
                    # APITimeout. We classify the cause from _err_msg so the
                    # log line still tells you what killed the attempt, and
                    # we pull whatever tool_execution_records are reachable
                    # off the exception (may be empty). No-op when shadow
                    # logging is off so this stays free in production.
                    _shadow_on_partial = (
                        os.environ.get(
                            "JARVIS_EXPLORATION_SHADOW_LOG", "",
                        ).strip().lower() in {"1", "true", "yes", "on"}
                    )
                    if _shadow_on_partial:
                        try:
                            from backend.core.ouroboros.governance.exploration_engine import (  # noqa: E501
                                ExplorationFloors,
                                ExplorationLedger,
                                evaluate_exploration,
                            )
                            _partial_records = getattr(
                                exc, "tool_execution_records", ()
                            ) or ()
                            _pledger = ExplorationLedger.from_records(_partial_records)
                            _ptask_complexity = getattr(
                                ctx, "task_complexity", "",
                            ) or ""
                            _pfloors = ExplorationFloors.from_env_with_adapted(_ptask_complexity)
                            _pverdict = evaluate_exploration(_pledger, _pfloors)
                            _pcovered = sorted(
                                c.value for c in _pverdict.categories_covered
                            )
                            # Classify cause from error string — cheap
                            # substring match, no regex. Order matters:
                            # most specific first.
                            if "background_dw_" in _err_msg:
                                _pcause = "bg_dw_failure"
                            elif "doubleword_schema_invalid" in _err_msg:
                                _pcause = "dw_schema_invalid"
                            elif "all_providers_exhausted" in _err_msg:
                                _pcause = "all_providers_exhausted"
                            elif "APITimeout" in _err_msg or "timeout" in _err_msg.lower():
                                _pcause = "provider_timeout"
                            else:
                                _pcause = "generic_gen_failure"
                            logger.info(
                                "[Orchestrator] ExplorationLedger(shadow,partial) "
                                "op=%s complexity=%s route=%s cause=%s "
                                "records=%d score=%.2f min_score=%.2f unique=%d "
                                "categories=%s would_pass=%s",
                                ctx.op_id[:12],
                                _ptask_complexity or "unknown",
                                _route or "unknown",
                                _pcause,
                                len(_partial_records),
                                _pverdict.score,
                                _pfloors.min_score,
                                _pledger.unique_call_count(),
                                ",".join(_pcovered) or "-",
                                _pverdict.sufficient,
                            )
                        except Exception:
                            logger.debug(
                                "[Orchestrator] ExplorationLedger partial shadow log error",
                                exc_info=True,
                            )

                    # ── BACKGROUND / SPECULATIVE route failures ──
                    # These routes intentionally avoid Claude. Don't retry
                    # with expensive providers — accept failure gracefully.
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

                    if _route == "background" and (
                        "background_dw_" in _err_msg
                        or "background_fallback_failed" in _err_msg
                    ):
                        # Background failure — accept gracefully, don't
                        # hammer the retry loop. Covers both the legacy
                        # DW-only failure mode ("background_dw_*") and the
                        # new cascade failure mode
                        # ("background_fallback_failed:...") introduced when
                        # JARVIS_BACKGROUND_ALLOW_FALLBACK=true and the
                        # Claude cascade itself also fails. In either case,
                        # the sensor will re-detect if the underlying work
                        # is still relevant.
                        _is_cascade_failure = "background_fallback_failed" in _err_msg
                        logger.info(
                            "[Orchestrator] BACKGROUND route: %s failed (%s), "
                            "accepting [%s]",
                            "DW+Claude cascade" if _is_cascade_failure else "DW",
                            _err_msg[:120], ctx.op_id,
                        )
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code=f"background_accepted:{_err_msg[:80]}",
                        )
                        await self._record_ledger(
                            ctx, OperationState.FAILED,
                            {
                                "reason": (
                                    "background_cascade_failure"
                                    if _is_cascade_failure else "background_dw_failure"
                                ),
                                "error": _err_msg[:200],
                                "route": "background",
                            },
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
                            try:
                                await self._stack.comm.emit_decision(
                                    op_id=ctx.op_id,
                                    outcome="standard",
                                    reason_code="route_demoted:immediate_exhausted",
                                    details={
                                        "route": "standard",
                                        "previous_route": "immediate",
                                        "route_description": "Demoted to STANDARD after IMMEDIATE exhaustion",
                                        "budget_profile": "220s fallback budget",
                                        "route_reason": getattr(ctx, "provider_route_reason", ""),
                                    },
                                )
                            except Exception:
                                pass
                            _route = "standard"  # update local for timeout calc
                            # Refresh the cost-governor cap for the new route so
                            # the demotion gets a proportional budget headroom.
                            try:
                                self._cost_governor.start(
                                    op_id=ctx.op_id,
                                    route="standard",
                                    complexity=getattr(ctx, "task_complexity", "") or "",
                                    is_read_only=bool(getattr(ctx, "is_read_only", False)),
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
                                        timeout=220.0 + _OUTER_GATE_GRACE_S,
                                    )
                                    # Charge demotion call cost (may be zero).
                                    try:
                                        _dem_cost = float(getattr(generation, "cost_usd", 0.0) or 0.0)
                                        _dem_prov = getattr(generation, "provider_name", "") or ""
                                        if _dem_cost > 0.0:
                                            _dem_phase = getattr(
                                                getattr(ctx, "phase", None),
                                                "name", "",
                                            ) or ""
                                            self._cost_governor.charge(
                                                ctx.op_id, _dem_cost, _dem_prov,
                                                phase=_dem_phase,
                                            )
                                            await self._emit_route_cost_heartbeat(
                                                ctx,
                                                cost_usd=_dem_cost,
                                                provider=_dem_prov,
                                                route="standard",
                                                cost_event="demotion_attempt",
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
                    # P2: Dynamic Re-Planning — suggest alternative strategy on failure.
                    # Two-stage cascade:
                    #   (1) PlanFalsificationDetector (Slice 4 bridge) — proactive,
                    #       structural, evidence-typed. Preempts when plan steps
                    #       are falsified by filesystem probe + typed validation
                    #       evidence.
                    #   (2) DynamicRePlanner (legacy reactive) — backstop when
                    #       structural detector returns NO_FALSIFICATION /
                    #       INSUFFICIENT_EVIDENCE / DISABLED / FAILED.
                    _replan_text = ""
                    try:
                        _fc = validation.failure_class or "" if 'validation' in dir() else ""
                        _em = validation.short_summary or "" if 'validation' in dir() else ""
                        _attempt_num = self._config.max_generate_retries - generate_retries_remaining + 1
                        # Stage 1 — structural falsification (proactive)
                        try:
                            from backend.core.ouroboros.governance.plan_falsification_orchestrator_bridge import (  # noqa: E501
                                bridge_to_replan as _falsification_bridge,
                            )
                            _fals_verdict, _fals_text = await _falsification_bridge(
                                plan_json=getattr(ctx, "implementation_plan", "") or "",
                                validation_failure_class=_fc,
                                validation_short_summary=_em,
                                target_files=tuple(getattr(ctx, "target_files", ()) or ()),
                                project_root=self._config.project_root,
                                op_id=ctx.op_id,
                            )
                            if _fals_text:
                                _replan_text = _fals_text
                                logger.info(
                                    "[Orchestrator] Falsification re-plan: "
                                    "step=%s kinds=%s (attempt %d) [%s]",
                                    _fals_verdict.falsified_step_index,
                                    ",".join(_fals_verdict.falsifying_evidence_kinds),
                                    _attempt_num, ctx.op_id,
                                )
                        except Exception as _fb_exc:
                            logger.debug(
                                "[Orchestrator] Falsification bridge degraded: %s",
                                _fb_exc,
                            )
                        # Stage 2 — legacy reactive (backstop, only if Stage 1 silent)
                        if not _replan_text:
                            from backend.core.ouroboros.governance.self_evolution import DynamicRePlanner
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
                        # Ledger path (#103): when the exception carries a
                        # verdict + floors, render a category-aware feedback
                        # block so the model sees *which* categories are missing
                        # rather than the generic "call more tools" boilerplate.
                        # Legacy counter path has neither attribute and falls
                        # through to the hand-written block below.
                        _exc_verdict = getattr(exc, "verdict", None)
                        _exc_floors = getattr(exc, "floors", None)
                        if _exc_verdict is not None and _exc_floors is not None:
                            try:
                                from backend.core.ouroboros.governance.exploration_engine import (  # noqa: E501
                                    render_retry_feedback,
                                )
                                _ledger_feedback = render_retry_feedback(
                                    _exc_verdict, _exc_floors,
                                )
                            except Exception:
                                _ledger_feedback = ""
                        else:
                            _ledger_feedback = ""
                        if _ledger_feedback:
                            # ── CRITICAL_SYSTEM_OVERRIDE escalation ──
                            # Live-fire botyivw5b proved the feedback was
                            # landing in the prompt but the model was
                            # attending to the front-loaded task description
                            # and tool boilerplate instead of the retry
                            # directive. This is an attention-mechanism
                            # interference problem, not an injection
                            # problem. The three-pronged fix (this block is
                            # prong 2):
                            #
                            #   1. recency bias — _build_lean_codegen_prompt
                            #      appends strategic_memory as the ABSOLUTE
                            #      LAST section (after output schema), so
                            #      the model reads it last.
                            #   2. XML structural override — frontier models
                            #      are fine-tuned to obey
                            #      ``<CRITICAL_SYSTEM_OVERRIDE>`` tags at
                            #      higher priority than general prompt text.
                            #      "Mathematically required" language raises
                            #      perceived authority.
                            #   3. simulated assistant prefill — the lean
                            #      builder appends a model-voice commitment
                            #      stub after this block (persona
                            #      continuation kill switch; literal API
                            #      prefill is incompatible with the JSON
                            #      contract + tool_use response type on
                            #      sonnet-4-6 stream).
                            #
                            # Derive the specific tool names from the missing
                            # categories so the override preempts ambiguity
                            # about what "call_graph" means.
                            _cat_to_tools = {
                                "call_graph": "get_callers",
                                "history": "git_blame or git_log",
                                "discovery": "search_code or glob_files",
                                "structure": "list_symbols",
                                "comprehension": "read_file",
                            }
                            try:
                                _missing_cats = sorted(
                                    c.value for c in _exc_verdict.missing_categories
                                )
                            except Exception:
                                _missing_cats = []
                            _required_tools = [
                                _cat_to_tools.get(c, c) for c in _missing_cats
                            ]
                            _cat_list = ", ".join(_missing_cats) or "diverse"
                            _tool_list = ", ".join(_required_tools) or "get_callers"
                            _error_feedback = (
                                "<CRITICAL_SYSTEM_OVERRIDE>\n"
                                "Previous attempt failed the Iron Gate exploration "
                                "ledger. You are mathematically required to invoke "
                                f"tools from the following missing categories: "
                                f"[{_cat_list}].\n"
                                f"You MUST invoke {_tool_list} before emitting any "
                                "patch.\n"
                                "The ExplorationLedger dedups by (tool, "
                                "arguments_hash) — repeating the same read_file on "
                                "the same path earns ZERO new credit.\n"
                                "Your next action MUST be one of the required tool "
                                "calls listed above. Do NOT emit a patch. Do NOT "
                                "call read_file again on files you already read.\n"
                                "</CRITICAL_SYSTEM_OVERRIDE>\n\n"
                                "## PREVIOUS GENERATION REJECTED — EXPLORATION GATE\n\n"
                                f"{_ledger_feedback}\n\n"
                                "INSTRUCTIONS FOR RETRY:\n"
                                "- Call the missing-category tools listed above BEFORE\n"
                                "  emitting any patch. The ledger dedups by (tool,\n"
                                "  arguments_hash) so repeating the same read_file on\n"
                                "  the same path adds no credit.\n"
                                "- Prefer get_callers, list_symbols, and git_blame over\n"
                                "  repeated read_file calls — diversity beats volume.\n"
                                "- Exploration is NOT optional. Patches without context\n"
                                "  corrupt code.\n"
                            )
                        else:
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
                    elif _err_str.startswith("multi_file_coverage_insufficient"):
                        # Gate 5 rejection — name the missing target paths and
                        # reiterate the files: [...] shape. The model saw the
                        # single-file schema example in its prompt; here we
                        # hand it the multi-file example plus the exact list
                        # of paths it failed to cover.
                        _mf_missing = getattr(exc, "_mf_missing_paths", None) or []
                        _mf_targets = getattr(exc, "_mf_target_files", None) or tuple(
                            ctx.target_files
                        )
                        try:
                            from backend.core.ouroboros.governance.multi_file_coverage_gate import (
                                render_missing_block as _mf_render,
                            )
                            _missing_block = _mf_render(_mf_missing, _mf_targets)
                        except Exception:  # noqa: BLE001
                            _missing_block = (
                                "\nMISSING TARGET FILES:\n"
                                + "\n".join(f"  - {p}" for p in list(_mf_missing)[:16])
                                + "\n"
                            )
                        _target_count = len(_mf_targets)
                        _error_feedback = (
                            "## PREVIOUS GENERATION REJECTED — "
                            "MULTI-FILE COVERAGE INSUFFICIENT\n\n"
                            f"{_err_str[:400]}\n"
                            f"{_missing_block}\n"
                            "INSTRUCTIONS FOR RETRY:\n"
                            f"- This operation targets {_target_count} files. "
                            "You MUST return the multi-file shape: a `files` "
                            "list with one entry per target file.\n"
                            "- Do NOT use the legacy single-file schema "
                            "(`file_path` + `full_content` at the top level of "
                            "the candidate). That shape can only express ONE "
                            "file and will be rejected again.\n"
                            "- Use this structure for each candidate:\n\n"
                            "    {\n"
                            "      \"candidate_id\": \"c1\",\n"
                            "      \"files\": [\n"
                            "        {\n"
                            "          \"file_path\": \"<target path 1>\",\n"
                            "          \"full_content\": \"<complete file 1 content>\",\n"
                            "          \"rationale\": \"<why file 1 changes>\"\n"
                            "        },\n"
                            "        {\n"
                            "          \"file_path\": \"<target path 2>\",\n"
                            "          \"full_content\": \"<complete file 2 content>\",\n"
                            "          \"rationale\": \"<why file 2 changes>\"\n"
                            "        }\n"
                            "      ],\n"
                            "      \"rationale\": \"<one-sentence summary of the change set>\"\n"
                            "    }\n\n"
                            f"- Every one of the {_target_count} target paths above "
                            "must appear as a `file_path` entry in the `files` "
                            "list. Do not omit any.\n"
                            "- `full_content` in each entry must be the COMPLETE "
                            "file (not a diff, not a patch, not just the changed "
                            "lines).\n"
                            "- Python files must be syntactically valid "
                            "(`ast.parse()`-clean) per file.\n"
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
                        elif _err_str.startswith("multi_file_coverage_insufficient"):
                            _gen_failure_class = "multi_file_coverage"
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
                            # Preserve iron-gate feedback already staged for retry
                            # (ExplorationInsufficientError etc). Reading from ctx
                            # here would silently drop _error_feedback — the
                            # severed nervous system bug that hid category-aware
                            # retry instructions from the model on every
                            # post-Iron-Gate retry.
                            _existing = _retry_ctx_kwargs.get("strategic_memory_prompt", "") or ""
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

            # Short-circuit: model signalled the change is already present.
            #
            # Read-only discipline (Session 10, Derek 2026-04-17 Manifesto §8):
            # when ctx.is_read_only=True the noop short-circuit represents the
            # structurally expected terminal state (findings delivered via
            # subagent rollup, no code change by contract). Emit a POSTMORTEM
            # event with root_cause="read_only_complete" so the Synthetic Soul
            # has a clean audit trail and post-hoc analysis can distinguish
            # cartography completions from "model said no-op" completions.
            # Terminal reason code + ledger reason are aligned to the same
            # value so log, ledger, and comm-protocol all agree.
            if generation.is_noop:
                _is_read_only_terminal = bool(
                    getattr(ctx, "is_read_only", False)
                )
                _terminal_reason = (
                    "read_only_complete"
                    if _is_read_only_terminal
                    else "noop"
                )
                logger.info(
                    "[Orchestrator] op=%s is_noop=True (provider=%s) "
                    "terminal_reason_code=%s — skipping APPLY",
                    ctx.op_id,
                    generation.provider_name,
                    _terminal_reason,
                )
                # POSTMORTEM emission for read-only ops (Manifesto §8).
                # Emitted BEFORE ctx.advance so the audit trail matches
                # the lifecycle: GENERATE → (synthesis produced findings)
                # → POSTMORTEM → COMPLETE. Non-read-only noop ops retain
                # the legacy silent-complete semantics (no POSTMORTEM) to
                # preserve backward compatibility with existing analytics
                # that treat noop as a null event.
                if _is_read_only_terminal:
                    try:
                        await self._stack.comm.emit_postmortem(
                            op_id=ctx.op_id,
                            root_cause="read_only_complete",
                            failed_phase=None,
                            next_safe_action="none",
                        )
                    except Exception:
                        logger.debug(
                            "[Orchestrator] read-only POSTMORTEM emit failed",
                            exc_info=True,
                        )
                ctx = ctx.advance(
                    OperationPhase.COMPLETE,
                    generation=generation,
                    terminal_reason_code=_terminal_reason,
                )
                await self._record_ledger(
                    ctx,
                    OperationState.APPLIED,
                    {
                        "reason": _terminal_reason,
                        "provider": generation.provider_name,
                    },
                )
                return ctx

        # Wave 2 (5) Slice 4a.1 - VALIDATERunner delegation gate.
        # Flag JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED (default false) routes
        # the 762-line VALIDATE block (nested retry FSM + L2 dispatch +
        # source-drift + shadow harness + entropy + read-only short-circuit)
        # through the extracted PhaseRunner. The best_candidate local leaks
        # downstream to GATE (37 refs); threaded via PhaseResult.artifacts.
        if _phase_runner_validate_extracted():
            from backend.core.ouroboros.governance.phase_runners.validate_runner import (
                VALIDATERunner,
            )
            logger.info("[PhaseRunnerDelegate] VALIDATE → runner op=%s", ctx.op_id[:16])
            _validate_runner = VALIDATERunner(
                self, _serpent, generation,
                generate_retries_remaining, _episodic_memory,
            )
            _validate_result = await _validate_runner.run(ctx)
            best_candidate = _validate_result.artifacts.get("best_candidate")
            best_validation = _validate_result.artifacts.get("best_validation")
            if _validate_result.next_phase is None:
                # Terminal exit (budget / infra / no_candidate / drift / cancel / read_only)
                return _validate_result.next_ctx
            ctx = _validate_result.next_ctx
        else:
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

            # ── [ValidateRetryFSM] instrumentation (Follow-up A, Session T aftermath) ──
            # Session T (bt-2026-04-15-211616) showed the op producing 1m40s of dead
            # air between `InteractiveRepair disabled — falling through to
            # VALIDATE_RETRY/L2` and cost_governor.finish, with phase=CLASSIFY in
            # the finalize line (ctx-reference mismatch). L2 never dispatched even
            # once. The retry loop has ~15 entry/exit/transition points and none
            # of them were logged at INFO level, so we couldn't tell which branch
            # the op took. This helper + tagged log lines make every transition
            # auditable so the next session's log pinpoints the silent-exit line.
            # Manifesto §8 (Absolute Observability): a path that ends cost
            # accounting without naming the terminal branch is a first-class bug.
            def _fsm_log(state: str, extra: str = "") -> None:
                _fc = (
                    getattr(best_validation, "failure_class", None)
                    if best_validation is not None else None
                )
                logger.info(
                    "[ValidateRetryFSM] %s op=%s ctx_id=%x phase=%s "
                    "retries_remaining=%d best_fc=%r n_cands=%d%s",
                    state,
                    ctx.op_id[:16],
                    id(ctx),
                    ctx.phase.name,
                    validate_retries_remaining,
                    _fc,
                    len(generation.candidates),
                    f" {extra}" if extra else "",
                )

            for _iter_idx in range(1 + self._config.max_validate_retries):
                _fsm_log("iter_start", f"iter={_iter_idx}")
                # Compute remaining budget from pipeline_deadline
                if ctx.pipeline_deadline is not None:
                    remaining_s = (
                        ctx.pipeline_deadline - datetime.now(tz=timezone.utc)
                    ).total_seconds()
                else:
                    remaining_s = self._config.validation_timeout_s  # fallback

                if remaining_s <= 0.0:
                    _fsm_log("budget_exhausted_pre", f"remaining_s={remaining_s:.1f}")
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code="validation_budget_exhausted",
                    )
                    await self._record_ledger(
                        ctx,
                        OperationState.FAILED,
                        {"reason": "validation_budget_exhausted"},
                    )
                    _fsm_log("budget_exhausted_return")
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
                        _fsm_log("infra_early_return_set")

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
                        _fsm_log("budget_early_return_set")

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
                    _fsm_log("early_return")
                    return _early_return_ctx

                if best_candidate is not None:
                    _fsm_log("candidate_passed_break")
                    break  # at least one candidate passed

                # All candidates failed this attempt
                # Short-circuit: if no tests were discovered, retrying is pointless —
                # the same candidates will produce the same 0-test result every time.
                if best_validation is not None and getattr(best_validation, "test_count", -1) == 0:
                    logger.info(
                        "[Orchestrator] Skipping retries — no tests discovered for op=%s",
                        ctx.op_id,
                    )
                    _fsm_log("no_tests_short_circuit")
                    validate_retries_remaining = -1  # fall through to L2 / cancel

                validate_retries_remaining -= 1
                if validate_retries_remaining < 0:
                    # ── L2 self-repair dispatch ───────────────────────────────────
                    if self._config.repair_engine is not None and best_validation is not None:
                        # ── L2 deadline reconciliation (Session V fix) ─────────
                        # Manifesto §8 (Absolute Observability): an env var named
                        # ``JARVIS_L2_TIMEBOX_S`` must mean **the wall time
                        # reserved for L2 from the moment of dispatch** — not
                        # "silently clamped to whatever the pipeline clock has
                        # left." The prior behavior passed ``ctx.pipeline_
                        # deadline`` through as L2's effective deadline, so the
                        # hidden ``min(L2 timebox, pipeline_deadline - now)``
                        # won silently whenever the pipeline clock was depleted
                        # by CLASSIFY → PLAN → GENERATE → VALIDATE.
                        #
                        # Session V (``bt-2026-04-15-223631``, ``op-019d934a``)
                        # proved it live: ``JARVIS_L2_TIMEBOX_S=600`` was set,
                        # but L2 reported ``Iteration 1/8 starting (0s elapsed,
                        # 120s remaining)`` because VALIDATE drained the
                        # pipeline clock over ~5 minutes before L2 saw it. One
                        # L2 iteration ran, returned ``directive='cancel'``,
                        # the op died. The env var name lied.
                        #
                        # Fix: compute L2's deadline fresh at dispatch as
                        # ``now + JARVIS_L2_TIMEBOX_S`` and reconcile
                        # ``ctx.pipeline_deadline`` via
                        # ``with_pipeline_deadline()`` so downstream phases
                        # (GATE, APPLY, VERIFY, POSTMORTEM) see a consistent
                        # op-level clock — preserving the "one notion of 'op
                        # must end by'" invariant without masking the L2 budget
                        # decision. If the pipeline_deadline is already LARGER
                        # than the L2 fresh budget (operator set a generous
                        # global cap), we keep the larger value: L2 must never
                        # shrink an op's envelope. Either way, both clocks
                        # and the winning cap are logged at INFO so operators
                        # can audit the decision without reading source.
                        _l2_timebox_s = float(
                            os.environ.get("JARVIS_L2_TIMEBOX_S", "120.0")
                        )
                        _now_dt = datetime.now(timezone.utc)
                        _l2_fresh_deadline = _now_dt + timedelta(
                            seconds=_l2_timebox_s
                        )
                        _orig_pl_deadline = ctx.pipeline_deadline
                        _orig_remaining_s = (
                            (_orig_pl_deadline - _now_dt).total_seconds()
                            if _orig_pl_deadline is not None else 0.0
                        )
                        if (
                            _orig_pl_deadline is None
                            or _orig_pl_deadline < _l2_fresh_deadline
                        ):
                            _l2_deadline = _l2_fresh_deadline
                            _winning_cap = "l2_timebox_fresh"
                            # Reconcile the op-level clock. `pipeline_deadline`
                            # is a cooperative budget; `cost_governor` and the
                            # harness idle watcher maintain their own wall
                            # clocks, so extending here does not violate any
                            # global safety invariant — it merely tells
                            # downstream phases that L2 has legitimately
                            # reserved additional time beyond the original
                            # envelope.
                            ctx = ctx.with_pipeline_deadline(_l2_fresh_deadline)
                        else:
                            _l2_deadline = _orig_pl_deadline
                            _winning_cap = "pipeline_deadline_inherited"
                        logger.info(
                            "[Orchestrator] L2 deadline reconciliation: "
                            "pipeline_remaining=%.1fs l2_timebox_env=%.1fs "
                            "effective=%.1fs winning_cap=%s op=%s",
                            _orig_remaining_s,
                            _l2_timebox_s,
                            (_l2_deadline - _now_dt).total_seconds(),
                            _winning_cap,
                            ctx.op_id[:16],
                        )
                        _fsm_log(
                            "l2_dispatch_pre",
                            f"effective_s={(_l2_deadline - _now_dt).total_seconds():.0f} "
                            f"cap={_winning_cap} l2_timebox_env={_l2_timebox_s:.0f}",
                        )
                        directive = await self._l2_hook(
                            ctx, best_validation, _l2_deadline,
                        )
                        _fsm_log("l2_dispatch_post", f"directive={directive[0]!r}")
                        if directive[0] == "break":
                            best_candidate, best_validation = directive[1], directive[2]
                            logger.info(
                                "[Orchestrator] L2 broke VALIDATE_RETRY loop for op=%s — "
                                "proceeding to source-drift / shadow / entropy / GATE "
                                "(candidate_id=%s, file=%s, source_hash=%s)",
                                ctx.op_id,
                                best_candidate.get("candidate_id", "?"),
                                best_candidate.get("file_path", "?"),
                                (best_candidate.get("source_hash") or "")[:12],
                            )
                            _fsm_log("l2_converged_break")
                            break  # fall through to GATE
                        elif directive[0] in ("cancel", "fatal"):
                            _fsm_log("l2_escape_return", f"directive={directive[0]!r}")
                            return directive[1]  # ctx was advanced inside _l2_hook
                    else:
                        _fsm_log(
                            "l2_skipped",
                            f"repair_engine={self._config.repair_engine is not None} "
                            f"best_validation={best_validation is not None}",
                        )
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
                    _fsm_log("no_candidate_valid_return")
                    return ctx

                # ── Micro-Fix: try InteractiveRepair before expensive VALIDATE_RETRY ──
                _fsm_log("micro_fix_pre")
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
                            _fsm_log(
                                "micro_fix_returned",
                                f"fixed={_repair_result.fixed} "
                                f"iterations={_repair_result.iterations_used}",
                            )
                            if _repair_result.fixed:
                                logger.info(
                                    "[Orchestrator] Micro-fix succeeded in %d iterations for op=%s",
                                    _repair_result.iterations_used, ctx.op_id,
                                )
                                # Skip full regeneration — advance to GATE
                                ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)
                                _fsm_log("micro_fix_succeeded_break")
                                break
                        else:
                            _fsm_log(
                                "micro_fix_skipped_new_file",
                                f"target={_repair_target!r}",
                            )
                    else:
                        _fsm_log("micro_fix_skipped_no_target")
                except asyncio.CancelledError:
                    _fsm_log("micro_fix_cancelled")
                    raise
                except Exception as _repair_exc:
                    # §8 (Absolute Observability): a swallowed exception on this
                    # path is not acceptable. Upgrade from DEBUG to WARNING, stamp
                    # the exc class and message, keep exc_info for the traceback.
                    # The retry loop is designed to continue after this exception
                    # (the subsequent ctx.advance(VALIDATE_RETRY) runs below), so
                    # we do NOT re-raise — but we DO name the terminal branch.
                    logger.warning(
                        "[Orchestrator] Micro-fix failed (exc_class=%s): %s",
                        type(_repair_exc).__name__,
                        _repair_exc,
                        exc_info=True,
                    )
                    _fsm_log(
                        "micro_fix_exception_swallowed",
                        f"exc_class={type(_repair_exc).__name__}",
                    )

                # Retry: advance to VALIDATE_RETRY with episodic memory context
                _vr_kwargs = {}
                if _episodic_memory is not None and _episodic_memory.has_failures():
                    _vr_context = _episodic_memory.format_for_prompt()
                    if _vr_context:
                        _existing_vr = getattr(ctx, "strategic_memory_prompt", "") or ""
                        _vr_kwargs["strategic_memory_prompt"] = (
                            f"{_existing_vr}\n\n{_vr_context}" if _existing_vr else _vr_context
                        )
                _fsm_log("retry_advance_pre")
                _pre_ctx_id = id(ctx)
                ctx = ctx.advance(OperationPhase.VALIDATE_RETRY, **_vr_kwargs)
                # After ctx.advance: log the NEW ctx identity so the next session's
                # log lets us verify ctx actually rebound (Session T hypothesis:
                # the outer finally saw a stale ctx, which is only possible if
                # this rebind happened in a scope that didn't propagate).
                _fsm_log(
                    "retry_advance_post",
                    f"old_ctx_id={_pre_ctx_id:x} new_ctx_id={id(ctx):x}",
                )

            _fsm_log(
                "loop_exit_normal",
                f"best_candidate_present={best_candidate is not None}",
            )
            assert best_candidate is not None  # guaranteed by loop logic
            assert best_validation is not None

            # Source-drift check: file must not have changed since generation
            drift_hash = self._check_source_drift(best_candidate, self._config.project_root)
            if drift_hash is not None:
                logger.info(
                    "[Orchestrator] Source drift detected for op=%s file=%s "
                    "(expected=%s, actual=%s) — advancing to CANCELLED",
                    ctx.op_id,
                    best_candidate.get("file_path", "?"),
                    (best_candidate.get("source_hash") or "")[:12],
                    (drift_hash or "")[:12],
                )
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
            logger.info(
                "[Orchestrator] Source-drift check passed for op=%s — "
                "proceeding to shadow harness + entropy + GATE",
                ctx.op_id,
            )

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

            # Read-only APPLY short-circuit (Manifesto §1 Boundary Principle).
            # When ctx.is_read_only is True the op is a cartography/analysis task
            # — the model's tool-round findings (including any dispatch_subagent
            # rollups) are the deliverable. GATE/APPLY/VERIFY have no semantic
            # meaning because nothing is being written. Skip straight to COMPLETE
            # with a structural terminal reason. This is the second half of the
            # cryptographic guarantee the Advisor's blast/coverage bypass rests
            # on: tool_executor refuses mutating tool calls, the orchestrator
            # refuses the APPLY transition.
            if ctx.is_read_only:
                logger.info(
                    "[Orchestrator] Read-only APPLY short-circuit op=%s — "
                    "skipping GATE/APPLY/VERIFY (no-mutation contract). "
                    "Findings are delivered via POSTMORTEM + ledger.",
                    ctx.op_id,
                )
                try:
                    await self._stack.comm.emit_decision(
                        op_id=ctx.op_id,
                        outcome="read_only_complete",
                        reason_code="read_only_complete",
                        diff_summary="",
                    )
                except Exception:
                    pass
                ctx = ctx.advance(
                    OperationPhase.COMPLETE,
                    terminal_reason_code="read_only_complete",
                    validation=best_validation,
                )
                if _serpent:
                    await _serpent.stop(success=True)
                return ctx

            # Store compact validation result in context; full output is in ledger
            ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)
            logger.info(
                "[Orchestrator] Entered GATE phase for op=%s — invoking "
                "can_write policy check on target_files=%s",
                ctx.op_id,
                list(ctx.target_files)[:3],
            )

            # Heartbeat: GATE phase (Manifesto §7)
            try:
                await self._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id, phase="gate", progress_pct=75.0,
                )
            except Exception:
                pass

        # Wave 2 (5) Slice 4a.2 - GATERunner delegation gate.
        # Flag JARVIS_PHASE_RUNNER_GATE_EXTRACTED (default false) routes
        # the 600-line GATE block (can_write + SecurityReviewer +
        # SimilarityGate + frozen_tier + risk ceiling + SemanticGuardian
        # + REVIEW shadow + MutationGate + MIN_RISK_TIER floor + 5a green
        # preview + 5b NOTIFY_APPLY yellow) through the extracted runner.
        # risk_tier mutates at up to 6 sites inside GATE and is threaded
        # back via PhaseResult.artifacts["risk_tier"] so APPROVE inline
        # code downstream sees the final (possibly escalated) value.
        if _phase_runner_gate_extracted():
            from backend.core.ouroboros.governance.phase_runners.gate_runner import (
                GATERunner,
            )
            logger.info("[PhaseRunnerDelegate] GATE → runner op=%s", ctx.op_id[:16])
            _gate_runner = GATERunner(self, _serpent, best_candidate, risk_tier)
            _gate_result = await _gate_runner.run(ctx)
            # Rebind risk_tier (GATE mutates it). best_candidate unchanged
            # but pass through for symmetry with other slices.
            risk_tier = _gate_result.artifacts.get("risk_tier", risk_tier)
            best_candidate = _gate_result.artifacts.get("best_candidate", best_candidate)
            if _gate_result.next_phase is None:
                # Terminal exit (gate_blocked / security_review_blocked /
                # user_rejected_safe_auto_preview / user_rejected_notify_apply)
                return _gate_result.next_ctx
            ctx = _gate_result.next_ctx
        else:
            if _serpent: _serpent.update_phase("GATE")
            # ---- Phase 5: GATE ----
            allowed, reason = self._stack.can_write(
                {"files": list(ctx.target_files)}
            )
            logger.info(
                "[Orchestrator] GATE can_write decision for op=%s: "
                "allowed=%s reason=%s",
                ctx.op_id, allowed, reason,
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

            # ---- SemanticGuardian: deterministic pre-APPLY pattern check ----
            #
            # Closes the SAFE_AUTO blast-radius gap (Priority 3 audit):
            # risk_engine.py classifies on size (blast radius / file count /
            # test confidence) only — a syntactically-valid but semantically-
            # inverted candidate (flipped boolean, removed import, collapsed
            # body, hardcoded credential, inverted test assertion, loosened
            # perms …) lands as SAFE_AUTO and auto-applies while the operator
            # is asleep. The guardian runs 10 deterministic AST/regex
            # patterns on (pre-apply on-disk content) vs (candidate content)
            # and, if any fire, upgrades the tier:
            #
            #   hard detection → APPROVAL_REQUIRED (force human gate)
            #   soft detection → NOTIFY_APPLY      (force 5s preview window)
            #
            # Pure-deterministic, no LLM, ~10ms per candidate. Master switch
            # JARVIS_SEMANTIC_GUARD_ENABLED (default on).
            _guardian_findings: list = []
            if best_candidate is not None:
                try:
                    from backend.core.ouroboros.governance.semantic_guardian import (
                        SemanticGuardian,
                        recommend_tier_floor,
                    )
                    _guardian = SemanticGuardian()
                    # Build (path, old, new) triples from the candidate. For
                    # multi-file candidates the orchestrator already has
                    # _iter_candidate_files; we replicate its unpacking here
                    # so we don't need to thread ctx through.
                    _pairs: list = []
                    _candidate_files = best_candidate.get("files") if isinstance(
                        best_candidate.get("files"), list,
                    ) else None
                    if _candidate_files:
                        _iter = [
                            (entry.get("file_path", ""), entry.get("full_content", ""))
                            for entry in _candidate_files
                            if isinstance(entry, dict)
                        ]
                    else:
                        _iter = [(
                            best_candidate.get("file_path", ""),
                            best_candidate.get("full_content", ""),
                        )]
                    for _path, _new in _iter:
                        if not _path or not isinstance(_new, str):
                            continue
                        _old = ""
                        try:
                            _abs = (
                                self._config.project_root / _path
                                if not Path(_path).is_absolute()
                                else Path(_path)
                            )
                            if _abs.is_file():
                                _old = _abs.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            _old = ""
                        _pairs.append((_path, _old, _new))

                    # Time the whole batch so operators can detect a pattern
                    # detector regressing into a slow path (Track A telemetry).
                    _sg_t0 = time.monotonic()
                    _guardian_findings = _guardian.inspect_batch(_pairs)
                    _sg_duration_ms = int((time.monotonic() - _sg_t0) * 1000)

                    # Compute structured telemetry fields BEFORE any tier
                    # upgrade so ``risk_before`` reflects the classifier's
                    # verdict pre-guardian. The single INFO contract below
                    # fires on every op (hit OR clean) so downstream grep /
                    # aggregation pipelines have a stable one-line record.
                    _hard_count = sum(
                        1 for f in _guardian_findings if f.severity == "hard"
                    )
                    _soft_count = sum(
                        1 for f in _guardian_findings if f.severity == "soft"
                    )
                    _risk_before_name = risk_tier.name

                    _floor_name = recommend_tier_floor(_guardian_findings)
                    _upgrade: Optional[RiskTier] = None
                    if _floor_name is not None:
                        _upgrade_map = {
                            "notify_apply": RiskTier.NOTIFY_APPLY,
                            "approval_required": RiskTier.APPROVAL_REQUIRED,
                        }
                        _upgrade = _upgrade_map.get(_floor_name)
                        if _upgrade is not None and risk_tier.value < _upgrade.value:
                            risk_tier = _upgrade
                        else:
                            _upgrade = None  # floor wasn't stricter; no upgrade

                    # Stable structured line — always emitted. Fields are
                    # intentionally key=value so a simple split("=") parser
                    # can build rollup counters (top patterns, top files,
                    # FP rate estimate). Track A observability contract.
                    _pattern_names = (
                        ",".join(sorted({f.pattern for f in _guardian_findings}))
                        if _guardian_findings else "none"
                    )
                    logger.info(
                        "[SemanticGuard] op=%s findings=%d hard=%d soft=%d "
                        "patterns=[%s] risk_before=%s risk_after=%s "
                        "duration_ms=%d files_scanned=%d",
                        ctx.op_id,
                        len(_guardian_findings),
                        _hard_count, _soft_count,
                        _pattern_names,
                        _risk_before_name, risk_tier.name,
                        _sg_duration_ms,
                        len(_pairs),
                    )
                except Exception:
                    logger.debug(
                        "[Orchestrator] SemanticGuardian skipped",
                        exc_info=True,
                    )

            # ---- REVIEW subagent (Slice 1a — SHADOW MODE observer only) ----
            # Gated by JARVIS_REVIEW_SUBAGENT_SHADOW. Emits verdict telemetry
            # only; FSM proceeds to GATE unchanged. See _run_review_shadow.
            await self._run_review_shadow(ctx, best_candidate)

            # ---- MutationGate: APPLY-phase execution boundary (cached) ----
            #
            # Closes the "tests pass != tests test" gap empirically surfaced
            # by the Session W calibration (28.6% mutation score on green
            # test suite). For operator-allowlisted critical paths only:
            #
            #   * Enumerate deterministic AST mutants (cached by content hash).
            #   * For each mutant, run the scoped test suite; cache outcomes
            #     by (sut_hash, tests_hash) so repeat ops on unchanged files
            #     are near-free.
            #   * Map score → decision:
            #       score >= allow_threshold  (default 0.75) → no change
            #       score in [block, allow)                  → force APPROVAL_REQUIRED
            #       score <  block_threshold (default 0.40)  → force BLOCKED
            #
            # Authority split (Manifesto §1): the tester measures; this
            # module decides; the orchestrator enforces. The gate never
            # auto-improves tests, never short-circuits VALIDATE, never
            # runs on non-critical paths (cost would be prohibitive).
            #
            # Master switch JARVIS_MUTATION_GATE_ENABLED (default 0).
            if best_candidate is not None:
                try:
                    from backend.core.ouroboros.governance import mutation_gate as _mg
                    if _mg.gate_enabled():
                        _mg_allowlist = _mg.load_allowlist()
                        # Reuse the _iter already built for SemanticGuardian.
                        _candidate_pairs = []
                        _candidate_files_mg = best_candidate.get("files") if isinstance(
                            best_candidate.get("files"), list,
                        ) else None
                        if _candidate_files_mg:
                            _candidate_pairs = [
                                entry.get("file_path", "")
                                for entry in _candidate_files_mg
                                if isinstance(entry, dict)
                            ]
                        else:
                            _single = best_candidate.get("file_path", "")
                            if _single:
                                _candidate_pairs = [_single]
                        # Filter to critical-only.
                        _critical = [
                            Path(p) for p in _candidate_pairs
                            if _mg.is_path_critical(Path(p), allowlist=_mg_allowlist)
                        ]
                        if _critical:
                            _verdicts = []
                            for _sp in _critical:
                                _abs_sp = (
                                    self._config.project_root / _sp
                                    if not _sp.is_absolute() else _sp
                                )
                                # Caller supplies tests — a path-correlated
                                # discovery helper keeps the wiring minimal
                                # (Session W style: tests/test_<stem>*.py).
                                _tests = self._discover_tests_for_gate(_sp)
                                _verdicts.append(
                                    _mg.evaluate_file(_abs_sp, _tests)
                                )
                            if _verdicts:
                                _merged = _mg.merge_verdicts(_verdicts)
                                _risk_before_mg = risk_tier.name
                                _mg_mode = _mg.gate_mode()
                                _enforced = (_mg_mode == _mg.MODE_ENFORCE)
                                _applied_change = ""
                                if _enforced:
                                    if _merged.decision == "block":
                                        risk_tier = RiskTier.BLOCKED
                                        _applied_change = (
                                            f"{_risk_before_mg}->BLOCKED"
                                        )
                                    elif _merged.decision == "upgrade_to_approval":
                                        if risk_tier.value < RiskTier.APPROVAL_REQUIRED.value:
                                            risk_tier = RiskTier.APPROVAL_REQUIRED
                                            _applied_change = (
                                                f"{_risk_before_mg}->APPROVAL_REQUIRED"
                                            )
                                # Ledger EVERY verdict regardless of mode so
                                # shadow-mode operators accumulate data for
                                # the enforce-mode flip decision.
                                try:
                                    _mg.append_ledger(
                                        op_id=ctx.op_id, verdict=_merged,
                                        mode=_mg_mode, enforced=_enforced,
                                        applied_tier_change=_applied_change,
                                    )
                                except Exception:
                                    logger.debug(
                                        "[MutationGate] ledger append skipped",
                                        exc_info=True,
                                    )
                                logger.info(
                                    "[MutationGate] op=%s mode=%s enforced=%s "
                                    "decision=%s score=%.2f grade=%s "
                                    "caught=%d/%d survivors=%d cache_hits=%d "
                                    "cache_misses=%d duration=%.1fs "
                                    "risk_before=%s risk_after=%s",
                                    ctx.op_id, _mg_mode, _enforced,
                                    _merged.decision, _merged.score,
                                    _merged.grade, _merged.caught,
                                    _merged.total_mutants, len(_merged.survivors),
                                    _merged.cache_hits, _merged.cache_misses,
                                    _merged.duration_s,
                                    _risk_before_mg, risk_tier.name,
                                )
                except Exception:
                    logger.debug(
                        "[Orchestrator] MutationGate skipped",
                        exc_info=True,
                    )

            # ---- MIN_RISK_TIER floor (paranoia mode + quiet hours) ----
            #
            # Separate from JARVIS_RISK_CEILING above — that knob is scoped
            # to the /risk REPL command. This floor composes THREE operator
            # signals into a single tier floor:
            #
            #   JARVIS_MIN_RISK_TIER=notify_apply  (explicit)
            #   JARVIS_PARANOIA_MODE=1              (shortcut for notify_apply)
            #   JARVIS_AUTO_APPLY_QUIET_HOURS=22-7 (time-of-day window)
            #
            # The strictest of the three applies. Flipping PARANOIA_MODE or
            # QUIET_HOURS before going to sleep guarantees zero SAFE_AUTO
            # auto-applies land overnight.
            try:
                from backend.core.ouroboros.governance.risk_tier_floor import (
                    apply_floor_to_name,
                    floor_reason,
                )
                _cur_name = risk_tier.name.lower()
                _effective, _applied = apply_floor_to_name(_cur_name)
                if _applied is not None:
                    _floor_tier_map = {
                        "safe_auto": RiskTier.SAFE_AUTO,
                        "notify_apply": RiskTier.NOTIFY_APPLY,
                        "approval_required": RiskTier.APPROVAL_REQUIRED,
                        "blocked": RiskTier.BLOCKED,
                    }
                    _tgt = _floor_tier_map.get(_effective)
                    if _tgt is not None and risk_tier.value < _tgt.value:
                        logger.info(
                            "[Orchestrator] GATE: MIN_RISK_TIER floor → %s→%s "
                            "op=%s reason=%s",
                            risk_tier.name, _tgt.name, ctx.op_id, floor_reason(),
                        )
                        risk_tier = _tgt
            except Exception:
                logger.debug(
                    "[Orchestrator] MIN_RISK_TIER floor skipped",
                    exc_info=True,
                )

            # ---- Phase 5a-green: SAFE_AUTO diff preview (Green — when human is watching) ----
            # Mythos §7.4 UX: when a human is watching (TTY or explicit flag),
            # show a brief diff preview even for Green ops so the operator can
            # /reject if they spot something wrong. The delay is shorter than
            # NOTIFY_APPLY because Green is inherently lower risk.
            if risk_tier is RiskTier.SAFE_AUTO and _human_is_watching():
                _green_delay_s = float(
                    os.environ.get("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "2")
                )
                if best_candidate is not None and _green_delay_s > 0:
                    _diff_preview = (
                        best_candidate.get("unified_diff")
                        or best_candidate.get("full_content", "")
                    )
                    if _diff_preview:
                        try:
                            for _t in getattr(self._stack.comm, "_transports", []):
                                try:
                                    _preview_msg = type("_Msg", (), {
                                        "payload": {
                                            "phase": "safe_auto_diff_preview",
                                            "diff_preview": str(_diff_preview)[:4000],
                                            "delay_s": _green_delay_s,
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
                        logger.info(
                            "[Orchestrator] SAFE_AUTO diff preview shown (human watching), "
                            "waiting %.0fs for /reject; op=%s",
                            _green_delay_s, ctx.op_id,
                        )
                        await asyncio.sleep(_green_delay_s)
                        # Check if user cancelled during the preview window
                        if self._is_cancel_requested(ctx.op_id):
                            ctx = ctx.advance(
                                OperationPhase.CANCELLED,
                                terminal_reason_code="user_rejected_safe_auto_preview",
                            )
                            await self._record_ledger(
                                ctx, OperationState.FAILED,
                                {"reason": "user_rejected_safe_auto_preview"},
                            )
                            return ctx

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

                # Render diff preview in CLI before auto-apply.
                #
                # V1 rich preview: file tree + per-file panels + status
                # badges + live countdown + cancel polling. Safe fallback
                # to the legacy plain-sleep path on TTY-absent / env-off /
                # any render failure — NOTIFY_APPLY behavior is preserved
                # exactly in those cases. See diff_preview.py for the
                # authority / kill-switch / dump-path contract.
                _notify_delay_s = float(os.environ.get("JARVIS_NOTIFY_APPLY_DELAY_S", "5"))
                if best_candidate is not None and _notify_delay_s > 0:
                    _changes: list = []
                    try:
                        from backend.core.ouroboros.battle_test.diff_preview import (
                            build_changes_from_candidate,
                        )
                        _changes = build_changes_from_candidate(
                            best_candidate, self._config.project_root,
                        )
                    except Exception:
                        logger.debug(
                            "[Orchestrator] build_changes_from_candidate failed; "
                            "using legacy plain preview",
                            exc_info=True,
                        )
                        _changes = []

                    # Resolve the SerpentFlow instance from the stack. When
                    # absent (headless / non-battle-test harness), take the
                    # plain asyncio.sleep path — behavior identical to legacy.
                    _serpent = getattr(self._stack, "serpent_flow", None)
                    _cancel_check = lambda: self._is_cancel_requested(ctx.op_id)
                    _cancelled = False

                    if _serpent is not None and hasattr(_serpent, "show_notify_apply_preview"):
                        logger.info(
                            "[Orchestrator] NOTIFY_APPLY rich preview — op=%s "
                            "files=%d delay=%.1fs",
                            ctx.op_id, len(_changes), _notify_delay_s,
                        )
                        try:
                            _cancelled = await _serpent.show_notify_apply_preview(
                                op_id=ctx.op_id,
                                reason=_reason,
                                changes=_changes,
                                delay_s=_notify_delay_s,
                                cancel_check=_cancel_check,
                            )
                        except Exception:
                            logger.debug(
                                "[Orchestrator] rich NOTIFY_APPLY preview raised; "
                                "plain-sleep fallback",
                                exc_info=True,
                            )
                            await asyncio.sleep(_notify_delay_s)
                            _cancelled = _cancel_check()
                    else:
                        # Legacy path preserved: emit heartbeat + sleep +
                        # post-sleep cancel check.
                        _diff_preview = (
                            best_candidate.get("unified_diff")
                            or best_candidate.get("full_content", "")
                        )
                        if _diff_preview:
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
                        logger.info(
                            "[Orchestrator] NOTIFY_APPLY diff preview shown, "
                            "waiting %.0fs for /reject",
                            _notify_delay_s,
                        )
                        await asyncio.sleep(_notify_delay_s)
                        _cancelled = _cancel_check()

                    if _cancelled:
                        ctx = ctx.advance(
                            OperationPhase.CANCELLED,
                            terminal_reason_code="user_rejected_notify_apply",
                        )
                        await self._record_ledger(
                            ctx, OperationState.FAILED,
                            {"reason": "user_rejected_notify_apply"},
                        )
                        return ctx

        # Wave 2 (5) Slice 4b - combined APPROVE+APPLY+VERIFY delegation gate.
        # Flag JARVIS_PHASE_RUNNER_SLICE4B_EXTRACTED (default false) routes
        # the ~1150-line APPROVE+APPLY+VERIFY block (including 7.5 INFRA +
        # 8a scoped tests + 8b auto-commit + 8b2 hot-reload + 8c self-critique
        # + 8d visual VERIFY) through Slice4bRunner. Single combined gate
        # because the three phases are deeply interleaved. t_apply is
        # threaded via artifacts for COMPLETERunner's canary latency.
        if _phase_runner_slice4b_extracted():
            from backend.core.ouroboros.governance.phase_runners.slice4b_runner import (
                Slice4bRunner,
            )
            logger.info("[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY → Slice4bRunner op=%s", ctx.op_id[:16])
            _slice4b_runner = Slice4bRunner(self, _serpent, best_candidate, risk_tier)
            _slice4b_result = await _slice4b_runner.run(ctx)
            # Rebind _t_apply (consumed by COMPLETERunner downstream).
            _t_apply = _slice4b_result.artifacts.get("t_apply", 0.0)
            if _slice4b_result.next_phase is None:
                # Terminal exit from APPROVE/APPLY/VERIFY (one of ~14 paths)
                return _slice4b_result.next_ctx
            ctx = _slice4b_result.next_ctx
        else:
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
                    self._add_session_lesson(
                        "code",
                        f"[REJECTED] {ctx.description[:60]} ({_files_short}) "
                        f"— human rejected: {_reason_tag}. "
                        f"Avoid this approach in future operations.",
                        op_id=ctx.op_id,
                    )

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

            # ── Session-scoped dry-run gate (Priority 4 /plan dry-run) ──
            #
            # Flipped by ``/plan dry-run`` in the REPL; ops pass every gate
            # up to here (CLASSIFY → PLAN → GENERATE → VALIDATE → SECURITY
            # → GATE → APPROVE → pre-APPLY checks), then short-circuit just
            # before disk writes begin. Operators get full observability
            # into "what the model wanted to do" without any filesystem /
            # git side effects. This is distinct from JARVIS_SHOW_PLAN_BEFORE_EXECUTE
            # (which gates at the PLAN→GENERATE boundary) — the dry-run
            # flag is the hard "no side effects this session" kill switch.
            if os.environ.get("JARVIS_DRY_RUN", "").strip().lower() in _TRUTHY:
                logger.info(
                    "[Orchestrator] DRY_RUN: op=%s would APPLY %d file(s) — "
                    "skipping disk writes (set JARVIS_DRY_RUN=0 or /plan off)",
                    ctx.op_id,
                    len(ctx.target_files) if ctx.target_files else 0,
                )
                ctx = ctx.advance(
                    OperationPhase.CANCELLED,
                    terminal_reason_code="dry_run_session",
                )
                await self._record_ledger(
                    ctx, OperationState.FAILED,
                    {"reason": "dry_run_session"},
                )
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

            # ── Lifecycle Hook PRE_APPLY gate (Slice 4, 2026-05-02) ──
            # Operator-defined hooks fire here BEFORE any file write.
            # BLOCK aggregate routes the op to CANCELLED via the
            # established ctx.advance(CANCELLED, terminal_reason_code=...)
            # pattern (mirrors emergency-cancel at line 1820+).
            # WARN/CONTINUE proceed normally. Master-flag-gated by
            # JARVIS_LIFECYCLE_HOOKS_ENABLED (default false through
            # Slices 1-4; Slice 5 graduates). NEVER raises out of the
            # bridge — fail-open on any bridge-side error (a broken
            # hook substrate cannot block the autonomous loop).
            try:
                from backend.core.ouroboros.governance.lifecycle_hook_orchestrator_bridge import (
                    gate_pre_apply,
                )
                _lh_gate = await gate_pre_apply(
                    ctx.op_id,
                    target_files=tuple(ctx.target_files or ()),
                    diff_summary=str(ctx.description or "")[:1000],
                    risk_tier=str(getattr(ctx, "risk_tier", "") or ""),
                )
                if not _lh_gate.passed:
                    _block_names = ",".join(_lh_gate.blocking_hooks)[:64]
                    logger.warning(
                        "[Orchestrator] Lifecycle hook PRE_APPLY "
                        "BLOCKED op=%s blocking_hooks=%s",
                        ctx.op_id, list(_lh_gate.blocking_hooks),
                    )
                    if _serpent:
                        await _serpent.stop(success=False)
                    ctx = ctx.advance(
                        OperationPhase.CANCELLED,
                        terminal_reason_code=(
                            f"lifecycle_hook_blocked:{_block_names}"
                        ),
                    )
                    return ctx
                if _lh_gate.should_warn:
                    logger.info(
                        "[Orchestrator] Lifecycle hook PRE_APPLY "
                        "WARNED op=%s warning_hooks=%s",
                        ctx.op_id, list(_lh_gate.warning_hooks),
                    )
            except Exception:
                logger.debug(
                    "[Orchestrator] Lifecycle hook bridge not "
                    "available",
                    exc_info=True,
                )

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

            # Session O (bt-2026-04-15-175547) APPLY-path observability:
            # log the multi-file decision at a single INFO line so logs
            # prove single- vs multi-file flow without reading the raw
            # candidate JSON. Session O's 4-file backlog probe wrote only
            # dedup.py because the winning candidate returned a single
            # (file_path, full_content) pair instead of a ``files`` list —
            # the multi-file coordinated path (_apply_multi_file_candidate)
            # is gated behind len(_candidate_files) > 1, which requires the
            # candidate to include a populated ``files: [...]`` array.
            # Without this log line, it took cross-referencing disk state
            # against diff_summary text to confirm the single-file path
            # was taken. This line makes that one grep.
            _files_field = best_candidate.get("files") if isinstance(
                best_candidate, dict
            ) else None
            _has_files_key = isinstance(_files_field, list) and len(_files_field) > 0
            _multi_enabled = (
                os.environ.get("JARVIS_MULTI_FILE_GEN_ENABLED", "true").lower()
                not in ("false", "0", "no", "off")
            )
            _apply_mode = "multi" if len(_candidate_files) > 1 else "single"
            _file_basenames = [
                (fp.rsplit("/", 1)[-1] if "/" in fp else fp)
                for fp, _ in _candidate_files
            ]
            logger.info(
                "[Orchestrator] APPLY mode=%s candidate_files=%d "
                "files_list_present=%s multi_enabled=%s targets=[%s] op=%s",
                _apply_mode,
                len(_candidate_files),
                _has_files_key,
                _multi_enabled,
                ",".join(_file_basenames),
                ctx.op_id[:16],
            )

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

            # OpsDigestObserver v1.1a — APPLY milestone (best-effort telemetry).
            # Reaching this point means ChangeEngine succeeded (failed paths
            # returned early). Derive mode from target-files count so we
            # don't rely on outer-scope local variables remaining in scope.
            try:
                from backend.core.ouroboros.governance.ops_digest_observer import (
                    APPLY_MODE_MULTI,
                    APPLY_MODE_SINGLE,
                    get_ops_digest_observer,
                )
                _apply_file_count = len(ctx.target_files or ())
                _apply_mode_tag = (
                    APPLY_MODE_MULTI if _apply_file_count > 1 else APPLY_MODE_SINGLE
                )
                get_ops_digest_observer().on_apply_succeeded(
                    op_id=ctx.op_id,
                    mode=_apply_mode_tag,
                    files=_apply_file_count,
                )
            except Exception:
                logger.debug(
                    "[Orchestrator] on_apply_succeeded observer call failed",
                    exc_info=True,
                )

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

                # OpsDigestObserver v1.1a — VERIFY milestone. Plan tightening
                # #1: ``scoped_to_applied_op=True`` because this branch only
                # runs when ``ctx.target_files`` was applied (it's the scoped
                # post-apply test run, not a repo-wide health check).
                try:
                    from backend.core.ouroboros.governance.ops_digest_observer import (
                        get_ops_digest_observer,
                    )
                    _verify_passed_count = max(
                        0, _verify_test_total - _verify_test_failures,
                    )
                    get_ops_digest_observer().on_verify_completed(
                        op_id=ctx.op_id,
                        passed=_verify_passed_count,
                        total=_verify_test_total,
                        scoped_to_applied_op=True,
                    )
                except Exception:
                    logger.debug(
                        "[Orchestrator] on_verify_completed observer call failed",
                        exc_info=True,
                    )

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
                        # Mythos §7.4: originating signal + rationale for
                        # zero-context reviewers.
                        signal_source=getattr(ctx, "signal_source", ""),
                        signal_urgency=getattr(ctx, "signal_urgency", ""),
                        rationale=ctx.description,
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

                    # OpsDigestObserver v1.1a — commit milestone. Hash shape
                    # validation happens in the observer implementer; this
                    # call site just forwards AutoCommitter's reported value.
                    try:
                        from backend.core.ouroboros.governance.ops_digest_observer import (
                            get_ops_digest_observer,
                        )
                        get_ops_digest_observer().on_commit_succeeded(
                            op_id=ctx.op_id,
                            commit_hash=_commit_result.commit_hash or "",
                        )
                    except Exception:
                        logger.debug(
                            "[Orchestrator] on_commit_succeeded observer call failed",
                            exc_info=True,
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
                        self._add_session_lesson(
                            "code",
                            f"[CRITIQUE POOR {getattr(_critique_result, 'rating', '?')}/5] "
                            f"{ctx.description[:60]} ({_files_short}): "
                            f"{str(getattr(_critique_result, 'rationale', ''))[:120]}",
                            op_id=ctx.op_id,
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

            # ---- Phase 8d: Visual VERIFY (Slices 3-4 — Task 22 handoff #4) ----
            # Runs deterministic UI-regression checks + model-assisted
            # advisory between VERIFY and COMPLETE. Master-switch-gated via
            # ``visual_verify_enabled()`` inside the driver; a disabled
            # sensor returns ``ran=False`` and we proceed to COMPLETE as
            # before (back-compat preserved).
            #
            # Routing per Manifesto §2 DAG:
            #   ran=False      → COMPLETE (unchanged back-compat path)
            #   result=pass    → COMPLETE (FSM transitions VERIFY → VISUAL_VERIFY → COMPLETE)
            #   result=fail OR l2_triggered=True → L2 Repair via ``_l2_hook``,
            #     same path VERIFY-red uses; on L2 convergence we re-apply
            #     the repair candidate and continue to COMPLETE; on L2 escape
            #     we inherit the terminal ctx L2 advanced to and return early.
            try:
                from backend.core.ouroboros.governance.visual_verify import (
                    run_post_verify,
                )
                _vv_outcome = run_post_verify(
                    target_files=ctx.target_files,
                    attachments=ctx.attachments,
                    op_id=ctx.op_id,
                    op_description=ctx.description,
                    plan_ui_affected=False,
                    test_targets_resolved=(
                        ctx.validation.adapter_names_run if ctx.validation else None
                    ),
                    risk_tier=(
                        ctx.risk_tier.name.lower() if ctx.risk_tier else ""
                    ),
                    # We only reach this block on the VERIFY-passed path, so
                    # the I4 clamp's "red" branch never fires here; passing
                    # "passed" explicitly makes the contract obvious.
                    test_runner_result="passed",
                )
                if _vv_outcome.ran:
                    _vv_verdict = (
                        _vv_outcome.result.verdict if _vv_outcome.result else "?"
                    )
                    logger.info(
                        "[Orchestrator] Visual VERIFY outcome=%s "
                        "l2_triggered=%s [%s] %s",
                        _vv_verdict, _vv_outcome.l2_triggered,
                        ctx.op_id, _vv_outcome.reasoning,
                    )
                    # Advance the FSM through VISUAL_VERIFY so the traversal
                    # is auditable in the hash-chained ledger.
                    try:
                        ctx = ctx.advance(OperationPhase.VISUAL_VERIFY)
                    except ValueError as _adv_exc:
                        # Should never happen on the happy VERIFY-passed path
                        # but guard against cancel / postmortem races that
                        # advanced ctx out from under us.
                        logger.debug(
                            "[Orchestrator] VISUAL_VERIFY advance rejected "
                            "(ctx at %s): %s", ctx.phase.name, _adv_exc,
                        )

                    _vv_fail = (
                        _vv_outcome.l2_triggered
                        or (
                            _vv_outcome.result is not None
                            and _vv_outcome.result.verdict == "fail"
                        )
                    )
                    if _vv_fail and self._config.repair_engine is not None:
                        logger.info(
                            "[Orchestrator] Visual VERIFY fail/advisory — "
                            "routing to L2 repair [%s]", ctx.op_id,
                        )
                        _vv_deadline = ctx.pipeline_deadline or (
                            datetime.now(timezone.utc) + timedelta(seconds=60)
                        )
                        _vv_synth_val = ValidationResult(
                            passed=False,
                            best_candidate=best_candidate,
                            validation_duration_s=0.0,
                            error=f"visual_verify: {_vv_outcome.reasoning}",
                            failure_class="test",
                            short_summary=(
                                f"visual_verify: "
                                f"{_vv_outcome.result.check if _vv_outcome.result else 'advisory'}"
                            ),
                            adapter_names_run=(),
                        )
                        try:
                            _vv_directive = await self._l2_hook(
                                ctx, _vv_synth_val, _vv_deadline,
                            )
                            if _vv_directive[0] == "break":
                                # L2 converged — apply the repair candidate.
                                _vv_l2_candidate = _vv_directive[1]
                                _vv_l2_change = self._build_change_request(
                                    ctx, _vv_l2_candidate,
                                )
                                try:
                                    _vv_l2_result = (
                                        await self._stack.change_engine.execute(
                                            _vv_l2_change
                                        )
                                    )
                                    if _vv_l2_result.success:
                                        logger.info(
                                            "[Orchestrator] Visual VERIFY L2 "
                                            "repair applied [%s]", ctx.op_id,
                                        )
                                    else:
                                        logger.warning(
                                            "[Orchestrator] Visual VERIFY L2 "
                                            "repair candidate failed to apply [%s]",
                                            ctx.op_id,
                                        )
                                except Exception as _vv_apply_exc:
                                    logger.debug(
                                        "[Orchestrator] Visual VERIFY L2 apply "
                                        "error: %s", _vv_apply_exc,
                                    )
                            elif _vv_directive[0] in ("cancel", "fatal"):
                                # L2 escaped — inherit the terminal ctx.
                                ctx = _vv_directive[1]
                                logger.info(
                                    "[Orchestrator] L2 escaped Visual VERIFY — "
                                    "op ctx advanced to %s [%s]",
                                    ctx.phase.name, ctx.op_id,
                                )
                                return ctx
                        except Exception as _vv_l2_exc:
                            logger.debug(
                                "[Orchestrator] Visual VERIFY L2 failed: "
                                "%s: %s",
                                type(_vv_l2_exc).__name__, _vv_l2_exc,
                            )
            except Exception as _vv_exc:
                # Visual VERIFY dispatch must never break the pipeline.
                # A bug in the driver drops us through to the normal
                # COMPLETE path.
                logger.debug(
                    "[Orchestrator] Visual VERIFY dispatch error: %s: %s",
                    type(_vv_exc).__name__, _vv_exc,
                )

        # Wave 2 (5) Slice 1 — COMPLETERunner delegation gate.
        # Flag JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED (default false) routes
        # the COMPLETE block through the extracted PhaseRunner. Parity
        # tests pin byte-identical observable output across both paths.
        if _phase_runner_complete_extracted():
            from backend.core.ouroboros.governance.phase_runners.complete_runner import (
                COMPLETERunner,
            )
            logger.info("[PhaseRunnerDelegate] COMPLETE → runner op=%s", ctx.op_id[:16])
            _complete_runner = COMPLETERunner(self, _serpent, _t_apply)
            _complete_result = await _complete_runner.run(ctx)
            return _complete_result.next_ctx

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

        # ---- Phase 4 P3 follow-on: Cognitive Metrics post-APPLY ----
        # Vindication call site — reads the pre-apply OracleSnapshot
        # captured at CONTEXT_EXPANSION (next to score_pre_apply) and
        # records a vindication CognitiveMetricRecord. Adjacent to
        # _oracle_incremental_update so the live oracle has the most
        # recent state when computing after-values. Best-effort: helper
        # body at module scope as
        # `_reflect_cognitive_metrics_post_apply_impl`.
        _reflect_cognitive_metrics_post_apply_impl(ctx, applied_files)

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
            self._add_session_lesson(_lesson_type, _lesson_text, op_id=ctx.op_id)

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
            # Post-L2 canonical validation is architecturally broken for
            # Python candidates: test_runner.PythonAdapter.run hard-codes
            # sandbox_dir=None (see its docstring at run() line 228-237) so
            # pytest always executes against the LIVE repo_root regardless
            # of the temp sandbox _run_validation wrote the candidate to.
            # For an L2-converged candidate that signal is always stale-
            # false, and the op escapes via l2_canonical_validate_failed
            # despite L2 having actually proven the fix in a real git
            # worktree with the candidate overlaid (RepairSandbox path).
            #
            # Skip the re-run: trust L2's full-repo sandbox convergence and
            # proceed straight to GATE → APPLY. Synthesize a passing
            # ValidationResult explicitly — do NOT reuse ``best_validation``
            # (that is the FAILED L1 validation which triggered L2 in the
            # first place).
            #
            # The architectural follow-up is teaching PythonAdapter to
            # honor sandbox_dir (via a full worktree overlay or
            # PYTHONPATH=repo_root + pytest paths under sandbox), which
            # also fixes the pre-L2 blind spot. Until then this skip is
            # gated by JARVIS_L2_SKIP_CANONICAL_AFTER_CONVERGE (default on)
            # so CI / operators can force the old double-validate path.
            _skip_canonical = os.environ.get(
                "JARVIS_L2_SKIP_CANONICAL_AFTER_CONVERGE", "true"
            ).strip().lower() in {"1", "true", "yes", "on"}

            if _skip_canonical:
                logger.info(
                    "[Orchestrator] L2_CONVERGED op=%s — skipping canonical "
                    "re-validation (PythonAdapter ignores sandbox_dir, L2 "
                    "already validated in git-worktree sandbox). Proceeding "
                    "to GATE → APPLY with L2's proven candidate.",
                    ctx.op_id,
                )
                canonical_val = ValidationResult(
                    passed=True,
                    best_candidate=l2_result.candidate,
                    validation_duration_s=0.0,
                    error=None,
                    failure_class=None,
                    short_summary=(
                        "L2 converged in sandbox; canonical re-run skipped "
                        "(PythonAdapter drops sandbox_dir, see "
                        "test_runner.py:227-237)"
                    ),
                    adapter_names_run=("l2-sandbox",),
                )
                await self._record_ledger(ctx, OperationState.SANDBOXING, {
                    "event": "l2_converged",
                    "iterations": len(l2_result.iterations),
                    "canonical_revalidation": "skipped",
                    "skip_reason": (
                        "PythonAdapter.run hard-codes sandbox_dir=None; "
                        "pytest cwd is always repo_root, ignoring the "
                        "temp sandbox _run_validation wrote the candidate "
                        "to. L2 used RepairSandbox (git worktree) which "
                        "honors the overlay — that signal is trusted."
                    ),
                    **l2_result.summary,
                })
                return ("break", l2_result.candidate, canonical_val)

            # Legacy path — run canonical validation anyway. Retained so
            # operators can force the old behavior via the env flag; will
            # almost always escape to CANCELLED for Python candidates
            # until PythonAdapter is fixed.
            _remaining_s = (deadline - datetime.now(timezone.utc)).total_seconds()
            canonical_val = await self._run_validation(ctx, l2_result.candidate, _remaining_s)
            if canonical_val.passed:
                await self._record_ledger(ctx, OperationState.SANDBOXING, {
                    "event": "l2_converged",
                    "iterations": len(l2_result.iterations),
                    "canonical_revalidation": "passed",
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
            _original_paths: Dict[Path, Path] = {}
            for _fp, _fc in _all_files:
                _rel = Path(_fp)
                if _rel.is_absolute():
                    _sandbox_file = sandbox / _rel.name
                else:
                    _sandbox_file = sandbox / _rel
                _sandbox_file.parent.mkdir(parents=True, exist_ok=True)
                _sandbox_file.write_text(_fc, encoding="utf-8")
                if _sandbox_file.suffix in _RUNNABLE_EXTENSIONS:
                    runner_changed.append(_sandbox_file)
                    _original_paths[_sandbox_file] = (
                        self._config.project_root / _rel
                        if not _rel.is_absolute()
                        else _rel
                    )

            if not runner_changed:
                _primary_rel = Path(target_file_str)
                _primary_file = sandbox / (_primary_rel.name if _primary_rel.is_absolute() else _primary_rel)
                runner_changed = [_primary_file]
                _original_paths[_primary_file] = (
                    self._config.project_root / _primary_rel
                    if not _primary_rel.is_absolute()
                    else _primary_rel
                )

            # Step 4: Run LanguageRouter (or any duck-typed runner)
            try:
                multi = await self._validation_runner.run(
                    changed_files=tuple(runner_changed),
                    sandbox_dir=sandbox,
                    timeout_budget_s=remaining_s,
                    op_id=ctx.op_id,
                    original_paths=_original_paths,
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

    def _discover_tests_for_gate(self, sut_path: Path) -> List[Path]:
        """Discover pytest files scoped to one SUT for the MutationGate.

        Matches Session-W style fan-out (``tests/test_<stem>*.py``) via
        rglob under the project root's ``tests/`` dir. Returned paths
        are absolute so the mutation runner sees stable targets even
        when the gate is called from a non-project cwd.
        """
        stem = sut_path.stem
        tests_dir = self._config.project_root / "tests"
        if not tests_dir.is_dir():
            return []
        found: List[Path] = []
        for candidate in tests_dir.rglob(f"test_{stem}*.py"):
            if candidate.is_file():
                found.append(candidate)
        return sorted(found)

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
