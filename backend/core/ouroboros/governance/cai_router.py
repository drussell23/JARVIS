"""Slice 131 Phase 4 — The CAI Autonomous Router (FrugalGPT cascade for O+V).

The "Cursor-auto" for O+V: autonomously pick the cheapest CAPABLE provider tier
per op, escalating to heavyweights only when the model signals high difficulty or
low confidence — with situational awareness acting as a cost-guard.

This module is **pure composition** of substrate that already exists — it builds
nothing the codebase already has:

  * **CAI** (Contextual Awareness Intelligence) — ``ContextAwarenessIntelligence
    .predict_intent`` is a cheap, synchronous, deterministic intent+confidence
    scorer (no LLM call). It supplies the per-op **confidence** signal.
  * **SAI** (Self-Aware Intelligence) — ``SelfAwareIntelligence.get_cognitive_state``
    supplies a **situational pressure** signal. Under high pressure the router
    suppresses confidence-driven escalation (cost-guard): only genuine *difficulty*
    is allowed to climb the ladder, so a strained system never burns extra money
    chasing low-confidence escalations.
  * **tiers** — the cheapest→heaviest provider ladder is resolved from
    ``brain_selection_policy.yaml`` (``cost_optimization.cascade_tiers``); the
    cheap Claude tier's model reuses Phase-1 ``economic_router`` policy resolution.
    **No model string is hardcoded in this module** (CLAUDE.md mandate).
  * **failover** — when the chosen cheap tier 402/429s, the existing
    ``economic_router`` matrix (Phase 1, default-on) owns the recovery. This
    module decides the *first* tier; economic_router decides the *fallback*.

**Decoupled + injectable.** ``decide`` accepts an injected ``classifier`` and
``sai_probe`` (sync or async) so the hot path / tests never depend on the heavy
CAI/SAI init. **Fail-closed:** any error → ``None`` → the caller keeps the
existing ``UrgencyRouter`` path. **Gated** ``JARVIS_CAI_ROUTER_ENABLED``
default-FALSE → OFF is byte-identical. **Adaptive:** ``record_outcome`` tunes the
confidence threshold from observed escalation need (self-tuning).
"""
from __future__ import annotations

import dataclasses
import inspect
import os
import pathlib
from typing import Any, Awaitable, Callable, List, Optional, Union

# ── env knobs ───────────────────────────────────────────────────────────────
_ENV_MASTER = "JARVIS_CAI_ROUTER_ENABLED"
_ENV_THRESHOLD = "JARVIS_CAI_CONFIDENCE_THRESHOLD"
_DEFAULT_THRESHOLD = 0.5
_THRESHOLD_FLOOR = 0.10
_THRESHOLD_CEIL = 0.95
_ADAPT_STEP = 0.01  # per-outcome EWMA nudge

# Default cheapest→heaviest tier ladder (NAMES are structural identifiers, not
# model strings — the concrete models resolve from the policy / providers).
_DEFAULT_LADDER = ["doubleword", "claude_low_cost", "claude_heavy"]
_POLICY_PATH = pathlib.Path(__file__).parent / "brain_selection_policy.yaml"

# Adaptive state (module-level, fail-soft). reset_adaptive_state() for tests.
_adaptive_offset = 0.0


def cai_router_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. Re-read each call so a flip
    hot-reverts. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


# ── signals ─────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class CAIClassification:
    """The CAI difficulty + confidence signal for one op.

    ``difficulty`` ∈ {low, medium, high}; ``confidence`` ∈ [0, 1]."""

    difficulty: str = "medium"
    confidence: float = 0.5


@dataclasses.dataclass(frozen=True)
class SituationalSignal:
    """The SAI situational signal. ``pressure`` ∈ {nominal, elevated, high}."""

    pressure: str = "nominal"


@dataclasses.dataclass(frozen=True)
class CAIRoutingDecision:
    """The autonomous routing decision. ``tier`` is a ladder name; ``model`` is
    the resolved concrete model for tiers that need one ("" → provider default)."""

    tier: str
    model: str
    difficulty: str
    confidence: float
    escalated: bool          # final tier is above the cheapest rung
    reason: str
    situational_pressure: str


# ── threshold (adaptive) ────────────────────────────────────────────────────
def confidence_threshold() -> float:
    """Confidence below which the router escalates one extra rung. Base from env,
    nudged by ``record_outcome`` (self-tuning). Clamped. NEVER raises."""
    try:
        base = float(os.getenv(_ENV_THRESHOLD, _DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        base = _DEFAULT_THRESHOLD
    return max(_THRESHOLD_FLOOR, min(_THRESHOLD_CEIL, base + _adaptive_offset))


def record_outcome(escalation_was_needed: bool) -> None:
    """Feed back whether an escalation turned out warranted. If escalations are
    frequently needed, raise the threshold so the router escalates more readily
    next time (and vice-versa). EWMA-style, bounded. NEVER raises."""
    global _adaptive_offset
    try:
        delta = _ADAPT_STEP if escalation_was_needed else -_ADAPT_STEP
        _adaptive_offset = max(-0.4, min(0.4, _adaptive_offset + delta))
    except Exception:  # noqa: BLE001
        pass


def reset_adaptive_state() -> None:
    """Reset the adaptive offset (tests / clean boot)."""
    global _adaptive_offset
    _adaptive_offset = 0.0


# ── tier ladder + model resolution (policy-driven, no hardcode) ──────────────
def cascade_tiers(policy_path: Optional[pathlib.Path] = None) -> List[str]:
    """Cheapest→heaviest provider ladder from
    ``brain_selection_policy.yaml`` (``cost_optimization.cascade_tiers``).
    Falls back to the default ladder. NEVER raises."""
    path = policy_path or _POLICY_PATH
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data: Any = yaml.safe_load(fh) or {}
        tiers = (data.get("cost_optimization", {}) or {}).get("cascade_tiers")
        if isinstance(tiers, list) and tiers:
            return [str(t) for t in tiers]
    except Exception:  # noqa: BLE001
        pass
    return list(_DEFAULT_LADDER)


def tier_model(tier: str, policy_path: Optional[pathlib.Path] = None) -> str:
    """Resolve the concrete model for a tier. The cheap Claude tier composes the
    Phase-1 ``economic_router`` policy resolution (env override → policy →
    ""). DW / heavy-Claude return "" (the provider picks its own model). NO model
    string is hardcoded here. NEVER raises."""
    try:
        if tier == "claude_low_cost":
            from backend.core.ouroboros.governance.economic_router import (
                economic_failover_model,
            )
            return economic_failover_model(policy_path)
    except Exception:  # noqa: BLE001
        return ""
    return ""


# ── default adapters (compose the real CAI / SAI, fail-soft) ─────────────────
_DIFFICULTY_FROM_COMPLEXITY = {
    "trivial": "low", "simple": "low", "light": "low",
    "moderate": "medium", "medium": "medium",
    "heavy_code": "high", "complex": "high", "architectural": "high",
}


def classify_default(prompt: str, context: Any) -> CAIClassification:
    """Default classifier: difficulty from the op's existing ``task_complexity``
    (O+V already computes it), confidence from CAI ``predict_intent``. Fail-soft
    to medium/0.5 so a missing CAI never blocks routing."""
    difficulty = "medium"
    raw = str(getattr(context, "task_complexity", "") or "").strip().lower()
    if raw in _DIFFICULTY_FROM_COMPLEXITY:
        difficulty = _DIFFICULTY_FROM_COMPLEXITY[raw]
    confidence = 0.5
    try:
        from backend.intelligence.context_awareness_intelligence import (
            ContextAwarenessIntelligence,
        )
        cai = ContextAwarenessIntelligence()
        pred = cai.predict_intent(prompt or "")
        confidence = float(pred.get("confidence", 0.5))
    except Exception:  # noqa: BLE001
        pass
    return CAIClassification(difficulty=difficulty, confidence=confidence)


def probe_situational_default() -> SituationalSignal:
    """Default SAI probe: derive situational pressure from
    ``SelfAwareIntelligence.get_cognitive_state``. Fail-soft to nominal."""
    try:
        from backend.intelligence.self_aware_intelligence import (
            SelfAwareIntelligence,
        )
        sai = SelfAwareIntelligence()
        state = sai.get_cognitive_state() or {}
        raw = str(
            state.get("pressure")
            or state.get("memory_pressure")
            or state.get("situational_pressure")
            or "nominal"
        ).strip().lower()
        if raw in ("high", "critical", "severe"):
            return SituationalSignal("high")
        if raw in ("elevated", "warning", "moderate"):
            return SituationalSignal("elevated")
    except Exception:  # noqa: BLE001
        pass
    return SituationalSignal("nominal")


# ── the cascade decision ─────────────────────────────────────────────────────
_DIFFICULTY_RUNG = {"low": 0, "medium": 1, "high": 2}

_Classifier = Callable[[str, Any], Union[CAIClassification, Awaitable[CAIClassification]]]
_SaiProbe = Callable[[], Union[SituationalSignal, Awaitable[SituationalSignal]]]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def decide(
    prompt: str,
    context: Any,
    *,
    classifier: Optional[_Classifier] = None,
    sai_probe: Optional[_SaiProbe] = None,
    policy_path: Optional[pathlib.Path] = None,
) -> Optional[CAIRoutingDecision]:
    """Autonomously choose the first provider tier for an op (FrugalGPT cascade).

    Returns ``None`` when disabled OR on ANY error (fail-closed → the caller
    keeps the existing route). When enabled: difficulty (CAI) sets the base rung;
    low confidence (CAI, vs the adaptive threshold) bumps one rung UP — but only
    when SAI situational pressure is not ``high`` (cost-guard). The chosen tier's
    concrete model is resolved from policy. ``economic_router`` owns failover.
    """
    if not cai_router_enabled():
        return None
    try:
        cls = await _maybe_await((classifier or classify_default)(prompt, context))
        sai = await _maybe_await((sai_probe or probe_situational_default)())
        if not isinstance(cls, CAIClassification):
            return None
        if not isinstance(sai, SituationalSignal):
            sai = SituationalSignal("nominal")

        tiers = cascade_tiers(policy_path)
        if not tiers:
            return None
        top = len(tiers) - 1

        base = min(_DIFFICULTY_RUNG.get(cls.difficulty, 1), top)
        rung = base
        cost_guard = sai.pressure == "high"
        confidence_bump = cls.confidence < confidence_threshold()
        if confidence_bump and not cost_guard:
            rung = min(rung + 1, top)

        tier = tiers[rung]
        model = tier_model(tier, policy_path)
        reason = (
            f"difficulty={cls.difficulty} confidence={cls.confidence:.2f} "
            f"thr={confidence_threshold():.2f} sai={sai.pressure} "
            f"rung={rung}/{top} -> {tier}"
            + (" [cost-guard suppressed bump]" if (confidence_bump and cost_guard) else "")
        )
        return CAIRoutingDecision(
            tier=tier,
            model=model,
            difficulty=cls.difficulty,
            confidence=cls.confidence,
            escalated=rung > 0,
            reason=reason,
            situational_pressure=sai.pressure,
        )
    except Exception:  # noqa: BLE001 — fail-closed
        return None


__all__ = [
    "cai_router_enabled",
    "CAIClassification",
    "SituationalSignal",
    "CAIRoutingDecision",
    "confidence_threshold",
    "record_outcome",
    "reset_adaptive_state",
    "cascade_tiers",
    "tier_model",
    "classify_default",
    "probe_situational_default",
    "decide",
]
