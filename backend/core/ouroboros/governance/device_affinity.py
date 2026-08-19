"""Which GPU should this op land on, when the GPUs are not the same size?

THE SITUATION THIS EXISTS FOR
-----------------------------
A 32GB RTX 5090 beside a 24GB card, each hosting its own model as an
INDEPENDENT lane (see PRD §31.4 -- two resident models beats one model spanning
both, because layer-splitting buys capacity at PCIe latency while two lanes buy
throughput at no latency). The moment the lanes are asymmetric, a queue that
treats them as interchangeable is wrong in both directions:

  * a long-context BACKGROUND op sent to the 24GB card OOMs, or silently
    truncates its window -- and a truncated window is a QUALITY failure that
    looks like a model getting dumber, not like an infrastructure error;
  * a trivial SPECULATIVE triage op sent to the 32GB card occupies the only
    device that could have taken the next heavy one.

TWO THINGS THAT MUST NOT BE CONFUSED
------------------------------------
**Capacity is a CONSTRAINT. Affinity is a PREFERENCE.** A preference may be
overridden by a constraint, never the reverse. Concretely: SPECULATIVE prefers
the smaller card, but if the payload does not fit there it goes to the larger
one rather than being deferred -- declining work that a present device could
do would be policy defeating the system it is meant to tune.

WHY THE CONTEXT MATTERS MORE THAN THE WEIGHTS
---------------------------------------------
Weights are constant per model; KV cache is LINEAR IN CONTEXT and is the term
that actually varies per op. Measured on Qwen3.8-27B: 0.5 GiB at 8K, 2.0 GiB
at 32K, 16.4 GiB at 262K -- a straight line at ~62.5 KiB/token, and enough at
full context to be the difference between fitting a 24GB card and not.

So admission has to ask "weights + KV(this context) on THIS device", not
"weights against a pooled number".

Python 3.9+, stdlib only. Every probe is injected or lazily imported and
fail-soft: an unresolvable device list declines to choose rather than guessing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.DeviceAffinity")

ENABLED_ENV = "JARVIS_DEVICE_AFFINITY_ENABLED"
KV_PER_TOKEN_ENV = "JARVIS_KV_BYTES_PER_TOKEN"
KV_PER_MODEL_ENV = "JARVIS_KV_BYTES_PER_TOKEN_BY_MODEL"
GROWTH_ENV = "JARVIS_DEVICE_AFFINITY_CTX_GROWTH"

_TRUTHY = ("1", "true", "yes", "on")

#: Routes whose ops are small, short-lived triage and should stay OFF the
#: largest device so it remains available for work that needs it.
_LIGHT_ROUTES = frozenset({"speculative"})


def affinity_enabled() -> bool:
    """Master gate. Default ON. OFF selects purely on capacity, which is the
    behaviour before this module existed. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def context_growth_factor() -> float:
    """How much larger than the PROMPT the context may become. Default 1.35.

    Load-bearing, and the subtlest number here. Venom is a multi-turn tool
    loop: tool results accumulate into the same window round after round until
    live compaction fires at `JARVIS_TOOL_LOOP_COMPACT_THRESHOLD` (75% of
    budget). Sizing KV against the FIRST prompt therefore admits an op that
    fits at round 1 and OOMs at round 7 -- and it OOMs mid-op, after the
    exploration calls the Iron Gate required have already been spent.

    Admission must size against the ceiling the op is ALLOWED to reach, not
    the value it starts at. Clamped to [1.0, 4.0]."""
    try:
        raw = float(os.environ.get(GROWTH_ENV, "1.35"))
    except (TypeError, ValueError):
        return 1.35
    return max(1.0, min(4.0, raw))


def _kv_overrides() -> Dict[str, int]:
    """``model=bytes_per_token`` pairs, comma separated. NEVER raises.

    KV cost per token is an ARCHITECTURAL property -- layers x kv-heads x
    head-dim x dtype -- and models differ by an order of magnitude (Qwen3.8-27B
    caches only 16 of its 64 layers, so it is far cheaper than its parameter
    count suggests). There is no correct single constant, so measured values
    go here per model and the default below stays deliberately pessimistic.
    """
    out: Dict[str, int] = {}
    try:
        raw = os.environ.get(KV_PER_MODEL_ENV, "") or ""
        for pair in raw.split(","):
            if "=" not in pair:
                continue
            name, _, val = pair.partition("=")
            name = name.strip()
            if name:
                out[name] = max(0, int(float(val.strip())))
    except Exception:  # noqa: BLE001
        return out
    return out


def kv_bytes_per_token(model_id: Optional[str] = None) -> int:
    """Bytes of KV cache per context token for *model_id*. Default 131072.

    The default (128 KiB/token) is an UPPER BOUND for a 30B-class model with
    full attention at fp16, chosen pessimistically on purpose: over-estimating
    KV defers an op that might have fit, while under-estimating it admits one
    that cannot, and the second failure lands mid-generation on the device.
    Measured per-model values via `JARVIS_KV_BYTES_PER_TOKEN_BY_MODEL` should
    replace it -- e.g. Qwen3.8-27B measures ~64000.
    """
    overrides = _kv_overrides()
    if model_id and model_id in overrides:
        return overrides[model_id]
    try:
        return max(0, int(float(os.environ.get(KV_PER_TOKEN_ENV, "131072"))))
    except (TypeError, ValueError):
        return 131072


def estimate_kv_bytes(ctx_tokens: int, *, model_id: Optional[str] = None,
                      grow: bool = True) -> int:
    """KV cache for *ctx_tokens*, optionally grown to the reachable ceiling.

    Linear in context -- see the module docstring for the measurement that
    establishes that. NEVER raises.
    """
    try:
        tokens = max(0, int(ctx_tokens))
    except (TypeError, ValueError):
        return 0
    if grow:
        tokens = int(tokens * context_growth_factor())
    return tokens * kv_bytes_per_token(model_id)


def max_ctx_tokens_for(free_bytes: int, weight_bytes: int, *,
                       model_id: Optional[str] = None) -> int:
    """Largest context that fits in *free_bytes* after the weights.

    Returned on a DEFER so the decision is actionable: "this will not fit" is
    a fact, "the largest context that would fit here is N" is a next step.
    NEVER raises.
    """
    per_token = kv_bytes_per_token(model_id)
    if per_token <= 0:
        return 0
    headroom = int(free_bytes) - int(weight_bytes)
    if headroom <= 0:
        return 0
    growth = context_growth_factor() or 1.0
    return max(0, int(headroom / per_token / growth))


@dataclass(frozen=True)
class DeviceSelection:
    """Which device an op should land on, and why."""

    #: The chosen device, or None when NOTHING can hold this payload.
    device: Optional[Any]
    reason: str
    kv_bytes: int = 0
    required_bytes: int = 0
    #: Largest context that WOULD fit on the best available device. Populated
    #: on failure so the caller can DEFER with a number instead of a shrug.
    max_ctx_tokens: int = 0
    #: True when the route's preferred device could not hold the payload and
    #: capacity overruled policy. Not an error -- but worth seeing, because a
    #: SPECULATIVE op repeatedly displacing heavy work off the big card is a
    #: sizing problem, not a routing one.
    fallback_from_preference: bool = False
    considered: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def admitted(self) -> bool:
        return self.device is not None

    def to_dict(self) -> Dict[str, Any]:
        dev = self.device
        return {
            "admitted": self.admitted,
            "device_index": getattr(dev, "index", None),
            "device_name": getattr(dev, "name", None),
            "device_uuid": getattr(dev, "uuid", None),
            "reason": self.reason,
            "kv_bytes": self.kv_bytes,
            "required_bytes": self.required_bytes,
            "max_ctx_tokens": self.max_ctx_tokens,
            "fallback_from_preference": self.fallback_from_preference,
            "considered": list(self.considered),
        }


def _free_of(dev: Any) -> int:
    try:
        return int(getattr(dev, "free_bytes", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def select_device(
    devices: Sequence[Any],
    *,
    ctx_tokens: int,
    weight_bytes: int = 0,
    route: Optional[str] = None,
    model_id: Optional[str] = None,
) -> DeviceSelection:
    """Pick the device this op should run on. NEVER raises.

    EDGE CASES, and the reasoning for each:

    1. **No devices enumerated.** Declines (`device=None`,
       ``reason="not_enumerated"``) and reports ``max_ctx_tokens=0``. The
       caller must fall back to its existing pooled check -- an empty device
       list means "we could not see", never "there is nothing". Fabricating a
       selection over devices we never read is the failure mode
       `ComputeReading.is_multi_device` already refuses.

    2. **One device.** Affinity is a no-op, but the CAPACITY check still runs.
       A single-GPU host gains the context validation even though it gains no
       routing.

    3. **The preferred device is too small.** Capacity overrules policy: the
       op goes to a device that fits and `fallback_from_preference` is set.
       Deferring work a present device could do would be policy defeating the
       system it exists to tune.

    4. **Nothing fits.** `device=None`, and `max_ctx_tokens` is computed from
       the roomiest device so the caller can DEFER with an actionable number
       ("largest context that fits here is 41k") rather than an opaque no.

    5. **Exact ties.** Broken deterministically by device index, never
       arbitrarily. Two identical cards must not flap between ops -- a stable
       assignment keeps each device's model resident instead of thrashing
       loads, which on a cold model costs the ~30s this codebase measured.

    6. **Context unknown or non-positive.** Treated as UNKNOWN, not as zero. A
       zero-KV assumption admits everything, which is the optimistic direction
       and therefore the wrong one; the caller's configured window is the
       honest ceiling and is what should be passed in.

    7. **Context grows mid-op.** Sized against `context_growth_factor()`, not
       the initial prompt -- Venom accumulates tool results until compaction
       fires, so an op that fits at round 1 can OOM at round 7. See that
       function's docstring.
    """
    devs = [d for d in (devices or []) if d is not None]
    if not devs:
        return DeviceSelection(device=None, reason="not_enumerated")

    kv = estimate_kv_bytes(ctx_tokens, model_id=model_id)
    required = max(0, int(weight_bytes)) + kv

    # Stable ordering first: index is the tie-break for every comparison
    # below, so a selection cannot depend on probe enumeration order.
    by_index = sorted(devs, key=lambda d: int(getattr(d, "index", 0) or 0))
    fits = [d for d in by_index if _free_of(d) >= required]
    considered = tuple(
        {
            "index": getattr(d, "index", None),
            "name": getattr(d, "name", None),
            "free_bytes": _free_of(d),
            "fits": _free_of(d) >= required,
        }
        for d in by_index
    )

    if not fits:
        # Case 4: report the ceiling of the roomiest device, so the caller's
        # DEFER carries a next step rather than only a refusal.
        best_free = max((_free_of(d) for d in by_index), default=0)
        return DeviceSelection(
            device=None, reason="no_device_fits", kv_bytes=kv,
            required_bytes=required, considered=considered,
            max_ctx_tokens=max_ctx_tokens_for(
                best_free, weight_bytes, model_id=model_id),
        )

    route_key = str(route or "").strip().lower()
    light = route_key in _LIGHT_ROUTES

    def _ideal(pool):
        """The device this route WOULD pick if capacity were no object."""
        if not route_key:
            return None
        if light:
            return min(pool, key=lambda d: (_free_of(d),
                                            int(getattr(d, "index", 0) or 0)))
        return max(pool, key=lambda d: (_free_of(d),
                                        -int(getattr(d, "index", 0) or 0)))

    ideal = _ideal(by_index)

    # Case 2/3: exactly one device can hold this payload.
    #
    # `fallback_from_preference` is computed HERE too, and that is the whole
    # point of hoisting `ideal`. With exactly two GPUs -- the configuration
    # this module was written for -- "the preferred device does not fit"
    # ALWAYS collapses into this branch, because excluding one of two devices
    # leaves one. An earlier draft only set the flag in the multi-candidate
    # path below, so on a 2-GPU host the flag could never be True and the one
    # event an operator actually wants to see (a SPECULATIVE op displaced onto
    # the big card) was invisible.
    if len(fits) == 1:
        chosen = fits[0]
        return DeviceSelection(
            device=chosen,
            reason="only_candidate", kv_bytes=kv,
            required_bytes=required, considered=considered,
            fallback_from_preference=bool(ideal is not None
                                          and ideal is not chosen),
            max_ctx_tokens=max_ctx_tokens_for(
                _free_of(chosen), weight_bytes, model_id=model_id),
        )

    if not affinity_enabled() or not route_key:
        # Capacity-only: the roomiest device, ties by index. This is also the
        # pre-affinity behaviour, so the master flag is a true rollback.
        chosen = max(fits, key=lambda d: (_free_of(d),
                                          -int(getattr(d, "index", 0) or 0)))
        return DeviceSelection(
            device=chosen, reason="most_free", kv_bytes=kv,
            required_bytes=required, considered=considered,
            max_ctx_tokens=max_ctx_tokens_for(
                _free_of(chosen), weight_bytes, model_id=model_id),
        )

    if light:
        # Smallest device that STILL FITS -- deliberately not simply the
        # smallest. Triage work should vacate the big card, but only into a
        # device that can actually hold it (case 3).
        preferred = min(fits, key=lambda d: (_free_of(d),
                                             int(getattr(d, "index", 0) or 0)))
        reason = "light_route_smallest_fit"
    else:
        preferred = max(fits, key=lambda d: (_free_of(d),
                                             -int(getattr(d, "index", 0) or 0)))
        reason = "heavy_route_most_free"

    # `preferred` was chosen from `fits`, so it fits by construction; the flag
    # records whether policy had to yield -- true exactly when the route's
    # ideal device was excluded by capacity above.
    fell_back = ideal is not None and ideal is not preferred

    return DeviceSelection(
        device=preferred, reason=reason, kv_bytes=kv, required_bytes=required,
        considered=considered, fallback_from_preference=fell_back,
        max_ctx_tokens=max_ctx_tokens_for(
            _free_of(preferred), weight_bytes, model_id=model_id),
    )
