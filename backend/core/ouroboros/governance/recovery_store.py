"""
RecoveryPlanStore — in-memory plan provider.
==============================================

Shared by :mod:`recovery_repl` (live ``/recover <op-id>``) and any
hook that wants to stash plans as they're generated. Orchestrator /
SessionRecorder / battle-test harness wire it at boot.

Authority posture
-----------------

* §1 read-only sidecar — the store observes plans; never fires them.
* §8 observable — bounded LRU, explicit ``snapshot()``.
* No imports from orchestrator / policy / iron_gate / risk_tier_floor
  / semantic_guardian / tool_executor / candidate_generator /
  change_engine. Grep-pinned at graduation.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import List, Optional

from backend.core.ouroboros.governance.recovery_advisor import (
    RecoveryPlan,
)


RECOVERY_STORE_SCHEMA_VERSION: str = "recovery_store.v1"


class RecoveryPlanStore:
    """Bounded per-op store of :class:`RecoveryPlan` instances."""

    def __init__(self, *, capacity: int = 128) -> None:
        self._capacity = max(16, int(capacity))
        self._lock = threading.Lock()
        self._plans: "OrderedDict[str, RecoveryPlan]" = OrderedDict()

    # --- provider contract (duck-typed by recovery_repl) -------------

    def record(self, plan: RecoveryPlan) -> None:
        """Store a plan, keyed by ``op_id``. Most-recent replaces prior."""
        if plan is None or not plan.op_id:
            return
        with self._lock:
            if plan.op_id in self._plans:
                # Update in place while preserving LRU position-at-tail
                self._plans.move_to_end(plan.op_id)
                self._plans[plan.op_id] = plan
                return
            self._plans[plan.op_id] = plan
            if len(self._plans) > self._capacity:
                self._plans.popitem(last=False)

    def get_plan(self, op_id: str) -> Optional[RecoveryPlan]:
        with self._lock:
            return self._plans.get(op_id)

    def recent_plans(self, limit: int = 5) -> List[RecoveryPlan]:
        """Return the N most recently recorded plans, newest first."""
        limit = max(1, int(limit))
        with self._lock:
            # OrderedDict preserves insertion order; the *newest* plan
            # is at the tail, so reverse the tail slice.
            all_plans = list(self._plans.values())
        return list(reversed(all_plans))[:limit]

    # --- diagnostics -------------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self._plans.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "schema_version": RECOVERY_STORE_SCHEMA_VERSION,
                "size": len(self._plans),
                "capacity": self._capacity,
            }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_default_store: Optional[RecoveryPlanStore] = None
_singleton_lock = threading.Lock()


def get_default_plan_store() -> RecoveryPlanStore:
    global _default_store
    with _singleton_lock:
        if _default_store is None:
            _default_store = RecoveryPlanStore()
        return _default_store


def reset_default_plan_store() -> None:
    global _default_store
    with _singleton_lock:
        if _default_store is not None:
            _default_store.clear()
        _default_store = None


__all__ = [
    "RECOVERY_STORE_SCHEMA_VERSION",
    "RecoveryPlanStore",
    "get_default_plan_store",
    "reset_default_plan_store",
]
