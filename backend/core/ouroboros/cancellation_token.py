"""Epoch-scoped cooperative cancellation token for Ouroboros Daemon REM cycles.

Tasks within a REM epoch check ``token.is_cancelled`` between work units to
cooperatively pause without killing asyncio TaskGroups.  The token is
intentionally lightweight: a single asyncio.Event plus the epoch identifier.
"""
from __future__ import annotations

import asyncio


class CancellationToken:
    """Cooperative cancellation token scoped to a single REM epoch.

    Parameters
    ----------
    epoch_id:
        Monotonically increasing identifier for the epoch this token belongs to.
    """

    __slots__ = ("_epoch_id", "_event")

    def __init__(self, epoch_id: int) -> None:
        self._epoch_id: int = epoch_id
        self._event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def epoch_id(self) -> int:
        """Read-only epoch identifier."""
        return self._epoch_id

    @epoch_id.setter
    def epoch_id(self, _value: int) -> None:  # type: ignore[misc]
        raise AttributeError("epoch_id is read-only")

    @property
    def is_cancelled(self) -> bool:
        """True once ``cancel()`` has been called."""
        return self._event.is_set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Signal cancellation.  Idempotent — safe to call multiple times."""
        self._event.set()

    async def wait(self) -> None:
        """Block until the token is cancelled.

        Combine with ``asyncio.wait_for`` to apply a timeout::

            await asyncio.wait_for(token.wait(), timeout=5.0)
        """
        await self._event.wait()
