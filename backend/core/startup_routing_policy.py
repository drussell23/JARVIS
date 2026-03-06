"""StartupRoutingPolicy — deadline-based deterministic fallback during boot.

Disease 10 — Startup Sequencing, Task 5.

Provides a routing policy that deterministically selects the best available
inference backend during the startup sequence.  The policy tracks signals
from GCP readiness, local model loading, and cloud fallback availability,
then applies a strict priority order with deadline enforcement to produce
a ``BootRoutingDecision``.

All decisions are logged in an observable ``decision_log`` for post-hoc
audit.  Once ``finalize()`` is called, the decision is locked and all
future signals are silently ignored.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BootRoutingDecision(str, enum.Enum):
    """Possible routing outcomes during boot."""

    PENDING = "pending"
    GCP_PRIME = "gcp_prime"
    LOCAL_MINIMAL = "local_minimal"
    CLOUD_CLAUDE = "cloud_claude"
    DEGRADED = "degraded"


class FallbackReason(str, enum.Enum):
    """Why the policy fell back from GCP_PRIME."""

    NONE = "none"
    GCP_DEADLINE_EXPIRED = "gcp_deadline_expired"
    GCP_REVOKED = "gcp_revoked"
    GCP_HANDSHAKE_FAILED = "gcp_handshake_failed"
    NO_AVAILABLE_PATH = "no_available_path"


# ---------------------------------------------------------------------------
# Dataclass: decision audit log entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionLogEntry:
    """Immutable record of a single routing decision for observability."""

    decision: BootRoutingDecision
    reason: FallbackReason
    timestamp: float = field(default_factory=time.monotonic)
    detail: str = ""


# ---------------------------------------------------------------------------
# StartupRoutingPolicy
# ---------------------------------------------------------------------------


class StartupRoutingPolicy:
    """Deadline-based deterministic fallback routing during boot.

    The policy follows a strict priority order:

    1. If GCP is ready and not revoked -> ``GCP_PRIME``
    2. If GCP was revoked -> fall back
    3. If GCP deadline expired -> fall back
    4. Otherwise -> ``PENDING`` (still waiting)

    Fallback chain: local_model -> cloud_claude -> degraded.

    Parameters
    ----------
    gcp_deadline_s:
        Maximum seconds to wait for GCP readiness before falling back.
    cloud_fallback_enabled:
        Whether Claude cloud API is available as a fallback option.
    """

    def __init__(
        self,
        gcp_deadline_s: float = 60.0,
        cloud_fallback_enabled: bool = True,
    ) -> None:
        self._created_at: float = time.monotonic()
        self._gcp_deadline_s: float = gcp_deadline_s
        self._cloud_fallback_enabled: bool = cloud_fallback_enabled

        # Signal state
        self._gcp_ready: bool = False
        self._gcp_host: Optional[str] = None
        self._gcp_port: Optional[int] = None
        self._gcp_revoked: bool = False
        self._gcp_revoke_reason: str = ""
        self._gcp_handshake_failed: bool = False
        self._gcp_handshake_fail_reason: str = ""
        self._local_loaded: bool = False

        # Finalization
        self._finalized: bool = False
        self._finalized_decision: Optional[Tuple[BootRoutingDecision, FallbackReason]] = None

        # Observability
        self._decision_log: List[DecisionLogEntry] = []

    # -- Properties ----------------------------------------------------------

    @property
    def is_finalized(self) -> bool:
        """Whether the policy has been finalized (decision locked)."""
        return self._finalized

    @property
    def decision_log(self) -> List[DecisionLogEntry]:
        """Copy of the decision audit log (safe to mutate)."""
        return list(self._decision_log)

    @property
    def gcp_deadline_remaining(self) -> float:
        """Seconds remaining until the GCP deadline expires (floored at 0)."""
        elapsed = time.monotonic() - self._created_at
        remaining = self._gcp_deadline_s - elapsed
        return max(0.0, remaining)

    # -- Signal methods ------------------------------------------------------

    def signal_gcp_ready(self, host: str, port: int) -> None:
        """Signal that the GCP VM is ready and reachable.

        No-op after ``finalize()``.
        """
        if self._finalized:
            logger.debug("signal_gcp_ready ignored — policy finalized")
            return
        self._gcp_ready = True
        self._gcp_host = host
        self._gcp_port = port
        logger.info("GCP readiness signalled: %s:%d", host, port)

    def signal_gcp_revoked(self, reason: str) -> None:
        """Signal that GCP readiness has been revoked.

        No-op after ``finalize()``.
        """
        if self._finalized:
            logger.debug("signal_gcp_revoked ignored — policy finalized")
            return
        self._gcp_revoked = True
        self._gcp_ready = False
        self._gcp_revoke_reason = reason
        logger.warning("GCP readiness revoked: %s", reason)

    def signal_local_model_loaded(self) -> None:
        """Signal that a local model has been loaded and is available.

        No-op after ``finalize()``.
        """
        if self._finalized:
            logger.debug("signal_local_model_loaded ignored — policy finalized")
            return
        self._local_loaded = True
        logger.info("Local model loaded")

    def signal_gcp_handshake_failed(self, reason: str) -> None:
        """Signal that the GCP handshake failed (capabilities mismatch, etc.).

        No-op after ``finalize()``.
        """
        if self._finalized:
            logger.debug("signal_gcp_handshake_failed ignored — policy finalized")
            return
        self._gcp_handshake_failed = True
        self._gcp_ready = False
        self._gcp_handshake_fail_reason = reason
        logger.warning("GCP handshake failed: %s", reason)

    # -- Decision engine -----------------------------------------------------

    def decide(self) -> Tuple[BootRoutingDecision, FallbackReason]:
        """Compute the current routing decision based on signal state.

        Each call appends a ``DecisionLogEntry`` to the audit log.

        Returns
        -------
        tuple of (BootRoutingDecision, FallbackReason)
            The decision and the reason for any fallback.
        """
        # If finalized, return the locked decision.
        if self._finalized and self._finalized_decision is not None:
            decision, reason = self._finalized_decision
            self._decision_log.append(
                DecisionLogEntry(
                    decision=decision,
                    reason=reason,
                    detail="finalized — decision locked",
                )
            )
            return decision, reason

        decision, reason, detail = self._compute_decision()

        entry = DecisionLogEntry(
            decision=decision,
            reason=reason,
            detail=detail,
        )
        self._decision_log.append(entry)

        logger.info(
            "Routing decision: %s (reason=%s, detail=%s)",
            decision.value,
            reason.value,
            detail,
        )

        return decision, reason

    def _compute_decision(
        self,
    ) -> Tuple[BootRoutingDecision, FallbackReason, str]:
        """Internal decision logic with strict priority ordering.

        Returns (decision, reason, detail_string).
        """
        # (1) GCP ready and not revoked -> GCP_PRIME
        if self._gcp_ready and not self._gcp_revoked:
            return (
                BootRoutingDecision.GCP_PRIME,
                FallbackReason.NONE,
                f"GCP ready at {self._gcp_host}:{self._gcp_port}",
            )

        # (2) GCP was revoked -> fallback
        if self._gcp_revoked:
            return self._select_fallback(
                FallbackReason.GCP_REVOKED,
                f"GCP revoked: {self._gcp_revoke_reason}",
            )

        # (3) GCP handshake failed -> fallback
        if self._gcp_handshake_failed:
            return self._select_fallback(
                FallbackReason.GCP_HANDSHAKE_FAILED,
                f"GCP handshake failed: {self._gcp_handshake_fail_reason}",
            )

        # (4) Deadline expired -> fallback
        if self.gcp_deadline_remaining <= 0.0:
            return self._select_fallback(
                FallbackReason.GCP_DEADLINE_EXPIRED,
                f"GCP deadline expired after {self._gcp_deadline_s:.1f}s",
            )

        # (5) Still waiting
        return (
            BootRoutingDecision.PENDING,
            FallbackReason.NONE,
            "waiting for GCP readiness or deadline",
        )

    def _select_fallback(
        self,
        reason: FallbackReason,
        context: str,
    ) -> Tuple[BootRoutingDecision, FallbackReason, str]:
        """Select the best available fallback path.

        Priority: local_model -> cloud_claude -> degraded.
        """
        if self._local_loaded:
            return (
                BootRoutingDecision.LOCAL_MINIMAL,
                reason,
                f"{context} — falling back to local model",
            )

        if self._cloud_fallback_enabled:
            return (
                BootRoutingDecision.CLOUD_CLAUDE,
                reason,
                f"{context} — falling back to cloud Claude",
            )

        return (
            BootRoutingDecision.DEGRADED,
            FallbackReason.NO_AVAILABLE_PATH,
            f"{context} — no fallback available, entering degraded mode",
        )

    # -- Finalization --------------------------------------------------------

    def finalize(self) -> None:
        """Lock the current decision, ignoring all future signals.

        The decision is computed once at finalization time and returned
        for all subsequent ``decide()`` calls.
        """
        if self._finalized:
            logger.debug("finalize() called but already finalized")
            return

        decision, reason, detail = self._compute_decision()
        self._finalized_decision = (decision, reason)
        self._finalized = True

        logger.info(
            "Policy finalized: %s (reason=%s, detail=%s)",
            decision.value,
            reason.value,
            detail,
        )
