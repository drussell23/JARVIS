"""Universal Terminal-State Lock Releaser — event-driven ingress-lock cleanup.

Empirical foil — soak bt-2026-07-22-174240 (the sensor-side wedge):

    A Saga repair op acquired the ``TestFailureSensor`` target lock
    (``_pending_target_keys``), then ended (failed / recovered) WITHOUT any
    path releasing that lock — ``release_target`` had no caller. Every
    subsequent re-emission was suppressed ("target already in-flight") for the
    rest of the session, so the op could never re-dispatch. The lease reaper
    (PR #70017) cures the *worker-hang* wedge; this cures the
    *clean-terminate-then-suppress* wedge on the ingress side.

The cure is a SYNCHRONIZATION BRIDGE — not scattered ``release_target`` calls —
between terminal operation states and the ingress registry:

  * An event-driven observer subscribes to the ``TrinityEventBus`` terminal
    topics (``op.terminal.#``). Any op reaching a terminal state (PROMOTED,
    TOMBSTONED, conflict_aborted, completed, failed) — or re-queued by the
    ``LeaseReaper`` — triggers a single centralized cleanup listener.
  * The listener performs a DETERMINISTIC REGISTRY SWEEP: it revokes every
    target lock + in-flight flag tied to that ``op_id`` across all registered
    ingress surfaces, resolving the op's target files from the event payload
    or (fallback, DRY) the ``in_flight_registry`` ``ctx_ref``.

This DECOUPLES lock release from execution flow: a failed-then-recovered op can
never remain locked out, regardless of which path terminated it.

DRY: reuses the sensors' existing ``release_target`` method, the
``in_flight_registry`` for op→target resolution, and the ``TrinityEventBus``
subscribe primitives — no duplicate state store. Master-gated, never raises.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.TerminalLockReleaser")

_MASTER_FLAG = "JARVIS_TERMINAL_LOCK_RELEASER_ENABLED"

# Terminal state tokens that must free ingress locks. Superset of the SSE
# broker's TERMINAL_OPERATION_STATES plus the re-queue trigger — matched
# case-insensitively as a substring of the event's state/topic so we never
# miss a variant spelling.
_TERMINAL_TOKENS = (
    "promoted", "tombstoned", "conflict_aborted", "completed", "complete",
    "failed", "rejected", "blocked", "requeued", "no_op", "abandoned",
)


def releaser_enabled() -> bool:
    """Master gate (default TRUE). OFF → the observer is inert and ingress
    locks fall back to their legacy TTL-prune-on-next-signal behavior."""
    return os.environ.get(_MASTER_FLAG, "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _is_terminal_token(value: Any) -> bool:
    if not value:
        return False
    low = str(value).lower()
    return any(tok in low for tok in _TERMINAL_TOKENS)


class TerminalLockReleaser:
    """Central bridge: terminal op-state → ingress-lock revocation.

    Ingress surfaces (e.g. ``TestFailureSensor``) register themselves; each
    exposes ``release_target(target_file)`` (and optionally
    ``release_op(op_id)``). On a terminal event the releaser sweeps every
    surface, revoking the locks for the op's target files."""

    def __init__(self) -> None:
        self._surfaces: List[Any] = []
        self._sub_id: Optional[str] = None

    # -- surface registry (DRY: sensors expose release_target already) -------

    def register_surface(self, surface: Any) -> None:
        """Register an ingress-lock surface. Idempotent; a surface is anything
        exposing ``release_target(target)`` and/or ``release_op(op_id)``."""
        if surface is None:
            return
        if not any(s is surface for s in self._surfaces):
            self._surfaces.append(surface)

    def unregister_surface(self, surface: Any) -> None:
        self._surfaces = [s for s in self._surfaces if s is not surface]

    # -- the deterministic registry sweep (the core) -------------------------

    def release_for_op(
        self,
        op_id: Optional[str],
        target_files: Optional[Sequence[str]] = None,
    ) -> int:
        """Revoke every ingress lock tied to ``op_id`` / ``target_files`` across
        all registered surfaces. Resolves targets from the arg or (fallback)
        the in-flight registry ``ctx_ref``. Returns the count of lock releases.
        NEVER raises — a cleanup failure must not perturb the terminating op."""
        if not releaser_enabled():
            return 0
        targets = list(target_files or [])
        if not targets and op_id:
            targets = self._resolve_targets(op_id)
        released = 0
        for surface in list(self._surfaces):
            # Target-keyed lock (the TestFailureSensor case).
            rt = getattr(surface, "release_target", None)
            if callable(rt):
                for t in targets:
                    try:
                        rt(t)
                        released += 1
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "[TerminalLockReleaser] release_target(%s) failed "
                            "on %r", t, type(surface).__name__, exc_info=True,
                        )
            # Op-keyed in-flight flag (surfaces that track by op_id).
            ro = getattr(surface, "release_op", None)
            if callable(ro) and op_id:
                try:
                    ro(op_id)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[TerminalLockReleaser] release_op(%s) failed on %r",
                        op_id, type(surface).__name__, exc_info=True,
                    )
        if released:
            logger.info(
                "[TerminalLockReleaser] op=%s terminal → revoked %d ingress "
                "lock(s) %s — re-dispatch unblocked",
                op_id, released, targets[:5],
            )
        return released

    def _resolve_targets(self, op_id: str) -> List[str]:
        """Fallback op→target resolution from the in-flight registry ctx_ref
        (DRY — no parallel state). Empty on any miss. Never raises."""
        try:
            from backend.core.ouroboros.governance.in_flight_registry import (
                get_default_registry,
            )
            rec = get_default_registry().lookup(op_id)
            ctx = getattr(rec, "ctx_ref", None)
            tf = getattr(ctx, "target_files", None)
            if tf:
                return [str(t) for t in tf]
        except Exception:  # noqa: BLE001
            pass
        return []

    # -- event-driven observer (the TrinityEventBus bridge) ------------------

    async def _on_terminal_event(self, event: Any) -> None:
        """Bus handler: on a terminal op event, sweep the ingress locks. Reads
        ``op_id`` + optional ``target_files`` from the event payload; a
        non-terminal event is ignored. Never raises."""
        if not releaser_enabled():
            return
        try:
            payload = getattr(event, "payload", None) or {}
            topic = getattr(event, "topic", "") or ""
            state = payload.get("state") or payload.get("outcome") or ""
            # The topic itself (op.terminal.<state>) is authoritative; also
            # accept an explicit terminal state/outcome in the payload.
            if not (_is_terminal_token(topic) or _is_terminal_token(state)):
                return
            op_id = payload.get("op_id")
            targets = payload.get("target_files")
            self.release_for_op(op_id, targets)
        except Exception:  # noqa: BLE001 — observer never perturbs the bus
            logger.debug("[TerminalLockReleaser] event handler error", exc_info=True)

    async def attach_to_bus(
        self, bus: Any, *, pattern: str = "op.terminal.#",
    ) -> Optional[str]:
        """Subscribe the terminal observer to the TrinityEventBus (DRY — reuses
        the bus ``subscribe`` primitive, same pattern the Context Distillation
        GC uses). Idempotent; returns the subscription id or None."""
        if not releaser_enabled() or bus is None:
            return None
        if self._sub_id is not None:
            return self._sub_id
        try:
            self._sub_id = await bus.subscribe(pattern, self._on_terminal_event)
            logger.info(
                "[TerminalLockReleaser] armed on TrinityEventBus pattern=%s "
                "surfaces=%d — ingress locks now self-heal on terminal state",
                pattern, len(self._surfaces),
            )
            return self._sub_id
        except Exception:  # noqa: BLE001
            logger.debug("[TerminalLockReleaser] attach failed", exc_info=True)
            return None


_singleton: Optional[TerminalLockReleaser] = None


def get_terminal_lock_releaser() -> TerminalLockReleaser:
    """Process-wide singleton — the one bridge every terminal seam calls."""
    global _singleton
    if _singleton is None:
        _singleton = TerminalLockReleaser()
    return _singleton


def reset_terminal_lock_releaser() -> None:
    """Test hook — drop the singleton so each case starts clean."""
    global _singleton
    _singleton = None


def release_locks_for_op(
    op_id: Optional[str], target_files: Optional[Iterable[str]] = None,
) -> int:
    """Module-level convenience for the direct terminal seams (LeaseReaper
    re-queue, GLS submit finally): sweep ingress locks for ``op_id``. Reuses
    the singleton. Never raises."""
    try:
        tf = list(target_files) if target_files is not None else None
        return get_terminal_lock_releaser().release_for_op(op_id, tf)
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "TerminalLockReleaser",
    "get_terminal_lock_releaser",
    "release_locks_for_op",
    "releaser_enabled",
    "reset_terminal_lock_releaser",
]
