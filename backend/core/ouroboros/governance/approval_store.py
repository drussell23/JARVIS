"""backend/core/ouroboros/governance/approval_store.py

Durable, atomic, cross-process safe approval persistence.
Uses JSON file with fcntl.flock(), tempfile + fsync + rename for atomicity.
CAS-style state transitions: PENDING to APPROVED|REJECTED|EXPIRED|SUPERSEDED.

Design ref: docs/plans/2026-03-07-vertical-integration-design.md
"""
from __future__ import annotations

import enum
import fcntl
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_STORE_PATH = Path.home() / ".jarvis" / "approvals" / "pending.json"
_STORE_VERSION = 1

# Ledger states that represent a terminal (non-recoverable) outcome.
# Kept as raw string values to avoid importing ledger.OperationState here
# (prevents a circular import between approval_store ↔ ledger).
_TERMINAL_LEDGER_STATES: frozenset[str] = frozenset({
    "rolled_back", "failed", "blocked",
})


class ApprovalState(enum.Enum):
    """Possible states for an approval record."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable approval record."""

    op_id: str
    state: ApprovalState
    actor: str
    channel: str
    reason: str
    policy_version: str
    created_at: float
    decided_at: Optional[float]


class ApprovalStore:
    """File-backed, atomic, cross-process safe approval persistence."""

    def __init__(self, store_path: Path = _DEFAULT_STORE_PATH) -> None:
        self._path = store_path

    def create(self, op_id: str, policy_version: str) -> ApprovalRecord:
        """Write a PENDING record. Atomic write with flock."""
        data = self._read()
        if op_id in data and op_id != "_version":
            # Idempotent: return existing
            return self._to_record(op_id, data[op_id])

        now = time.time()
        entry: Dict[str, Any] = {
            "state": ApprovalState.PENDING.value,
            "actor": "",
            "channel": "cli",
            "reason": "",
            "policy_version": policy_version,
            "created_at": now,
            "decided_at": None,
        }
        data[op_id] = entry
        self._atomic_write(data)
        return self._to_record(op_id, entry)

    def decide(
        self, op_id: str, decision: ApprovalState, reason: str = "", actor: str = "cli_user",
    ) -> ApprovalRecord:
        """CAS transition: PENDING to decision. First valid wins.

        The read-check-write is performed under a single exclusive flock so
        that concurrent callers are serialised and exactly one writer wins.

        Under concurrency, only the thread that observes PENDING and
        successfully writes the decision receives a record with the chosen
        state.  Any thread that observes an already-decided record (even if
        the decision matches) receives an ApprovalRecord with state SUPERSEDED.
        """
        lock_path = self._path.with_suffix(".lock")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            # Read inside the exclusive lock so no other writer can race.
            data = self._read_unlocked()
            entry = data.get(op_id)
            if entry is None:
                raise KeyError(f"Unknown approval op_id: {op_id!r}")

            current_state = ApprovalState(entry["state"])

            # Already decided (by this call or another concurrent one) → SUPERSEDED.
            if current_state != ApprovalState.PENDING:
                return ApprovalRecord(
                    op_id=op_id,
                    state=ApprovalState.SUPERSEDED,
                    actor=actor,
                    channel="cli",
                    reason=reason,
                    policy_version=entry["policy_version"],
                    created_at=entry["created_at"],
                    decided_at=time.time(),
                )

            # Apply decision — write while still holding the lock.
            now = time.time()
            entry["state"] = decision.value
            entry["reason"] = reason
            entry["actor"] = actor
            entry["decided_at"] = now
            data[op_id] = entry
            self._atomic_write_locked(data)
            return self._to_record(op_id, entry)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def decide_with_ledger(
        self,
        op_id: str,
        decision: ApprovalState,
        ledger: Any,  # OperationLedger — kept as Any to avoid circular import
        reason: str = "",
        actor: str = "api",
    ) -> str:
        """CAS transition with ledger terminal check in same lock scope.

        Combines the approval store CAS write with a synchronous ledger read
        so that both the "already decided" guard and the "ledger is terminal"
        guard are evaluated atomically under the same exclusive flock.

        Parameters
        ----------
        op_id:
            The operation identifier to decide on.
        decision:
            The :class:`ApprovalState` to apply (e.g. APPROVED or REJECTED).
        ledger:
            An ``OperationLedger`` instance.  Its ``get_latest_state_sync()``
            method is called inside the lock scope.
        reason:
            Optional human-readable explanation for the decision.
        actor:
            Identity of the caller making the decision. Defaults to ``"api"``.

        Returns
        -------
        str
            ``"ok"``         — transition applied successfully.
            ``"superseded"`` — ledger is already terminal, or the store record
                               was already decided by another writer.
            ``"not_found"``  — *op_id* is unknown in the approval store.

        Note: The ledger terminal check is time-of-check only. The approval-store
        CAS is atomic (protected by the exclusive flock), but the ledger JSONL
        has separate locking. A concurrent ledger write could theoretically
        terminal-ize the op between the check and the commit. Callers should
        treat "ok" as "was not terminal at the moment of check."
        """
        lock_path = self._path.with_suffix(".lock")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            # Read inside the exclusive lock — no other writer can race.
            data = self._read_unlocked()
            entry = data.get(op_id)
            if entry is None or not isinstance(entry, dict):
                return "not_found"

            current_state = ApprovalState(entry["state"])
            if current_state != ApprovalState.PENDING:
                return "superseded"

            # Check ledger terminal state synchronously — safe inside flock.
            ledger_state = ledger.get_latest_state_sync(op_id)
            if ledger_state is not None and ledger_state.value in _TERMINAL_LEDGER_STATES:
                return "superseded"

            # All guards passed — write while still holding the lock.
            now = time.time()
            entry["state"] = decision.value
            entry["decided_at"] = now
            entry["reason"] = reason
            entry["actor"] = actor
            data[op_id] = entry
            self._atomic_write_locked(data)
            return "ok"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def get(self, op_id: str) -> Optional[ApprovalRecord]:
        """Read current state for an op_id."""
        data = self._read()
        entry = data.get(op_id)
        if entry is None or not isinstance(entry, dict):
            return None
        return self._to_record(op_id, entry)

    def expire_stale(self, timeout_seconds: float = 1800.0) -> List[str]:
        """Expire PENDING records older than timeout. Returns expired op_ids."""
        data = self._read()
        now = time.time()
        expired: List[str] = []

        for op_id, entry in data.items():
            if op_id == "_version":
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("state") != ApprovalState.PENDING.value:
                continue
            age = now - entry.get("created_at", now)
            if age > timeout_seconds:
                entry["state"] = ApprovalState.EXPIRED.value
                entry["decided_at"] = now
                entry["reason"] = f"expired_after_{timeout_seconds}s"
                expired.append(op_id)

        if expired:
            self._atomic_write(data)
            logger.info("Expired %d stale approvals: %s", len(expired), expired)

        return expired

    # -- internal --

    def _read(self) -> Dict[str, Any]:
        """Read store file. Returns empty dict on missing/corrupt."""
        if not self._path.exists():
            return {"_version": _STORE_VERSION}
        try:
            with open(self._path, encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            if not isinstance(data, dict):
                return {"_version": _STORE_VERSION}
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Approval store corrupt, returning empty: %s", exc)
            return {"_version": _STORE_VERSION}

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        """Atomic write: flock + tempfile + fsync + rename."""
        data["_version"] = _STORE_VERSION
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Acquire exclusive lock on a lockfile to prevent concurrent writers
        lock_path = self._path.with_suffix(".lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._atomic_write_locked(data)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _read_unlocked(self) -> Dict[str, Any]:
        """Read store file without acquiring a lock.

        Must only be called when the caller already holds the exclusive
        lockfile flock.  Returns empty dict on missing or corrupt file.
        """
        if not self._path.exists():
            return {"_version": _STORE_VERSION}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"_version": _STORE_VERSION}
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Approval store corrupt in _read_unlocked: %s", exc)
            return {"_version": _STORE_VERSION}

    def _atomic_write_locked(self, data: Dict[str, Any]) -> None:
        """Write via tempfile + fsync + rename.

        Must only be called when the caller already holds the exclusive
        lockfile flock.  Does NOT re-acquire the lock.
        """
        data["_version"] = _STORE_VERSION
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self._path.parent, delete=False, suffix=".tmp",
        ) as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            tmp = Path(f.name)
        tmp.rename(self._path)

    @staticmethod
    def _to_record(op_id: str, entry: Dict[str, Any]) -> ApprovalRecord:
        """Convert a dict entry to an ApprovalRecord."""
        return ApprovalRecord(
            op_id=op_id,
            state=ApprovalState(entry["state"]),
            actor=entry.get("actor", ""),
            channel=entry.get("channel", "cli"),
            reason=entry.get("reason", ""),
            policy_version=entry.get("policy_version", ""),
            created_at=entry.get("created_at", 0.0),
            decided_at=entry.get("decided_at"),
        )
