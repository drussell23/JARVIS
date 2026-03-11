"""Advanced Autonomy Service — L4 Advanced Coordination.

Hosts cross-repo saga persistence, consensus voting, and dynamic tier
recommendations. All outputs are advisory CommandEnvelopes routed to L1
via the CommandBus.

Single-writer invariant: this module NEVER mutates op_context, ledger,
filesystem, or trust tiers directly. Saga state is internal to L4.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

logger = logging.getLogger("Ouroboros.AdvancedCoordination")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AdvancedCoordinationConfig:
    """Configuration for the L4 Advanced Coordination layer.

    Parameters
    ----------
    state_dir:
        Directory where saga state JSON files are persisted. Created
        automatically if it does not exist.
    saga_timeout_s:
        Maximum wall-clock seconds a saga may remain in a non-terminal
        phase before it is considered stale.
    max_concurrent_sagas:
        Upper bound on in-flight sagas. Advisory; enforced by
        ``create_saga()``.
    consensus_timeout_per_brain_s:
        Maximum seconds to wait for a single brain's consensus vote.
    """

    state_dir: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "saga_state"
    )
    saga_timeout_s: float = 600.0
    max_concurrent_sagas: int = 1
    consensus_timeout_per_brain_s: float = 120.0


# ---------------------------------------------------------------------------
# SagaState — value object persisted to disk
# ---------------------------------------------------------------------------


@dataclass
class SagaState:
    """Represents the durable state of a cross-repo saga.

    Phases
    ------
    CREATED      — Saga has been initialised but no repo has been touched.
    IN_PROGRESS  — At least one (but not all) repos have been applied.
    COMPLETED    — All repos applied successfully.
    FAILED       — At least one repo failed to apply.
    """

    saga_id: str
    repos: List[str]
    patches: Dict[str, str]
    phase: str = "CREATED"  # CREATED | IN_PROGRESS | COMPLETED | FAILED
    repos_applied: List[str] = field(default_factory=list)
    repos_failed: List[str] = field(default_factory=list)
    idempotency_key: str = ""
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = self.saga_id
        self._update_checksum()

    def _update_checksum(self) -> None:
        """Recompute the integrity checksum from canonical state fields."""
        raw = json.dumps(
            {
                "saga_id": self.saga_id,
                "phase": self.phase,
                "repos_applied": sorted(self.repos_applied),
                "repos_failed": sorted(self.repos_failed),
            },
            sort_keys=True,
        )
        self.checksum = hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# AdvancedAutonomyService — L4 advisory coordinator
# ---------------------------------------------------------------------------


@dataclass
class ConsensusResult:
    """Result of a multi-brain consensus vote.

    Attributes
    ----------
    op_id:
        The operation identifier the vote pertains to.
    votes:
        Mapping of ``{brain_name: "approve"|"reject"}``.
    majority:
        ``True`` if strictly more than half the votes are ``"approve"``.
    approved_count:
        Number of ``"approve"`` votes.
    total_count:
        Total number of votes cast.
    """

    op_id: str
    votes: Dict[str, str]
    majority: bool
    approved_count: int
    total_count: int


# ---------------------------------------------------------------------------
# AdvancedAutonomyService — L4 advisory coordinator
# ---------------------------------------------------------------------------


class AdvancedAutonomyService:
    """L4 — Advanced Coordination. Advisory only.

    Manages cross-repo saga lifecycle with durable persistence and
    idempotent state transitions. Emits ``REQUEST_SAGA_SUBMIT``
    commands to L1 via the :class:`CommandBus` when a saga is ready
    to be committed.

    Recovery
    --------
    On construction, all ``saga_*.json`` files in the configured
    ``state_dir`` are loaded back into memory so that a process restart
    can seamlessly resume in-flight sagas.
    """

    def __init__(
        self,
        command_bus: CommandBus,
        config: Optional[AdvancedCoordinationConfig] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config or AdvancedCoordinationConfig()
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._sagas: Dict[str, SagaState] = {}
        self._load_persisted_sagas()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _saga_path(self, saga_id: str) -> Path:
        """Return the filesystem path for a saga's state file."""
        return self._config.state_dir / f"saga_{saga_id}.json"

    def _persist_saga(self, state: SagaState) -> None:
        """Write the saga state to disk as atomic-ish JSON."""
        state._update_checksum()
        data = {
            "saga_id": state.saga_id,
            "repos": state.repos,
            "patches": state.patches,
            "phase": state.phase,
            "repos_applied": state.repos_applied,
            "repos_failed": state.repos_failed,
            "idempotency_key": state.idempotency_key,
            "checksum": state.checksum,
        }
        self._saga_path(state.saga_id).write_text(json.dumps(data, indent=2))

    def _load_persisted_sagas(self) -> None:
        """Recover all persisted saga state files from disk."""
        for path in self._config.state_dir.glob("saga_*.json"):
            try:
                data = json.loads(path.read_text())
                state = SagaState(
                    saga_id=data["saga_id"],
                    repos=data["repos"],
                    patches=data.get("patches", {}),
                    phase=data.get("phase", "CREATED"),
                    repos_applied=data.get("repos_applied", []),
                    repos_failed=data.get("repos_failed", []),
                    idempotency_key=data.get("idempotency_key", data["saga_id"]),
                )
                expected = data.get("checksum", "")
                if expected and state.checksum != expected:
                    logger.warning(
                        "[AdvancedCoord] Saga %s checksum mismatch — "
                        "state may have been tampered with",
                        state.saga_id,
                    )
                self._sagas[state.saga_id] = state
            except Exception as exc:
                logger.warning(
                    "[AdvancedCoord] Failed to load %s: %s", path.name, exc
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_saga(
        self,
        repos: List[str],
        patches: Dict[str, str],
    ) -> str:
        """Create a new cross-repo saga and persist its initial state.

        Parameters
        ----------
        repos:
            List of repository identifiers participating in this saga.
        patches:
            Mapping of ``{repo_name: patch_data}`` for each participant.

        Returns
        -------
        str
            A short saga identifier (UUID prefix).
        """
        saga_id = str(uuid.uuid4())[:12]
        state = SagaState(saga_id=saga_id, repos=repos, patches=patches)
        self._sagas[saga_id] = state
        self._persist_saga(state)
        logger.info(
            "[AdvancedCoord] Created saga %s for repos %s",
            saga_id,
            repos,
        )
        return saga_id

    def advance_saga(self, saga_id: str, repo: str, success: bool) -> None:
        """Advance a saga by recording the outcome for one repo.

        Idempotent: advancing the same repo+outcome twice is a no-op.
        Phase transitions are computed automatically:

        - All repos applied -> COMPLETED
        - Any repo failed   -> FAILED
        - Otherwise         -> IN_PROGRESS
        """
        state = self._sagas.get(saga_id)
        if state is None:
            logger.warning("[AdvancedCoord] Unknown saga: %s", saga_id)
            return

        if success:
            if repo not in state.repos_applied:
                state.repos_applied.append(repo)
        else:
            if repo not in state.repos_failed:
                state.repos_failed.append(repo)

        # Compute phase
        if set(state.repos_applied) >= set(state.repos):
            state.phase = "COMPLETED"
        elif state.repos_failed:
            state.phase = "FAILED"
        else:
            state.phase = "IN_PROGRESS"

        self._persist_saga(state)
        logger.info(
            "[AdvancedCoord] Saga %s advanced: repo=%s success=%s phase=%s",
            saga_id,
            repo,
            success,
            state.phase,
        )

    def get_saga_state(self, saga_id: str) -> Optional[SagaState]:
        """Return the in-memory saga state, or ``None`` if not found."""
        return self._sagas.get(saga_id)

    def request_saga_submit(self, saga_id: str) -> None:
        """Emit a REQUEST_SAGA_SUBMIT command to L1 for this saga.

        Does nothing if the saga ID is unknown.
        """
        state = self._sagas.get(saga_id)
        if state is None:
            logger.warning(
                "[AdvancedCoord] Cannot submit unknown saga: %s", saga_id
            )
            return

        cmd = CommandEnvelope(
            source_layer="L4",
            target_layer="L1",
            command_type=CommandType.REQUEST_SAGA_SUBMIT,
            payload={
                "saga_id": saga_id,
                "repo_patches": state.patches,
                "idempotency_key": state.idempotency_key,
            },
            ttl_s=300.0,
        )
        self._bus.try_put(cmd)
        logger.info(
            "[AdvancedCoord] Submitted saga %s to command bus", saga_id
        )

    def record_vote(
        self,
        op_id: str,
        candidates: List[str],
        votes: Dict[str, str],
    ) -> ConsensusResult:
        """Record multi-brain votes and emit consensus result.

        Each brain casts an ``"approve"`` or ``"reject"`` vote.  A majority
        requires strictly more than half the votes to be ``"approve"``.
        The result is emitted as a ``REPORT_CONSENSUS`` command to L1.

        Parameters
        ----------
        op_id:
            Operation identifier the vote pertains to.
        candidates:
            List of candidate identifiers being voted on.
        votes:
            Mapping of ``{brain_name: "approve"|"reject"}``.

        Returns
        -------
        ConsensusResult
            Aggregated vote result including majority determination.
        """
        approved = sum(1 for v in votes.values() if v == "approve")
        total = len(votes)
        majority = approved > total / 2

        result = ConsensusResult(
            op_id=op_id,
            votes=votes,
            majority=majority,
            approved_count=approved,
            total_count=total,
        )

        cmd = CommandEnvelope(
            source_layer="L4",
            target_layer="L1",
            command_type=CommandType.REPORT_CONSENSUS,
            payload={
                "op_id": op_id,
                "candidates": candidates,
                "votes": votes,
                "majority": majority,
            },
            ttl_s=300.0,
        )
        self._bus.try_put(cmd)

        logger.info(
            "[AdvancedCoord] Consensus for op %s: %d/%d approve, majority=%s",
            op_id,
            approved,
            total,
            majority,
        )

        return result

    def recommend_tier_change(
        self,
        repo: str,
        canary_slice: str,
        recommended_tier: str,
        evidence: Dict[str, Any],
    ) -> bool:
        """Recommend a trust tier change. Requires non-empty evidence."""
        if not evidence:
            logger.warning("[AdvancedCoord] Tier recommendation rejected: empty evidence")
            return False

        cmd = CommandEnvelope(
            source_layer="L4",
            target_layer="L1",
            command_type=CommandType.RECOMMEND_TIER_CHANGE,
            payload={
                "trigger_source": "l4_dynamic_override",
                "repo": repo,
                "canary_slice": canary_slice,
                "recommended_tier": recommended_tier,
                "evidence": evidence,
            },
            ttl_s=300.0,
        )
        return self._bus.try_put(cmd)
