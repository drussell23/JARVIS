"""P3 Slice 1 — Lightweight inline approval primitive (Phase 3 P3).

Per OUROBOROS_VENOM_PRD.md §9 Phase 3 P3:

  > Yellow/Orange-tier approval today = create a PR + review. That's
  > heavy for fast iterations.
  >
  > Solution: SerpentFlow inline approval mode for development:
  >   - Show full diff in terminal with hunks
  >   - Prompt: ``[y]es / [n]o / [s]how stack / [e]dit / [w]ait``
  >     with 30s default timeout
  >   - On ``y``: apply (same path as auto-apply for SAFE_AUTO)
  >   - On ``e``: open in $EDITOR, then re-prompt
  >   - Keep existing PR path for production work (operator setting
  >     decides)

This slice ships the **pure-data primitive** + **decision parser** +
**bounded FIFO queue**. Slice 2 wires the ``ApprovalProvider`` Protocol
implementation; Slice 3 adds SerpentFlow rendering + ``$EDITOR``
shell-out; Slice 4 graduates the env-knob default flip.

Default-off behind ``JARVIS_APPROVAL_UX_INLINE_ENABLED`` until Slice 4.
When off, no inline-approval surface is exposed; existing
``CLIApprovalProvider`` / ``OrangePRReviewer`` paths remain
authoritative.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * No subprocess, no file I/O, no env mutation. The 30s prompt I/O
    is Slice 3's job; this primitive is pure data + thread-safe queue.
  * Best-effort — malformed inputs return ``WAIT`` (safest default;
    never auto-approves a bad parse).
  * Bounded — FIFO queue capped at ``MAX_QUEUED_REQUESTS`` so a runaway
    op-flood can't accumulate unbounded approval state.
"""
from __future__ import annotations

import enum
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


_TRUTHY = ("1", "true", "yes", "on")

# Default decision timeout per PRD spec — 30 seconds. On timeout, the
# request defers to the queue (NOT auto-approves; safety-first).
DEFAULT_DECISION_TIMEOUT_S: float = 30.0

# FIFO queue cap. Multiple concurrent ops needing approval get queued;
# this cap bounds how many can pile up before new requests start
# returning ``QUEUE_FULL`` to the caller. Defensive against the
# "operator AFK + sensor flood" failure mode.
MAX_QUEUED_REQUESTS: int = 16

# Risk tiers eligible for IMMEDIATE-priority queue promotion.
# Per PRD: "single queue, FIFO with priority for IMMEDIATE".
_IMMEDIATE_PRIORITY_RISK_TIERS = frozenset({"IMMEDIATE", "BLOCKED"})


def is_enabled() -> bool:
    """Master flag — ``JARVIS_APPROVAL_UX_INLINE_ENABLED`` (default **true**
    post Slice 4 graduation).

    Slices 1–3 shipped default-off (primitive + provider + renderer all
    dormant). Slice 4 flipped the default after layered evidence:
    cross-slice authority pins + in-process live-fire smoke +
    factory-reachability supplement + dual-tier hot-revert matrix.

    When off, the build_approval_provider() factory returns the legacy
    ``CLIApprovalProvider`` — the queue + audit ledger remain
    inspectable but no inline prompt surface is wired."""
    return os.environ.get(
        "JARVIS_APPROVAL_UX_INLINE_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def decision_timeout_s() -> float:
    """Per-request decision timeout in seconds.

    Default 30.0 per PRD spec. Negative values clamp to 1.0 (never
    auto-defer instantly — give the operator at least a frame to react).
    Invalid values fall back to default."""
    raw = os.environ.get("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S")
    if raw is None:
        return DEFAULT_DECISION_TIMEOUT_S
    try:
        v = float(raw)
        return max(1.0, v)
    except (TypeError, ValueError):
        return DEFAULT_DECISION_TIMEOUT_S


# ---------------------------------------------------------------------------
# Decision enum + parser
# ---------------------------------------------------------------------------


class InlineApprovalChoice(str, enum.Enum):
    """Operator's response to an inline approval prompt."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SHOW_STACK = "SHOW_STACK"
    EDIT = "EDIT"
    WAIT = "WAIT"
    TIMEOUT_DEFERRED = "TIMEOUT_DEFERRED"


# Single-char + verbose form mapping. Matches PRD prompt:
#   [y]es / [n]o / [s]how stack / [e]dit / [w]ait
#
# Whitespace + case ignored. Verbose forms accept any prefix that
# uniquely matches one of the accepted words.
_CHOICE_TOKENS: Dict[str, InlineApprovalChoice] = {
    # Single-char (preferred; matches the prompt's `[x]` brackets)
    "y": InlineApprovalChoice.APPROVE,
    "n": InlineApprovalChoice.REJECT,
    "s": InlineApprovalChoice.SHOW_STACK,
    "e": InlineApprovalChoice.EDIT,
    "w": InlineApprovalChoice.WAIT,
    # Common verbose synonyms
    "yes": InlineApprovalChoice.APPROVE,
    "no": InlineApprovalChoice.REJECT,
    "approve": InlineApprovalChoice.APPROVE,
    "reject": InlineApprovalChoice.REJECT,
    "show": InlineApprovalChoice.SHOW_STACK,
    "stack": InlineApprovalChoice.SHOW_STACK,
    "edit": InlineApprovalChoice.EDIT,
    "wait": InlineApprovalChoice.WAIT,
    "defer": InlineApprovalChoice.WAIT,
}


def parse_decision_input(text: str) -> InlineApprovalChoice:
    """Parse operator input into one of the five live choices.

    Returns ``WAIT`` on:
      * empty / whitespace-only input
      * unknown token
      * ambiguous input (multiple tokens — caller should re-prompt)

    Safety-first contract: never returns ``APPROVE`` from ambiguous
    input. ``WAIT`` is the safest default — it queues the request for
    the operator to revisit. Pinned by tests."""
    if not text or not text.strip():
        return InlineApprovalChoice.WAIT
    cleaned = text.strip().lower()
    # Strip surrounding whitespace + take first token only (defends
    # against accidental "y\nsomething else").
    first_token = re.split(r"\s+", cleaned)[0]
    if not first_token:
        return InlineApprovalChoice.WAIT
    return _CHOICE_TOKENS.get(first_token, InlineApprovalChoice.WAIT)


# ---------------------------------------------------------------------------
# Request + decision dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InlineApprovalRequest:
    """One pending inline approval — written by the orchestrator
    (Slice 2 ApprovalProvider impl) when an op needs human ack."""

    request_id: str
    op_id: str
    risk_tier: str
    target_files: Tuple[str, ...]
    diff_summary: str
    created_unix: float
    deadline_unix: float

    def is_immediate_priority(self) -> bool:
        """True when the op should jump the FIFO queue."""
        return self.risk_tier in _IMMEDIATE_PRIORITY_RISK_TIERS

    def seconds_remaining(self, now_unix: Optional[float] = None) -> float:
        """Wall-clock seconds until the request times out. Negative
        values mean past-deadline (caller should auto-defer)."""
        now = now_unix if now_unix is not None else time.time()
        return max(-1.0, self.deadline_unix - now)


@dataclass(frozen=True)
class InlineApprovalDecision:
    """One operator decision recorded by the queue."""

    request_id: str
    choice: InlineApprovalChoice
    reason: str
    decided_unix: float
    operator: str = "operator"


# ---------------------------------------------------------------------------
# Bounded FIFO queue (with IMMEDIATE-priority promotion)
# ---------------------------------------------------------------------------


@dataclass
class _QueueEntry:
    """Internal mutable container — one queued request + its
    realized decision (None until decided)."""

    request: InlineApprovalRequest
    decision: Optional[InlineApprovalDecision] = None


class InlineApprovalQueue:
    """Process-wide FIFO queue of pending inline approval requests.

    Thread-safe via a single coarse lock — all public methods take it
    because each call mutates ≤2 maps. Mirrors the
    ``RealtimeProgressTracker`` lock pattern (P3.5).

    Public surface (Slice 1):
      * ``enqueue(request)`` — add request; promotes IMMEDIATE-tier to
        front. Returns False when queue is at cap (caller falls back
        to non-inline approval path).
      * ``record_decision(request_id, choice, reason)`` — operator
        marks a decision; queue entry stays for ``next_pending`` to
        skip. Returns False when request_id unknown.
      * ``next_pending()`` — returns the highest-priority undecided
        request, or None.
      * ``mark_timeout(request_id, now)`` — records a TIMEOUT_DEFERRED
        decision so the queue stays accurate. Returns False when unknown.
      * ``forget(request_id)`` — drop entry (called by Slice 2 provider
        after the decision flows back through the FSM).
      * ``snapshot()`` — list[InlineApprovalRequest] of all pending.
        Used by REPL list/show in Slice 3.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Insertion-ordered dict so FIFO iteration is naturally
        # well-defined; IMMEDIATE-promotion via popping + re-inserting
        # at the front (Python preserves insertion order).
        self._entries: Dict[str, _QueueEntry] = {}

    # ---- public API ----

    def enqueue(self, request: InlineApprovalRequest) -> bool:
        """Add a request to the queue. Returns False when queue is
        full OR when ``request_id`` collides with an existing entry."""
        if not request.request_id or not request.op_id:
            return False
        with self._lock:
            if request.request_id in self._entries:
                return False
            if len(self._entries) >= MAX_QUEUED_REQUESTS:
                return False
            entry = _QueueEntry(request=request)
            if request.is_immediate_priority():
                # Promote to front: rebuild dict with this entry first.
                new_entries = {request.request_id: entry}
                new_entries.update(self._entries)
                self._entries = new_entries
            else:
                self._entries[request.request_id] = entry
            return True

    def next_pending(
        self, now_unix: Optional[float] = None,
    ) -> Optional[InlineApprovalRequest]:
        """Return the highest-priority undecided request whose deadline
        hasn't already passed. None if queue empty / all decided / all
        past-deadline."""
        now = now_unix if now_unix is not None else time.time()
        with self._lock:
            for entry in self._entries.values():
                if entry.decision is not None:
                    continue
                if entry.request.deadline_unix < now:
                    continue
                return entry.request
        return None

    def record_decision(
        self,
        request_id: str,
        choice: InlineApprovalChoice,
        reason: str = "",
        operator: str = "operator",
        now_unix: Optional[float] = None,
    ) -> bool:
        """Record an operator decision. ``False`` when request_id unknown
        OR when entry already has a decision recorded (idempotent —
        the FIRST decision wins; subsequent calls are silent no-ops).
        """
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None or entry.decision is not None:
                return False
            entry.decision = InlineApprovalDecision(
                request_id=request_id,
                choice=choice,
                reason=str(reason)[:500],
                decided_unix=now_unix if now_unix is not None else time.time(),
                operator=operator,
            )
            return True

    def mark_timeout(
        self, request_id: str, now_unix: Optional[float] = None,
    ) -> bool:
        """Record a TIMEOUT_DEFERRED decision. Used by the Slice 3 prompt
        loop when 30s expires without operator input."""
        return self.record_decision(
            request_id=request_id,
            choice=InlineApprovalChoice.TIMEOUT_DEFERRED,
            reason="prompt timeout",
            operator="system",
            now_unix=now_unix,
        )

    def get_decision(
        self, request_id: str,
    ) -> Optional[InlineApprovalDecision]:
        with self._lock:
            entry = self._entries.get(request_id)
            return entry.decision if entry is not None else None

    def forget(self, request_id: str) -> bool:
        """Drop an entry from the queue. Called by Slice 2 provider after
        the decision has flowed back through the FSM. Idempotent."""
        with self._lock:
            return self._entries.pop(request_id, None) is not None

    def snapshot(self) -> List[InlineApprovalRequest]:
        """Return all pending (undecided + not-past-deadline) requests
        in queue order. Used by Slice 3 REPL list/show."""
        with self._lock:
            return [
                entry.request for entry in self._entries.values()
                if entry.decision is None
            ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_queue: Optional[InlineApprovalQueue] = None
_default_queue_lock = threading.Lock()


def get_default_queue() -> InlineApprovalQueue:
    """Return the process-wide queue. Lazy-construct on first call.

    Mirrors the P3.5 ``get_default_tracker`` pattern. NO master flag
    on the accessor — operators may want to inspect prior decisions
    even after rolling back the env knob. The flag controls whether
    Slice 2's provider EMITS into the queue, not whether the queue is
    available for inspection."""
    global _default_queue
    with _default_queue_lock:
        if _default_queue is None:
            _default_queue = InlineApprovalQueue()
    return _default_queue


def reset_default_queue() -> None:
    """Reset the singleton — for tests."""
    global _default_queue
    with _default_queue_lock:
        _default_queue = None


__all__ = [
    "DEFAULT_DECISION_TIMEOUT_S",
    "MAX_QUEUED_REQUESTS",
    "InlineApprovalChoice",
    "InlineApprovalDecision",
    "InlineApprovalQueue",
    "InlineApprovalRequest",
    "decision_timeout_s",
    "get_default_queue",
    "is_enabled",
    "parse_decision_input",
    "reset_default_queue",
]
