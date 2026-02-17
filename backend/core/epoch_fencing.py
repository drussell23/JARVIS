"""
Epoch Fencing v1.0 — Split-brain prevention for the Trinity ecosystem.

When the supervisor crashes mid-operation, repos can disagree on system state.
This module provides:

1. Epoch counter: Monotonically increasing generation number stored in
   ~/.jarvis/trinity/epoch.json. Incremented on each supervisor startup.

2. Fencing tokens: Operations carry the epoch in which they were initiated.
   Recipients reject operations with stale epochs.

3. Re-sync protocol: On reconnect, repos compare epochs. Stale state
   triggers a full re-sync from the supervisor.

Usage:
    from backend.core.epoch_fencing import (
        get_current_epoch, increment_epoch, validate_epoch,
        EpochFencingToken,
    )

    # Supervisor startup
    epoch = increment_epoch()

    # Create fencing token for an operation
    token = EpochFencingToken.create("vm-provision")

    # Validate incoming operations
    if not validate_epoch(incoming_epoch):
        raise StaleEpochError("Operation from stale epoch")
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("jarvis.epoch_fencing")

_EPOCH_DIR = Path.home() / ".jarvis" / "trinity"
_EPOCH_FILE = _EPOCH_DIR / "epoch.json"
_lock = threading.Lock()


class StaleEpochError(Exception):
    """Raised when an operation carries a stale epoch."""
    def __init__(self, current: int, received: int):
        self.current_epoch = current
        self.received_epoch = received
        super().__init__(
            f"Stale epoch: received {received}, current is {current}"
        )


@dataclass(frozen=True)
class EpochFencingToken:
    """
    Fencing token carried by operations to prevent split-brain.

    The token includes the epoch at creation time, a unique operation ID,
    and the operation name for debugging.
    """
    epoch: int
    operation_id: str
    operation_name: str
    created_at: float

    @classmethod
    def create(cls, operation_name: str) -> "EpochFencingToken":
        """Create a new fencing token with the current epoch."""
        return cls(
            epoch=get_current_epoch(),
            operation_id=uuid.uuid4().hex[:12],
            operation_name=operation_name,
            created_at=time.time(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "epoch": self.epoch,
            "operation_id": self.operation_id,
            "operation_name": self.operation_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EpochFencingToken":
        return cls(
            epoch=data["epoch"],
            operation_id=data["operation_id"],
            operation_name=data.get("operation_name", ""),
            created_at=data.get("created_at", 0.0),
        )

    def validate(self) -> bool:
        """Check if this token's epoch matches the current epoch."""
        return self.epoch == get_current_epoch()

    def validate_or_raise(self) -> None:
        """Raise StaleEpochError if epoch doesn't match."""
        current = get_current_epoch()
        if self.epoch != current:
            raise StaleEpochError(current, self.epoch)


# ---------------------------------------------------------------------------
# Epoch storage
# ---------------------------------------------------------------------------

def _read_epoch_data() -> Dict[str, Any]:
    """Read epoch data from disk."""
    try:
        if _EPOCH_FILE.exists():
            return json.loads(_EPOCH_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"[Epoch] Failed to read epoch file: {exc}")
    return {"epoch": 0, "last_incremented": 0.0, "supervisor_id": ""}


def _write_epoch_data(data: Dict[str, Any]) -> None:
    """Atomically write epoch data to disk."""
    try:
        _EPOCH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _EPOCH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, _EPOCH_FILE)
    except OSError as exc:
        logger.error(f"[Epoch] Failed to write epoch file: {exc}")
        raise


def get_current_epoch() -> int:
    """Get the current epoch number."""
    with _lock:
        return _read_epoch_data().get("epoch", 0)


def get_epoch_info() -> Dict[str, Any]:
    """Get full epoch metadata."""
    with _lock:
        return _read_epoch_data()


def increment_epoch(supervisor_id: Optional[str] = None) -> int:
    """
    Increment the epoch counter. Called on supervisor startup.

    Returns the new epoch number.
    """
    with _lock:
        data = _read_epoch_data()
        new_epoch = data.get("epoch", 0) + 1
        data.update({
            "epoch": new_epoch,
            "last_incremented": time.time(),
            "supervisor_id": supervisor_id or uuid.uuid4().hex[:8],
            "history": data.get("history", [])[-9:] + [{
                "epoch": new_epoch,
                "timestamp": time.time(),
            }],
        })
        _write_epoch_data(data)
        logger.info(f"[Epoch] Incremented to epoch {new_epoch}")
        return new_epoch


def validate_epoch(epoch: int) -> bool:
    """Check if the given epoch matches the current epoch."""
    return epoch == get_current_epoch()


def validate_epoch_or_raise(epoch: int) -> None:
    """Raise StaleEpochError if epoch doesn't match."""
    current = get_current_epoch()
    if epoch != current:
        raise StaleEpochError(current, epoch)


# ---------------------------------------------------------------------------
# IPC message helpers
# ---------------------------------------------------------------------------

def stamp_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Add epoch and message metadata to an IPC message."""
    msg["_epoch"] = get_current_epoch()
    msg["_msg_id"] = uuid.uuid4().hex[:12]
    msg["_ts"] = time.time()
    return msg


def validate_message(msg: Dict[str, Any], *, allow_stale: bool = False) -> bool:
    """
    Validate epoch in an incoming IPC message.

    Returns True if valid. If allow_stale is True, only logs a warning
    instead of rejecting.
    """
    msg_epoch = msg.get("_epoch")
    if msg_epoch is None:
        # No epoch — legacy message, accept but warn
        logger.debug("[Epoch] Message without epoch stamp — legacy format")
        return True

    current = get_current_epoch()
    if msg_epoch != current:
        if allow_stale:
            logger.warning(
                f"[Epoch] Stale message: epoch {msg_epoch} (current {current}), "
                f"msg_id={msg.get('_msg_id', '?')}"
            )
            return True
        logger.warning(
            f"[Epoch] Rejected stale message: epoch {msg_epoch} (current {current})"
        )
        return False
    return True
