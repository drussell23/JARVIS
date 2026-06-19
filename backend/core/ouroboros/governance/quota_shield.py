"""Predictive Quota Shield (Phases 1+2) -- pure route-local-vs-remote decision.

Fuses ALREADY-COMPUTED signals (OperationAdvisor risk_score + blast_radius, a
precomputed token volume, and the live MemoryPressureGate level) into a single
decision: route a trivial/localized op to the zero-cost local J-Prime tier
(preserving remote DW quota), UNLESS host memory is CRITICAL (host stability wins
-> hard upstream override). Pure + deterministic; the orchestrator supplies the
signals and acts on the result. Reuses existing intelligence layers; computes
nothing itself.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from dataclasses import dataclass
from typing import Any

_TRUE = {"1", "true", "yes", "on"}


def quota_shield_enabled() -> bool:
    return os.environ.get("JARVIS_QUOTA_SHIELD_ENABLED", "").strip().lower() in _TRUE


def _f(name: str, d: float) -> float:
    try:
        return float(os.environ.get(name, str(d)))
    except Exception:
        return d


def compute_cognitive_load(*, risk_score: float, blast_radius: int, token_volume: int) -> float:
    """Fuse three normalized axes into a 0-1 cognitive-load score. Higher = heavier.

    - risk_score: OperationAdvisor composite (already 0-1).
    - blast_radius: downstream dependency count -> normalized by JARVIS_QUOTA_SHIELD_BLAST_NORM.
    - token_volume: target payload size -> normalized by JARVIS_QUOTA_SHIELD_TOKEN_NORM.
    Weights env-tunable; result clamped to [0,1].
    """
    blast_norm = max(1.0, _f("JARVIS_QUOTA_SHIELD_BLAST_NORM", 10.0))
    token_norm = max(1.0, _f("JARVIS_QUOTA_SHIELD_TOKEN_NORM", 8000.0))
    w_risk = _f("JARVIS_QUOTA_SHIELD_W_RISK", 0.5)
    w_blast = _f("JARVIS_QUOTA_SHIELD_W_BLAST", 0.3)
    w_tok = _f("JARVIS_QUOTA_SHIELD_W_TOKENS", 0.2)
    r = min(1.0, max(0.0, float(risk_score)))
    b = min(1.0, max(0.0, float(blast_radius) / blast_norm))
    t = min(1.0, max(0.0, float(token_volume) / token_norm))
    wsum = w_risk + w_blast + w_tok
    if wsum <= 0:
        return 0.0
    return min(1.0, (w_risk * r + w_blast * b + w_tok * t) / wsum)


@dataclass(frozen=True)
class ShieldDecision:
    route_local: bool
    memory_override: bool
    cognitive_load: float
    reason: str


def decide(*, advisory: Any, pressure_level: Any, token_volume: int,
           local_enabled: bool) -> ShieldDecision:
    """Decide whether to proactively route this op to the local tier.

    Order: (1) local disabled -> never local. (2) CRITICAL memory -> hard upstream
    override (host stability over quota savings). (3) low cognitive load -> local
    (quota shield). (4) otherwise -> remote.
    """
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    risk = float(getattr(advisory, "risk_score", 0.0) or 0.0)
    blast = int(getattr(advisory, "blast_radius", 0) or 0)
    load = compute_cognitive_load(risk_score=risk, blast_radius=blast, token_volume=token_volume)

    if not local_enabled:
        return ShieldDecision(False, False, load, "local_tier_disabled")
    if pressure_level is PressureLevel.CRITICAL:
        return ShieldDecision(False, True, load, "memory_critical_hard_override")
    threshold = _f("JARVIS_QUOTA_SHIELD_THRESHOLD", 0.35)
    if load < threshold:
        return ShieldDecision(True, False, load, f"low_cognitive_load:{load:.3f}<{threshold:.3f}")
    return ShieldDecision(False, False, load, f"high_cognitive_load:{load:.3f}>={threshold:.3f}")


logger = logging.getLogger(__name__)

# Strong refs so fire-and-forget pre-warm tasks are not GC'd mid-flight.
_PREWARM_TASKS: set = set()


def _default_token_estimator(ctx: Any) -> int:
    """Best-effort token estimate from target file sizes (len//4). Never raises."""
    total = 0
    for path in (getattr(ctx, "target_files", ()) or ()):
        try:
            with open(path, "r", errors="ignore") as fh:
                total += len(fh.read()) // 4
        except Exception:
            continue
    return total


async def apply_quota_shield(
    ctx: Any,
    *,
    advisory: Any,
    gate: Any = None,
    governor: Any = None,
    local_enabled: Any = None,
    token_estimator: Any = None,
) -> Any:
    """Orchestrator-side application of the quota shield.

    Returns ctx unchanged when disabled; otherwise returns a (possibly
    prefer_local-stamped) ctx and fires a non-blocking JIT pre-warm of the
    local daemon when routing local. Fail-soft: any error returns the original
    ctx untouched.
    """
    if not quota_shield_enabled():
        return ctx
    # No advisory -> we cannot assess cognitive load. Do NOT hijack routing on a
    # blind 0-load read (which would route everything local). Leave ctx untouched.
    if advisory is None:
        return ctx
    try:
        if local_enabled is None:
            from backend.core.ouroboros.governance.local_inference_director import (
                local_prime_enabled as _lpe,
            )
            local_enabled = _lpe()
        if gate is None:
            from backend.core.ouroboros.governance.memory_pressure_gate import get_default_gate
            gate = get_default_gate()
        est = token_estimator or _default_token_estimator
        token_volume = int(est(ctx))
        level = gate.pressure()
        decision = decide(
            advisory=advisory,
            pressure_level=level,
            token_volume=token_volume,
            local_enabled=bool(local_enabled),
        )
        logger.info(
            "[QuotaShield] op=%s load=%.3f route_local=%s mem_override=%s reason=%s",
            getattr(ctx, "op_id", "?"),
            decision.cognitive_load,
            decision.route_local,
            decision.memory_override,
            decision.reason,
        )
        if not decision.route_local:
            return ctx
        # JIT pre-warm: fire-and-forget so daemon boot latency is masked.
        _gov = governor
        try:
            if _gov is None:
                from backend.core.ouroboros.governance.local_daemon_governor import (
                    daemon_governor_enabled,
                    LocalDaemonGovernor,
                )
                if daemon_governor_enabled():
                    _gov = LocalDaemonGovernor()
            if _gov is not None:
                _t = asyncio.ensure_future(_gov.start_if_enabled())
                _PREWARM_TASKS.add(_t)
                _t.add_done_callback(_PREWARM_TASKS.discard)
        except Exception:
            logger.debug("[QuotaShield] JIT pre-warm skipped", exc_info=True)
        try:
            return dataclasses.replace(ctx, prefer_local=True)
        except Exception:
            return ctx  # ctx not a dataclass / immutable replace failed -> leave as-is
    except Exception:
        logger.debug("[QuotaShield] apply skipped (fail-soft)", exc_info=True)
        return ctx
