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

# VRAM (GiB) per GCP accelerator type -- the physical ceiling the Context-Hardware
# Negotiator derives the safe num_ctx from. Descriptive hardware facts, NOT a
# tunable cap; an operator may override the resolved value with JARVIS_GPU_VRAM_GIB.
_GPU_VRAM_GIB = {
    "nvidia-l4": 24,
    "nvidia-tesla-t4": 16,
    "nvidia-t4": 16,
    "nvidia-tesla-p100": 16,
    "nvidia-tesla-v100": 16,
    "nvidia-tesla-a100": 40,
    "nvidia-a100": 40,
    "nvidia-a100-80gb": 80,
    "nvidia-h100": 80,
    "nvidia-h100-80gb": 80,
    "nvidia-h100-mega-80gb": 80,
}


def accelerator_vram_bytes(accel_type: str) -> int:
    """Physical VRAM (bytes) for a GCP accelerator type. ``JARVIS_GPU_VRAM_GIB``
    force-overrides the lookup (any node); an unknown type returns 0 so the caller
    floors the context window rather than guessing. NEVER raises."""
    try:
        forced = (os.environ.get("JARVIS_GPU_VRAM_GIB", "") or "").strip()
        if forced:
            return int(float(forced) * (1024 ** 3))
        gib = _GPU_VRAM_GIB.get((accel_type or "").strip().lower(), 0)
        return int(gib * (1024 ** 3))
    except Exception:  # noqa: BLE001 -- descriptive helper must never raise
        return 0


class HardwareProvisioningMismatchError(RuntimeError):
    """Raised by the RAM Pre-Flight Gate when a machine type's system RAM cannot
    physically hold the model + OS overhead -- so the impossible ``instances.insert``
    is NEVER attempted (the g2-standard-4/16GB vs 19.85GB-model kernel OOM class)."""


# System RAM (MB) for machine types we provision. Descriptive hardware facts. The
# g2-standard-N family derives N*4GiB by pattern; this map covers the survival
# tier + pins the common g2 sizes. Override any type via
# ``JARVIS_MACHINE_RAM_MB_<TYPE>`` (dashes -> underscores, upper-cased).
_STATIC_MACHINE_RAM_MB = {
    "e2-highmem-2": 16384,
    "e2-highmem-4": 32768,
    "e2-highmem-8": 65536,
    "g2-standard-4": 16384,
    "g2-standard-8": 32768,
    "g2-standard-12": 49152,
    "g2-standard-16": 65536,
    "g2-standard-24": 98304,
    "g2-standard-32": 131072,
    "g2-standard-48": 196608,
    "g2-standard-96": 393216,
}

_G2_STANDARD_RE = re.compile(r"^g2-standard-(\d+)$")


def machine_type_ram_bytes(machine_type: str) -> int:
    """System RAM (bytes) for a GCP machine type. Resolution order: env override
    (``JARVIS_MACHINE_RAM_MB_<TYPE>``) -> static map -> g2-standard-N pattern
    (N*4GiB). 0 when undeterminable (the gate then fails OPEN). NEVER raises."""
    t = (machine_type or "").strip().lower()
    if not t:
        return 0
    try:
        env = os.environ.get("JARVIS_MACHINE_RAM_MB_" + t.upper().replace("-", "_"), "").strip()
        if env:
            return int(float(env) * 1024 * 1024)
        mb = _STATIC_MACHINE_RAM_MB.get(t)
        if mb:
            return int(mb) * 1024 * 1024
        m = _G2_STANDARD_RE.match(t)
        if m:
            return int(m.group(1)) * 4 * (1024 ** 3)
        return 0
    except Exception:  # noqa: BLE001
        return 0


def estimate_gguf_bytes(model_label: str) -> int:
    """Estimate a model's on-disk GGUF size (bytes) from its parameter count:
    ``params_B * 1e9 * JARVIS_GGUF_BYTES_PER_PARAM`` (default 0.62, ~q4_K_M -- this
    yields ~19.8GB for a 32B, matching the observed 19.85GB /api/tags size).
    ``JARVIS_FAILOVER_QUALITY_MODEL_BYTES`` force-overrides. 0 if the label has no
    parseable param count. NEVER raises."""
    try:
        forced = (os.environ.get("JARVIS_FAILOVER_QUALITY_MODEL_BYTES", "") or "").strip()
        if forced:
            return int(forced)
        billions = model_param_billions(model_label)
        if billions <= 0:
            return 0
        bpp = float(os.environ.get("JARVIS_GGUF_BYTES_PER_PARAM", "0.62"))
        return int(billions * 1e9 * bpp)
    except Exception:  # noqa: BLE001
        return 0


def assert_host_ram_fits_model(
    machine_type: str,
    model_label: str,
    *,
    overhead_bytes: "int | None" = None,
) -> None:
    """RAM Pre-Flight Gate: assert the machine's system RAM strictly exceeds the
    model's GGUF size + OS overhead (``JARVIS_HOST_RAM_OVERHEAD_BYTES``, default
    4GiB), BEFORE ``instances.insert``. Raises :class:`HardwareProvisioningMismatchError`
    on a certain mismatch (the kernel-OOM-at-load class). Fails OPEN when RAM or
    GGUF size is undeterminable -- block only when CERTAIN the load is impossible."""
    ram = machine_type_ram_bytes(machine_type)
    gguf = estimate_gguf_bytes(model_label)
    if ram <= 0 or gguf <= 0:
        return  # undeterminable -> do not block on ignorance
    ovh = overhead_bytes if overhead_bytes is not None else _env_int(
        "JARVIS_HOST_RAM_OVERHEAD_BYTES", 4 * (1024 ** 3))
    required = gguf + max(0, ovh)
    if ram <= required:
        raise HardwareProvisioningMismatchError(
            "host RAM %d bytes (%s) <= model GGUF %d + overhead %d = %d required -- "
            "refusing to provision an impossible load (kernel OOM-at-load class)"
            % (ram, machine_type, gguf, ovh, required)
        )


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


def quality_tier() -> FailoverTier:
    """The QUALITY (GPU/32B) provisioning spec, resolved UNCONDITIONALLY -- the
    single source of truth for what the golden image must contain.

    Deliberately bypasses ``quality_tier_enabled()``: that cost gate governs
    whether to PROVISION a GPU node at RUNTIME, not what to BAKE ahead of time. The
    image is manufactured before any outage; the baker must always produce the 32B
    image the provisioner WILL request once the gate is opened. Routing the baker
    through ``resolve_tier`` would (gate-off, the default) bake the 7B SURVIVAL
    image -- a silent drift bug. This accessor is that drift's structural cure: the
    baker, the bake CLI, and the Packer default all derive from HERE. NEVER raises.
    """
    return _quality_tier()


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


def op_exceeds_small_capacity(*, estimated_tokens: int) -> bool:
    """Predictable cost-aware gate: True iff the op's estimated token budget
    exceeds the small (7B) model's working capacity (env
    ``JARVIS_FAILOVER_7B_TOKEN_CAPACITY`` default 24000 -- headroom under the 32K
    window for the generation). 0/unknown -> False (never forces GPU on no info).
    NEVER raises."""
    try:
        n = int(estimated_tokens or 0)
        if n <= 0:
            return False
        return n > _env_int("JARVIS_FAILOVER_7B_TOKEN_CAPACITY", 24000)
    except Exception:  # noqa: BLE001
        return False


def resolve_tier_for_op(
    *, urgency: str = "", complexity: str = "", estimated_tokens: int = 0,
) -> FailoverTier:
    """Tier selection with the cost-aware gate folded in. The quality (GPU/32B)
    tier is selected when the master gate is ON AND EITHER the op is high-priority
    (urgency/complexity) OR its token budget mathematically overflows the 7B's
    capacity (a strict guarantee -- the 7B literally cannot fit the context).
    The master cost gate is absolute: disabled -> survival even on overflow
    (degraded best-effort, never silent GPU spend). NEVER raises."""
    try:
        if quality_tier_enabled() and (
            _is_high_priority(urgency, complexity)
            or op_exceeds_small_capacity(estimated_tokens=estimated_tokens)
        ):
            return _quality_tier()
    except Exception:  # noqa: BLE001
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
    "FailoverTier", "resolve_tier", "resolve_tier_for_op", "quality_tier_enabled",
    "op_exceeds_small_capacity", "model_param_billions", "is_small_model",
]
