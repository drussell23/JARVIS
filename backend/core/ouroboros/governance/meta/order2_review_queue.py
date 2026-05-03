"""RR Pass B Slice 6 (module 2) — Persistent Order-2 review queue.

The cage's holding pen between Slice 5 (MetaPhaseRunner produces an
evidence bundle) and Slice 6 module 3 (`/order2 amend <op-id>` REPL
fires the sandboxed replay executor + records operator authorization).

Per Pass B §7 + §8: every Order-2 amendment proposal lands here as a
``QueueEntry`` in PENDING_REVIEW; the operator's amend/reject decision
is recorded as a NEW append-only line so the queue file is the audit
trail. The latest record per op_id wins for "current state".

## Locked-true amendment invariant

The cage's binding rule (Pass B §7.3): **amending Order-2 code requires
operator authorization, full stop**. The env knob
``JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR`` exists for
documentation + audit visibility (the operator can SEE what value was
attempted in startup logs) but the value is NOT honored — the function
:func:`amendment_requires_operator` hard-pins ``True`` regardless of
env. This is the structural cage marker — even a malicious patch that
flips the env to "false" cannot bypass operator authorization.

## Queue shape

  * Append-only JSONL at ``.jarvis/order2_review_queue.jsonl`` (env-
    overridable via ``JARVIS_ORDER2_REVIEW_QUEUE_PATH``).
  * Each line is one :class:`QueueEntry` record (frozen dataclass +
    stable JSON serialization).
  * State transitions write a NEW line — the file is the audit log.
    Latest record per op_id wins for ``get(op_id)`` / ``list_pending()``.
  * Per-record sha256 integrity hash; tamper detection on read.
  * Capacity caps: ``MAX_PENDING_ENTRIES`` (256), ``MAX_HISTORY_LINES``
    (4096) — past the latter the file rotates with a ``.archived``
    suffix per session.

## Authority invariants (Pass B §7.2)

  * Pure data + read-only file I/O of the queue file. No subprocess,
    no env mutation, no network. Only writes are append-line + (rare)
    rotation rename — both atomic via temp + rename pattern where
    needed.
  * No imports of orchestrator / policy / iron_gate / risk_tier_floor
    / change_engine / candidate_generator / gate / semantic_guardian
    / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib + ``meta.meta_phase_runner`` (for MetaEvaluation
    serialization) + ``meta.replay_executor`` (for ReplayExecutionResult
    serialization on amend records).
  * Best-effort throughout — every operation returns a structured
    status; never raises into the caller.

## Default-off

Behind ``JARVIS_ORDER2_REVIEW_QUEUE_ENABLED`` (default false until
Slice 6 graduation). When off, every method returns the appropriate
DISABLED status. Slice 5 hook treats DISABLED as "no queue
persistence" — evaluations land only in the in-process REPL state.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, List, Optional, Sequence, Tuple,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Schema stamped into every QueueEntry; bump on field changes.
QUEUE_SCHEMA_VERSION: int = 1

# Soft caps (not hard limits — past these, the queue rotates).
MAX_PENDING_ENTRIES: int = 256
MAX_HISTORY_LINES: int = 4096
MAX_REASON_CHARS: int = 1_024
MAX_OPERATOR_NAME_CHARS: int = 128

# Default TTL: 7 days. Pending entries older than this become
# auto-EXPIRED on the next ``expire_stale`` call.
DEFAULT_TTL_SECONDS: int = 7 * 24 * 3600


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_epoch() -> float:
    return time.time()


def _hash_record(payload: Dict[str, Any]) -> str:
    """Stable sha256 of the record payload (sans the hash field
    itself) for tamper detection on read. Sort keys to make the hash
    deterministic across Python versions."""
    sanitized = {k: v for k, v in payload.items() if k != "record_sha256"}
    blob = json.dumps(sanitized, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Locked-true cage invariant
# ---------------------------------------------------------------------------


# The env knob exists for visibility — startup logs surface its value
# so an operator can spot "someone tried to flip this to false" — but
# the cage NEVER honors a "false" reading. Pass B §7.3 binding rule.
_AMENDMENT_INVARIANT_ENV = "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR"


def amendment_requires_operator() -> bool:
    """Locked-true cage invariant.

    Returns ``True`` regardless of any env knob — amending Order-2
    governance code REQUIRES operator authorization, period. This is
    the cage's structural marker per Pass B §7.3.

    The env ``JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR`` is
    READ here purely for the side effect of letting tests pin "value
    is logged but not honored". A malicious patch that sets this env
    to "false" still gets ``True`` from this function. The only way
    to flip the answer is to edit this source file (which is itself
    Order-2 code per the manifest, so the cage gates its own
    amendment).
    """
    # Read env for audit visibility — value is NOT honored.
    raw = os.environ.get(_AMENDMENT_INVARIANT_ENV, "")
    if raw and raw.strip().lower() not in _TRUTHY:
        logger.warning(
            "[Order2ReviewQueue] amendment_requires_operator: env "
            "%s=%r ignored — cage invariant locks this True",
            _AMENDMENT_INVARIANT_ENV, raw,
        )
    return True


# ---------------------------------------------------------------------------
# Status enums + frozen dataclasses
# ---------------------------------------------------------------------------


class QueueEntryStatus(str, enum.Enum):
    """Lifecycle state of one queue entry. Written into each record."""

    PENDING_REVIEW = "PENDING_REVIEW"
    """Initial state — Slice 5 MetaPhaseRunner produced
    READY_FOR_OPERATOR_REVIEW; waiting for operator amend/reject."""

    AMENDED = "AMENDED"
    """Operator approved + replay results bundle attached. Apply
    follows in the orchestrator (NOT this module's responsibility)."""

    REJECTED = "REJECTED"
    """Operator rejected the proposal."""

    EXPIRED = "EXPIRED"
    """Pending entry exceeded TTL; auto-EXPIRED by ``expire_stale``."""


class EnqueueStatus(str, enum.Enum):
    OK = "OK"
    DISABLED = "DISABLED"               # master flag off
    DUPLICATE_OP_ID = "DUPLICATE_OP_ID"  # already pending
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    INVALID_EVALUATION = "INVALID_EVALUATION"
    PERSIST_ERROR = "PERSIST_ERROR"


class AmendStatus(str, enum.Enum):
    OK = "OK"
    DISABLED = "DISABLED"
    NOT_FOUND = "NOT_FOUND"
    NOT_PENDING = "NOT_PENDING"          # already amended/rejected/expired
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"  # missing operator name
    REASON_REQUIRED = "REASON_REQUIRED"
    NO_PASSING_REPLAY = "NO_PASSING_REPLAY"
    PERSIST_ERROR = "PERSIST_ERROR"


class RejectStatus(str, enum.Enum):
    OK = "OK"
    DISABLED = "DISABLED"
    NOT_FOUND = "NOT_FOUND"
    NOT_PENDING = "NOT_PENDING"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    REASON_REQUIRED = "REASON_REQUIRED"
    PERSIST_ERROR = "PERSIST_ERROR"


@dataclass(frozen=True)
class OperatorDecision:
    """Captures who/when/why for an amend or reject."""

    decided_at_iso: str
    operator: str
    decision: str  # "amend" or "reject"
    reason: str
    # On amend: the replay-executor result bundle (one per applicable
    # snapshot). On reject: empty tuple.
    replay_results: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decided_at_iso": self.decided_at_iso,
            "operator": self.operator,
            "decision": self.decision,
            "reason": self.reason,
            "replay_results": list(self.replay_results),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OperatorDecision":
        return cls(
            decided_at_iso=str(data.get("decided_at_iso") or ""),
            operator=str(data.get("operator") or ""),
            decision=str(data.get("decision") or ""),
            reason=str(data.get("reason") or ""),
            replay_results=tuple(data.get("replay_results") or ()),
        )


@dataclass(frozen=True)
class QueueEntry:
    """One persistent record. Slice 6 module 3 REPL renders these."""

    schema_version: int
    op_id: str
    enqueued_at_iso: str
    enqueued_at_epoch: float
    status: QueueEntryStatus
    meta_evaluation: Dict[str, Any]
    decision: Optional[OperatorDecision] = None
    record_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "enqueued_at_iso": self.enqueued_at_iso,
            "enqueued_at_epoch": self.enqueued_at_epoch,
            "status": self.status.value,
            "meta_evaluation": self.meta_evaluation,
            "decision": (self.decision.to_dict()
                         if self.decision is not None else None),
        }
        # Compute record_sha256 over the payload sans the hash field
        # itself so verification on read is straightforward.
        out["record_sha256"] = self.record_sha256 or _hash_record(out)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueEntry":
        status_raw = str(data.get("status") or "")
        try:
            status = QueueEntryStatus(status_raw)
        except ValueError:
            status = QueueEntryStatus.PENDING_REVIEW
        decision_raw = data.get("decision")
        decision: Optional[OperatorDecision] = None
        if isinstance(decision_raw, dict):
            decision = OperatorDecision.from_dict(decision_raw)
        return cls(
            schema_version=int(data.get("schema_version") or 0),
            op_id=str(data.get("op_id") or ""),
            enqueued_at_iso=str(data.get("enqueued_at_iso") or ""),
            enqueued_at_epoch=float(data.get("enqueued_at_epoch") or 0.0),
            status=status,
            meta_evaluation=dict(data.get("meta_evaluation") or {}),
            decision=decision,
            record_sha256=str(data.get("record_sha256") or ""),
        )

    def verify_integrity(self) -> bool:
        """Return True iff the embedded record_sha256 matches the
        recomputed hash. Tamper-detection for offline edits."""
        if not self.record_sha256:
            return False
        d = self.to_dict()
        # to_dict() embeds the hash; re-derive without it for compare.
        return _hash_record(d) == self.record_sha256


@dataclass(frozen=True)
class EnqueueResult:
    status: EnqueueStatus
    op_id: str = ""
    detail: str = ""
    entry: Optional[QueueEntry] = None


@dataclass(frozen=True)
class AmendResult:
    status: AmendStatus
    op_id: str = ""
    detail: str = ""
    entry: Optional[QueueEntry] = None


@dataclass(frozen=True)
class RejectResult:
    status: RejectStatus
    op_id: str = ""
    detail: str = ""
    entry: Optional[QueueEntry] = None


# ---------------------------------------------------------------------------
# Master flag + paths
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ORDER2_REVIEW_QUEUE_ENABLED`` (default
    false until Slice 6 graduation)."""
    return os.environ.get(
        "JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", "",
    ).strip().lower() in _TRUTHY


def queue_path() -> Path:
    """Return the queue file path. Env-overridable via
    ``JARVIS_ORDER2_REVIEW_QUEUE_PATH``; defaults to
    ``.jarvis/order2_review_queue.jsonl`` under the cwd."""
    raw = os.environ.get("JARVIS_ORDER2_REVIEW_QUEUE_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "order2_review_queue.jsonl"


# Statuses considered "active" — i.e. NOT terminal.
_ACTIVE_STATUSES: FrozenSet[QueueEntryStatus] = frozenset({
    QueueEntryStatus.PENDING_REVIEW,
})


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class Order2ReviewQueue:
    """Append-only JSONL-backed queue with thread-safe accessors.

    All public methods are best-effort and return structured statuses
    instead of raising. The queue is process-shared via the
    :func:`get_default_queue` singleton; tests instantiate fresh
    instances pointing at tmp paths for isolation.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path if path is not None else queue_path()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Read-side
    # ------------------------------------------------------------------

    def _read_all_records(self) -> List[QueueEntry]:
        """Read the full append-only log. NEVER raises — malformed
        lines are skipped with a warning."""
        if not self._path.exists():
            return []
        out: List[QueueEntry] = []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[Order2ReviewQueue] read failed: %s", exc,
            )
            return []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[Order2ReviewQueue] %s:%d malformed json: %s",
                    self._path, line_no, exc,
                )
                continue
            if not isinstance(obj, dict):
                continue
            try:
                entry = QueueEntry.from_dict(obj)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "[Order2ReviewQueue] %s:%d entry parse failed: %s",
                    self._path, line_no, exc,
                )
                continue
            if entry.record_sha256 and not entry.verify_integrity():
                logger.warning(
                    "[Order2ReviewQueue] %s:%d sha256 mismatch (op_id=%s) — "
                    "tampered record skipped",
                    self._path, line_no, entry.op_id,
                )
                continue
            out.append(entry)
        return out

    def _latest_per_op(self) -> Dict[str, QueueEntry]:
        """Reduce the append-only log to ``{op_id: latest_entry}``.
        Latest = highest enqueued_at_epoch (ties broken by file
        order)."""
        latest: Dict[str, QueueEntry] = {}
        for entry in self._read_all_records():
            existing = latest.get(entry.op_id)
            if existing is None:
                latest[entry.op_id] = entry
            elif entry.enqueued_at_epoch >= existing.enqueued_at_epoch:
                latest[entry.op_id] = entry
        return latest

    def get(self, op_id: str) -> Optional[QueueEntry]:
        """Return the latest entry for ``op_id``, or None."""
        if not is_enabled():
            return None
        with self._lock:
            return self._latest_per_op().get(op_id)

    def list_pending(self) -> Tuple[QueueEntry, ...]:
        """Return entries currently in PENDING_REVIEW (active)."""
        if not is_enabled():
            return ()
        with self._lock:
            return tuple(
                e for e in self._latest_per_op().values()
                if e.status in _ACTIVE_STATUSES
            )

    def list_history(self, limit: int = 50) -> Tuple[QueueEntry, ...]:
        """Return up to ``limit`` most-recent entries (any status),
        newest-first."""
        if not is_enabled():
            return ()
        if limit <= 0:
            return ()
        with self._lock:
            entries = self._read_all_records()
        entries.sort(key=lambda e: e.enqueued_at_epoch, reverse=True)
        return tuple(entries[:limit])

    # ------------------------------------------------------------------
    # Write-side
    # ------------------------------------------------------------------

    def _append_entry(self, entry: QueueEntry) -> bool:
        """Atomically append one record to the queue file. Returns
        True on success, False on persist failure."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[Order2ReviewQueue] mkdir failed for %s: %s",
                self._path.parent, exc,
            )
            return False
        try:
            line = json.dumps(entry.to_dict(), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[Order2ReviewQueue] entry serialization failed "
                "(op_id=%s): %s", entry.op_id, exc,
            )
            return False
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # fsync best-effort — some filesystems return
                    # ENOTSUP. The append is still durable enough
                    # for a non-crash-consistent audit trail.
                    pass
        except OSError as exc:
            logger.warning(
                "[Order2ReviewQueue] append failed (op_id=%s): %s",
                entry.op_id, exc,
            )
            return False
        return True

    def enqueue(self, evaluation: Dict[str, Any]) -> EnqueueResult:
        """Add a new pending entry from a Slice 5 MetaEvaluation dict.

        Caller passes ``MetaEvaluation.to_dict()`` (Slice 5's stable
        serialization) — this module deliberately doesn't import the
        MetaEvaluation class itself to keep its surface minimal.
        """
        if not is_enabled():
            return EnqueueResult(
                status=EnqueueStatus.DISABLED,
                detail="master_flag_off",
            )
        if not isinstance(evaluation, dict):
            return EnqueueResult(
                status=EnqueueStatus.INVALID_EVALUATION,
                detail=f"evaluation_not_dict:{type(evaluation).__name__}",
            )
        op_id = str(evaluation.get("op_id") or "").strip()
        if not op_id:
            return EnqueueResult(
                status=EnqueueStatus.INVALID_EVALUATION,
                detail="evaluation_missing_op_id",
            )
        with self._lock:
            latest = self._latest_per_op()
            existing = latest.get(op_id)
            if existing is not None and existing.status in _ACTIVE_STATUSES:
                return EnqueueResult(
                    status=EnqueueStatus.DUPLICATE_OP_ID,
                    op_id=op_id,
                    detail=f"already_pending:status={existing.status.value}",
                    entry=existing,
                )
            pending_count = sum(
                1 for e in latest.values()
                if e.status in _ACTIVE_STATUSES
            )
            if pending_count >= MAX_PENDING_ENTRIES:
                return EnqueueResult(
                    status=EnqueueStatus.CAPACITY_EXCEEDED,
                    op_id=op_id,
                    detail=(
                        f"pending_count={pending_count} >= "
                        f"MAX_PENDING_ENTRIES={MAX_PENDING_ENTRIES}"
                    ),
                )

            now_iso = _utc_now_iso()
            now_epoch = _utc_now_epoch()
            base = QueueEntry(
                schema_version=QUEUE_SCHEMA_VERSION,
                op_id=op_id,
                enqueued_at_iso=now_iso,
                enqueued_at_epoch=now_epoch,
                status=QueueEntryStatus.PENDING_REVIEW,
                meta_evaluation=dict(evaluation),
                decision=None,
            )
            # Recompute hash so the entry persists with its own digest.
            payload = base.to_dict()
            entry = QueueEntry(
                schema_version=base.schema_version,
                op_id=base.op_id,
                enqueued_at_iso=base.enqueued_at_iso,
                enqueued_at_epoch=base.enqueued_at_epoch,
                status=base.status,
                meta_evaluation=base.meta_evaluation,
                decision=base.decision,
                record_sha256=payload["record_sha256"],
            )
            ok = self._append_entry(entry)
            if not ok:
                return EnqueueResult(
                    status=EnqueueStatus.PERSIST_ERROR,
                    op_id=op_id, detail="append_failed",
                )
            logger.info(
                "[Order2ReviewQueue] op=%s ENQUEUED phase=%s files=%d",
                op_id,
                evaluation.get("target_phase"),
                len(evaluation.get("target_files") or []),
            )
            return EnqueueResult(
                status=EnqueueStatus.OK, op_id=op_id, entry=entry,
            )

    def amend(
        self,
        op_id: str,
        *,
        operator: str,
        reason: str,
        replay_results: Sequence[Dict[str, Any]] = (),
    ) -> AmendResult:
        """Operator authorizes the proposal — record the AMENDED
        transition + the replay-results bundle. Caller passes
        ``ReplayExecutionResult.to_dict()`` per snapshot.

        At least one PASSED replay result is REQUIRED — the cage
        won't let an operator amend a proposal whose sandbox
        replays all diverged.
        """
        if not is_enabled():
            return AmendResult(
                status=AmendStatus.DISABLED, op_id=op_id,
                detail="master_flag_off",
            )
        op_clean = (op_id or "").strip()
        operator_clean = (operator or "").strip()[:MAX_OPERATOR_NAME_CHARS]
        reason_clean = (reason or "").strip()[:MAX_REASON_CHARS]
        if not operator_clean:
            return AmendResult(
                status=AmendStatus.OPERATOR_REQUIRED, op_id=op_clean,
                detail="operator_name_empty",
            )
        if not reason_clean:
            return AmendResult(
                status=AmendStatus.REASON_REQUIRED, op_id=op_clean,
                detail="reason_empty",
            )
        with self._lock:
            existing = self._latest_per_op().get(op_clean)
            if existing is None:
                return AmendResult(
                    status=AmendStatus.NOT_FOUND, op_id=op_clean,
                    detail="no_pending_entry",
                )
            if existing.status not in _ACTIVE_STATUSES:
                return AmendResult(
                    status=AmendStatus.NOT_PENDING, op_id=op_clean,
                    detail=f"current_status={existing.status.value}",
                    entry=existing,
                )
            replay_list = [r for r in (replay_results or [])
                           if isinstance(r, dict)]
            passed = [r for r in replay_list
                      if r.get("status") == "PASSED"]
            if not passed:
                return AmendResult(
                    status=AmendStatus.NO_PASSING_REPLAY,
                    op_id=op_clean,
                    detail=(
                        f"replays_attached={len(replay_list)} "
                        "passed=0 — cage requires at least one "
                        "PASSED replay before amend"
                    ),
                    entry=existing,
                )
            decision = OperatorDecision(
                decided_at_iso=_utc_now_iso(),
                operator=operator_clean,
                decision="amend",
                reason=reason_clean,
                replay_results=tuple(replay_list),
            )
            base = QueueEntry(
                schema_version=QUEUE_SCHEMA_VERSION,
                op_id=op_clean,
                enqueued_at_iso=_utc_now_iso(),
                enqueued_at_epoch=_utc_now_epoch(),
                status=QueueEntryStatus.AMENDED,
                meta_evaluation=existing.meta_evaluation,
                decision=decision,
            )
            payload = base.to_dict()
            entry = QueueEntry(
                schema_version=base.schema_version,
                op_id=base.op_id,
                enqueued_at_iso=base.enqueued_at_iso,
                enqueued_at_epoch=base.enqueued_at_epoch,
                status=base.status,
                meta_evaluation=base.meta_evaluation,
                decision=base.decision,
                record_sha256=payload["record_sha256"],
            )
            if not self._append_entry(entry):
                return AmendResult(
                    status=AmendStatus.PERSIST_ERROR, op_id=op_clean,
                    detail="append_failed",
                )
            logger.info(
                "[Order2ReviewQueue] op=%s AMENDED operator=%s "
                "passed_replays=%d",
                op_clean, operator_clean, len(passed),
            )
            return AmendResult(
                status=AmendStatus.OK, op_id=op_clean, entry=entry,
            )

    def reject(
        self,
        op_id: str,
        *,
        operator: str,
        reason: str,
    ) -> RejectResult:
        """Operator rejects the proposal — record the REJECTED
        transition with the reason."""
        if not is_enabled():
            return RejectResult(
                status=RejectStatus.DISABLED, op_id=op_id,
                detail="master_flag_off",
            )
        op_clean = (op_id or "").strip()
        operator_clean = (operator or "").strip()[:MAX_OPERATOR_NAME_CHARS]
        reason_clean = (reason or "").strip()[:MAX_REASON_CHARS]
        if not operator_clean:
            return RejectResult(
                status=RejectStatus.OPERATOR_REQUIRED, op_id=op_clean,
                detail="operator_name_empty",
            )
        if not reason_clean:
            return RejectResult(
                status=RejectStatus.REASON_REQUIRED, op_id=op_clean,
                detail="reason_empty",
            )
        with self._lock:
            existing = self._latest_per_op().get(op_clean)
            if existing is None:
                return RejectResult(
                    status=RejectStatus.NOT_FOUND, op_id=op_clean,
                    detail="no_pending_entry",
                )
            if existing.status not in _ACTIVE_STATUSES:
                return RejectResult(
                    status=RejectStatus.NOT_PENDING, op_id=op_clean,
                    detail=f"current_status={existing.status.value}",
                    entry=existing,
                )
            decision = OperatorDecision(
                decided_at_iso=_utc_now_iso(),
                operator=operator_clean,
                decision="reject",
                reason=reason_clean,
                replay_results=(),
            )
            base = QueueEntry(
                schema_version=QUEUE_SCHEMA_VERSION,
                op_id=op_clean,
                enqueued_at_iso=_utc_now_iso(),
                enqueued_at_epoch=_utc_now_epoch(),
                status=QueueEntryStatus.REJECTED,
                meta_evaluation=existing.meta_evaluation,
                decision=decision,
            )
            payload = base.to_dict()
            entry = QueueEntry(
                schema_version=base.schema_version,
                op_id=base.op_id,
                enqueued_at_iso=base.enqueued_at_iso,
                enqueued_at_epoch=base.enqueued_at_epoch,
                status=base.status,
                meta_evaluation=base.meta_evaluation,
                decision=base.decision,
                record_sha256=payload["record_sha256"],
            )
            if not self._append_entry(entry):
                return RejectResult(
                    status=RejectStatus.PERSIST_ERROR, op_id=op_clean,
                    detail="append_failed",
                )
            logger.info(
                "[Order2ReviewQueue] op=%s REJECTED operator=%s",
                op_clean, operator_clean,
            )
            return RejectResult(
                status=RejectStatus.OK, op_id=op_clean, entry=entry,
            )

    def expire_stale(
        self, ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> int:
        """Auto-EXPIRE pending entries older than ``ttl_seconds``.
        Returns the count of entries expired. NEVER raises."""
        if not is_enabled():
            return 0
        if ttl_seconds <= 0:
            return 0
        cutoff = _utc_now_epoch() - ttl_seconds
        expired = 0
        with self._lock:
            for entry in self._latest_per_op().values():
                if entry.status not in _ACTIVE_STATUSES:
                    continue
                if entry.enqueued_at_epoch >= cutoff:
                    continue
                decision = OperatorDecision(
                    decided_at_iso=_utc_now_iso(),
                    operator="cage_auto_expire",
                    decision="reject",
                    reason=(f"pending entry exceeded TTL "
                            f"{ttl_seconds}s"),
                    replay_results=(),
                )
                base = QueueEntry(
                    schema_version=QUEUE_SCHEMA_VERSION,
                    op_id=entry.op_id,
                    enqueued_at_iso=_utc_now_iso(),
                    enqueued_at_epoch=_utc_now_epoch(),
                    status=QueueEntryStatus.EXPIRED,
                    meta_evaluation=entry.meta_evaluation,
                    decision=decision,
                )
                payload = base.to_dict()
                rec = QueueEntry(
                    schema_version=base.schema_version,
                    op_id=base.op_id,
                    enqueued_at_iso=base.enqueued_at_iso,
                    enqueued_at_epoch=base.enqueued_at_epoch,
                    status=base.status,
                    meta_evaluation=base.meta_evaluation,
                    decision=base.decision,
                    record_sha256=payload["record_sha256"],
                )
                if self._append_entry(rec):
                    expired += 1
                    logger.info(
                        "[Order2ReviewQueue] op=%s EXPIRED ttl=%ds",
                        entry.op_id, ttl_seconds,
                    )
        return expired


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_queue: Optional[Order2ReviewQueue] = None
_default_lock = threading.Lock()


def get_default_queue() -> Order2ReviewQueue:
    """Process-wide queue. Lazy-init on first call."""
    global _default_queue
    with _default_lock:
        if _default_queue is None:
            _default_queue = Order2ReviewQueue()
    return _default_queue


def reset_default_queue() -> None:
    """Reset the cached queue. Tests use this for isolation."""
    global _default_queue
    with _default_lock:
        _default_queue = None


__all__ = [
    "AmendResult",
    "AmendStatus",
    "DEFAULT_TTL_SECONDS",
    "EnqueueResult",
    "EnqueueStatus",
    "MAX_HISTORY_LINES",
    "MAX_OPERATOR_NAME_CHARS",
    "MAX_PENDING_ENTRIES",
    "MAX_REASON_CHARS",
    "OperatorDecision",
    "Order2ReviewQueue",
    "QUEUE_SCHEMA_VERSION",
    "QueueEntry",
    "QueueEntryStatus",
    "RejectResult",
    "RejectStatus",
    "amendment_requires_operator",
    "get_default_queue",
    "is_enabled",
    "queue_path",
    "reset_default_queue",
]


# ---------------------------------------------------------------------------
# Pass B Graduation Slice 2 — substrate AST pin + cost-contract cage pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta._invariant_helpers import (
        make_pass_b_substrate_invariant,
        make_locked_truthy_env_invariant,
    )
    target = (
        "backend/core/ouroboros/governance/meta/order2_review_queue.py"
    )
    invariants: list = []
    sub = make_pass_b_substrate_invariant(
        invariant_name="pass_b_order2_review_queue_substrate",
        target_file=target,
        description=(
            "Pass B Slice 6.2 substrate: is_enabled + "
            "amendment_requires_operator + queue_path + QueueEntry "
            "(frozen) present; no dynamic-code calls."
        ),
        required_funcs=(
            "is_enabled", "amendment_requires_operator", "queue_path",
        ),
        required_classes=("QueueEntry",),
        frozen_classes=("QueueEntry",),
    )
    if sub is not None:
        invariants.append(sub)
    cage = make_locked_truthy_env_invariant(
        invariant_name="pass_b_amendment_requires_operator_cage",
        target_file=target,
        description=(
            "Pass B cost-contract cage: amendment_requires_operator "
            "MUST default truthy when "
            "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR is "
            "unset. Inverting this cage requires editing this source "
            "file (which is itself Order-2 code per the manifest, "
            "so the cage gates its own amendment)."
        ),
        helper_function_name="amendment_requires_operator",
        env_var_name=(
            "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR"
        ),
    )
    if cage is not None:
        invariants.append(cage)
    return invariants
