"""M10 Slice 4 — Validation pipeline + OrangePRReviewer
integration (PRD §32.4.2).

Takes a :class:`SynthesizedProposal` (verdict=SYNTHESIZED) from
Slice 3 and advances it through the §32.4 lifecycle:

  GENERATING(done) → VALIDATING → COMMITTING → PUSHING →
  AWAITING_APPROVAL  (or FAILED / PUSH_FAILED on faults)

5-layer validation per §32.4.1 — Layers 3+4 parallel via
:func:`asyncio.gather`:

  1. **SideEffectFirewall** — synthesized code must not import
     stdlib I/O / network at module level (proposal modules
     are pure declaratives).
  2. **Protocol conformance** — kind-specific Protocol checks
     (`IntakeSensor` for NEW_SENSOR, etc.) on the parsed AST.
  3. **SemanticGuardian** — 10-pattern AST scanner (replaces
     §30.5.2's ASTValidator per §32.4.1).
  4. **SecurityScanner** — `__subclasses__` / `eval` /
     `__import__` introspection-escape detector (matches Phase
     7 P7.7 hardening).
  5. **pytest in worktree** — runs the proposal's auto-generated
     test against the worktree (bounded wall-clock).

Architectural decisions (operator mandate, AST-pinned at Slice 5):

  * **SP-V1** — Layered pipeline, Layers 3+4 parallel via
    `asyncio.gather`. Layers 1, 2, 5 sequential because each
    depends on the previous (firewall first, parse second,
    pytest only after worktree write).
  * **SP-V2** — `ValidationLayersProtocol` caller-injected
    so production wires real `SemanticGuardian` /
    `WorktreeManager` / `pytest`, while tests inject pure
    in-memory stubs. Synthesizer pattern (Slice 3 SP-G1)
    extended verbatim.
  * **SP-V3** — PR via `OrangePRReviewer.create_review_pr`
    (caller-injected via `OrangePRBridgeProtocol` for
    testability; NEVER calls subprocess directly).
  * **SP-V4** — Worktree via `WorktreeManager.create()`
    (caller-injected via `WorktreeBridgeProtocol`).
  * **SP-V5** — Commit via `AutoCommitter`-style structured
    message (caller-injected via `CommitBridgeProtocol`); NO
    custom `_commit_to_branch` logic.
  * **SP-V6** — NEVER raises out — every fault path produces
    a structured :class:`ProposalLifecycleResult` with the
    failed phase + diagnostic.
  * **SP-V7** — H3 inheritance — on `PUSH_FAILED`, branch is
    PRESERVED locally; the proposal record stays at
    `PUSH_FAILED` phase for retry. NEVER deletes branches on
    push failure.
  * **SP-V8** — H4 inheritance — approval timeout handling is
    Slice 5's responsibility (this slice produces records at
    `AWAITING_APPROVAL`); the orchestrator emits the timeout
    deadline as part of the result.
  * **SP-V9** — Authority asymmetry — module MUST NOT import
    orchestrator / iron_gate / providers / candidate_generator
    / urgency_router / tool_executor / auto_action_router /
    strategic_direction / change_engine / subagent_scheduler /
    semantic_guardian / policy / graduation_orchestrator. All
    real-world dependencies caller-injected via Protocol.

Closed enums: :class:`ValidationLayer` (5 values), :class:`Layer-
Verdict` (5 values, includes SKIPPED + DISABLED), :class:`Life-
cycleStage` (4-stage rollup for projection).

Frozen result containers: :class:`LayerResult` (per-layer),
:class:`ValidationResult` (aggregated), :class:`PRQueueResult`,
:class:`WorktreeResult`, :class:`CommitResult`,
:class:`ProposalLifecycleResult` (terminal aggregate).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import (
    Any, Awaitable, Callable, Dict, Optional, Protocol,
    Sequence, Tuple,
)

logger = logging.getLogger(__name__)


M10_LIFECYCLE_SCHEMA_VERSION: str = "m10_lifecycle.1"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        if n < floor:
            return floor
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        f = float(raw)
        if not math.isfinite(f):
            return default
        if f < floor:
            return floor
        if f > ceiling:
            return ceiling
        return f
    except (TypeError, ValueError):
        return default


def m10_lifecycle_pytest_timeout_s() -> float:
    """``JARVIS_M10_LIFECYCLE_PYTEST_TIMEOUT_S`` — wall-clock
    cap for Layer 5 pytest run. Default 120s; clamped
    [10, 1800]. Worktree pytest runs MUST be bounded — a
    proposal that hangs validation indefinitely consumes
    operator-attention budget."""
    return _read_float_knob(
        "JARVIS_M10_LIFECYCLE_PYTEST_TIMEOUT_S",
        120.0, 10.0, 1800.0,
    )


def m10_lifecycle_layer_timeout_s() -> float:
    """``JARVIS_M10_LIFECYCLE_LAYER_TIMEOUT_S`` — per-layer
    wall-clock for Layers 1-4. Default 30s; clamped
    [1, 300]."""
    return _read_float_knob(
        "JARVIS_M10_LIFECYCLE_LAYER_TIMEOUT_S",
        30.0, 1.0, 300.0,
    )


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class ValidationLayer(str, enum.Enum):
    """5 validation layers per §32.4.1. ``str``-subclass for
    JSON serialization + closed-enum dispatch."""

    SIDE_EFFECT_FIREWALL = "side_effect_firewall"
    """Layer 1 — pure declarative module check (no module-
    level I/O / network)."""

    PROTOCOL_CONFORMANCE = "protocol_conformance"
    """Layer 2 — kind-specific Protocol structural check
    (e.g., ``IntakeSensor.scan_once`` async method present)."""

    SEMANTIC_GUARDIAN = "semantic_guardian"
    """Layer 3 — 10-pattern AST scanner (replaces §30.5.2's
    ASTValidator)."""

    SECURITY_SCANNER = "security_scanner"
    """Layer 4 — introspection-escape detector
    (`__subclasses__` / `eval` / `__import__`)."""

    PYTEST_IN_WORKTREE = "pytest_in_worktree"
    """Layer 5 — runs auto-generated pytest in the proposal's
    worktree."""


class LayerVerdict(str, enum.Enum):
    """5-value closed taxonomy — INCLUDES `SKIPPED` /
    `DISABLED` so consumers can distinguish skipped-by-design
    from passed-without-running."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Layer was structurally skipped (e.g., NEW_FLAG_FAMILY
    proposals don't get a Protocol check). NOT a failure."""

    DISABLED = "disabled"
    """Master flag off OR caller declined to inject this
    layer. Default to PASSED for the overall verdict — caller
    explicitly opted out."""

    PROVIDER_ERROR = "provider_error"
    """Layer raised an unexpected exception (defensive
    capture). Contributes to overall FAILED."""


class LifecycleStage(str, enum.Enum):
    """4-stage rollup for observability. Mirrors §32.4.1
    H1-H6 phase clusters (validation / commit / push /
    review)."""

    VALIDATION = "validation"
    COMMIT = "commit"
    PUSH = "push"
    REVIEW = "review"


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerResult:
    """One validation layer's outcome. Frozen."""

    layer: ValidationLayer
    verdict: LayerVerdict
    detail: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer.value,
            "verdict": self.verdict.value,
            "detail": self.detail,
            "elapsed_s": float(self.elapsed_s),
        }


@dataclass(frozen=True)
class ValidationResult:
    """Aggregated 5-layer validation outcome."""

    proposal_id: str
    overall_verdict: LayerVerdict
    layer_results: Tuple[LayerResult, ...] = field(
        default_factory=tuple,
    )
    elapsed_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.overall_verdict is LayerVerdict.PASSED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "overall_verdict": self.overall_verdict.value,
            "passed": self.passed,
            "layer_results": [
                lr.to_dict() for lr in self.layer_results
            ],
            "elapsed_s": float(self.elapsed_s),
        }


@dataclass(frozen=True)
class WorktreeResult:
    """Outcome of WorktreeManager.create wrapped via
    :class:`WorktreeBridgeProtocol`."""

    success: bool
    worktree_path: str = ""
    branch_name: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "error": self.error,
        }


@dataclass(frozen=True)
class CommitResult:
    """Outcome of AutoCommitter-style structured commit."""

    success: bool
    commit_hash: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "commit_hash": self.commit_hash,
            "error": self.error,
        }


@dataclass(frozen=True)
class PRQueueResult:
    """Outcome of OrangePRReviewer.create_review_pr wrapped
    via :class:`OrangePRBridgeProtocol`."""

    success: bool
    pr_url: str = ""
    branch_name: str = ""
    push_failed: bool = False
    """True iff the underlying `git push` failed; the branch
    is PRESERVED locally per §32.4.4 H3 inheritance."""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "pr_url": self.pr_url,
            "branch_name": self.branch_name,
            "push_failed": self.push_failed,
            "error": self.error,
        }


@dataclass(frozen=True)
class ProposalLifecycleResult:
    """Aggregate Slice 4 outcome. Frozen — Slice 5 reads to
    project the lifecycle into observability surfaces."""

    proposal_id: str
    final_phase: Any
    """Held as :class:`Any` to avoid cross-module type-hint
    coupling; populated with :class:`M10ProposalPhase`
    values."""

    validation_result: Optional[ValidationResult] = None
    worktree_result: Optional[WorktreeResult] = None
    commit_result: Optional[CommitResult] = None
    pr_result: Optional[PRQueueResult] = None
    failure_reason: str = ""
    elapsed_s: float = 0.0
    schema_version: str = field(
        default=M10_LIFECYCLE_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        try:
            phase_value = (
                self.final_phase.value
                if hasattr(self.final_phase, "value")
                else str(self.final_phase)
            )
        except Exception:  # noqa: BLE001 — defensive
            phase_value = "unknown"
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "final_phase": phase_value,
            "validation_result": (
                self.validation_result.to_dict()
                if self.validation_result is not None
                else None
            ),
            "worktree_result": (
                self.worktree_result.to_dict()
                if self.worktree_result is not None
                else None
            ),
            "commit_result": (
                self.commit_result.to_dict()
                if self.commit_result is not None
                else None
            ),
            "pr_result": (
                self.pr_result.to_dict()
                if self.pr_result is not None
                else None
            ),
            "failure_reason": self.failure_reason,
            "elapsed_s": float(self.elapsed_s),
        }


# ---------------------------------------------------------------------------
# Caller-injected Protocols (testability)
# ---------------------------------------------------------------------------


class ValidationLayersProtocol(Protocol):
    """Caller-injected validation layer implementations.
    Production wires real :class:`SemanticGuardian` /
    `WorktreeManager` / pytest; tests inject in-memory stubs.
    Synthesizer Slice 3 SP-G1 pattern verbatim."""

    async def run_side_effect_firewall(
        self, *, code_text: str,
    ) -> LayerResult: ...

    async def run_protocol_conformance(
        self, *, code_text: str, class_name: str,
        proposal_kind_value: str,
    ) -> LayerResult: ...

    async def run_semantic_guardian(
        self, *, code_text: str,
    ) -> LayerResult: ...

    async def run_security_scanner(
        self, *, code_text: str,
    ) -> LayerResult: ...

    async def run_pytest_in_worktree(
        self, *, worktree_path: str,
    ) -> LayerResult: ...


class WorktreeBridgeProtocol(Protocol):
    """Caller-injected wrapper around ``WorktreeManager.create``.
    Production lazy-imports the manager + invokes; tests
    inject mocks."""

    async def create_worktree(
        self, *, proposal_id: str, branch_name: str,
    ) -> WorktreeResult: ...


class CommitBridgeProtocol(Protocol):
    """Caller-injected wrapper around ``AutoCommitter``-style
    structured commit. Writes ``code_text`` to
    ``worktree_path / module_path`` and creates a structured
    commit; production lazy-imports AutoCommitter."""

    async def write_and_commit(
        self,
        *,
        proposal_id: str,
        worktree_path: str,
        module_path: str,
        code_text: str,
        ast_pin_name: str,
    ) -> CommitResult: ...


class OrangePRBridgeProtocol(Protocol):
    """Caller-injected wrapper around
    ``OrangePRReviewer.create_review_pr``."""

    async def queue_review_pr(
        self,
        *,
        proposal_id: str,
        branch_name: str,
        worktree_path: str,
        proposal_summary: str,
    ) -> PRQueueResult: ...


# ---------------------------------------------------------------------------
# ProposalLifecycleOrchestrator
# ---------------------------------------------------------------------------


def _aggregate_validation_verdict(
    layer_results: Sequence[LayerResult],
) -> LayerVerdict:
    """Combine per-layer verdicts into the overall verdict.
    Closed-enum dispatch:

      * Any FAILED / PROVIDER_ERROR → overall FAILED
      * All DISABLED → overall DISABLED
      * Otherwise → PASSED (SKIPPED + PASSED layers count)

    NEVER raises."""
    try:
        if not layer_results:
            return LayerVerdict.DISABLED
        if any(
            r.verdict in (
                LayerVerdict.FAILED,
                LayerVerdict.PROVIDER_ERROR,
            )
            for r in layer_results
        ):
            return LayerVerdict.FAILED
        if all(
            r.verdict is LayerVerdict.DISABLED
            for r in layer_results
        ):
            return LayerVerdict.DISABLED
        return LayerVerdict.PASSED
    except Exception:  # noqa: BLE001 — defensive
        return LayerVerdict.FAILED


class ProposalLifecycleOrchestrator:
    """Stateless orchestrator. NEVER raises out of
    :meth:`advance` — every fault path produces a frozen
    :class:`ProposalLifecycleResult` with the failed phase +
    diagnostic.

    Production: lazy-singleton via
    :func:`get_default_lifecycle`. Tests: construct fresh +
    inject all 4 Protocol-typed bridges."""

    async def validate_only(
        self,
        synthesized: Any,
        *,
        layers: ValidationLayersProtocol,
    ) -> ValidationResult:
        """Run the 5-layer validation pipeline only —
        without worktree/commit/PR. Useful for tests + dry-run
        operator REPL invocations. NEVER raises."""
        started = time.monotonic()
        proposal_id = getattr(synthesized, "proposal_id", "")
        code_text = getattr(synthesized, "code_text", "") or ""
        class_name = getattr(synthesized, "class_name", "") or ""
        kind_value = ""
        try:
            kind = getattr(synthesized, "kind", None)
            kind_value = (
                kind.value
                if hasattr(kind, "value")
                else str(kind)
            )
        except Exception:  # noqa: BLE001 — defensive
            kind_value = "unknown"

        if layers is None:
            return ValidationResult(
                proposal_id=proposal_id,
                overall_verdict=LayerVerdict.DISABLED,
                elapsed_s=time.monotonic() - started,
            )

        results: list = []

        # Layer 1 — SideEffectFirewall (sequential)
        results.append(
            await self._run_layer_safe(
                ValidationLayer.SIDE_EFFECT_FIREWALL,
                layers.run_side_effect_firewall(
                    code_text=code_text,
                ),
            ),
        )
        if results[-1].verdict in (
            LayerVerdict.FAILED, LayerVerdict.PROVIDER_ERROR,
        ):
            # Short-circuit: if firewall fails, parsing the
            # module is moot.
            return ValidationResult(
                proposal_id=proposal_id,
                overall_verdict=LayerVerdict.FAILED,
                layer_results=tuple(results),
                elapsed_s=time.monotonic() - started,
            )

        # Layer 2 — Protocol conformance (sequential)
        results.append(
            await self._run_layer_safe(
                ValidationLayer.PROTOCOL_CONFORMANCE,
                layers.run_protocol_conformance(
                    code_text=code_text,
                    class_name=class_name,
                    proposal_kind_value=kind_value,
                ),
            ),
        )
        if results[-1].verdict in (
            LayerVerdict.FAILED, LayerVerdict.PROVIDER_ERROR,
        ):
            return ValidationResult(
                proposal_id=proposal_id,
                overall_verdict=LayerVerdict.FAILED,
                layer_results=tuple(results),
                elapsed_s=time.monotonic() - started,
            )

        # Layers 3 + 4 — parallel via gather (SP-V1)
        try:
            l3_l4 = await asyncio.gather(
                self._run_layer_safe(
                    ValidationLayer.SEMANTIC_GUARDIAN,
                    layers.run_semantic_guardian(
                        code_text=code_text,
                    ),
                ),
                self._run_layer_safe(
                    ValidationLayer.SECURITY_SCANNER,
                    layers.run_security_scanner(
                        code_text=code_text,
                    ),
                ),
                return_exceptions=False,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[m10_lifecycle] gather Layers 3+4 raised: %s",
                exc,
            )
            l3_l4 = [
                LayerResult(
                    layer=ValidationLayer.SEMANTIC_GUARDIAN,
                    verdict=LayerVerdict.PROVIDER_ERROR,
                    detail=(
                        f"gather raised: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                ),
                LayerResult(
                    layer=ValidationLayer.SECURITY_SCANNER,
                    verdict=LayerVerdict.PROVIDER_ERROR,
                    detail="gather raised",
                ),
            ]
        results.extend(l3_l4)

        if any(
            r.verdict in (
                LayerVerdict.FAILED,
                LayerVerdict.PROVIDER_ERROR,
            )
            for r in l3_l4
        ):
            return ValidationResult(
                proposal_id=proposal_id,
                overall_verdict=LayerVerdict.FAILED,
                layer_results=tuple(results),
                elapsed_s=time.monotonic() - started,
            )

        # Layer 5 — pytest is deferred until after worktree
        # write (it needs the on-disk module to import).
        # Slice-4 `validate_only` returns the 4-layer subset;
        # Layer 5 fires inside :meth:`advance` AFTER commit.
        # For dry-run we mark Layer 5 as DISABLED with a
        # diagnostic so the operator-explainability stays
        # honest.
        results.append(
            LayerResult(
                layer=ValidationLayer.PYTEST_IN_WORKTREE,
                verdict=LayerVerdict.DISABLED,
                detail=(
                    "Layer 5 deferred to advance() — requires "
                    "worktree write before pytest can import"
                ),
            ),
        )
        return ValidationResult(
            proposal_id=proposal_id,
            overall_verdict=_aggregate_validation_verdict(
                results,
            ),
            layer_results=tuple(results),
            elapsed_s=time.monotonic() - started,
        )

    async def _run_layer_safe(
        self,
        layer: ValidationLayer,
        coroutine: Awaitable[LayerResult],
    ) -> LayerResult:
        """Run one layer with timeout + exception isolation.
        NEVER raises."""
        try:
            return await asyncio.wait_for(
                coroutine,
                timeout=m10_lifecycle_layer_timeout_s(),
            )
        except asyncio.TimeoutError:
            return LayerResult(
                layer=layer,
                verdict=LayerVerdict.PROVIDER_ERROR,
                detail=(
                    f"layer timeout after "
                    f"{m10_lifecycle_layer_timeout_s()}s"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return LayerResult(
                layer=layer,
                verdict=LayerVerdict.PROVIDER_ERROR,
                detail=(
                    f"layer raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

    async def advance(
        self,
        synthesized: Any,
        *,
        layers: ValidationLayersProtocol,
        worktree_bridge: WorktreeBridgeProtocol,
        commit_bridge: CommitBridgeProtocol,
        pr_bridge: OrangePRBridgeProtocol,
    ) -> ProposalLifecycleResult:
        """**Authoritative entry point.** Take SYNTHESIZED
        proposal → validate → create worktree → write+commit →
        run Layer 5 pytest → push (via PR bridge) → queue PR.
        Maps to M10ProposalPhase transitions per §32.4.1
        H1-H6.

        Returns frozen :class:`ProposalLifecycleResult` with
        terminal phase + per-stage results. NEVER raises."""
        started = time.monotonic()
        proposal_id = getattr(synthesized, "proposal_id", "")

        # Lazy-import phases (testability — module-load
        # decoupled from primitives)
        try:
            from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
                M10ProposalPhase,
                m10_arch_proposer_enabled,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase="unknown",
                failure_reason=(
                    f"primitives import failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                elapsed_s=time.monotonic() - started,
            )

        if not m10_arch_proposer_enabled():
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase=M10ProposalPhase.DECIDED_SKIP,
                failure_reason="master_flag_off",
                elapsed_s=time.monotonic() - started,
            )

        # ─── VALIDATING ────────────────────────────────────
        validation = await self.validate_only(
            synthesized, layers=layers,
        )
        # Early-exit if any non-pytest layer FAILED
        non_pytest = [
            r for r in validation.layer_results
            if r.layer is not ValidationLayer.PYTEST_IN_WORKTREE
        ]
        if any(
            r.verdict in (
                LayerVerdict.FAILED,
                LayerVerdict.PROVIDER_ERROR,
            )
            for r in non_pytest
        ):
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase=M10ProposalPhase.FAILED,
                validation_result=validation,
                failure_reason=(
                    "validation failed at layers 1-4"
                ),
                elapsed_s=time.monotonic() - started,
            )

        # ─── WORKTREE_CREATING ─────────────────────────────
        # Branch name discipline — namespace under ouroboros/m10
        # so OrangePRReviewer's existing branch-cleanup paths
        # don't conflict with M10 proposal branches.
        branch_name = (
            f"ouroboros/m10/{proposal_id}"
        )
        worktree = await self._safe_call_worktree(
            worktree_bridge, proposal_id=proposal_id,
            branch_name=branch_name,
        )
        if not worktree.success:
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase=M10ProposalPhase.FAILED,
                validation_result=validation,
                worktree_result=worktree,
                failure_reason=(
                    f"worktree_create_failed: "
                    f"{worktree.error}"
                ),
                elapsed_s=time.monotonic() - started,
            )

        # ─── COMMITTING ────────────────────────────────────
        commit = await self._safe_call_commit(
            commit_bridge,
            proposal_id=proposal_id,
            worktree_path=worktree.worktree_path,
            module_path=getattr(
                synthesized, "module_path", "",
            ) or "",
            code_text=getattr(
                synthesized, "code_text", "",
            ) or "",
            ast_pin_name=getattr(
                synthesized, "ast_pin_name", "",
            ) or "",
        )
        if not commit.success:
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase=M10ProposalPhase.FAILED,
                validation_result=validation,
                worktree_result=worktree,
                commit_result=commit,
                failure_reason=(
                    f"commit_failed: {commit.error}"
                ),
                elapsed_s=time.monotonic() - started,
            )

        # ─── Layer 5 — pytest in worktree ───────────────────
        # Now that the module exists on disk, pytest can
        # import + run the auto-generated test.
        try:
            pytest_result = await asyncio.wait_for(
                layers.run_pytest_in_worktree(
                    worktree_path=worktree.worktree_path,
                ),
                timeout=m10_lifecycle_pytest_timeout_s(),
            )
        except asyncio.TimeoutError:
            pytest_result = LayerResult(
                layer=ValidationLayer.PYTEST_IN_WORKTREE,
                verdict=LayerVerdict.PROVIDER_ERROR,
                detail=(
                    f"pytest timeout after "
                    f"{m10_lifecycle_pytest_timeout_s()}s"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            pytest_result = LayerResult(
                layer=ValidationLayer.PYTEST_IN_WORKTREE,
                verdict=LayerVerdict.PROVIDER_ERROR,
                detail=(
                    f"pytest raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        # Replace the deferred Layer 5 placeholder in the
        # validation result with the real outcome
        updated_layer_results = tuple(
            r if r.layer is not (
                ValidationLayer.PYTEST_IN_WORKTREE
            )
            else pytest_result
            for r in validation.layer_results
        )
        full_validation = ValidationResult(
            proposal_id=proposal_id,
            overall_verdict=_aggregate_validation_verdict(
                updated_layer_results,
            ),
            layer_results=updated_layer_results,
            elapsed_s=validation.elapsed_s,
        )

        if pytest_result.verdict in (
            LayerVerdict.FAILED, LayerVerdict.PROVIDER_ERROR,
        ):
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase=M10ProposalPhase.FAILED,
                validation_result=full_validation,
                worktree_result=worktree,
                commit_result=commit,
                failure_reason=(
                    f"pytest_failed: {pytest_result.detail}"
                ),
                elapsed_s=time.monotonic() - started,
            )

        # ─── PUSHING + AWAITING_APPROVAL via OrangePR ──────
        proposal_summary = (
            f"M10 proposal {proposal_id} — "
            f"validation {full_validation.overall_verdict.value}"
        )
        pr = await self._safe_call_pr(
            pr_bridge,
            proposal_id=proposal_id,
            branch_name=branch_name,
            worktree_path=worktree.worktree_path,
            proposal_summary=proposal_summary,
        )
        if pr.push_failed:
            # H3 inheritance — branch preserved locally
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase=M10ProposalPhase.PUSH_FAILED,
                validation_result=full_validation,
                worktree_result=worktree,
                commit_result=commit,
                pr_result=pr,
                failure_reason=(
                    f"push_failed (branch preserved): "
                    f"{pr.error}"
                ),
                elapsed_s=time.monotonic() - started,
            )
        if not pr.success:
            return ProposalLifecycleResult(
                proposal_id=proposal_id,
                final_phase=M10ProposalPhase.FAILED,
                validation_result=full_validation,
                worktree_result=worktree,
                commit_result=commit,
                pr_result=pr,
                failure_reason=(
                    f"pr_queue_failed: {pr.error}"
                ),
                elapsed_s=time.monotonic() - started,
            )

        # Happy path → AWAITING_APPROVAL
        return ProposalLifecycleResult(
            proposal_id=proposal_id,
            final_phase=M10ProposalPhase.AWAITING_APPROVAL,
            validation_result=full_validation,
            worktree_result=worktree,
            commit_result=commit,
            pr_result=pr,
            elapsed_s=time.monotonic() - started,
        )

    # -- defensive bridge wrappers --------------------------------

    async def _safe_call_worktree(
        self,
        bridge: WorktreeBridgeProtocol,
        *,
        proposal_id: str,
        branch_name: str,
    ) -> WorktreeResult:
        try:
            result = await bridge.create_worktree(
                proposal_id=proposal_id,
                branch_name=branch_name,
            )
            if not isinstance(result, WorktreeResult):
                return WorktreeResult(
                    success=False,
                    error="bridge returned non-WorktreeResult",
                )
            return result
        except Exception as exc:  # noqa: BLE001 — defensive
            return WorktreeResult(
                success=False,
                error=(
                    f"bridge raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

    async def _safe_call_commit(
        self,
        bridge: CommitBridgeProtocol,
        *,
        proposal_id: str,
        worktree_path: str,
        module_path: str,
        code_text: str,
        ast_pin_name: str,
    ) -> CommitResult:
        try:
            result = await bridge.write_and_commit(
                proposal_id=proposal_id,
                worktree_path=worktree_path,
                module_path=module_path,
                code_text=code_text,
                ast_pin_name=ast_pin_name,
            )
            if not isinstance(result, CommitResult):
                return CommitResult(
                    success=False,
                    error="bridge returned non-CommitResult",
                )
            return result
        except Exception as exc:  # noqa: BLE001 — defensive
            return CommitResult(
                success=False,
                error=(
                    f"bridge raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

    async def _safe_call_pr(
        self,
        bridge: OrangePRBridgeProtocol,
        *,
        proposal_id: str,
        branch_name: str,
        worktree_path: str,
        proposal_summary: str,
    ) -> PRQueueResult:
        try:
            result = await bridge.queue_review_pr(
                proposal_id=proposal_id,
                branch_name=branch_name,
                worktree_path=worktree_path,
                proposal_summary=proposal_summary,
            )
            if not isinstance(result, PRQueueResult):
                return PRQueueResult(
                    success=False,
                    error="bridge returned non-PRQueueResult",
                )
            return result
        except Exception as exc:  # noqa: BLE001 — defensive
            return PRQueueResult(
                success=False,
                error=(
                    f"bridge raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )


# ---------------------------------------------------------------------------
# Process-singleton
# ---------------------------------------------------------------------------


_DEFAULT_LIFECYCLE: Optional[
    ProposalLifecycleOrchestrator
] = None


def get_default_lifecycle() -> ProposalLifecycleOrchestrator:
    """Lazy-constructed singleton. NEVER raises."""
    global _DEFAULT_LIFECYCLE  # noqa: PLW0603
    if _DEFAULT_LIFECYCLE is None:
        _DEFAULT_LIFECYCLE = ProposalLifecycleOrchestrator()
    return _DEFAULT_LIFECYCLE


def reset_default_lifecycle_for_tests() -> None:
    global _DEFAULT_LIFECYCLE  # noqa: PLW0603
    _DEFAULT_LIFECYCLE = None


__all__ = [
    "CommitBridgeProtocol",
    "CommitResult",
    "LayerResult",
    "LayerVerdict",
    "LifecycleStage",
    "M10_LIFECYCLE_SCHEMA_VERSION",
    "OrangePRBridgeProtocol",
    "PRQueueResult",
    "ProposalLifecycleOrchestrator",
    "ProposalLifecycleResult",
    "ValidationLayer",
    "ValidationLayersProtocol",
    "ValidationResult",
    "WorktreeBridgeProtocol",
    "WorktreeResult",
    "get_default_lifecycle",
    "m10_lifecycle_layer_timeout_s",
    "m10_lifecycle_pytest_timeout_s",
    "reset_default_lifecycle_for_tests",
]
