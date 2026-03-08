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

    CLASSIFY -> ROUTE -> GENERATE -> VALIDATE -> GATE -> APPROVE -> APPLY -> VERIFY -> COMPLETE
                              |           |        |       |          |          |
                              v           v        v       v          v          v
                         GEN_RETRY   VAL_RETRY          EXPIRED   POSTMORTEM  POSTMORTEM
                              |           |
                              v           v
                          VALIDATE       GATE

    (most non-terminal phases can also transition to CANCELLED)

Terminal phases: COMPLETE, CANCELLED, EXPIRED, POSTMORTEM
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Dict, Optional, Set, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.routing_policy import RoutingDecision


# ---------------------------------------------------------------------------
# Phase Enum
# ---------------------------------------------------------------------------


class OperationPhase(Enum):
    """Pipeline phase for an autonomous Ouroboros operation."""

    CLASSIFY = auto()
    ROUTE = auto()
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

PHASE_TRANSITIONS: Dict[OperationPhase, Set[OperationPhase]] = {
    OperationPhase.CLASSIFY: {
        OperationPhase.ROUTE,
        OperationPhase.CANCELLED,
    },
    OperationPhase.ROUTE: {
        OperationPhase.GENERATE,
        OperationPhase.CANCELLED,
    },
    OperationPhase.GENERATE: {
        OperationPhase.VALIDATE,
        OperationPhase.GENERATE_RETRY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.GENERATE_RETRY: {
        OperationPhase.VALIDATE,
        OperationPhase.GENERATE_RETRY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.VALIDATE: {
        OperationPhase.GATE,
        OperationPhase.VALIDATE_RETRY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.VALIDATE_RETRY: {
        OperationPhase.GATE,
        OperationPhase.VALIDATE_RETRY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.GATE: {
        OperationPhase.APPROVE,
        OperationPhase.APPLY,
        OperationPhase.CANCELLED,
    },
    OperationPhase.APPROVE: {
        OperationPhase.APPLY,
        OperationPhase.CANCELLED,
        OperationPhase.EXPIRED,
    },
    OperationPhase.APPLY: {
        OperationPhase.VERIFY,
        OperationPhase.POSTMORTEM,
        OperationPhase.CANCELLED,
    },
    OperationPhase.VERIFY: {
        OperationPhase.COMPLETE,
        OperationPhase.POSTMORTEM,
    },
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
        _timestamp: Optional[datetime] = None,
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
