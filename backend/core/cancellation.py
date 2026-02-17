"""
Cancellation Token v1.0 — Cross-boundary cancellation propagation.

Provides a cooperative cancellation mechanism that works across:
- Async task boundaries (via asyncio.Event)
- Thread boundaries (via threading.Event)
- Process boundaries (via IPC file in ~/.jarvis/trinity/)

When the supervisor shuts down, the root token is cancelled, and
all child tokens (including cross-process listeners) receive the signal.

Usage:
    from backend.core.cancellation import CancellationToken, get_root_token

    # Create a child token scoped to an operation
    root = get_root_token()
    child = root.create_child("gcp-vm-provision")

    # Check cancellation
    if child.is_cancelled:
        return

    # Wait for cancellation with timeout
    cancelled = await child.wait(timeout=30.0)

    # Use as async context manager
    async with child.scope():
        await long_running_operation()
        # Raises CancelledError if token cancelled during operation

    # Cancel explicitly
    child.cancel(reason="User requested shutdown")
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

logger = logging.getLogger("jarvis.cancellation")

_IPC_DIR = Path.home() / ".jarvis" / "trinity"
_CANCEL_FILE = _IPC_DIR / "cancellation.json"


@dataclass
class CancellationRecord:
    """Serializable cancellation state for IPC."""
    cancelled: bool = False
    reason: str = ""
    cancelled_at: float = 0.0
    token_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cancelled": self.cancelled,
            "reason": self.reason,
            "cancelled_at": self.cancelled_at,
            "token_name": self.token_name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CancellationRecord":
        return cls(
            cancelled=data.get("cancelled", False),
            reason=data.get("reason", ""),
            cancelled_at=data.get("cancelled_at", 0.0),
            token_name=data.get("token_name", ""),
        )


class CancelledError(Exception):
    """Raised when an operation is cancelled via CancellationToken."""
    def __init__(self, reason: str = ""):
        self.reason = reason
        super().__init__(reason or "Operation cancelled")


class CancellationToken:
    """
    Cooperative cancellation token with hierarchy support.

    Tokens form a tree: cancelling a parent cancels all descendants.
    Supports both async (Event) and sync (threading.Event) waiting.
    """

    __slots__ = (
        "name",
        "_cancelled",
        "_reason",
        "_cancelled_at",
        "_async_event",
        "_sync_event",
        "_children",
        "_parent",
        "_callbacks",
        "_lock",
        "_propagate_ipc",
    )

    def __init__(
        self,
        name: str = "root",
        *,
        parent: Optional["CancellationToken"] = None,
        propagate_ipc: bool = False,
    ) -> None:
        self.name = name
        self._cancelled = False
        self._reason = ""
        self._cancelled_at = 0.0
        self._async_event = asyncio.Event()
        self._sync_event = threading.Event()
        self._children: List["CancellationToken"] = []
        self._parent = parent
        self._callbacks: List[Callable[["CancellationToken"], None]] = []
        self._lock = threading.Lock()
        self._propagate_ipc = propagate_ipc

    # -- Core API -----------------------------------------------------------

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "") -> None:
        """
        Cancel this token and all children.

        Thread-safe. Idempotent (second cancel is a no-op).
        """
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason
            self._cancelled_at = time.time()

        # Signal waiters
        self._async_event.set()
        self._sync_event.set()

        logger.info(f"[Cancel] Token '{self.name}' cancelled: {reason or 'no reason'}")

        # Propagate to children
        for child in self._children:
            child.cancel(reason=f"Parent '{self.name}' cancelled: {reason}")

        # Fire callbacks
        for cb in self._callbacks:
            try:
                cb(self)
            except Exception as exc:
                logger.warning(f"[Cancel] Callback error on '{self.name}': {exc}")

        # IPC propagation
        if self._propagate_ipc:
            self._write_ipc()

    def throw_if_cancelled(self) -> None:
        """Raise CancelledError if this token is cancelled."""
        if self._cancelled:
            raise CancelledError(self._reason)

    # -- Waiting ------------------------------------------------------------

    async def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for cancellation. Returns True if cancelled, False on timeout.
        """
        if self._cancelled:
            return True
        try:
            await asyncio.wait_for(self._async_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def wait_sync(self, timeout: Optional[float] = None) -> bool:
        """Synchronous wait for cancellation."""
        if self._cancelled:
            return True
        return self._sync_event.wait(timeout=timeout)

    # -- Hierarchy ----------------------------------------------------------

    def create_child(
        self,
        name: str,
        *,
        propagate_ipc: bool = False,
    ) -> "CancellationToken":
        """Create a child token. Cancelling this token cancels the child."""
        child = CancellationToken(
            name=name,
            parent=self,
            propagate_ipc=propagate_ipc,
        )
        self._children.append(child)
        # If parent already cancelled, cancel child immediately
        if self._cancelled:
            child.cancel(reason=f"Parent '{self.name}' already cancelled")
        return child

    def on_cancel(self, callback: Callable[["CancellationToken"], None]) -> None:
        """Register a callback to fire when cancelled."""
        with self._lock:
            if self._cancelled:
                # Already cancelled — fire immediately
                try:
                    callback(self)
                except Exception:
                    pass
            else:
                self._callbacks.append(callback)

    # -- Context manager ----------------------------------------------------

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[None]:
        """
        Context manager that raises CancelledError if token is cancelled.

        Checks at entry and monitors during the scope.
        """
        self.throw_if_cancelled()

        # Create a task that waits for cancellation
        cancel_task = asyncio.create_task(
            self._async_event.wait(),
            name=f"cancel-watch-{self.name}",
        )
        try:
            yield
        except asyncio.CancelledError:
            if self._cancelled:
                raise CancelledError(self._reason)
            raise
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass

    # -- IPC ----------------------------------------------------------------

    def _write_ipc(self) -> None:
        """Write cancellation state to IPC file for cross-process propagation."""
        try:
            _IPC_DIR.mkdir(parents=True, exist_ok=True)
            record = CancellationRecord(
                cancelled=True,
                reason=self._reason,
                cancelled_at=self._cancelled_at,
                token_name=self.name,
            )
            tmp_path = _CANCEL_FILE.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(record.to_dict()))
            os.replace(tmp_path, _CANCEL_FILE)
        except Exception as exc:
            logger.warning(f"[Cancel] IPC write failed: {exc}")

    @staticmethod
    def check_ipc() -> Optional[CancellationRecord]:
        """Check if cancellation was signalled via IPC."""
        try:
            if _CANCEL_FILE.exists():
                data = json.loads(_CANCEL_FILE.read_text())
                record = CancellationRecord.from_dict(data)
                if record.cancelled:
                    return record
        except Exception:
            pass
        return None

    @staticmethod
    def clear_ipc() -> None:
        """Clear the IPC cancellation file (call on fresh startup)."""
        try:
            if _CANCEL_FILE.exists():
                _CANCEL_FILE.unlink()
        except Exception:
            pass

    # -- Stats --------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cancelled": self._cancelled,
            "reason": self._reason,
            "cancelled_at": self._cancelled_at,
            "children": len(self._children),
            "callbacks": len(self._callbacks),
        }

    def __repr__(self) -> str:
        state = "CANCELLED" if self._cancelled else "ACTIVE"
        return f"CancellationToken('{self.name}', {state})"


# ---------------------------------------------------------------------------
# Module-level root token (singleton)
# ---------------------------------------------------------------------------

_root_token: Optional[CancellationToken] = None
_root_lock = threading.Lock()


def get_root_token() -> CancellationToken:
    """Get or create the root cancellation token."""
    global _root_token
    with _root_lock:
        if _root_token is None:
            # Check IPC for stale cancellation from previous run
            CancellationToken.clear_ipc()
            _root_token = CancellationToken("root", propagate_ipc=True)
        return _root_token


def reset_root_token() -> CancellationToken:
    """Reset the root token (for testing or restart)."""
    global _root_token
    with _root_lock:
        CancellationToken.clear_ipc()
        _root_token = CancellationToken("root", propagate_ipc=True)
        return _root_token
