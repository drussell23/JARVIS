"""
Operation Context & Phase State Machine
========================================

Typed, frozen state object that flows through every Ouroboros pipeline phase.

``OperationContext`` is immutable -- all mutations produce a **new** instance
via :meth:`OperationContext.advance`, which enforces the phase state machine
and extends a SHA-256 hash chain so that every state transition is
cryptographically linked to the previous one.

Phase Transitions
-----------------

.. code-block:: text

    CLASSIFY -> ROUTE -> [CONTEXT_EXPANSION] -> [PLAN] -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE
                                                           |              |        |       |           |          |
                                                           v              v        v       v           v          v
                                                      GEN_RETRY      VAL_RETRY         EXPIRED    POSTMORTEM  POSTMORTEM
                                                           |              |
                                                           v              v
                                                       VALIDATE         GATE

    (most non-terminal phases can also transition to CANCELLED)

Terminal phases: COMPLETE, CANCELLED, EXPIRED, POSTMORTEM
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Dict, Optional, Set, Tuple

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult

from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.routing_policy import RoutingDecision


class ArchitecturalCycleError(ValueError):
    """Raised when dependency_edges contains a directed cycle.

    Detected at OperationContext construction via Kahn's algorithm.
    Prevents deadlock before the GENERATE phase.
    """


# ---------------------------------------------------------------------------
# Phase Enum
# ---------------------------------------------------------------------------


class OperationPhase(Enum):
    """Pipeline phase for an autonomous Ouroboros operation."""

    CLASSIFY = auto()
    ROUTE = auto()
    CONTEXT_EXPANSION = auto()
    PLAN = auto()           # Model-reasoned implementation planning (Manifesto §5)
    GENERATE = auto()
    GENERATE_RETRY = auto()
    VALIDATE = auto()
    VALIDATE_RETRY = auto()
    GATE = auto()
    APPROVE = auto()
    APPLY = auto()
    VERIFY = auto()
    COMPLETE = auto()
    CANCELLED = auto()
    EXPIRED = auto()
    POSTMORTEM = auto()


# ---------------------------------------------------------------------------
# Phase Transition Table
# ---------------------------------------------------------------------------

# Progress transitions only — terminal escapes (CANCELLED, POSTMORTEM,
# EXPIRED, COMPLETE-noop) are auto-injected below by
# _inject_terminal_reachability() so the table does not need to repeat them.
# Keep this table focused on the *forward flow* of the pipeline.
PHASE_TRANSITIONS: Dict[OperationPhase, Set[OperationPhase]] = {
    OperationPhase.CLASSIFY: {
        OperationPhase.ROUTE,
    },
    OperationPhase.ROUTE: {
        OperationPhase.CONTEXT_EXPANSION,
        OperationPhase.PLAN,            # fast-path: skip expansion, go directly to planning
        OperationPhase.GENERATE,
    },
    OperationPhase.CONTEXT_EXPANSION: {
        OperationPhase.PLAN,
        OperationPhase.GENERATE,       # direct-to-GENERATE for trivial ops (skip planning)
    },
    OperationPhase.PLAN: {
        OperationPhase.GENERATE,
    },
    OperationPhase.GENERATE: {
        OperationPhase.VALIDATE,
        OperationPhase.GENERATE_RETRY,
    },
    OperationPhase.GENERATE_RETRY: {
        OperationPhase.VALIDATE,
        OperationPhase.GENERATE_RETRY,
    },
    OperationPhase.VALIDATE: {
        OperationPhase.GATE,
        OperationPhase.VALIDATE_RETRY,
    },
    OperationPhase.VALIDATE_RETRY: {
        OperationPhase.GATE,
        OperationPhase.VALIDATE_RETRY,
    },
    OperationPhase.GATE: {
        OperationPhase.APPROVE,
        OperationPhase.APPLY,
    },
    OperationPhase.APPROVE: {
        OperationPhase.APPLY,
    },
    OperationPhase.APPLY: {
        OperationPhase.VERIFY,
    },
    OperationPhase.VERIFY: set(),  # terminals only — no forward progress
    # Terminal phases -- no outgoing transitions
    OperationPhase.COMPLETE: set(),
    OperationPhase.CANCELLED: set(),
    OperationPhase.EXPIRED: set(),
    OperationPhase.POSTMORTEM: set(),
}

TERMINAL_PHASES: Set[OperationPhase] = {
    OperationPhase.COMPLETE,
    OperationPhase.CANCELLED,
    OperationPhase.EXPIRED,
    OperationPhase.POSTMORTEM,
}

# ---------------------------------------------------------------------------
# Dynamic Terminal Reachability Invariant
# ---------------------------------------------------------------------------
#
# Rule: every non-terminal phase can transition to every terminal phase.
#
# Why this is enforced here rather than maintained by hand per-phase:
#   • The hand-maintained table above only needs to declare *progress*
#     transitions (non-terminal → non-terminal) — terminal escapes
#     (CANCELLED / POSTMORTEM / EXPIRED / COMPLETE-noop) are auto-injected.
#   • Prevents the entire class of bugs where a new phase (e.g. VERIFY)
#     silently forbids an escape route and corrupts the FSM at runtime.
#     This was the root cause of the "Illegal phase transition:
#     VERIFY -> CANCELLED" incident the L2 repair path was hitting.
#   • COMPLETE is also terminal-reachable from any non-terminal phase to
#     preserve the noop fast-path semantics (model signals no change needed).
#
# Callers that want to restrict specific terminals (e.g. forbid CANCELLED
# from VERIFY for semantic reasons) should do so at the call site, not via
# the FSM. The FSM's job is to guarantee reachability; semantic choices
# belong to the orchestrator/hooks.
def _inject_terminal_reachability(
    transitions: Dict[OperationPhase, Set[OperationPhase]],
    terminals: Set[OperationPhase],
) -> None:
    """Auto-inject every terminal phase into every non-terminal phase's
    allowed-transition set. Idempotent and in-place.
    """
    for phase, allowed in transitions.items():
        if phase in terminals:
            continue  # terminals stay terminal (empty set)
        allowed.update(terminals)


def _verify_terminal_invariant(
    transitions: Dict[OperationPhase, Set[OperationPhase]],
    terminals: Set[OperationPhase],
) -> None:
    """Assert the terminal-reachability invariant at module load.

    Raises RuntimeError if any non-terminal phase is missing a terminal
    target — an explicit fast-fail instead of silently shipping a broken FSM.
    """
    for phase, allowed in transitions.items():
        if phase in terminals:
            if allowed:
                raise RuntimeError(
                    f"Terminal phase {phase.name} has outgoing transitions "
                    f"{sorted(p.name for p in allowed)} — terminals must be dead ends"
                )
            continue
        missing = terminals - allowed
        if missing:
            raise RuntimeError(
                f"Phase {phase.name} is missing terminal-escape routes: "
                f"{sorted(p.name for p in missing)}. "
                f"Every non-terminal phase must be able to reach every terminal phase."
            )


_inject_terminal_reachability(PHASE_TRANSITIONS, TERMINAL_PHASES)
_verify_terminal_invariant(PHASE_TRANSITIONS, TERMINAL_PHASES)


# ---------------------------------------------------------------------------
# Typed Sub-objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of the candidate generation phase.

    Parameters
    ----------
    candidates:
        Tuple of candidate dicts (each describing a proposed change).
    provider_name:
        Name of the model/provider that generated candidates.
    generation_duration_s:
        Wall-clock seconds spent generating candidates.
    """

    candidates: Tuple[Dict[str, Any], ...]
    provider_name: str
    generation_duration_s: float
    model_id: str = ""      # provider model identifier; empty = not reported
    is_noop: bool = False   # True when model signals change already present
    # L1: audit records from tool-use loop (empty when tools disabled)
    tool_execution_records: Tuple[Any, ...] = ()
    # Venom edit/write/delete audit trail captured from ToolExecutor at
    # tool_loop.run() exit. Each entry carries tool/path/action/before_hash/
    # after_hash/timestamp (see ToolExecutor._record_edit). Empty when no
    # mutating tool calls were issued.
    venom_edit_history: Tuple[Dict[str, Any], ...] = ()
    # Target files that the prompt builder embedded as file-content
    # regions *before* the tool loop ran. The lean prompt builder
    # in-lines ~100 lines of each target file into the initial prompt,
    # which is the semantic equivalent of the model having called
    # ``read_file`` on that path — the Iron Gate treats one entry here
    # as one unit of exploration credit so BACKGROUND-route DW ops
    # (which tend to emit patches directly without a tool round) are
    # not falsely tripped by ``exploration_insufficient``.
    prompt_preloaded_files: Tuple[str, ...] = ()
    # Token usage (0 = not reported by provider)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # Cost in USD (0.0 = not reported by provider)
    cost_usd: float = 0.0

    def with_tool_records(self, records: Tuple[Any, ...]) -> "GenerationResult":
        """Return a new GenerationResult with tool_execution_records set (called by provider after tool loop)."""
        return dataclasses.replace(self, tool_execution_records=records)

    def with_venom_edits(self, edits: Tuple[Dict[str, Any], ...]) -> "GenerationResult":
        """Return a new GenerationResult carrying Venom's edit history.

        Called by providers right after ``tool_loop.run()`` completes, so
        the orchestrator ledger and SerpentFlow can surface every autonomous
        mutation Venom performed during generation.
        """
        return dataclasses.replace(self, venom_edit_history=edits)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of the validation phase.

    Parameters
    ----------
    passed:
        Whether validation passed.
    best_candidate:
        The winning candidate dict, or ``None`` if validation failed.
    validation_duration_s:
        Wall-clock seconds spent validating.
    error:
        Human-readable error string if validation failed.
    """

    passed: bool
    best_candidate: Optional[Dict[str, Any]]
    validation_duration_s: float
    error: Optional[str]
    # Phase 2A: compact provenance fields (full output goes to ledger, not here)
    failure_class: Optional[str] = None          # "test" | "build" | "infra" | "budget" | None
    short_summary: str = ""                      # ≤300 chars human-readable summary
    adapter_names_run: Tuple[str, ...] = ()      # e.g. ("python",) or ("python", "cpp")


@dataclass(frozen=True)
class ApprovalDecision:
    """Context-embedded approval decision.

    This is the version stored inside :class:`OperationContext`, separate
    from any provider-specific approval model.

    Parameters
    ----------
    status:
        One of ``"approved"``, ``"rejected"``, ``"pending"``, ``"expired"``.
    approver:
        Identifier of the human or system that made the decision.
    reason:
        Free-text justification.
    decided_at:
        Timestamp of the decision.
    request_id:
        Unique identifier for the approval request.
    """

    status: str
    approver: Optional[str]
    reason: Optional[str]
    decided_at: Optional[datetime]
    request_id: str


@dataclass(frozen=True)
class ShadowResult:
    """Outcome of a shadow-mode comparison run.

    Parameters
    ----------
    confidence:
        Float in ``[0, 1]`` representing structural match confidence.
    comparison_mode:
        Comparison strategy used (e.g. ``"structural"``, ``"exact"``).
    violations:
        Tuple of violation descriptions found during comparison.
    shadow_duration_s:
        Wall-clock seconds the shadow run took.
    production_match:
        Whether the shadow output matched the production output.
    disqualified:
        Whether the shadow candidate was disqualified from promotion.
    """

    confidence: float
    comparison_mode: str
    violations: Tuple[str, ...]
    shadow_duration_s: float
    production_match: bool
    disqualified: bool


# ---------------------------------------------------------------------------
# Saga Types
# ---------------------------------------------------------------------------


class SagaStepStatus(str, Enum):
    """Per-repo lifecycle status inside a multi-repo saga."""

    PENDING = "pending"
    APPLYING = "applying"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"


@dataclass(frozen=True)
class RepoSagaStatus:
    """Frozen per-repo status entry in a multi-repo saga."""

    repo: str
    status: SagaStepStatus
    attempt: int = 0
    last_error: str = ""
    reason_code: str = ""
    compensation_attempted: bool = False


# ---------------------------------------------------------------------------
# Telemetry Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostTelemetry:
    """Snapshot of local hardware state at operation intake."""

    schema_version: str           # "1.0"
    arch: str                     # platform.machine() → "arm64"
    cpu_percent: float            # quantized to 2dp
    ram_available_gb: float       # quantized to 2dp
    pressure: str                 # PressureLevel.name: "NORMAL"|"ELEVATED"|"CRITICAL"|"EMERGENCY"
    sampled_at_utc: str           # datetime.now(utc).isoformat()
    sampled_monotonic_ns: int     # time.monotonic_ns() at sample time
    collector_status: str         # "ok" | "partial" | "stale"
    sample_age_ms: int            # (now_ns - sampled_monotonic_ns) // 1_000_000


@dataclass(frozen=True)
class RoutingIntentTelemetry:
    """Routing decision EXPECTED at FSM intake (before any execution)."""

    expected_provider: str        # e.g. "GCP_PRIME_SPOT", "LOCAL_CLAUDE"
    policy_reason: str            # e.g. "PRIMARY_AVAILABLE", "NORMAL"
    # Brain selector causal fields (Phase 4 — default "" for backwards compat)
    brain_id: str = ""            # e.g. "qwen_coder_32b", "phi3_lightweight"
    brain_model: str = ""         # exact model name passed to j-prime
    routing_reason: str = ""      # causal code: "task_gate_trivial", "cost_gate_triggered_queue"
    task_complexity: str = ""     # "trivial" | "light" | "heavy_code" | "complex"
    estimated_prompt_tokens: int = 0
    daily_spend_usd: float = 0.0  # snapshot of daily spend at intake
    schema_capability: str = "full_content_only"  # "full_content_only" | "full_content_and_diff"
    # Urgency-aware provider routing (Phase 5)
    provider_route: str = ""      # "immediate" | "standard" | "complex" | "background" | "speculative"
    provider_route_reason: str = ""  # causal code from UrgencyRouter


@dataclass(frozen=True)
class RoutingActualTelemetry:
    """Routing outcome AFTER execution (stamped at COMPLETE or POSTMORTEM)."""

    provider_name: str
    endpoint_class: str           # "gcp_spot" | "local" | "cloud_api"
    fallback_chain: Tuple[str, ...]
    was_degraded: bool


@dataclass(frozen=True)
class TelemetryContext:
    """Root telemetry envelope stamped once at intake, updated once at completion."""

    local_node: HostTelemetry
    routing_intent: RoutingIntentTelemetry
    routing_actual: Optional[RoutingActualTelemetry] = None


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------


def _compute_hash(ctx_dict: Dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hex digest of *ctx_dict*.

    Keys are sorted and non-serialisable values are coerced to ``str``
    via ``json.dumps(..., sort_keys=True, default=str)``.

    Parameters
    ----------
    ctx_dict:
        Dictionary of context fields to hash.

    Returns
    -------
    str
        64-character lowercase hex string (SHA-256).
    """
    canonical = json.dumps(ctx_dict, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_dag(edges: Tuple[Tuple[str, str], ...]) -> None:
    """Kahn's algorithm cycle detection. Raises ArchitecturalCycleError if cycle found."""
    if not edges:
        return
    graph: Dict[str, list] = collections.defaultdict(list)
    in_degree: Dict[str, int] = collections.defaultdict(int)
    nodes: set = set()
    for src, dst in edges:
        graph[src].append(dst)
        in_degree[dst] += 1
        nodes.add(src)
        nodes.add(dst)
    queue = collections.deque(n for n in nodes if in_degree[n] == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    if visited < len(nodes):
        cycle_nodes = [n for n in nodes if in_degree[n] > 0]
        raise ArchitecturalCycleError(
            f"Cycle detected in dependency_edges involving repos: {sorted(cycle_nodes)}"
        )


# ---------------------------------------------------------------------------
# OperationContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationContext:
    """Frozen, hash-chained state object for an Ouroboros pipeline run.

    All mutations go through :meth:`advance` which returns a **new** instance
    with an updated phase, timestamp, and cryptographic hash chain.

    Parameters
    ----------
    op_id:
        Globally unique, time-sortable operation identifier.
    created_at:
        Timestamp when the operation was first created.
    phase:
        Current pipeline phase.
    phase_entered_at:
        Timestamp when the current phase was entered.
    context_hash:
        SHA-256 hex of all fields (except ``context_hash`` itself).
    previous_hash:
        Hash of the predecessor context (``None`` for the initial state).
    target_files:
        Tuple of file paths this operation targets.
    risk_tier:
        Assigned risk tier (set after classification).
    description:
        Human-readable description of the operation.
    routing:
        Routing decision (set after routing phase).
    approval:
        Approval decision (set after approval phase).
    shadow:
        Shadow-mode comparison result.
    generation:
        Candidate generation result.
    validation:
        Validation result.
    policy_version:
        Version of the governance policy in effect.
    side_effects_blocked:
        Whether side effects (writes, network calls) are blocked.
    """

    op_id: str
    created_at: datetime
    phase: OperationPhase
    phase_entered_at: datetime
    context_hash: str
    previous_hash: Optional[str]
    target_files: Tuple[str, ...]
    risk_tier: Optional[RiskTier] = None
    description: str = ""
    routing: Optional[RoutingDecision] = None
    approval: Optional[ApprovalDecision] = None
    shadow: Optional[ShadowResult] = None
    generation: Optional[GenerationResult] = None
    validation: Optional[ValidationResult] = None
    policy_version: str = ""
    side_effects_blocked: bool = True
    pipeline_deadline: Optional[datetime] = None  # stamped once at submit(); phases compute remaining budget

    # ---- Phase 3: Multi-repo saga fields ----
    primary_repo: str = "jarvis"
    repo_scope: Tuple[str, ...] = ("jarvis",)
    cross_repo: bool = dataclasses.field(default=False, init=False)
    dependency_edges: Tuple[Tuple[str, str], ...] = ()
    apply_plan: Tuple[str, ...] = ()
    repo_snapshots: Tuple[Tuple[str, str], ...] = ()
    saga_id: str = ""
    saga_state: Tuple[RepoSagaStatus, ...] = ()
    schema_version: str = "3.0"
    expanded_context_files: Tuple[str, ...] = ()
    benchmark_result: Optional["BenchmarkResult"] = None
    pre_apply_snapshots: Dict[str, str] = field(default_factory=dict)
    execution_graph_id: str = ""
    execution_plan_digest: str = ""
    subagent_count: int = 0
    parallelism_budget: int = 0
    causal_trace_id: str = ""
    strategic_intent_id: str = ""
    strategic_memory_fact_ids: Tuple[str, ...] = ()
    strategic_memory_prompt: str = ""
    strategic_memory_digest: str = ""
    terminal_reason_code: str = ""
    rollback_occurred: bool = False
    # P2-6: Runbook-grade observability — cross-operation correlation identifier.
    # For single-repo ops: defaults to op_id (self-referential).
    # For multi-repo sagas: all saga-member ops share the root op's correlation_id.
    correlation_id: str = ""

    # ---- Telemetry (stamped at intake and COMPLETE) ----
    telemetry: Optional[TelemetryContext] = None
    previous_op_hash_by_scope: Tuple[Tuple[str, str], ...] = ()
    # e.g. (("jarvis", "abc123..."), ("prime", "def456..."))
    # Frozen-safe representation of Dict[repo_name, last_context_hash]

    # ---- Autonomy tier frozen at submit() — gate reads this, never re-queries TrustGraduator ----
    frozen_autonomy_tier: str = "governed"  # "governed" | "observe"; default = backward compat

    # ---- Human-authored instructions from OUROBOROS.md hierarchy (injected at submit time) ----
    human_instructions: str = ""  # injected from OUROBOROS.md hierarchy at submit time

    # ---- Cumulative session intelligence (injected before GENERATE) ----
    session_lessons: str = ""  # ephemeral lessons from prior ops in this session

    # ---- Signal metadata (propagated from IntentEnvelope at intake) ----
    signal_urgency: str = ""   # "critical" | "high" | "normal" | "low"
    signal_source: str = ""    # "test_failure" | "voice_human" | "ai_miner" | etc.

    # ---- Complexity classification (stamped at CLASSIFY by ComplexityClassifier) ----
    task_complexity: str = ""  # "trivial" | "simple" | "light" | "heavy_code" | "complex"

    # ---- Provider routing (stamped at ROUTE by UrgencyRouter) ----
    # Determines which provider strategy CandidateGenerator uses.
    provider_route: str = ""   # "immediate" | "standard" | "complex" | "background" | "speculative"
    provider_route_reason: str = ""  # human-readable reason for telemetry

    # ---- Dependency intelligence from Oracle graph (injected at CONTEXT_EXPANSION) ----
    # ~200-token summary: direct dependents, transitive importers, blast radius.
    # Prevents breaking downstream consumers that import the target files.
    dependency_summary: str = ""

    # ---- Stale-exploration guard: file hashes captured at GENERATE ----
    # Tuple of (filepath, sha256_hex) pairs snapshotted when GENERATE begins.
    # Compared at APPLY time — if any hash differs, the file was modified by
    # a concurrent operation and the candidate is stale.
    generate_file_hashes: Tuple[Tuple[str, str], ...] = ()

    # ---- Model-reasoned implementation plan (stamped at PLAN phase) ----
    # Structured JSON plan produced by PlanGenerator before GENERATE.
    # Contains: approach, file_changes (ordered with dependencies), risk_factors,
    # test_strategy, complexity estimate. Injected into GENERATE prompt so the
    # model follows a coherent strategy instead of ad-hoc patching.
    implementation_plan: str = ""

    # ---- Reasoning chain result (stamped at CLASSIFY if chain is active) ----
    reasoning_chain_result: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Post-init
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        object.__setattr__(self, "cross_repo", len(self.repo_scope) > 1)
        _validate_dag(self.dependency_edges)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        target_files: Tuple[str, ...],
        description: str,
        op_id: Optional[str] = None,
        policy_version: str = "",
        pipeline_deadline: Optional[datetime] = None,
        _timestamp: Optional[datetime] = None,
        primary_repo: str = "jarvis",
        repo_scope: Optional[Tuple[str, ...]] = None,
        dependency_edges: Tuple[Tuple[str, str], ...] = (),
        apply_plan: Tuple[str, ...] = (),
        repo_snapshots: Tuple[Tuple[str, str], ...] = (),
        saga_id: str = "",
        saga_state: Tuple[RepoSagaStatus, ...] = (),
        schema_version: str = "3.0",
        previous_op_hash_by_scope: Tuple[Tuple[str, str], ...] = (),
        correlation_id: str = "",
        signal_urgency: str = "",
        signal_source: str = "",
    ) -> OperationContext:
        """Create an initial CLASSIFY-phase context.

        Parameters
        ----------
        target_files:
            Tuple of file paths this operation targets.
        description:
            Human-readable description of the operation.
        op_id:
            Optional explicit operation ID; generated if omitted.
        policy_version:
            Version of the governance policy in effect.
        _timestamp:
            Optional explicit timestamp for deterministic tests.

        Returns
        -------
        OperationContext
            A new context in the CLASSIFY phase with a computed hash.
        """
        now = _timestamp or datetime.now(tz=timezone.utc)
        resolved_op_id = op_id or generate_operation_id()
        resolved_repo_scope = repo_scope if repo_scope is not None else (primary_repo,)
        # P2-6: default correlation_id to op_id for single-repo ops
        resolved_correlation_id = correlation_id or resolved_op_id

        # Build a temporary dict of all fields (except context_hash) for hashing
        fields_for_hash: Dict[str, Any] = {
            "op_id": resolved_op_id,
            "created_at": now,
            "phase": OperationPhase.CLASSIFY.name,
            "phase_entered_at": now,
            "previous_hash": None,
            "target_files": target_files,
            "risk_tier": None,
            "description": description,
            "routing": None,
            "approval": None,
            "shadow": None,
            "generation": None,
            "validation": None,
            "policy_version": policy_version,
            "side_effects_blocked": True,
            "pipeline_deadline": pipeline_deadline,
            "primary_repo": primary_repo,
            "repo_scope": resolved_repo_scope,
            "cross_repo": len(resolved_repo_scope) > 1,
            "dependency_edges": dependency_edges,
            "apply_plan": apply_plan,
            "repo_snapshots": repo_snapshots,
            "saga_id": saga_id,
            "saga_state": saga_state,
            "schema_version": schema_version,
            "expanded_context_files": (),
            "benchmark_result": None,
            "pre_apply_snapshots": {},
            "execution_graph_id": "",
            "execution_plan_digest": "",
            "subagent_count": 0,
            "parallelism_budget": 0,
            "causal_trace_id": "",
            "strategic_intent_id": "",
            "strategic_memory_fact_ids": (),
            "strategic_memory_prompt": "",
            "strategic_memory_digest": "",
            "terminal_reason_code": "",
            "rollback_occurred": False,
            "correlation_id": resolved_correlation_id,
            "telemetry": None,
            "previous_op_hash_by_scope": previous_op_hash_by_scope,
            "frozen_autonomy_tier": "governed",
            "reasoning_chain_result": None,
            "signal_urgency": signal_urgency,
            "signal_source": signal_source,
            "task_complexity": "",
            "provider_route": "",
            "provider_route_reason": "",
        }
        context_hash = _compute_hash(fields_for_hash)

        return cls(
            op_id=resolved_op_id,
            created_at=now,
            phase=OperationPhase.CLASSIFY,
            phase_entered_at=now,
            context_hash=context_hash,
            previous_hash=None,
            target_files=target_files,
            risk_tier=None,
            description=description,
            routing=None,
            approval=None,
            shadow=None,
            generation=None,
            validation=None,
            policy_version=policy_version,
            side_effects_blocked=True,
            pipeline_deadline=pipeline_deadline,
            primary_repo=primary_repo,
            repo_scope=resolved_repo_scope,
            dependency_edges=dependency_edges,
            apply_plan=apply_plan,
            repo_snapshots=repo_snapshots,
            saga_id=saga_id,
            saga_state=saga_state,
            schema_version=schema_version,
            strategic_intent_id="",
            strategic_memory_fact_ids=(),
            strategic_memory_prompt="",
            strategic_memory_digest="",
            terminal_reason_code="",
            rollback_occurred=False,
            correlation_id=resolved_correlation_id,
            previous_op_hash_by_scope=previous_op_hash_by_scope,
            frozen_autonomy_tier="governed",
            signal_urgency=signal_urgency,
            signal_source=signal_source,
        )

    # ------------------------------------------------------------------
    # State Machine Transition
    # ------------------------------------------------------------------

    def advance(
        self,
        new_phase: OperationPhase,
        _timestamp: Optional[datetime] = None,
        **updates: Any,
    ) -> OperationContext:
        """Transition to *new_phase*, returning a new context instance.

        Validates that the transition is legal according to
        :data:`PHASE_TRANSITIONS`, then produces a new frozen instance with:

        - ``phase`` set to *new_phase*
        - ``phase_entered_at`` set to now (or *_timestamp* for deterministic tests)
        - ``previous_hash`` set to ``self.context_hash``
        - ``context_hash`` recomputed over all fields
        - Any keyword arguments in *updates* applied via ``dataclasses.replace``

        Parameters
        ----------
        new_phase:
            The target phase.
        _timestamp:
            Optional explicit timestamp for deterministic tests.
        **updates:
            Additional field updates to apply (e.g. ``risk_tier=RiskTier.SAFE_AUTO``).

        Returns
        -------
        OperationContext
            A new context instance in *new_phase*.

        Raises
        ------
        ValueError
            If the transition from ``self.phase`` to *new_phase* is not allowed.
        """
        allowed = PHASE_TRANSITIONS.get(self.phase, set())
        if new_phase not in allowed:
            raise ValueError(
                f"Illegal phase transition: {self.phase.name} -> {new_phase.name}. "
                f"Allowed targets from {self.phase.name}: "
                f"{sorted(p.name for p in allowed) if allowed else '(terminal)'}"
            )

        now = _timestamp or datetime.now(tz=timezone.utc)

        # Build the replacement dict
        replacements: Dict[str, Any] = {
            "phase": new_phase,
            "phase_entered_at": now,
            "previous_hash": self.context_hash,
            **updates,
        }

        # Create intermediate instance without final hash
        # We need to compute hash over the new state, so build the dict first
        intermediate = dataclasses.replace(
            self,
            context_hash="",  # placeholder
            **replacements,
        )

        # Compute hash over all fields except context_hash
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)

        # Final instance with correct hash
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_pipeline_deadline(self, deadline: "datetime") -> "OperationContext":
        """Return a new context with pipeline_deadline set (no phase change).

        Uses the same hash-chain update as advance() but does not validate a
        phase transition. Called exactly once by GovernedLoopService.submit()
        before handing ctx to the orchestrator.
        """
        # Create intermediate with updated deadline and previous_hash chain
        intermediate = dataclasses.replace(
            self,
            pipeline_deadline=deadline,
            previous_hash=self.context_hash,
            context_hash="",  # placeholder — will be recomputed below
        )

        # Compute hash over all fields except context_hash
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)

        # Final instance with correct hash
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_expanded_files(self, files: Tuple[str, ...]) -> "OperationContext":
        """Return a new context with expanded_context_files set (no phase change).

        Called by ContextExpander after expansion rounds complete.
        Uses the same hash-chain mechanics as with_pipeline_deadline().
        """
        intermediate = dataclasses.replace(
            self,
            expanded_context_files=files,
            previous_hash=self.context_hash,
            context_hash="",  # placeholder — recomputed below
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_benchmark_result(self, result: "BenchmarkResult") -> "OperationContext":
        """Return a new context with benchmark_result set (no phase change)."""
        intermediate = dataclasses.replace(
            self,
            benchmark_result=result,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_pre_apply_snapshots(self, snapshots: Dict[str, str]) -> "OperationContext":
        """Return a new context with pre_apply_snapshots set (no phase change)."""
        intermediate = dataclasses.replace(
            self,
            pre_apply_snapshots=dict(snapshots),  # shallow copy for immutability
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_execution_graph_metadata(
        self,
        *,
        execution_graph_id: str,
        execution_plan_digest: str,
        subagent_count: int,
        parallelism_budget: int,
        causal_trace_id: str,
    ) -> "OperationContext":
        """Stamp execution-graph metadata onto the context (no phase change)."""
        intermediate = dataclasses.replace(
            self,
            execution_graph_id=execution_graph_id,
            execution_plan_digest=execution_plan_digest,
            subagent_count=subagent_count,
            parallelism_budget=parallelism_budget,
            causal_trace_id=causal_trace_id,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_strategic_memory_context(
        self,
        *,
        strategic_intent_id: str,
        strategic_memory_fact_ids: Tuple[str, ...],
        strategic_memory_prompt: str,
        strategic_memory_digest: str,
    ) -> "OperationContext":
        """Stamp L4 strategic-memory prompt metadata onto the context."""
        intermediate = dataclasses.replace(
            self,
            strategic_intent_id=strategic_intent_id,
            strategic_memory_fact_ids=tuple(strategic_memory_fact_ids),
            strategic_memory_prompt=strategic_memory_prompt,
            strategic_memory_digest=strategic_memory_digest,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_terminal_outcome(
        self,
        *,
        terminal_reason_code: str,
        rollback_occurred: bool = False,
    ) -> "OperationContext":
        """Stamp terminal outcome metadata onto the context."""
        intermediate = dataclasses.replace(
            self,
            terminal_reason_code=terminal_reason_code,
            rollback_occurred=rollback_occurred,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_telemetry(self, tc: TelemetryContext) -> "OperationContext":
        """Stamp TelemetryContext onto the context (no phase change).

        Called exactly once by GovernedLoopService.submit() at intake,
        after concurrency/dedup gates and pipeline_deadline stamping.
        Uses the same hash-chain mechanics as with_pipeline_deadline().
        """
        intermediate = dataclasses.replace(
            self,
            telemetry=tc,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_routing_actual(self, ra: RoutingActualTelemetry) -> "OperationContext":
        """Stamp actual routing outcome onto the existing TelemetryContext (no phase change).

        Called at COMPLETE or POSTMORTEM when the actual provider is known.

        Raises
        ------
        ValueError
            If ``telemetry`` has not been set yet (with_telemetry must precede this).
        """
        if self.telemetry is None:
            raise ValueError(
                "with_routing_actual() called before telemetry was set; "
                "call with_telemetry() first."
            )
        updated_tc = dataclasses.replace(self.telemetry, routing_actual=ra)
        intermediate = dataclasses.replace(
            self,
            telemetry=updated_tc,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_frozen_autonomy_tier(self, tier: str) -> "OperationContext":
        """Stamp autonomy tier onto context at submit time (no phase change).

        Called exactly once by GovernedLoopService.submit() before handing ctx
        to the orchestrator. Gate phase reads ctx.frozen_autonomy_tier instead
        of querying TrustGraduator live, preventing promotion races.

        Parameters
        ----------
        tier:
            ``"governed"`` (auto-proceed) or ``"observe"`` (requires approval).
        """
        intermediate = dataclasses.replace(
            self,
            frozen_autonomy_tier=tier,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_shadow_result(self, result: "ShadowResult") -> "OperationContext":
        """Attach shadow harness result to context (no phase change, hash updates)."""
        intermediate = dataclasses.replace(
            self,
            shadow=result,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)

    def with_human_instructions(self, instructions: str) -> "OperationContext":
        """Stamp human-authored OUROBOROS.md instructions onto context."""
        intermediate = dataclasses.replace(
            self,
            human_instructions=instructions,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _context_to_hash_dict(ctx: OperationContext) -> Dict[str, Any]:
    """Extract all fields from *ctx* into a dict suitable for hashing.

    The ``context_hash`` field is excluded since it is the value being
    computed.  Enum values are serialized by name for stability.
    """
    d: Dict[str, Any] = {}
    for f in dataclasses.fields(ctx):
        if f.name == "context_hash":
            continue
        value = getattr(ctx, f.name)
        # Serialize enums by name for cross-version stability
        if isinstance(value, Enum):
            value = value.name
        # Serialize frozen dataclass sub-objects to dict
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            value = dataclasses.asdict(value)
        d[f.name] = value
    return d


# ---------------------------------------------------------------------------
# RepairContext  (L2 self-repair — typed seam between RepairEngine + providers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairContext:
    """Failure context injected into the correction prompt for L2 repair iterations.

    Passed from RepairEngine to PrimeProvider.generate() to _build_codegen_prompt()
    where it triggers the REPAIR MODE section.

    Parameters
    ----------
    iteration:
        1-based current repair iteration number.
    max_iterations:
        Budget ceiling from RepairBudget.max_iterations.
    failure_class:
        One of "syntax", "test", "env", "flake".
    failure_signature_hash:
        SHA-256 of sorted failing test IDs + failure_class (stable across retries).
    failing_tests:
        Top-5 failing test node IDs from the most recent sandbox run.
    failure_summary:
        300-char human-readable error excerpt for the correction prompt.
    current_candidate_content:
        Full text of the failing file as it exists in the sandbox after the
        last patch was applied. The model is asked to diff against this.
    current_candidate_file_path:
        Repo-relative path of the file being repaired.
    """

    iteration: int
    max_iterations: int
    failure_class: str
    failure_signature_hash: str
    failing_tests: Tuple[str, ...]
    failure_summary: str
    current_candidate_content: str
    current_candidate_file_path: str
