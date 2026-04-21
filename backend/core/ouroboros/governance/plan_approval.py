"""PlanApproval — operator-visible plan-first modality.

Surfaces the existing PLAN phase (``plan_generator.py``) as a
distinct operator mode. When active, every operation halts after
PLAN and requires **human approval** before the orchestrator
proceeds to GENERATE. Rejection short-circuits the op into
POSTMORTEM with a ``plan_rejected`` reason threaded back to
ConversationBridge so future PLAN attempts see why.

Companion to ``plan_mode.py`` — that module is a deterministic
dry-run simulator that estimates what the pipeline WOULD do
without running it. This module is about halting the pipeline
after a real PLAN has been produced and waiting for a human
approve/reject decision. The two are complementary.

## Authority posture (locked by authorization)

- **Human approval is required.** The module exposes
  :meth:`approve` and :meth:`reject` — callable only from
  operator surfaces (SerpentFlow REPL, IDE approval endpoint in a
  later slice). The orchestrator itself never calls approve().
  Manifesto §1 Boundary Principle.
- **Deny-by-default.** ``JARVIS_PLAN_APPROVAL_MODE`` defaults
  ``false``; while Slices 1-4 ship, the PLAN phase runs as-is and
  GENERATE proceeds without pause. Slice 5 graduates the default.
- **Single source of truth.** Every pending plan lives in this
  module's per-op registry. The orchestrator asks
  :func:`needs_approval` once per op; if true, it awaits a Future
  resolved only via approve()/reject().
- **Bounded timeouts.** Pending approvals expire after
  ``JARVIS_PLAN_APPROVAL_TIMEOUT_S`` seconds (default 600 = 10 min).
  Expired plans auto-reject with reason ``plan_expired`` — the op
  does not hang forever.
- **§8 audit.** Every state transition emits an INFO log line
  before the method returns; operators grep ``[PlanApproval]``
  for the audit trail.

## State machine

    pending ──approve()──> approved  → orchestrator continues
       │
       ├───reject()─────> rejected  → POSTMORTEM
       │
       └───timeout──────> expired   → POSTMORTEM (treated as reject)

Terminal states are sticky. approve() on a rejected plan raises.

## Why not a hook in orchestrator.py?

Two reasons: (1) testability — the primitive is a pure Python
class tested in isolation, same pattern as TaskBoard /
StreamEventBroker. (2) operator-tooling boundary — the REPL,
IDE, and future webhook endpoints all dispatch into this single
module. One registry, one audit log, one lock.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional


logger = logging.getLogger(__name__)


# --- Env knobs -------------------------------------------------------------


def plan_approval_mode_enabled() -> bool:
    """Master switch. Default ``false`` until Slice 5 graduation."""
    return os.environ.get(
        "JARVIS_PLAN_APPROVAL_MODE", "false",
    ).strip().lower() == "true"


def _default_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "JARVIS_PLAN_APPROVAL_TIMEOUT_S", "600",
        )))
    except (TypeError, ValueError):
        return 600.0


def _max_pending_plans() -> int:
    """Upper bound on simultaneous pending plans. Prevents the
    registry from growing without bound if operators disappear."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_PLAN_APPROVAL_MAX_PENDING", "32",
        )))
    except (TypeError, ValueError):
        return 32


def _reason_max_len() -> int:
    """Cap on rejection-reason length. Keeps log + POSTMORTEM
    propagation bounded and prevents an operator from stuffing a
    giant payload through the reject path."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_PLAN_APPROVAL_REASON_MAX_LEN", "2000",
        )))
    except (TypeError, ValueError):
        return 2000


# --- States + outcome ------------------------------------------------------


STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"
STATE_EXPIRED = "expired"

_TERMINAL_STATES = frozenset({STATE_APPROVED, STATE_REJECTED, STATE_EXPIRED})


@dataclass(frozen=True)
class PlanApprovalOutcome:
    """Result of :meth:`PlanApprovalController.await_approval`.

    Callers branch on ``approved`` — when ``True`` the orchestrator
    proceeds to GENERATE; when ``False`` it routes to POSTMORTEM
    with ``reason`` threaded through.
    """

    approved: bool
    state: str  # STATE_APPROVED / STATE_REJECTED / STATE_EXPIRED
    reason: str = ""
    reviewer: str = ""  # "repl" / "ide" / "auto-timeout"
    elapsed_s: float = 0.0


# --- Exceptions ------------------------------------------------------------


class PlanApprovalError(Exception):
    """Base for PlanApproval errors."""


class PlanApprovalStateError(PlanApprovalError):
    """Illegal state transition."""


class PlanApprovalCapacityError(PlanApprovalError):
    """Too many pending plans."""


# --- Pending-plan record ---------------------------------------------------


@dataclass
class _Pending:
    """Internal — one pending plan awaiting operator action."""

    op_id: str
    plan: Mapping[str, Any]
    created_ts: float
    timeout_s: float
    future: "asyncio.Future[PlanApprovalOutcome]"
    state: str = STATE_PENDING
    reviewer: str = ""
    reason: str = ""
    _timeout_task: Optional["asyncio.Task[None]"] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


# --- Controller ------------------------------------------------------------


class PlanApprovalController:
    """Per-process registry of pending plans.

    Thread-safe: the registry uses a lock for read/write. Approval
    futures are resolved via ``loop.call_soon_threadsafe`` so
    approve/reject from a different thread still works cleanly.
    """

    def __init__(
        self,
        *,
        max_pending: Optional[int] = None,
        default_timeout_s: Optional[float] = None,
    ) -> None:
        self._max_pending = max_pending or _max_pending_plans()
        self._default_timeout_s = default_timeout_s or _default_timeout_s()
        self._pending: Dict[str, _Pending] = {}
        self._history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # --- introspection --------------------------------------------------

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for p in self._pending.values()
                if p.state == STATE_PENDING
            )

    def pending_op_ids(self) -> List[str]:
        with self._lock:
            return [
                p.op_id for p in self._pending.values()
                if p.state == STATE_PENDING
            ]

    def snapshot(self, op_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            p = self._pending.get(op_id)
            return None if p is None else self._project(p)

    def snapshot_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._project(p) for p in self._pending.values()]

    @staticmethod
    def _project(p: _Pending) -> Dict[str, Any]:
        return {
            "op_id": p.op_id,
            "state": p.state,
            "created_ts": p.created_ts,
            "timeout_s": p.timeout_s,
            "expires_ts": p.created_ts + p.timeout_s,
            "reviewer": p.reviewer,
            "reason": p.reason,
            "plan": dict(p.plan),
        }

    # --- listener hooks -------------------------------------------------

    def on_transition(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Register a transition hook. Listener sees payloads of
        shape ``{"event_type": "plan_pending"|"plan_approved"|...,
        "projection": {op_id, state, ...}}``. Used by the IDE
        stream (Slice 4) to emit SSE frames. Never raises."""
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return _unsub

    def _fire(self, event_type: str, pending: _Pending) -> None:
        payload = {
            "event_type": event_type,
            "projection": self._project(pending),
        }
        for l in list(self._listeners):
            try:
                l(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[PlanApproval] listener exception: %s", exc)

    # --- request --------------------------------------------------------

    def request_approval(
        self,
        op_id: str,
        plan: Mapping[str, Any],
        *,
        timeout_s: Optional[float] = None,
    ) -> "asyncio.Future[PlanApprovalOutcome]":
        """Register a pending plan and return a Future.

        Raises:
          PlanApprovalStateError on empty op_id or duplicate.
          PlanApprovalCapacityError when registry is full.
        """
        if not isinstance(op_id, str) or not op_id:
            raise PlanApprovalStateError(
                "op_id must be a non-empty string",
            )
        loop = asyncio.get_event_loop()
        future: "asyncio.Future[PlanApprovalOutcome]" = loop.create_future()
        effective_timeout = (
            timeout_s if timeout_s is not None and timeout_s > 0
            else self._default_timeout_s
        )

        with self._lock:
            existing = self._pending.get(op_id)
            if existing is not None and not existing.is_terminal:
                raise PlanApprovalStateError(
                    "pending plan already exists for op_id=" + op_id,
                )
            # Count pending (non-terminal) against cap.
            non_terminal = sum(
                1 for p in self._pending.values() if not p.is_terminal
            )
            if non_terminal >= self._max_pending:
                raise PlanApprovalCapacityError(
                    "plan-approval registry at capacity ("
                    + str(self._max_pending) + "); reject older plans first",
                )
            pending = _Pending(
                op_id=op_id,
                plan=dict(plan),
                created_ts=time.monotonic(),
                timeout_s=effective_timeout,
                future=future,
            )
            self._pending[op_id] = pending

        # Timeout scheduled OUTSIDE the lock.
        pending._timeout_task = loop.create_task(
            self._run_timeout(op_id, effective_timeout),
            name="plan-approval-timeout-" + op_id,
        )

        logger.info(
            "[PlanApproval] plan_pending op=%s timeout_s=%.1f pending_total=%d",
            op_id, effective_timeout, self.pending_count,
        )
        self._fire("plan_pending", pending)
        return future

    async def _run_timeout(self, op_id: str, timeout_s: float) -> None:
        try:
            await asyncio.sleep(timeout_s)
        except asyncio.CancelledError:
            return
        with self._lock:
            p = self._pending.get(op_id)
            if p is None or p.is_terminal:
                return
        try:
            self._resolve(
                op_id, state=STATE_EXPIRED, reviewer="auto-timeout",
                reason="plan_expired after " + str(int(timeout_s)) + "s",
            )
        except PlanApprovalStateError:
            # Raced with an approve/reject — fine, the future is
            # already resolved.
            pass

    # --- approve / reject ------------------------------------------------

    def approve(
        self, op_id: str, *, reviewer: str = "",
    ) -> PlanApprovalOutcome:
        return self._resolve(
            op_id, state=STATE_APPROVED,
            reviewer=reviewer or "unknown", reason="",
        )

    def reject(
        self, op_id: str, *, reason: str = "", reviewer: str = "",
    ) -> PlanApprovalOutcome:
        r = reason.strip() if isinstance(reason, str) else ""
        if not r:
            r = "(no reason)"
        max_len = _reason_max_len()
        if len(r) > max_len:
            r = r[:max_len] + "...<truncated>"
        return self._resolve(
            op_id, state=STATE_REJECTED,
            reviewer=reviewer or "unknown", reason=r,
        )

    def _resolve(
        self, op_id: str, *, state: str, reviewer: str, reason: str,
    ) -> PlanApprovalOutcome:
        with self._lock:
            p = self._pending.get(op_id)
            if p is None:
                raise PlanApprovalStateError(
                    "no pending plan for op_id=" + op_id,
                )
            if p.is_terminal:
                raise PlanApprovalStateError(
                    "plan for op_id=" + op_id
                    + " already in terminal state " + p.state,
                )
            elapsed = time.monotonic() - p.created_ts
            p.state = state
            p.reviewer = reviewer
            p.reason = reason
            outcome = PlanApprovalOutcome(
                approved=(state == STATE_APPROVED),
                state=state, reason=reason, reviewer=reviewer,
                elapsed_s=elapsed,
            )
            t = p._timeout_task
            if t is not None and not t.done():
                try:
                    t.cancel()
                except RuntimeError:
                    # Loop already closed — the timeout task is
                    # effectively dead; nothing further to do.
                    pass
            self._history.append({
                "op_id": op_id, "state": state, "reviewer": reviewer,
                "reason": reason, "elapsed_s": elapsed,
                "resolved_ts": time.monotonic(),
            })
            future_to_resolve = p.future

        event_type = (
            "plan_approved" if state == STATE_APPROVED
            else "plan_rejected" if state == STATE_REJECTED
            else "plan_expired"
        )
        logger.info(
            "[PlanApproval] %s op=%s reviewer=%s elapsed_s=%.1f reason=%.200s",
            event_type, op_id, reviewer, elapsed, reason or "",
        )
        if not future_to_resolve.done():
            # Schedule on the future's loop — safe from any thread.
            # If that loop has already closed (typical in test
            # tear-down where the caller drops an event loop and
            # then resolves plans from sync code), fall back to a
            # direct set_result — the future won't await anything
            # anyway because its loop is dead.
            _loop = future_to_resolve.get_loop()
            try:
                _loop.call_soon_threadsafe(
                    future_to_resolve.set_result, outcome,
                )
            except RuntimeError:
                try:
                    future_to_resolve.set_result(outcome)
                except Exception:  # noqa: BLE001
                    pass
        self._fire(event_type, p)
        return outcome

    # --- cleanup --------------------------------------------------------

    def evict_terminal(self, op_id: str) -> bool:
        """Remove a terminal record. Idempotent. Returns True if
        something was evicted."""
        with self._lock:
            p = self._pending.get(op_id)
            if p is None or not p.is_terminal:
                return False
            del self._pending[op_id]
        logger.debug("[PlanApproval] evicted terminal op=%s", op_id)
        return True

    def reset(self) -> None:
        """Test helper. Never called from production."""
        with self._lock:
            for p in self._pending.values():
                if not p.future.done():
                    try:
                        p.future.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                t = p._timeout_task
                if t is not None and not t.done():
                    try:
                        t.cancel()
                    except RuntimeError:
                        # Loop already closed (test-tearndown race).
                        # The task will be cleaned up with the loop.
                        pass
            self._pending.clear()
            self._history.clear()
            self._listeners.clear()

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    async def await_outcome(
        self, op_id: str, timeout_s: Optional[float] = None,
    ) -> PlanApprovalOutcome:
        """Await the terminal outcome for ``op_id``. Returns the
        same :class:`PlanApprovalOutcome` that `approve()` /
        `reject()` / timeout produce.

        Raises :class:`PlanApprovalStateError` on unknown op_id.
        ``timeout_s`` is an upper-bound wall-clock timeout — if the
        controller's own timeout is longer, the returned outcome
        has ``state=STATE_EXPIRED`` with reason ``await_timeout``.
        """
        with self._lock:
            p = self._pending.get(op_id)
            if p is None:
                raise PlanApprovalStateError(
                    "no pending plan for op_id=" + op_id,
                )
            future = p.future
        if timeout_s is None:
            return await future
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            return PlanApprovalOutcome(
                approved=False,
                state=STATE_EXPIRED,
                reason="await_timeout",
                reviewer="await_outcome",
                elapsed_s=float(timeout_s),
            )


# --- Module singleton ------------------------------------------------------


_default: Optional[PlanApprovalController] = None
_default_lock = threading.Lock()


def get_default_controller() -> PlanApprovalController:
    global _default
    with _default_lock:
        if _default is None:
            _default = PlanApprovalController()
        return _default


def reset_default_controller() -> None:
    """Test-only reset."""
    global _default
    with _default_lock:
        if _default is not None:
            _default.reset()
        _default = None


# --- Orchestrator entry points --------------------------------------------


def needs_approval(ctx: Any = None) -> bool:
    """Orchestrator checks this after PLAN phase.

    If the env flag is off → returns False (plan continues straight
    to GENERATE, matching pre-graduation behavior).

    When ``ctx`` is passed, a ``ctx.plan_approval_override``
    attribute force-toggles plan approval for this op:
      - ``False`` → skip approval even if env flag is on
      - ``True``  → require approval even if env flag is off
      - ``None`` / absent → defer to env flag
    """
    if ctx is not None:
        override = getattr(ctx, "plan_approval_override", None)
        if override is False:
            return False
        if override is True:
            return True
    return plan_approval_mode_enabled()


# Back-compat alias for callers that want the older name.
plan_approval_enabled = plan_approval_mode_enabled


async def await_approval(
    op_id: str,
    plan: Mapping[str, Any],
    *,
    timeout_s: Optional[float] = None,
) -> PlanApprovalOutcome:
    """Register a plan + await approval via the default controller."""
    controller = get_default_controller()
    future = controller.request_approval(op_id, plan, timeout_s=timeout_s)
    return await future


def should_force_plan_review(ctx: Any = None) -> bool:
    """Orchestrator hook: when plan-approval-mode is on, force the
    existing plan-approval hard gate to engage on **every** op
    regardless of the complexity heuristic.

    Equivalent to :func:`needs_approval` but named differently to
    match the orchestrator-side semantic: "is this plan-mode ON?"
    The orchestrator OR's this into its ``_should_gate`` predicate so
    the existing per-complexity wiring stays intact; plan mode
    simply widens the coverage.
    """
    return needs_approval(ctx)


# --- ApprovalProvider adapter ---------------------------------------------


# Late import: the adapter translates between the approval_provider
# protocol (ApprovalStatus / ApprovalResult with datetime + approver
# fields) and this module's PlanApprovalController. Late import also
# avoids a cycle if approval_provider ever pulls something in that
# eventually imports plan_approval (it doesn't today, but future
# refactors are cheap to break without discipline).


class PlanApprovalProviderAdapter:
    """Adapter that implements a subset of the ``ApprovalProvider``
    protocol by delegating to :class:`PlanApprovalController`.

    Methods mirrored:
      * ``request_plan(context, plan_text)`` — registers a pending
        plan keyed on ``context.op_id``. Returns the
        ``<op_id>::plan`` request id used by the existing orchestrator
        wiring. ``plan_text`` is stored as the plan payload's
        ``markdown`` field so REPL/IDE renderers have a human-
        readable view.
      * ``approve(request_id, approver)`` — resolves the matching
        plan with APPROVED.
      * ``reject(request_id, approver, reason)`` — resolves with
        REJECTED.
      * ``await_decision(request_id, timeout_s)`` — awaits the
        controller's Future and maps the outcome into
        :class:`ApprovalResult`.

    The adapter does NOT own any state — everything lives in the
    controller. Instantiating multiple adapters against the same
    controller is safe.

    NOT implemented (intentionally):
      * ``request(context)`` (code-approval path — unrelated to
        plan approval; callers that need it still use the
        existing ``InMemoryApprovalProvider``).
      * ``elicit(...)`` — scope for a later slice.
    """

    _PLAN_SUFFIX = "::plan"

    def __init__(
        self,
        controller: Optional["PlanApprovalController"] = None,
    ) -> None:
        self._controller = controller or get_default_controller()

    @classmethod
    def _strip_suffix(cls, request_id: str) -> str:
        if request_id.endswith(cls._PLAN_SUFFIX):
            return request_id[: -len(cls._PLAN_SUFFIX)]
        return request_id

    @staticmethod
    def _format_request_id(op_id: str) -> str:
        return op_id + PlanApprovalProviderAdapter._PLAN_SUFFIX

    @staticmethod
    def is_plan_request(request_id: str) -> bool:
        return request_id.endswith(
            PlanApprovalProviderAdapter._PLAN_SUFFIX,
        )

    async def request_plan(self, context: Any, plan_text: str) -> str:
        """Submit an op's plan for approval. Idempotent on op_id."""
        op_id = getattr(context, "op_id", None)
        if not isinstance(op_id, str) or not op_id:
            raise PlanApprovalStateError(
                "context.op_id must be a non-empty string",
            )
        # Idempotency: if a pending record already exists, return the
        # existing request id without re-registering.
        if self._controller.snapshot(op_id) is not None:
            return self._format_request_id(op_id)
        plan_payload: Dict[str, Any] = {
            "markdown": plan_text,
            "description": getattr(context, "description", ""),
            "target_files": list(
                getattr(context, "target_files", []) or [],
            ),
        }
        self._controller.request_approval(op_id, plan_payload)
        return self._format_request_id(op_id)

    async def approve(self, request_id: str, approver: str) -> Any:
        """Resolve the matching plan with APPROVED. Returns an
        ApprovalResult-compatible object (lazily imported)."""
        from backend.core.ouroboros.governance.approval_provider import (
            ApprovalResult,
            ApprovalStatus,
        )
        from datetime import datetime, timezone

        op_id = self._strip_suffix(request_id)
        self._controller.approve(op_id, reviewer=approver)
        return ApprovalResult(
            status=ApprovalStatus.APPROVED,
            approver=approver,
            reason=None,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )

    async def reject(
        self, request_id: str, approver: str, reason: str,
    ) -> Any:
        from backend.core.ouroboros.governance.approval_provider import (
            ApprovalResult,
            ApprovalStatus,
        )
        from datetime import datetime, timezone

        op_id = self._strip_suffix(request_id)
        outcome = self._controller.reject(
            op_id, reason=reason, reviewer=approver,
        )
        return ApprovalResult(
            status=ApprovalStatus.REJECTED,
            approver=approver,
            reason=outcome.reason,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )

    async def await_decision(
        self, request_id: str, timeout_s: float,
    ) -> Any:
        """Block until a decision is made. Maps the controller's
        :class:`PlanApprovalOutcome` to :class:`ApprovalResult`.

        On timeout: the controller's own timeout task auto-rejects
        with ``plan_expired`` — this method observes that via the
        shared Future and maps to ``ApprovalStatus.EXPIRED``.
        """
        from backend.core.ouroboros.governance.approval_provider import (
            ApprovalResult,
            ApprovalStatus,
        )
        from datetime import datetime, timezone

        op_id = self._strip_suffix(request_id)
        try:
            outcome = await self._controller.await_outcome(
                op_id, timeout_s=timeout_s,
            )
        except PlanApprovalStateError:
            return ApprovalResult(
                status=ApprovalStatus.EXPIRED,
                approver=None, reason="unknown_request_id",
                decided_at=datetime.now(tz=timezone.utc),
                request_id=request_id,
            )
        status_map = {
            STATE_APPROVED: ApprovalStatus.APPROVED,
            STATE_REJECTED: ApprovalStatus.REJECTED,
            STATE_EXPIRED: ApprovalStatus.EXPIRED,
        }
        mapped = status_map.get(outcome.state, ApprovalStatus.EXPIRED)
        return ApprovalResult(
            status=mapped,
            approver=outcome.reviewer or None,
            reason=outcome.reason or None,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )
