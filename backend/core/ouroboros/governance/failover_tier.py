"""failover_tier.py -- Adaptive Workload Provisioning tier router.

J-Prime is a TEMPORARY, cost-bounded survival tier that O+V hands back to DW the
moment DW recovers. This router deterministically selects WHICH node to provision
for the outage based on the workload's urgency/complexity:

  * survival (DEFAULT): cheap ``e2-highmem-2`` CPU node + 7B model -- keeps O+V
    alive during ANY outage at ~$0.04/hr Spot.
  * quality (OPT-IN, gated OFF by default): a ``g2-standard`` GPU node + 32B model
    for a high-priority IMMEDIATE / COMPLEX op -- dynamically authorizing the
    higher OPEX ONLY for critical workloads.

THE GPU TIER CAN NEVER SPEND BY ACCIDENT: ``JARVIS_FAILOVER_QUALITY_TIER_ENABLED``
defaults OFF -> ``resolve_tier`` always returns the survival tier. Every spec is
env-driven (machine type / image / model / accelerator) -- zero hardcoding past
the final defaults. Pure + frozen value objects.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def _env(name: str, default: str) -> str:
    return (os.environ.get(name, default) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class FailoverTier:
    """An immutable provisioning spec: which machine, image (== baked model),
    model label, and optional GPU accelerator to awaken for the outage."""

    name: str
    machine_type: str
    image_family: str
    model_label: str
    accelerator_type: str = ""
    accelerator_count: int = 0

    @property
    def is_gpu(self) -> bool:
        return bool(self.accelerator_type and self.accelerator_count > 0)


def quality_tier_enabled() -> bool:
    """Master gate for the GPU/32B quality tier. DEFAULT OFF -- a GPU node is
    NEVER provisioned unless the operator explicitly opts in (cost safety)."""
    val = (os.environ.get("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "false") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _survival_tier() -> FailoverTier:
    return FailoverTier(
        name="survival",
        machine_type=_env("JARVIS_FAILOVER_SURVIVAL_MACHINE", "e2-highmem-2"),
        image_family=_env("JARVIS_FAILOVER_SURVIVAL_IMAGE", "jarvis-prime-coder"),
        model_label=_env("JARVIS_FAILOVER_SURVIVAL_MODEL", "qwen2.5-coder:7b"),
    )


def _quality_tier() -> FailoverTier:
    return FailoverTier(
        name="quality",
        machine_type=_env("JARVIS_FAILOVER_QUALITY_MACHINE", "g2-standard-8"),
        image_family=_env("JARVIS_FAILOVER_QUALITY_IMAGE", "jarvis-prime-coder-32b"),
        model_label=_env("JARVIS_FAILOVER_QUALITY_MODEL", "qwen2.5-coder:32b"),
        accelerator_type=_env("JARVIS_FAILOVER_QUALITY_ACCEL_TYPE", "nvidia-l4"),
        accelerator_count=max(0, _env_int("JARVIS_FAILOVER_QUALITY_ACCEL_COUNT", 1)),
    )


def _is_high_priority(urgency: str, complexity: str) -> bool:
    """A workload warrants the GPU tier iff it is IMMEDIATE urgency OR a COMPLEX /
    heavy-code op. BACKGROUND / STANDARD / simple never do (cost discipline)."""
    u = (urgency or "").strip().lower()
    c = (complexity or "").strip().lower()
    return u in ("immediate",) or c in ("complex", "heavy_code", "heavy")


def resolve_tier(*, urgency: str = "", complexity: str = "") -> FailoverTier:
    """Deterministically pick the provisioning tier for the interrupted workload.
    Quality (GPU/32B) ONLY when the master gate is ON AND the op is high-priority;
    otherwise the cost-optimized survival tier. NEVER raises."""
    try:
        if quality_tier_enabled() and _is_high_priority(urgency, complexity):
            return _quality_tier()
    except Exception:  # noqa: BLE001 -- any error -> safe survival default
        pass
    return _survival_tier()


def model_param_billions(model_label: str) -> float:
    """Parse the parameter size (in billions) from a model label, e.g.
    ``qwen2.5-coder:32b-instruct`` -> 32.0. Unknown -> 0.0. NEVER raises."""
    try:
        m = _PARAM_RE.search(str(model_label or ""))
        return float(m.group(1)) if m else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def is_small_model(model_label: str, *, threshold_b: float = 0.0) -> bool:
    """True iff the model is 'small' enough to warrant aggressive cognitive
    compaction (<= the threshold, env ``JARVIS_VENOM_COMPACT_MAX_MODEL_B`` default
    14B). An UNKNOWN size (0.0) is treated as small (conservative -> compact).
    A large model (e.g. 32B GPU) is NOT small -> it gets the full schema."""
    if threshold_b <= 0:
        threshold_b = float(_env("JARVIS_VENOM_COMPACT_MAX_MODEL_B", "14") or 14)
    b = model_param_billions(model_label)
    return b == 0.0 or b <= threshold_b


__all__ = [
    "FailoverTier", "resolve_tier", "quality_tier_enabled",
    "model_param_billions", "is_small_model",
]
