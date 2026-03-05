"""UMF Heartbeat Projection -- derives global health truth from heartbeat stream.

Consumes ``UmfMessage`` instances on the lifecycle/heartbeat stream and
maintains an in-memory projection of every subsystem's latest health state.
Staleness detection uses ``time.monotonic()`` to avoid wall-clock skew.

Design rules
------------
* Stdlib only -- no third-party or JARVIS imports beyond UMF types.
* Thread-safe reads via dict-copy in ``get_all_states()``.
* Last-write-wins semantics: the most recent heartbeat for a subsystem
  overwrites any previous state.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional

from backend.core.umf.types import UmfMessage


class HeartbeatProjection:
    """Derive single global health truth from UMF heartbeat messages.

    Parameters
    ----------
    stale_timeout_s:
        Seconds after which a subsystem with no heartbeat is considered stale.
    """

    def __init__(self, stale_timeout_s: float = 30.0) -> None:
        self._stale_timeout_s = stale_timeout_s
        self._states: Dict[str, Dict[str, Any]] = {}
        self._last_seen: Dict[str, float] = {}

    # ── ingestion ────────────────────────────────────────────────────

    def ingest(self, msg: UmfMessage) -> None:
        """Extract health fields from *msg* and update internal projection.

        The subsystem key is derived from ``payload["subsystem_role"]`` when
        present, falling back to ``msg.source.component``.
        """
        payload = msg.payload
        subsystem: str = payload.get("subsystem_role", msg.source.component)

        self._states[subsystem] = {
            "liveness": payload.get("liveness", False),
            "readiness": payload.get("readiness", False),
            "state": payload.get("state"),
            "last_error_code": payload.get("last_error_code"),
            "queue_depth": payload.get("queue_depth", 0),
            "resource_pressure": payload.get("resource_pressure", 0.0),
            "observed_at_unix_ms": msg.observed_at_unix_ms,
            "source_repo": msg.source.repo,
            "session_id": msg.source.session_id,
        }
        self._last_seen[subsystem] = time.monotonic()

    # ── queries ──────────────────────────────────────────────────────

    def get_state(self, subsystem: str) -> Optional[Dict[str, Any]]:
        """Return the latest state dict for *subsystem*, or ``None``."""
        state = self._states.get(subsystem)
        if state is None:
            return None
        return dict(state)

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of all subsystem states."""
        return copy.deepcopy(self._states)

    def get_stale_subsystems(self) -> List[str]:
        """Return subsystem names whose last heartbeat exceeds the stale timeout."""
        now = time.monotonic()
        return [
            subsystem
            for subsystem, last_seen in self._last_seen.items()
            if (now - last_seen) > self._stale_timeout_s
        ]
