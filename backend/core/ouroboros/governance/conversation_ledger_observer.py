"""Arc #1 — Conversation Ledger Observer: auto-persist turn observer.

Registers on the canonical :class:`ConversationBridge` via its
:meth:`register_turn_observer` mechanism. On each admitted turn,
shadow-writes the turn to the JSONL ledger. The observer fires
outside the bridge's lock (per the bridge's observer contract,
L415-L428) so disk I/O never stalls ``record_turn``.

Composition contract (operator-binding 2026-05-12):

  * Composes :func:`conversation_ledger.append_turn` ONLY for
    persistence — NO raw file I/O.
  * Session ID resolved from the active
    :class:`session_manager.SessionManager` session; falls back
    to a stable process-epoch UUID if no session is active.
  * Gated on ``JARVIS_CONVERSATION_LEDGER_ENABLED`` — when off,
    the observer is not registered and no writes occur.
  * NEVER raises — exceptions are swallowed per the bridge's
    observer contract. The bridge's never-raise guarantee is
    preserved.

Authority asymmetry (AST-pinned):
  Imports stdlib + ``conversation_ledger`` +
  ``session_manager`` ONLY. NEVER imports orchestrator /
  iron_gate / policy / etc.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-epoch session fallback
# ---------------------------------------------------------------------------
#
# When no SessionManager session is active, we use a stable
# process-epoch UUID so turns from the same process land in one
# ledger file. Generated once per process lifetime.

_PROCESS_EPOCH_SESSION_ID: str = f"ephemeral-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Observer implementation
# ---------------------------------------------------------------------------


class ConversationLedgerObserver:
    """Bridge turn observer that auto-persists to the JSONL ledger.

    Registered via ``bridge.register_turn_observer(observer)`` at
    startup. Thread-safe (the bridge calls observers from the
    ``record_turn`` caller's thread, outside the lock).

    The observer resolves the active session_id on each call —
    session changes mid-process are handled correctly (turns land
    in the correct session file).
    """

    def __init__(self) -> None:
        self._registered = False
        self._lock = threading.Lock()

    def __call__(self, turn: Any) -> None:
        """Observer callback. Signature matches the bridge's
        observer contract: ``cb(turn: ConversationTurn) -> None``.
        NEVER raises."""
        try:
            from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
                append_turn,
                ledger_enabled,
            )
            if not ledger_enabled():
                return

            session_id = self._resolve_session_id()
            append_turn(
                session_id,
                role=str(getattr(turn, "role", "")),
                text=str(getattr(turn, "text", "")),
                source=str(getattr(turn, "source", "")),
                op_id=str(getattr(turn, "op_id", "")),
                ts=float(getattr(turn, "ts", 0.0) or 0.0),
            )
        except Exception:  # noqa: BLE001 — NEVER raises
            logger.debug(
                "[ConversationLedgerObserver] observer raised, "
                "dropping",
                exc_info=True,
            )

    @staticmethod
    def _resolve_session_id() -> str:
        """Resolve the active session_id from SessionManager.
        Falls back to the process-epoch UUID if no session is
        active or SessionManager is unavailable. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.session_manager import (  # noqa: E501
                get_session_manager,
            )
            mgr = get_session_manager()
            active = mgr.list_active()
            if active:
                return active[0].session_id
        except Exception:  # noqa: BLE001 — defensive
            pass
        return _PROCESS_EPOCH_SESSION_ID

    def register(self) -> bool:
        """Register this observer on the default bridge.

        Idempotent — calling multiple times is safe. Returns True
        if registration succeeded, False otherwise. NEVER raises.
        """
        with self._lock:
            if self._registered:
                return True
            try:
                from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
                    ledger_enabled,
                )
                if not ledger_enabled():
                    return False

                from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
                    get_default_bridge,
                )
                bridge = get_default_bridge()
                bridge.register_turn_observer(self)
                self._registered = True
                logger.info(
                    "[ConversationLedgerObserver] registered on "
                    "default bridge"
                )
                return True
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[ConversationLedgerObserver] registration "
                    "failed",
                    exc_info=True,
                )
                return False

    def unregister(self) -> bool:
        """Unregister this observer from the default bridge.
        Idempotent. NEVER raises."""
        with self._lock:
            if not self._registered:
                return True
            try:
                from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
                    get_default_bridge,
                )
                bridge = get_default_bridge()
                bridge.unregister_turn_observer(self)
                self._registered = False
                return True
            except Exception:  # noqa: BLE001 — defensive
                return False

    @property
    def is_registered(self) -> bool:
        return self._registered


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_default_observer: Optional[ConversationLedgerObserver] = None
_default_lock = threading.Lock()


def get_default_observer() -> ConversationLedgerObserver:
    """Return the process-wide observer singleton. Lazy-construct."""
    global _default_observer
    with _default_lock:
        if _default_observer is None:
            _default_observer = ConversationLedgerObserver()
        return _default_observer


def reset_default_observer_for_tests() -> None:
    """Test isolation helper."""
    global _default_observer
    with _default_lock:
        if _default_observer is not None:
            _default_observer.unregister()
        _default_observer = None


def ensure_registered() -> bool:
    """Convenience: get-or-create the singleton and register it.
    Idempotent. NEVER raises. Returns True if registered."""
    try:
        observer = get_default_observer()
        return observer.register()
    except Exception:  # noqa: BLE001 — defensive
        return False


__all__ = [
    "ConversationLedgerObserver",
    "ensure_registered",
    "get_default_observer",
    "reset_default_observer_for_tests",
]
