"""Do not start a generation while a model hot-swap is landing.

The other half of the GPU exclusion. Reactor's ``deployment/gpu_lease.py``
stops a deploy from starting while another leased job holds the card;
this stops a generation from starting while a deploy holds it. Without
both halves the protection is one-sided: ``ollama create`` can replace
the blob under a model that a live O+V generation is streaming from.

## Why this CHECKS the lock instead of acquiring it

The Trinity lock is exclusive-only. If every generation acquired it,
generations would serialise against each other -- turning a shared
resource into a queue and distorting the very throughput the local lane
exists to provide. What is actually wanted is a reader-writer lock:
generations shared, deployment exclusive. The file protocol does not
offer one, so this asks whether the writer holds it and parks if so.

**The residual race is real and deliberate**: between the check and the
dispatch, a deploy can still acquire. That window is milliseconds against
a deploy that takes minutes, and closing it properly means building an RW
lock over the file protocol -- worth doing, not worth blocking on.

## Polarity is the OPPOSITE of the reactor side, on purpose

``gpu_lease`` defers unless proven free: a false "free" starts a training
job that OOMs a live soak. This gate proceeds unless proven blocked: a
false "blocked" would halt the organism every time the lock subsystem
hiccups. The costs are asymmetric, so the safe defaults point in opposite
directions. Fail-open here is not laziness; it is the cheaper failure.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

GPU_DEPLOYMENT_GATE_SCHEMA_VERSION = "gpu_deployment_gate.1"

#: MUST match reactor_core.deployment.gpu_lease.GPU_LOCK_NAME. Two repos
#: cannot import each other, so the lock NAME is the contract -- the same
#: shape as the ExperienceEvent schema agreement.
GPU_LOCK_NAME = "trinity_gpu_vram"

_ENV_MASTER = "JARVIS_GPU_DEPLOYMENT_GATE_ENABLED"
_ENV_LOCK_NAME = "JARVIS_GPU_DEPLOYMENT_LOCK_NAME"

_TRUTHY = ("1", "true", "yes", "on")


def gate_enabled() -> bool:
    """Master flag. Default FALSE per §33.1 (shadow-first)."""
    return os.getenv(_ENV_MASTER, "").strip().lower() in _TRUTHY


def lock_name() -> str:
    return os.getenv(_ENV_LOCK_NAME, "").strip() or GPU_LOCK_NAME


async def deployment_in_progress() -> Tuple[bool, str]:
    """``(blocked, reason)``. NEVER raises.

    Blocked only on POSITIVE evidence: a lock that exists, has not
    expired, is not stale, and whose owner is alive. Anything else --
    flag off, manager unavailable, unreadable lock, dead owner -- is not
    blocked, because an unprovable deployment must not stop the loop.
    """
    if not gate_enabled():
        return False, "gate_disabled"
    try:
        from backend.core.distributed_lock_manager import (  # noqa: PLC0415
            get_lock_manager,
        )
        manager = await get_lock_manager()
        status: Optional[dict] = await manager.get_lock_status(lock_name())
    except Exception as exc:  # noqa: BLE001 -- fail OPEN, see module docstring
        logger.debug("[GPUDeployGate] status probe failed: %s", exc)
        return False, f"probe_failed:{type(exc).__name__}"

    if not status:
        return False, "no_deployment_lock"
    if status.get("is_expired"):
        return False, "lock_expired"
    if status.get("is_stale"):
        return False, "lock_stale"
    if status.get("owner_alive") is False:
        return False, "owner_dead"

    owner = str(status.get("owner", "?"))
    remaining = status.get("time_remaining")
    return True, (
        f"model deployment in progress (owner={owner}, "
        f"{remaining if remaining is None else round(float(remaining), 1)}s "
        "remaining) -- parking this dispatch rather than generating from a "
        "model that is being replaced"
    )


def register_flags(registry: Any) -> int:  # noqa: ANN401
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GPUDeployGate] register_flags degraded: %s", exc)
        return 0
    src = "backend/core/ouroboros/governance/gpu_deployment_gate.py"
    specs = [
        FlagSpec(
            name=_ENV_MASTER, type=FlagType.BOOL, default=False,
            category=Category.SAFETY, source_file=src,
            example=f"{_ENV_MASTER}=true",
            description=(
                "Park a local dispatch while a model hot-swap holds the "
                "Trinity GPU lock. The other half of reactor's gpu_lease: "
                "without it, `ollama create` can replace the blob under a "
                "generation that is streaming from it. Fails OPEN -- an "
                "unprovable deployment never blocks the loop."
            ),
        ),
        FlagSpec(
            name=_ENV_LOCK_NAME, type=FlagType.STR, default=GPU_LOCK_NAME,
            category=Category.INTEGRATION, source_file=src,
            example=f"{_ENV_LOCK_NAME}={GPU_LOCK_NAME}",
            description=(
                "Trinity lock name shared with reactor's "
                "deployment/gpu_lease.GPU_LOCK_NAME. The two repos cannot "
                "import each other, so this NAME is the contract; changing "
                "it on one side alone silently removes the exclusion."
            ),
        ),
    ]
    try:
        registry.bulk_register(specs, override=True)
    except Exception:  # noqa: BLE001
        return 0
    return len(specs)


__all__ = [
    "GPU_DEPLOYMENT_GATE_SCHEMA_VERSION",
    "GPU_LOCK_NAME",
    "deployment_in_progress",
    "gate_enabled",
    "lock_name",
    "register_flags",
]
