"""ReviewCoordinator — the integration façade for Gap #4 review flow.
=====================================================================

Slice 3 of the **Gap #4 closure arc**.

Root problem
------------

Slices 1 + 2 produced the substrate (``DiffArchive`` for audit trail;
``ReviewBranchManager`` for non-destructive preview branches). They are
correct in isolation but not yet a single integration point — the
orchestrator at ``orchestrator.py:6685`` would have to call both of
them, manage the ``asyncio.Event`` waiter, and handle the timeout +
cancel-check + decision branching. Threading that across a 102K-line
FSM is exactly the kind of orchestrator-edit blast radius the manifesto
warns against.

Slice 3 supplies a **single integration point**: ``coordinate_review``.
The orchestrator calls one method, gets back a structured ``ReviewDecision``,
and routes accordingly. All the cross-substrate plumbing lives here.

Architectural reuse
-------------------

* :class:`DiffArchive` (Slice 1) — archives the diff text + lifecycle
* :class:`ReviewBranchManager` (Slice 2) — creates the local preview branch
* ``asyncio.Event`` per-op for the wait/decision rendezvous
* Master flag :data:`MASTER_FLAG_ENV_VAR` follows the Gap #2 graduation
  pattern (default false during this slice; flipped at Slice 6)

Authority boundary
------------------

* §1 deterministic — pure orchestration; no LLM
* §6 Iron Gate — refuses to apply when DiffArchive / ReviewBranchManager
  signal failure; falls through to the orchestrator's legacy 5s overlay path
* §7 fail-closed — every op has a documented decision path:
  ACCEPTED / REJECTED / EXPIRED / FAILED. Timeout default: auto-REJECT
  (operator must explicitly opt into the legacy auto-apply via
  ``JARVIS_REVIEW_TIMEOUT_S=0``)
* §8 observable — every decision is recordable + projectable

What this module does NOT do
----------------------------

* Edit orchestrator.py — that's a separate, minimal hook (~30 lines)
  in the same slice
* Render anything — Slice 4 wires SSE events; Slice 5 wires VS Code
* Run tests / verify — those are subsequent orchestrator phases
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from backend.core.ouroboros.battle_test.diff_archive import (
    ArchivedDiff,
    DiffArchive,
    DiffOutcome,
    get_default_archive,
)
from backend.core.ouroboros.governance.review_branch_manager import (
    AcceptOutcome,
    CreateOutcome,
    ReviewBranchManager,
)

logger = logging.getLogger("Ouroboros.ReviewCoordinator")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


REVIEW_COORDINATOR_SCHEMA_VERSION: str = "review_coordinator.v1"


MASTER_FLAG_ENV_VAR: str = "JARVIS_REVIEW_BRANCH_ENABLED"
TIMEOUT_ENV_VAR: str = "JARVIS_REVIEW_TIMEOUT_S"


# Default 300s (5 min). Operators set ``=0`` to bypass review entirely
# and restore the legacy auto-apply behavior. Anything > 0 is the
# wall-clock window for operator decision.
_DEFAULT_TIMEOUT_S: float = 300.0


def is_master_flag_enabled() -> bool:
    """Read :data:`MASTER_FLAG_ENV_VAR`. Default false until Slice 6.
    NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def read_timeout_s() -> float:
    """Resolve :data:`TIMEOUT_ENV_VAR`. ``0`` means "skip review entirely
    (legacy auto-apply)"; positive means the wall-clock window before
    auto-EXPIRE. Negative / garbage falls back to the default."""
    raw = os.environ.get(TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S
    if parsed < 0:
        return _DEFAULT_TIMEOUT_S
    return parsed


# ===========================================================================
# Closed taxonomy — coordinator decisions
# ===========================================================================


class ReviewDecision(str, enum.Enum):
    """Closed 5-value coordinator outcome.

    Distinct from :class:`ReviewState` because the coordinator's
    decision is what the orchestrator routes on — it composes the
    branch manager outcome with the timeout/cancel/master-flag state.
    """

    ACCEPTED = "accepted"            # operator accepted; orchestrator proceeds with APPLY
    REJECTED = "rejected"            # operator rejected; orchestrator skips APPLY (CANCELLED)
    EXPIRED = "expired"              # timeout elapsed; default policy = treat as REJECTED
    SKIPPED = "skipped"              # master flag off OR timeout=0; orchestrator uses legacy path
    FAILED = "failed"                # substrate error; orchestrator falls back to legacy path

    @classmethod
    def coerce(cls, raw: object) -> "ReviewDecision":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return cls.FAILED

    @property
    def implies_apply(self) -> bool:
        """``True`` iff the orchestrator should proceed with APPLY.

        SKIPPED implies the legacy auto-apply path; ACCEPTED implies
        explicit operator approval. Both proceed. The other three do not.
        """
        return self in (ReviewDecision.ACCEPTED, ReviewDecision.SKIPPED)


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class CoordinatedReview:
    """One coordination record — the joined view of an archive entry +
    its review branch + the operator's decision."""

    op_id: str
    archive_ref: str
    branch_name: Optional[str]
    decision: ReviewDecision
    elapsed_s: float
    error: str = ""
    schema_version: str = REVIEW_COORDINATOR_SCHEMA_VERSION


# ===========================================================================
# ReviewCoordinator
# ===========================================================================


class ReviewCoordinator:
    """Joins :class:`DiffArchive` + :class:`ReviewBranchManager` + an
    asyncio rendezvous into one integration point for the orchestrator.

    Lifecycle
    ---------

    For each op needing review:

      1. ``coordinate_review(...)`` archives the diff + creates a preview
         branch + returns an awaitable that resolves to a
         :class:`ReviewDecision` once one of:

           * The operator calls :meth:`record_accept(op_id)`
           * The operator calls :meth:`record_reject(op_id)`
           * ``timeout_s`` elapses (timeout watchdog auto-EXPIRES)

      2. The orchestrator routes on the decision. APPLY proceeds for
         ACCEPTED + SKIPPED; CANCELLED otherwise.

      3. After APPLY: ``mark_applied(op_id, outcome)`` updates the
         archive entry (lifecycle outcome).

      4. After VERIFY: ``mark_verified(op_id, outcome)`` updates the
         verify slot (terminal once first set).

    Thread safety
    -------------

    The pending-events dict is guarded by a thread lock; the archive +
    branch manager have their own internal synchronization.
    """

    def __init__(
        self,
        *,
        archive: Optional[DiffArchive] = None,
        branch_manager: Optional[ReviewBranchManager] = None,
        project_root: Optional[Path] = None,
    ) -> None:
        self._archive = archive or get_default_archive()
        if branch_manager is not None:
            self._branch_manager = branch_manager
        elif project_root is not None:
            self._branch_manager = ReviewBranchManager(project_root)
        else:
            # Lazy: caller must supply the manager before
            # coordinate_review can succeed. We keep a None placeholder
            # so the singleton can be constructed before project_root
            # is known (e.g. import time).
            self._branch_manager = None  # type: ignore[assignment]

        # op_id → (asyncio.Event, ReviewDecision once resolved)
        self._pending: Dict[str, Tuple[asyncio.Event, list]] = {}
        # op_id → archive_ref (so mark_applied/verified can find the entry)
        self._op_to_ref: Dict[str, str] = {}
        # op_id → branch_name (for /review listings + decision plumbing)
        self._op_to_branch: Dict[str, str] = {}
        self._lock = threading.RLock()

    # ---- introspection ------------------------------------------------

    @property
    def archive(self) -> DiffArchive:
        return self._archive

    @property
    def branch_manager(self) -> Optional[ReviewBranchManager]:
        return self._branch_manager

    def attach_branch_manager(
        self, manager: ReviewBranchManager,
    ) -> None:
        """Late-bind the branch manager. Used when project_root isn't
        known at construction time."""
        self._branch_manager = manager

    def archive_ref_for_op(self, op_id: object) -> Optional[str]:
        """The DiffArchive ``d-N`` for a coordinated op, or ``None``."""
        if not isinstance(op_id, str):
            return None
        with self._lock:
            return self._op_to_ref.get(op_id)

    def branch_for_op(self, op_id: object) -> Optional[str]:
        """The preview branch name for a coordinated op, or ``None``."""
        if not isinstance(op_id, str):
            return None
        with self._lock:
            return self._op_to_branch.get(op_id)

    # ---- main coordination entry --------------------------------------

    async def coordinate_review(
        self,
        op_id: str,
        files: Sequence[Tuple[str, str]],
        *,
        risk_tier: str,
        diff_text: str = "",
        summary: str = "",
        timeout_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> CoordinatedReview:
        """The single integration point for the orchestrator.

        Workflow:

          1. If master flag off OR ``timeout_s == 0`` → :data:`SKIPPED`
             (caller falls through to legacy auto-apply).
          2. Archive the diff (Slice 1) → record archive ref.
          3. Create the preview branch (Slice 2) → record branch name.
             On failure (BLOCKED / COLLISION / FAILED), return :data:`FAILED`.
          4. Wait for operator decision via per-op
             ``asyncio.Event`` with ``timeout_s`` wall-clock cap.
             ``cancel_check`` is polled every 1s (lets the existing
             ``/cancel`` REPL verb still work).
          5. Map outcome → :class:`ReviewDecision`.

        NEVER raises. All failures degrade to :data:`FAILED` with
        diagnostic context.
        """
        started = time.monotonic()

        if not is_master_flag_enabled():
            return CoordinatedReview(
                op_id=op_id, archive_ref="", branch_name=None,
                decision=ReviewDecision.SKIPPED,
                elapsed_s=0.0,
                error="master flag off",
            )

        eff_timeout = (
            float(timeout_s) if timeout_s is not None else read_timeout_s()
        )
        if eff_timeout == 0:
            return CoordinatedReview(
                op_id=op_id, archive_ref="", branch_name=None,
                decision=ReviewDecision.SKIPPED,
                elapsed_s=0.0,
                error="timeout=0 (legacy auto-apply opt-in)",
            )

        if self._branch_manager is None:
            return CoordinatedReview(
                op_id=op_id, archive_ref="", branch_name=None,
                decision=ReviewDecision.FAILED,
                elapsed_s=0.0,
                error="branch_manager not attached",
            )

        # --- Step 2: archive the diff
        try:
            paths = tuple(p for p, _ in files)
            archived = self._archive.add(
                op_id=op_id,
                risk_tier=risk_tier,
                file_paths=paths,
                diff_text=diff_text,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ReviewCoordinator] archive.add failed for op=%s: %s",
                op_id, exc, exc_info=True,
            )
            return CoordinatedReview(
                op_id=op_id, archive_ref="", branch_name=None,
                decision=ReviewDecision.FAILED,
                elapsed_s=time.monotonic() - started,
                error=f"archive failed: {exc}",
            )

        with self._lock:
            self._op_to_ref[op_id] = archived.ref

        # --- Step 3: create preview branch
        try:
            create_result = await self._branch_manager.create(
                op_id, list(files),
                risk_tier=risk_tier,
                diff_archive_ref=archived.ref,
            )
        except Exception as exc:  # noqa: BLE001
            return CoordinatedReview(
                op_id=op_id, archive_ref=archived.ref, branch_name=None,
                decision=ReviewDecision.FAILED,
                elapsed_s=time.monotonic() - started,
                error=f"branch create raised: {exc}",
            )

        if create_result.outcome is not CreateOutcome.CREATED:
            # Mark archive as failed apply for visibility.
            self._archive.mark_applied(
                archived.ref, DiffOutcome.FAILED,
                error=f"branch create {create_result.outcome.value}: "
                      f"{create_result.error}",
            )
            return CoordinatedReview(
                op_id=op_id, archive_ref=archived.ref, branch_name=None,
                decision=ReviewDecision.FAILED,
                elapsed_s=time.monotonic() - started,
                error=create_result.error,
            )

        branch_name = create_result.branch.branch_name
        with self._lock:
            self._op_to_branch[op_id] = branch_name
        # Stamp the branch onto the archive for /diff listings.
        self._archive.attach_review_branch(archived.ref, branch_name)

        # --- Step 4: register an event + wait for decision/timeout
        event = asyncio.Event()
        result_box: list = []  # one-element box: [ReviewDecision]
        with self._lock:
            self._pending[op_id] = (event, result_box)

        try:
            decision = await self._wait_with_cancel(
                event, eff_timeout, cancel_check,
            )
        finally:
            with self._lock:
                self._pending.pop(op_id, None)

        # If no decision was recorded but we exited the wait, it's a
        # timeout-equivalent (cancel_check fired or timeout elapsed
        # without explicit accept/reject).
        if decision is None:
            decision = ReviewDecision.EXPIRED

        # --- Step 5: act on decision via the branch manager
        await self._apply_decision(op_id, decision)

        return CoordinatedReview(
            op_id=op_id,
            archive_ref=archived.ref,
            branch_name=branch_name,
            decision=decision,
            elapsed_s=time.monotonic() - started,
        )

    # ---- decision recording (called by REPL/HTTP/SSE handlers) --------

    def record_accept(self, op_id: object) -> bool:
        """Operator accepted via REPL or VS Code button. Returns
        ``True`` if a pending review was found + signalled."""
        return self._record_decision(op_id, ReviewDecision.ACCEPTED)

    def record_reject(self, op_id: object) -> bool:
        """Operator rejected. Returns ``True`` if signalled."""
        return self._record_decision(op_id, ReviewDecision.REJECTED)

    def _record_decision(
        self, op_id: object, decision: ReviewDecision,
    ) -> bool:
        if not isinstance(op_id, str):
            return False
        with self._lock:
            entry = self._pending.get(op_id)
            if entry is None:
                return False
            event, box = entry
            box.append(decision)
        event.set()
        return True

    # ---- post-decision lifecycle hooks (called by orchestrator) -------

    def mark_applied(
        self, op_id: object, outcome: object, *, error: str = "",
    ) -> Optional[ArchivedDiff]:
        """Stamp the APPLY outcome on the archive entry. Looks up the
        archive ref via the op→ref map populated at coordinate-time."""
        if not isinstance(op_id, str):
            return None
        with self._lock:
            ref = self._op_to_ref.get(op_id)
        if ref is None:
            return None
        return self._archive.mark_applied(ref, outcome, error=error)

    def mark_verified(
        self, op_id: object, outcome: object,
    ) -> Optional[ArchivedDiff]:
        """Stamp the VERIFY outcome on the archive entry."""
        if not isinstance(op_id, str):
            return None
        with self._lock:
            ref = self._op_to_ref.get(op_id)
        if ref is None:
            return None
        return self._archive.mark_verified(ref, outcome)

    # ---- internals -----------------------------------------------------

    async def _wait_with_cancel(
        self,
        event: asyncio.Event,
        timeout_s: float,
        cancel_check: Optional[Callable[[], bool]],
    ) -> Optional[ReviewDecision]:
        """Wait for either the decision event or timeout. Polls
        ``cancel_check`` every 1s — when it returns True, treat as
        a synthetic REJECTED (operator hit /cancel)."""
        deadline = time.monotonic() + timeout_s
        poll_s = 1.0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None  # timeout
            wait_for = min(poll_s, remaining)
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_for)
                # Event fired — read the decision from the result box
                # (set by _record_decision).
                op_id = self._find_op_id_for_event(event)
                if op_id is None:
                    return None
                with self._lock:
                    entry = self._pending.get(op_id)
                    if entry is not None:
                        _, box = entry
                        if box:
                            return box[-1]
                return None
            except asyncio.TimeoutError:
                # Poll for cancel.
                if cancel_check is not None:
                    try:
                        if cancel_check():
                            return ReviewDecision.REJECTED
                    except Exception:  # noqa: BLE001
                        pass
                # Loop back to check remaining timeout.

    def _find_op_id_for_event(
        self, event: asyncio.Event,
    ) -> Optional[str]:
        """Reverse-lookup the op_id whose event was just set. Used
        only inside :meth:`_wait_with_cancel` to fetch the result."""
        with self._lock:
            for op_id, (e, _box) in self._pending.items():
                if e is event:
                    return op_id
        return None

    async def _apply_decision(
        self, op_id: str, decision: ReviewDecision,
    ) -> None:
        """Drive the branch manager to the terminal state matching
        ``decision``. Best-effort: failures here are logged but don't
        re-raise — the orchestrator already has its decision."""
        if self._branch_manager is None:
            return
        try:
            if decision is ReviewDecision.ACCEPTED:
                result = await self._branch_manager.accept(op_id)
                if result.outcome is not AcceptOutcome.ACCEPTED:
                    logger.debug(
                        "[ReviewCoordinator] accept failed: %s",
                        result.error,
                    )
            elif decision is ReviewDecision.REJECTED:
                await self._branch_manager.reject(
                    op_id, reason="operator rejected",
                )
            elif decision is ReviewDecision.EXPIRED:
                await self._branch_manager.expire(op_id)
            # SKIPPED / FAILED — branch never created, nothing to do
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ReviewCoordinator] _apply_decision raised for op=%s, "
                "decision=%s",
                op_id, decision.value, exc_info=True,
            )


# ===========================================================================
# Module singleton
# ===========================================================================


_default_coordinator: Optional[ReviewCoordinator] = None
_singleton_lock = threading.Lock()


def get_default_coordinator() -> ReviewCoordinator:
    """Return the process-wide coordinator. Constructed lazily; the
    branch manager is attached separately via
    :meth:`ReviewCoordinator.attach_branch_manager` once the
    orchestrator knows the project root."""
    global _default_coordinator
    with _singleton_lock:
        if _default_coordinator is None:
            _default_coordinator = ReviewCoordinator()
        return _default_coordinator


def reset_default_coordinator_for_tests() -> None:
    global _default_coordinator
    with _singleton_lock:
        _default_coordinator = None


__all__ = [
    "CoordinatedReview",
    "MASTER_FLAG_ENV_VAR",
    "REVIEW_COORDINATOR_SCHEMA_VERSION",
    "ReviewCoordinator",
    "ReviewDecision",
    "TIMEOUT_ENV_VAR",
    "get_default_coordinator",
    "is_master_flag_enabled",
    "read_timeout_s",
    "reset_default_coordinator_for_tests",
]
