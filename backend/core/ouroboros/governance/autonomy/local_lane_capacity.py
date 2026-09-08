"""How many generations one local GPU can actually serve at once.

`CandidateGenerator(primary_concurrency=4)` is right for a paid lane: four
requests to a hosted endpoint are four requests to somebody else's fleet. It is
wrong — structurally, not by tuning — when the primary lane is a single local
GPU serving one 30B model, because those four requests contend for ONE card.

Measured on this host, 2026-09-08: four ops entered GENERATE together, each
negotiating a 32k context against the 30B, and every stream returned
`tokens=0 first_token_ms=-1 tps=0.0`. The orchestrator recorded them as
`no_candidates_returned` → `generation_failed`, which reads like a model
quality problem and is not one. Thirteen of thirty ops in that session died
this way. The model never got the chance to be wrong; it never ran.

## The derivation

Weights load ONCE — Ollama keeps a single copy resident — so concurrency is not
"how many models fit". What each additional in-flight request needs is its own
KV cache, and that is what exhausts the card:

    headroom   = vram_bytes - model_bytes          (what the weights leave)
    per_stream = headroom * _KV_FRACTION           (one request's KV budget)
    concurrency = 1 + floor(headroom / per_stream) - 1, bounded

Both inputs are already computed by the Context-Hardware Negotiator
(`_awakened_vram_bytes`, the served-model byte size), which prefers a MEASURED
reading of the card over a provisioning guess — the same correction that stopped
a 32 GiB card being sized as an `nvidia-l4` 24 GiB. Composing them means this
module and the negotiator cannot disagree about the hardware.

## Fail-safe direction

When the signals are unreadable the answer is **1**, not the cloud default. The
failure being prevented is over-subscription, so an unknown card must not be
assumed roomy. One generation at a time is slower; four that all return zero
tokens is not faster, it is nothing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("Ouroboros.LocalLaneCapacity")

__all__ = [
    "LaneCapacity",
    "local_lane_is_primary",
    "resolve_primary_concurrency",
]

#: Share of post-weights VRAM one in-flight request may claim for its KV cache.
#: Not a tuning knob so much as a statement that a stream needs a MEANINGFUL
#: slice: too small and we claim a capacity the card cannot honour, which is
#: the bug. Env-overridable like every other coefficient here.
_ENV_KV_FRACTION = "JARVIS_LOCAL_KV_FRACTION"
_ENV_MAX_CONCURRENCY = "JARVIS_LOCAL_MAX_CONCURRENCY"
_ENV_FORCE = "JARVIS_LOCAL_PRIMARY_CONCURRENCY"


def _env_float(name: str, default: float, *, minimum: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return max(minimum, float(raw) if raw else default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return max(minimum, int(raw) if raw else default)
    except (TypeError, ValueError):
        return default


def local_lane_is_primary() -> bool:
    """True when generation is served by the local engine. NEVER raises.

    Composed from `local_prime_enabled` — the same predicate the generator
    logs as "local-primary: no paid lane is configured" — rather than a second
    reading of the environment that could disagree with it.
    """
    try:
        from backend.core.ouroboros.governance.local_inference_director import (
            local_prime_enabled,
        )
        return bool(local_prime_enabled())
    except Exception:  # noqa: BLE001
        # Unknown lane: assume local, because the cost of wrongly assuming
        # CLOUD is the over-subscription this module exists to prevent.
        return True


@dataclass(frozen=True)
class LaneCapacity:
    """What the card can serve, and how that was decided."""

    concurrency: int
    basis: str
    vram_bytes: int = 0
    model_bytes: int = 0

    def render(self) -> str:
        gib = 1024 ** 3
        return (
            f"local lane concurrency={self.concurrency} basis={self.basis} "
            f"vram={self.vram_bytes / gib:.1f}GiB "
            f"model={self.model_bytes / gib:.1f}GiB"
        )


def resolve_primary_concurrency(
    cloud_default: int = 4, *, endpoint: str = "",
) -> LaneCapacity:
    """Concurrency for the PRIMARY generation lane. NEVER raises.

    Returns *cloud_default* untouched when the paid lane is primary — this
    changes nothing for a hosted fleet.
    """
    try:
        # Ordering first. This resolver reading `JARVIS_LOCAL_PRIME_ENABLED`
        # before `.env` had loaded is what made the clamp inert: the flag came
        # back unset, the lane resolved as CLOUD, and a single GPU was given a
        # six-worker pool. "Unset" and "the operator set it to false" demand
        # opposite answers, and only the guard can tell them apart.
        #
        # Fail-safe direction on a violation: the SMALLEST lane. An
        # unanswerable question about capacity must not resolve to "plenty".
        from backend.core.ouroboros.governance.init_guard import require_hydrated
        if not require_hydrated("local_lane_capacity"):
            return LaneCapacity(1, "unhydrated_fail_safe")

        forced = _env_int(_ENV_FORCE, 0, minimum=0)
        if forced:
            return LaneCapacity(forced, "operator_override")

        if not local_lane_is_primary():
            return LaneCapacity(max(1, int(cloud_default)), "cloud_lane")

        vram = 0
        model = 0
        try:
            from backend.core.ouroboros.governance.candidate_generator import (
                _awakened_vram_bytes,
            )
            vram = int(_awakened_vram_bytes() or 0)
        except Exception:  # noqa: BLE001
            vram = 0
        try:
            # Read the negotiator's MEMOISED size rather than re-fetching it.
            # `_resolve_served_model_bytes` is async and this resolver is not,
            # but the negotiator has already populated the cache by the time
            # capacity matters — and a second fetch would be a second answer
            # to a question the negotiator has already asked the endpoint.
            from backend.core.ouroboros.governance.candidate_generator import (
                _JPRIME_SERVED_BYTES_CACHE,
            )
            if endpoint:
                model = int(_JPRIME_SERVED_BYTES_CACHE.get(endpoint, 0) or 0)
            elif len(_JPRIME_SERVED_BYTES_CACHE) == 1:
                # One endpoint served: it is unambiguously the local one.
                model = int(next(iter(_JPRIME_SERVED_BYTES_CACHE.values())) or 0)
            else:
                model = 0
        except Exception:  # noqa: BLE001
            model = 0

        ceiling = _env_int(_ENV_MAX_CONCURRENCY, max(1, int(cloud_default)))

        if vram <= 0 or model <= 0 or model >= vram:
            # Unknown or fully-consumed card. ONE — the failure being
            # prevented is over-subscription, so an unreadable card must not
            # be assumed roomy.
            return LaneCapacity(1, "unmeasured_fail_safe", vram, model)

        headroom = vram - model
        fraction = _env_float(_ENV_KV_FRACTION, 0.35, minimum=0.05)
        per_stream = max(1.0, headroom * fraction)
        concurrency = int(headroom // per_stream)
        concurrency = max(1, min(ceiling, concurrency))
        cap = LaneCapacity(concurrency, "derived_from_vram", vram, model)
        logger.info("[LocalLaneCapacity] %s", cap.render())
        return cap
    except Exception as exc:  # noqa: BLE001
        logger.debug("[LocalLaneCapacity] degraded: %r", exc)
        return LaneCapacity(1, "degraded_fail_safe")
