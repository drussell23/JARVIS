"""
Subagent type contracts — Phase 1 scaffolding.

Defines the typed interface between the parent generation loop and
child subagents spawned via the `dispatch_subagent` Venom tool.

Phase 1 ships only the `explore` subagent type. Additional types
(`plan`, `review`, `research`, `refactor`) are reserved for Phases B/C
and will be rejected by the policy engine until those phases land.

Master switch: `JARVIS_SUBAGENT_DISPATCH_ENABLED` (default `true` as
of 2026-04-18 graduation). The switch remains env-tunable so operators
can disable dispatch for isolation battle tests, but Phase 1 is now
in production by default after the three-consecutive-clean-session
graduation arc (Sessions 14 / 15 / 16, Trinity cartography task,
all reaching POSTMORTEM root_cause=read_only_complete). See
`memory/project_phase_1_subagent_graduation.md` for the full
architectural genealogy + regression spine.

Manifesto alignment:
  §3 — Asynchronous tendrils: frozen-dataclass types are safe across
       asyncio boundaries without locks.
  §5 — Intelligence-driven routing: type-safe subagent dispatch with
       Nervous System Reflex fallback semantics (see SubagentContext
       provider fields).
  §6 — The Iron Gate: read-only manifest + tool-diversity requirement
       + mathematical forbiddance of mutation tools, enforced
       structurally via `READONLY_TOOL_MANIFEST`.
  §7 — Absolute observability: every field is auditable and serialized
       into the parent's ledger via `SubagentResult.to_dict()`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Optional, Tuple

if TYPE_CHECKING:
    # Forward-only; runtime imports deferred to avoid cycles with orchestrator.
    from backend.core.ouroboros.governance.op_context import OperationContext  # noqa: F401


# ============================================================================
# Schema version — bump when SubagentResult or SubagentRequest shape changes.
# ============================================================================

SCHEMA_VERSION = "subagent.1"


# ============================================================================
# Master switch
# ============================================================================

def subagent_dispatch_enabled() -> bool:
    """Master switch for subagent dispatch.

    Default `true` as of 2026-04-18 — Phase 1 graduated per Manifesto §6
    neuroplasticity threshold (three consecutive clean battle-test sessions
    14/15/16, Trinity cartography task, all reaching POSTMORTEM
    root_cause=read_only_complete with 80 findings × 3 subagents, 35-68KB
    synthesis payloads, within the 900s/630s/570s/60s/(soft+30s)
    five-budget-layer envelope). The switch remains env-tunable: set
    `JARVIS_SUBAGENT_DISPATCH_ENABLED=false` to disable (isolation battle
    tests, debugging a regression, etc.).
    """
    return os.environ.get("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true").lower() == "true"


# ============================================================================
# Hard caps — conservative Phase 1 values per Derek's mandate.
# ============================================================================

MAX_PARALLEL_SCOPES = int(os.environ.get("JARVIS_SUBAGENT_MAX_PARALLEL", "3"))
MAX_TOOL_ROUNDS = int(os.environ.get("JARVIS_SUBAGENT_MAX_ROUNDS", "5"))
MAX_FINDINGS_RETURNED = int(os.environ.get("JARVIS_SUBAGENT_MAX_FINDINGS", "50"))
MAX_SUMMARY_CHARS = int(os.environ.get("JARVIS_SUBAGENT_MAX_SUMMARY_CHARS", "2000"))
MAX_EVIDENCE_CHARS_PER_FINDING = int(
    os.environ.get("JARVIS_SUBAGENT_MAX_EVIDENCE_CHARS", "240")
)

# Temporal budgets — Nervous System Reflex (Manifesto §5, Tier 3).
# Primary provider (parent's choice, e.g., DW 397B) gets the first shot.
# If it stalls or fails to return a well-formed SubagentResult within
# `PRIMARY_PROVIDER_TIMEOUT_S`, the orchestrator severs the thread and
# retries via the Claude API within `FALLBACK_PROVIDER_TIMEOUT_S`.
# Survival supersedes cost.
PRIMARY_PROVIDER_TIMEOUT_S = float(
    os.environ.get("JARVIS_SUBAGENT_PRIMARY_TIMEOUT_S", "90")
)
FALLBACK_PROVIDER_TIMEOUT_S = float(
    os.environ.get("JARVIS_SUBAGENT_FALLBACK_TIMEOUT_S", "60")
)


# ============================================================================
# Read-only tool manifest — Iron Gate (Manifesto §6).
#
# The subagent's own ToolLoopCoordinator is constructed with a restricted
# manifest. Any tool call outside this set is rejected at the subagent's
# backend boundary BEFORE reaching the global GoverningToolPolicy.
# This is defense-in-depth: two structural refusals in series, both
# pre-linguistic.
# ============================================================================

READONLY_TOOL_MANIFEST: FrozenSet[str] = frozenset({
    "read_file",
    "search_code",
    "list_symbols",
    "get_callers",
    "glob_files",
    "list_dir",
    "git_log",
    "git_diff",
    "git_blame",
})


# Iron Gate diversity requirement: the subagent must call tools from at
# least MIN_TOOL_DIVERSITY distinct *classes* before producing a final
# result. A subagent that only calls `read_file` five times fails the
# diversity check and has its result rejected by the Iron Gate, per
# Derek's Phase 1 mandate.
#
# Tool classes (matches ExplorationLedger's categorization):
#   comprehension → read_file, list_symbols
#   discovery     → glob_files, list_dir
#   call_graph    → get_callers
#   pattern       → search_code
#   history       → git_log, git_diff, git_blame
MIN_TOOL_DIVERSITY = int(os.environ.get("JARVIS_SUBAGENT_MIN_DIVERSITY", "2"))


TOOL_CLASS_MAP: Dict[str, str] = {
    "read_file": "comprehension",
    "list_symbols": "comprehension",
    "glob_files": "discovery",
    "list_dir": "discovery",
    "get_callers": "call_graph",
    "search_code": "pattern",
    "git_log": "history",
    "git_diff": "history",
    "git_blame": "history",
}


def classify_tools(tool_names: Tuple[str, ...]) -> FrozenSet[str]:
    """Return the set of tool *classes* covered by a sequence of tool calls."""
    return frozenset(
        TOOL_CLASS_MAP[name]
        for name in tool_names
        if name in TOOL_CLASS_MAP
    )


# ============================================================================
# Persona prompt — Manifesto §6 strict definition.
#
# Per Derek: this is not a generic helpful-assistant system prompt. The
# subagent is explicitly constrained as a read-only cartographer with
# mathematical forbiddance of mutation intent. The Iron Gate rejection
# clause is included directly in the prompt so the model understands
# that shallow behavior triggers result rejection, not a retry.
# ============================================================================

EXPLORE_SUBAGENT_SYSTEM_PROMPT = (
    "You are a read-only architectural cartographer. Your sole objective is "
    "to aggressively map the call graph and state boundaries of the "
    "requested code. You are mathematically forbidden from proposing "
    "mutations. If you fail to utilize diverse search tools (e.g., relying "
    "solely on shallow file reads instead of search_code and get_callers), "
    "the Iron Gate will reject your result.\n"
    "\n"
    "Available tools (read-only, strictly enforced):\n"
    "  " + ", ".join(sorted(READONLY_TOOL_MANIFEST)) + "\n"
    "\n"
    "Tool diversity requirement: you must exercise tools from at least "
    f"{MIN_TOOL_DIVERSITY} distinct classes — comprehension (read_file, "
    "list_symbols), discovery (glob_files, list_dir), call_graph "
    "(get_callers), pattern (search_code), history (git_log/diff/blame). "
    "A result built only on comprehension tools will be rejected.\n"
    "\n"
    "Emit your final answer as a structured JSON SubagentResult with "
    "typed findings grouped by category (import_chain | call_graph | "
    "complexity | pattern | structure | api_surface). Each finding must "
    "have file_path + line + evidence. Prefer high-signal findings "
    "(relevance >= 0.4) over exhaustive but shallow enumeration."
)


# ============================================================================
# Types — frozen dataclasses for cross-task safety under asyncio.
# ============================================================================


class SubagentType(str, Enum):
    """Subagent types available for dispatch.

    Phase 1 ships only EXPLORE. Additional types are reserved; the policy
    engine denies requests for them in Phase 1 with a specific reason.
    """
    EXPLORE = "explore"
    # PLAN = "plan"          # Phase C
    # REVIEW = "review"      # Phase B
    # RESEARCH = "research"  # Phase B
    # REFACTOR = "refactor"  # Phase B


class SubagentStatus(str, Enum):
    """Terminal status for a subagent dispatch."""
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"           # agentic path failed; deterministic fallback merged
    BUDGET_EXHAUSTED = "budget_exhausted"
    DIVERSITY_REJECTED = "diversity_rejected"
    NOT_IMPLEMENTED = "not_implemented"  # Step-1 scaffolding sentinel


@dataclass(frozen=True)
class SubagentFinding:
    """One structured finding from subagent exploration.

    Parallels `ExplorationFinding` in `exploration_subagent.py` but is the
    *model-layer* contract — what the parent generation model receives
    via the tool result channel.
    """
    category: str
    description: str
    file_path: str = ""
    line: int = 0
    evidence: str = ""
    relevance: float = 0.0

    def truncated(self) -> "SubagentFinding":
        """Return a copy whose evidence respects MAX_EVIDENCE_CHARS_PER_FINDING."""
        ev = self.evidence
        if len(ev) > MAX_EVIDENCE_CHARS_PER_FINDING:
            ev = ev[:MAX_EVIDENCE_CHARS_PER_FINDING] + "…[truncated]"
        return SubagentFinding(
            category=self.category,
            description=self.description,
            file_path=self.file_path,
            line=self.line,
            evidence=ev,
            relevance=self.relevance,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "description": self.description,
            "file_path": self.file_path,
            "line": self.line,
            "evidence": self.evidence,
            "relevance": round(self.relevance, 3),
        }


@dataclass(frozen=True)
class SubagentRequest:
    """Request schema for a subagent dispatch — parsed from the
    `dispatch_subagent` Venom tool call arguments.

    Hard caps from module constants are enforced at construction time
    (parallel_scopes clamped rather than rejected so a too-ambitious
    model doesn't get a confusing policy denial).
    """
    subagent_type: SubagentType
    goal: str
    target_files: Tuple[str, ...] = ()
    scope_paths: Tuple[str, ...] = ()
    max_files: int = 20
    max_depth: int = 3
    timeout_s: float = 120.0
    parallel_scopes: int = 1

    def __post_init__(self) -> None:
        if not self.goal:
            raise ValueError("SubagentRequest.goal is required")
        if self.parallel_scopes < 1:
            raise ValueError(
                f"parallel_scopes={self.parallel_scopes} must be >= 1"
            )
        if self.parallel_scopes > MAX_PARALLEL_SCOPES:
            raise ValueError(
                f"parallel_scopes={self.parallel_scopes} exceeds "
                f"MAX_PARALLEL_SCOPES={MAX_PARALLEL_SCOPES}"
            )
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s={self.timeout_s} must be > 0")
        if self.max_files < 1:
            raise ValueError(f"max_files={self.max_files} must be >= 1")
        if self.max_depth < 1:
            raise ValueError(f"max_depth={self.max_depth} must be >= 1")

    @classmethod
    def from_args(cls, args: Dict[str, Any]) -> "SubagentRequest":
        """Parse a request from the `dispatch_subagent` tool call arguments.

        Clamps parallel_scopes to MAX_PARALLEL_SCOPES so a model requesting
        parallel_scopes=8 receives MAX_PARALLEL_SCOPES (=3) subagents rather
        than a confusing validation error. Unknown subagent_type raises.
        """
        parallel = min(int(args.get("parallel_scopes", 1) or 1), MAX_PARALLEL_SCOPES)
        parallel = max(parallel, 1)
        type_str = str(args.get("subagent_type", "explore") or "explore").lower()
        try:
            st = SubagentType(type_str)
        except ValueError as e:
            raise ValueError(
                f"Unknown subagent_type={type_str!r}. "
                f"Phase 1 supports only: {[t.value for t in SubagentType]}"
            ) from e
        return cls(
            subagent_type=st,
            goal=str(args.get("goal", "")).strip(),
            target_files=tuple(str(p) for p in (args.get("target_files") or ())),
            scope_paths=tuple(str(p) for p in (args.get("scope_paths") or ())),
            max_files=int(args.get("max_files", 20) or 20),
            max_depth=int(args.get("max_depth", 3) or 3),
            timeout_s=float(args.get("timeout_s", 120.0) or 120.0),
            parallel_scopes=parallel,
        )


@dataclass(frozen=True)
class SubagentResult:
    """Structured result returned to the parent generation model.

    Truncation via `truncated_for_prompt()` enforces MAX_FINDINGS_RETURNED,
    MAX_SUMMARY_CHARS, and MAX_EVIDENCE_CHARS_PER_FINDING before the
    payload is reinjected into the parent's prompt.
    """
    schema_version: str = SCHEMA_VERSION
    subagent_id: str = ""
    subagent_type: SubagentType = SubagentType.EXPLORE
    status: SubagentStatus = SubagentStatus.COMPLETED
    goal: str = ""
    started_at_ns: int = 0
    finished_at_ns: int = 0
    findings: Tuple[SubagentFinding, ...] = ()
    files_read: Tuple[str, ...] = ()
    search_queries: Tuple[str, ...] = ()
    summary: str = ""
    cost_usd: float = 0.0
    tool_calls: int = 0
    tool_diversity: int = 0
    provider_used: str = ""              # e.g. "doubleword-397b", "claude-api"
    fallback_triggered: bool = False     # True when Nervous System Reflex fired
    error_class: str = ""
    error_detail: str = ""

    @property
    def duration_s(self) -> float:
        if self.finished_at_ns and self.started_at_ns:
            return (self.finished_at_ns - self.started_at_ns) / 1_000_000_000.0
        return 0.0

    def truncated_for_prompt(self) -> "SubagentResult":
        """Return a copy truncated for re-injection into the parent's prompt.

        Findings are sorted by descending relevance before truncation so
        the highest-signal findings are preserved. Summary is hard-clipped
        at MAX_SUMMARY_CHARS with a visible `…[truncated]` marker.
        """
        sorted_findings = sorted(
            self.findings,
            key=lambda f: (-f.relevance, f.file_path, f.line),
        )
        kept = tuple(f.truncated() for f in sorted_findings[:MAX_FINDINGS_RETURNED])
        summary = self.summary
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + "…[truncated]"
        return SubagentResult(
            schema_version=self.schema_version,
            subagent_id=self.subagent_id,
            subagent_type=self.subagent_type,
            status=self.status,
            goal=self.goal,
            started_at_ns=self.started_at_ns,
            finished_at_ns=self.finished_at_ns,
            findings=kept,
            files_read=self.files_read,
            search_queries=self.search_queries,
            summary=summary,
            cost_usd=self.cost_usd,
            tool_calls=self.tool_calls,
            tool_diversity=self.tool_diversity,
            provider_used=self.provider_used,
            fallback_triggered=self.fallback_triggered,
            error_class=self.error_class,
            error_detail=self.error_detail,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subagent_id": self.subagent_id,
            "subagent_type": self.subagent_type.value,
            "status": self.status.value,
            "goal": self.goal,
            "started_at_ns": self.started_at_ns,
            "finished_at_ns": self.finished_at_ns,
            "duration_s": round(self.duration_s, 3),
            "findings": [f.to_dict() for f in self.findings],
            "files_read": list(self.files_read),
            "search_queries": list(self.search_queries),
            "summary": self.summary,
            "cost_usd": round(self.cost_usd, 6),
            "tool_calls": self.tool_calls,
            "tool_diversity": self.tool_diversity,
            "provider_used": self.provider_used,
            "fallback_triggered": self.fallback_triggered,
            "error_class": self.error_class,
            "error_detail": self.error_detail,
        }


@dataclass
class SubagentContext:
    """Runtime state for a single subagent dispatch.

    Not frozen — `yield_requested` and `cost_remaining_usd` mutate under
    cooperative-cancellation and cost-accumulation semantics. All other
    fields are set at construction and never mutated after.

    The orchestrator builds one context per dispatch (single or each
    parallel scope). The context carries the Nervous System Reflex
    provider pair: `primary_provider_name` and `fallback_provider_name`.
    Step 1 stores them; Step 2 wires the actual fallback logic.
    """
    parent_op_id: str
    parent_ctx: Any                         # OperationContext; forward ref to avoid cycle
    subagent_id: str                        # op-<parent-uuid>::sub-<seq>
    subagent_type: SubagentType
    request: SubagentRequest
    deadline: Optional[datetime] = None
    scope_path: str = ""                    # set for parallel fan-out, empty otherwise
    yield_requested: bool = False
    cost_remaining_usd: float = 0.0
    primary_provider_name: str = ""         # inherited from parent
    fallback_provider_name: str = "claude-api"   # Nervous System Reflex target
    tool_loop: Optional[Any] = None         # ToolLoopCoordinator; forward ref

    def request_yield(self) -> None:
        """Cooperative cancellation — signals the subagent to exit at next checkpoint."""
        self.yield_requested = True

    def accrue_cost(self, usd: float) -> None:
        """Reduce remaining budget by cost of a subagent call."""
        self.cost_remaining_usd = max(0.0, self.cost_remaining_usd - max(0.0, usd))


# ============================================================================
# Exceptions — each represents a structurally distinct failure mode.
# ============================================================================


class SubagentError(Exception):
    """Base exception for subagent dispatch failures."""


class ParentBudgetExhausted(SubagentError):
    """Raised when the parent op's cost cap is exceeded mid-subagent."""


class IronGateDiversityRejection(SubagentError):
    """Raised when the subagent fails to meet MIN_TOOL_DIVERSITY before emitting a result.

    Per Derek's Phase 1 mandate: a subagent that relies only on shallow
    `read_file` calls has its result REJECTED — not retried, not demoted.
    The parent receives a SubagentResult with status=DIVERSITY_REJECTED
    and no findings.
    """


class SubagentTimeout(SubagentError):
    """Raised when both primary and fallback providers time out.

    Only used when Nervous System Reflex fallback is wired (Step 2). In
    Step 1 scaffolding, timeouts surface as SubagentStatus.FAILED with
    error_class="SubagentTimeout".
    """


class SubagentMutationAttempt(SubagentError):
    """Raised when the subagent attempts a tool call outside READONLY_TOOL_MANIFEST.

    Manifesto §6 enforcement. Structurally impossible for well-behaved
    models but defense-in-depth against model drift or prompt injection.
    """


class SubagentDispatchDisabled(SubagentError):
    """Raised when dispatch is attempted while the master switch is off.

    Exists so a miswired caller gets a clear error rather than silent
    no-op behavior.
    """
