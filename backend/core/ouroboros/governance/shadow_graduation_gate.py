"""Event-driven graduation gate + graceful-degradation circuit breaker
(Unit C).

Reads the telemetry store at each op boundary; once an agent has N
consecutive aligned ops it flips that agent's ``_AUTHORITATIVE`` flag
and persists it via the existing credential-safe ``persist_flag_to_env``
writer. Promotion is idempotent and honors explicit operator settings.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from backend.core.ouroboros.governance.graduation_orchestrator import (
    persist_flag_to_env,
)
# Intentional reuse of shadow_evaluator's cycle detector (private by
# convention but co-owned); keeps a single Kahn's implementation.
from backend.core.ouroboros.governance.shadow_evaluator import _has_cycle

logger = logging.getLogger("Ouroboros.ShadowGraduationGate")

_AUTH_FLAG = {
    "plan": "JARVIS_PLAN_SUBAGENT_AUTHORITATIVE",
    "review": "JARVIS_REVIEW_SUBAGENT_AUTHORITATIVE",
}
_SHADOW_FLAG = {
    "plan": "JARVIS_PLAN_SUBAGENT_SHADOW",
    "review": "JARVIS_REVIEW_SUBAGENT_SHADOW",
}


def gate_enabled() -> bool:
    raw = os.environ.get("JARVIS_SHADOW_GRADUATION_GATE_ENABLED")
    return raw is None or raw.strip().lower() in ("true", "1", "yes")


def _threshold() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")))
    except (TypeError, ValueError):
        return 50


def _is_authoritative(agent: str) -> bool:
    return os.environ.get(_AUTH_FLAG[agent], "false").strip().lower() in (
        "true", "1", "yes")


class ShadowGraduationGate:
    def __init__(self, *, store: Any) -> None:
        self._store = store

    async def maybe_promote(self, agent: str) -> bool:
        if not gate_enabled() or agent not in _AUTH_FLAG:
            return False
        if _is_authoritative(agent):
            return False  # idempotent — already graduated
        try:
            streak = await self._store.recent_aligned_streak(agent)
        except Exception:  # noqa: BLE001 — gate must not break the FSM
            logger.warning(
                "[ShadowGraduationGate] streak read failed (non-fatal)",
                exc_info=True)
            return False
        if streak < _threshold():
            return False
        return self._promote(agent, streak)

    def _promote(self, agent: str, streak: int) -> bool:
        auth = _AUTH_FLAG[agent]
        shadow = _SHADOW_FLAG[agent]
        ok1 = persist_flag_to_env(auth, "true")
        ok2 = persist_flag_to_env(shadow, "false")
        if ok1:
            os.environ[auth] = "true"
        if ok2:
            os.environ[shadow] = "false"
        logger.info(
            "[GRADUATION] agent=%s streak=%d -> authoritative "
            "(auth_persist=%s shadow_persist=%s)",
            agent, streak, ok1, ok2)
        if ok1 and not ok2:
            # Benign-but-sticky: auth is now persisted, so the next
            # maybe_promote() short-circuits on _is_authoritative and never
            # retries the shadow-flag write. The leftover SHADOW=true only
            # keeps the (no-op) observer running alongside authoritative — no
            # wrong behavior. Follow-up: a self-heal pass could re-attempt the
            # shadow write when auth is already set but shadow is still on.
            logger.warning(
                "[GRADUATION] agent=%s auth persisted but shadow flag "
                "persist failed — .env inconsistent until next promote", agent)
        return bool(ok1)


# ---------------------------------------------------------------------------
# PlanBreaker — graceful-degradation circuit breaker (Unit C)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BreakerDecision:
    trip: bool
    reason: str
    pressure_level: str


def _default_pressure_fn() -> str:
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate,
        )
        return get_default_gate().pressure().value
    except Exception:  # noqa: BLE001
        return "ok"  # probe failure -> assume OK (governor handles fan-out)


class PlanBreaker:
    """Graceful-degradation breaker for the authoritative PLAN path.

    Trip order (first-match-wins):
      1. CRITICAL memory pressure  -> pre-emptive, do NOT touch the DAG.
      2. Empty / unparsable DAG.
      3. Cyclical DAG.
    A trip routes the operation to the retained legacy flat-plan
    generator, guaranteeing execution continuity.
    """

    def __init__(self, *, pressure_fn=None) -> None:
        self._pressure_fn = pressure_fn or _default_pressure_fn

    def should_use_legacy(self, *, dag) -> BreakerDecision:
        level = "ok"
        try:
            level = (self._pressure_fn() or "ok").lower()
        except Exception:  # noqa: BLE001
            level = "ok"
        if level == "critical":
            return BreakerDecision(True, "critical_memory_pressure", level)
        units = dag.get("units") if isinstance(dag, dict) else None
        if not isinstance(units, list) or len(units) == 0:
            return BreakerDecision(True, "unparsable_or_empty_dag", level)
        try:
            if _has_cycle(units):
                return BreakerDecision(True, "cyclical_dag", level)
        except Exception:  # noqa: BLE001
            return BreakerDecision(True, "unparsable_or_empty_dag", level)
        return BreakerDecision(False, "", level)


# ---------------------------------------------------------------------------
# Rail evaluator adapter
# ---------------------------------------------------------------------------

def build_rail_evaluator():
    """Adapter: (agent, legacy, shadow) -> (aligned, reason), routing to
    the right pure evaluator and unwrapping the stored shapes."""
    from backend.core.ouroboros.governance.shadow_evaluator import (
        evaluate_plan, evaluate_review,
    )

    def _ev(agent: str, legacy: dict, shadow: dict):
        if agent == "review":
            a = evaluate_review(legacy, shadow)
        elif agent == "plan":
            a = evaluate_plan(legacy.get("flat", []), shadow)
        else:
            return (False, "malformed:unknown_agent")
        return (a.aligned, a.reason)

    return _ev
