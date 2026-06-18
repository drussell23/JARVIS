"""Pre-Flight Critic — Phases 2-3 of the Self-Correction & DPO Alignment Engine.

A cheap local quality gate on a generated patch candidate BEFORE the expensive paths run — the heavy
sandbox test suite AND (critically) the frontier-model cascade. Targets the DoubleWord stability
problem directly: DW is the cheap primary provider but produces structurally-unreliable output
(malformed JSON, non-diff, schema drift); a local critic that predicts "this DW candidate will fail"
lets O+V catch it early and regenerate, instead of cascading to Claude (which costs money — and just
hit a zero-credit wall in the live soak).

Phase 2 — Predictive Failure Probability: ``predict_failure(candidate, context)`` calls the
fine-tuned local model served by Reactor-Core (Llama-3.2-3B) → a 0-1 failure probability.

Phase 3 — Stochastic gating + policy feedback: if the failure probability crosses the threshold,
``evaluate()`` short-circuits — bypass the sandbox, convert the critic's inference into a prompt
constraint clause, and signal "route back to generation" for an instant targeted correction.

**Model-collapse / confirmation-bias armor:** the critic is ADVISORY pressure, not ground truth. A
sampling floor (``JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE``) always lets some candidates through to the
real sandbox regardless of the critic's verdict, so the empirical test signal keeps flowing and the
loop can never collapse into trusting the critic blindly.

**Honest status:** this is the CONSUMER of the training loop. It is **inert until a critic model is
actually trained (by the RepairTrajectoryEmitter → Reactor pipeline) and served** — with no model,
``predict_failure`` returns ``None`` and ``evaluate`` never short-circuits (the system behaves exactly
as today). Gated ``JARVIS_PREFLIGHT_CRITIC_ENABLED`` (default OFF). Fail-soft throughout.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["PreflightCritic", "CriticVerdict", "critic_enabled"]


def critic_enabled() -> bool:
    """``JARVIS_PREFLIGHT_CRITIC_ENABLED`` (default OFF) — master for the local pre-flight critic."""
    return os.environ.get("JARVIS_PREFLIGHT_CRITIC_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _fail_threshold() -> float:
    """``JARVIS_PREFLIGHT_CRITIC_FAIL_THRESHOLD`` (default 0.85) — predicted-failure prob above which
    the candidate is short-circuited back to generation."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("JARVIS_PREFLIGHT_CRITIC_FAIL_THRESHOLD", "0.85"))))
    except ValueError:
        return 0.85


def _sample_rate() -> float:
    """``JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE`` (default 0.1) — fraction of candidates that ALWAYS go to
    the real sandbox regardless of the critic, keeping the empirical signal alive (anti-collapse)."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE", "0.1"))))
    except ValueError:
        return 0.1


@dataclass
class CriticVerdict:
    """The critic's pre-flight judgment on a candidate."""
    failure_probability: Optional[float]   # None → critic unavailable / inert
    short_circuit: bool                    # True → skip sandbox, route back to generation
    constraint_clause: str = ""            # prompt constraint injected on short-circuit
    reason: str = ""


class PreflightCritic:
    """Local pre-flight critic gate. Injectable inference fn (production = Reactor model_server)."""

    def __init__(self, infer: Any = None, *, sampler: Any = None) -> None:
        # infer: async callable(candidate_source, context) -> Optional[float] (failure prob)
        self._infer = infer
        # sampler: callable() -> float in [0,1) for the anti-collapse always-sandbox draw
        self._sampler = sampler

    async def predict_failure(self, candidate_source: str, context: str = "") -> Optional[float]:
        """Phase 2: predicted failure probability for *candidate_source*. None when no critic model is
        available (inert). Fail-soft."""
        if not candidate_source:
            return None
        infer = self._infer
        if infer is None:
            infer = await self._resolve_reactor_infer()
        if infer is None:
            return None  # no served critic model yet → inert
        try:
            import asyncio
            res = infer(candidate_source, context)
            prob = await res if asyncio.iscoroutine(res) else res
            if prob is None:
                return None
            return min(1.0, max(0.0, float(prob)))
        except Exception as exc:  # noqa: BLE001 — critic is advisory; never break the loop
            logger.debug("[PreflightCritic] inference failed (non-fatal): %s", exc)
            return None

    async def _resolve_reactor_infer(self) -> Any:
        """Resolve the Reactor-served critic inference callable. Returns None when unavailable —
        which is the expected state until a critic model has been trained + served (honest inert)."""
        try:
            from backend.clients.reactor_core_client import ReactorCoreClient, ReactorCoreConfig
            client = ReactorCoreClient(ReactorCoreConfig())
            critic_infer = getattr(client, "critic_infer", None)  # not present until model is served
            if critic_infer is None:
                return None
            self._infer = critic_infer
            return critic_infer
        except Exception:  # noqa: BLE001
            return None

    async def evaluate(self, candidate_source: str, context: str = "") -> CriticVerdict:
        """Phase 3: predict + gate. Short-circuits (skip sandbox → regenerate) only when the critic is
        confident the candidate will fail AND this candidate wasn't drawn into the anti-collapse
        always-sandbox sample. Inert (never short-circuits) when disabled / no model."""
        if not critic_enabled():
            return CriticVerdict(failure_probability=None, short_circuit=False, reason="disabled")
        prob = await self.predict_failure(candidate_source, context)
        if prob is None:
            return CriticVerdict(failure_probability=None, short_circuit=False, reason="no_critic_model")
        if prob < _fail_threshold():
            return CriticVerdict(failure_probability=prob, short_circuit=False,
                                 reason=f"below_threshold({prob:.2f})")
        # Anti-collapse: always let a sampled fraction reach the real sandbox even if the critic
        # predicts failure — preserves the empirical signal that would retrain/calibrate the critic.
        draw = self._sampler() if self._sampler is not None else _deterministic_draw(candidate_source)
        if draw < _sample_rate():
            return CriticVerdict(failure_probability=prob, short_circuit=False,
                                 reason=f"anti_collapse_sampled({prob:.2f})")
        clause = (
            "## PRE-FLIGHT CRITIC CONSTRAINT\n"
            f"A local critic predicts this candidate is very likely to FAIL validation "
            f"(p_fail={prob:.0%}). Do NOT resubmit it as-is. Reconsider the approach — common "
            "structural failure modes to avoid: malformed/incomplete output, wrong response schema "
            "(emit a valid 2b.1-diff), and breaking the failing test's contract. Produce a "
            "materially different candidate."
        )
        return CriticVerdict(failure_probability=prob, short_circuit=True, constraint_clause=clause,
                             reason=f"short_circuit(p_fail={prob:.2f})")


def _deterministic_draw(s: str) -> float:
    """Deterministic [0,1) draw from the candidate text (no RNG — auditable, reproducible)."""
    import hashlib
    h = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()
    return (int(h[:8], 16) % 10000) / 10000.0
