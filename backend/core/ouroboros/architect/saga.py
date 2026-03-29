"""Saga state schemas for the Architecture Reasoning Agent's WAL-backed execution.

`StepState` tracks the lifecycle of a single plan step; `SagaRecord` tracks
the overall execution of a plan across all steps.  Both types are fully
serialisable to/from plain dicts so that the Write-Ahead Log (WAL) can
persist and replay them without any framework dependency.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SagaPhase(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ABORTED = "aborted"


class StepPhase(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# StepState
# ---------------------------------------------------------------------------


@dataclass
class StepState:
    """Mutable state container for a single saga step."""

    step_index: int
    phase: StepPhase
    envelope_id: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None

    # --- serialization ---

    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "phase": self.phase.value,
            "envelope_id": self.envelope_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StepState":
        return cls(
            step_index=d["step_index"],
            phase=StepPhase(d["phase"]),
            envelope_id=d.get("envelope_id"),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            error=d.get("error"),
        )


# ---------------------------------------------------------------------------
# SagaRecord
# ---------------------------------------------------------------------------


@dataclass
class SagaRecord:
    """Mutable record representing the full execution state of one plan saga."""

    saga_id: str
    plan_id: str
    plan_hash: str
    phase: SagaPhase
    step_states: Dict[int, StepState]
    created_at: float
    completed_at: Optional[float] = None
    abort_reason: Optional[str] = None

    # --- factory ---

    @classmethod
    def create(
        cls,
        saga_id: str,
        plan_id: str,
        plan_hash: str,
        num_steps: int,
    ) -> "SagaRecord":
        """Create a new saga with all steps initialised to PENDING."""
        return cls(
            saga_id=saga_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            phase=SagaPhase.PENDING,
            step_states={
                i: StepState(step_index=i, phase=StepPhase.PENDING)
                for i in range(num_steps)
            },
            created_at=time.time(),
        )

    # --- computed properties ---

    @property
    def all_steps_complete(self) -> bool:
        """True iff every step (if any) has reached COMPLETE phase."""
        return all(s.phase is StepPhase.COMPLETE for s in self.step_states.values())

    @property
    def has_failed_step(self) -> bool:
        """True iff at least one step is in FAILED phase."""
        return any(s.phase is StepPhase.FAILED for s in self.step_states.values())

    # --- serialization ---

    def to_dict(self) -> dict:
        return {
            "saga_id": self.saga_id,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "phase": self.phase.value,
            "step_states": {
                str(k): v.to_dict() for k, v in self.step_states.items()
            },
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "abort_reason": self.abort_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SagaRecord":
        return cls(
            saga_id=d["saga_id"],
            plan_id=d["plan_id"],
            plan_hash=d["plan_hash"],
            phase=SagaPhase(d["phase"]),
            step_states={
                int(k): StepState.from_dict(v)
                for k, v in d["step_states"].items()
            },
            created_at=d["created_at"],
            completed_at=d.get("completed_at"),
            abort_reason=d.get("abort_reason"),
        )
