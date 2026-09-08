"""Lifecycle ordering: nothing may read the environment before it exists.

The concurrency clamp in `local_lane_capacity` was correct, tested, and
completely inert. It derived the lane from `JARVIS_LOCAL_PRIME_ENABLED`, and it
ran during boot BEFORE `.env` was loaded — so the flag read as unset, the lane
resolved as CLOUD, and the background pool came up at `pool_size=6` on a single
GPU. Every test passed. The evidence that it was dead was one line in a session
log (`pool_size=6`, and no `LocalLaneCapacity` line at all).

That is not a bug in the clamp. It is a missing ORDER: a component derived a
decision from configuration that had not been hydrated yet, and nothing in the
system was able to notice. This module makes that noticing structural.

## The contract

    load_env_once()          # the existing loader — not reimplemented here
    mark_hydrated()          # exactly one caller, at the boot seam
    ...
    require_hydrated("x")    # every component that derives from the env

`require_hydrated` answers False and records a `ConfigHydrationFault` when
called too early. The CALLER decides what that means, because the right
response differs: a capacity resolver should return its fail-safe value (a
small lane), while the boot sequence should refuse to continue at all. A module
that made that choice for both would be wrong for one of them.

## Why a phase marker and not "just check the variable"

Checking `os.environ.get(...)` cannot distinguish "the operator set this to
false" from "nobody has loaded the file yet". Those demand opposite responses —
honour it, versus refuse to decide — and conflating them is exactly how a
default-shaped value ("unset means cloud") silently became a decision nobody
made. The marker is the only thing that can tell them apart.

## Fail-closed, and honest about what it can prove

This enforces ORDER, not correctness: it cannot know whether the values loaded
are the right ones. What it guarantees is that no capacity or capability
decision is taken against an environment that has not been populated, and that
a violation is loud, attributed, and recorded rather than silently absorbed.
NEVER raises from the query path — only `assert_ready` raises, and only when a
caller explicitly asks for the fatal form.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.InitGuard")

__all__ = [
    "ConfigHydrationFault",
    "HydrationState",
    "assert_ready",
    "faults",
    "is_hydrated",
    "mark_hydrated",
    "require_hydrated",
    "reset_for_tests",
    "validate_required",
]


class ConfigHydrationFault(RuntimeError):
    """Raised only by :func:`assert_ready` — a boot that cannot be trusted."""


@dataclass(frozen=True)
class HydrationState:
    """What happened, for the operator and for the ledger."""

    component: str
    reason: str
    at_monotonic: float = field(default_factory=time.monotonic)

    def render(self) -> str:
        return f"ConfigHydrationFault component={self.component} reason={self.reason}"


_lock = threading.Lock()
_hydrated_at: Optional[float] = None
_faults: List[HydrationState] = []


def mark_hydrated() -> None:
    """Declare the environment fully loaded. Idempotent. NEVER raises.

    Exactly one caller: the boot seam, immediately after the existing
    `load_env_once`. Marking it anywhere else would make the guarantee a
    formality.
    """
    global _hydrated_at  # noqa: PLW0603
    with _lock:
        if _hydrated_at is None:
            _hydrated_at = time.monotonic()
            logger.info("[InitGuard] environment hydrated — capacity decisions unlocked")


def is_hydrated() -> bool:
    with _lock:
        return _hydrated_at is not None


def faults() -> Tuple[HydrationState, ...]:
    with _lock:
        return tuple(_faults)


def _record(component: str, reason: str) -> HydrationState:
    fault = HydrationState(component=component, reason=reason)
    with _lock:
        _faults.append(fault)
    logger.warning("[InitGuard] %s", fault.render())
    _to_ledger(fault)
    return fault


def _to_ledger(fault: HydrationState) -> None:
    """Record the fault where the operator already looks. NEVER raises.

    Composed onto the same `OperationLedger` every phase writes to, under a
    synthetic boot op id — a fault that only exists in stdout is a fault that
    is gone by the time anybody asks what happened.
    """
    try:
        import asyncio
        from pathlib import Path

        from backend.core.ouroboros.governance.ledger import (
            LedgerEntry, OperationLedger, OperationState,
        )

        storage = Path(os.environ.get(
            "OUROBOROS_LEDGER_DIR",
            str(Path.home() / ".jarvis" / "ouroboros" / "ledger"),
        ))
        entry = LedgerEntry(
            op_id="op-boot-config-hydration",
            state=OperationState.BLOCKED,
            data={
                "reason": "config_hydration_fault",
                "component": fault.component,
                "detail": fault.reason,
            },
            entry_id=f"hydration:{fault.component}",
        )
        coro = OperationLedger(storage_dir=storage).append(entry)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        loop.create_task(coro)
    except Exception:  # noqa: BLE001 — bookkeeping never blocks a refusal
        logger.debug("[InitGuard] fault not written to the ledger", exc_info=True)


def require_hydrated(component: str) -> bool:
    """True when *component* may derive decisions from the environment.

    False — plus a recorded fault — when called before hydration. The caller
    chooses the response; a capacity resolver should take its FAIL-SAFE branch,
    which on a single-GPU host means the smallest lane, not the largest.
    NEVER raises.
    """
    if is_hydrated():
        return True
    _record(component, "read the environment before load_env_once/mark_hydrated")
    return False


def validate_required(names: Sequence[str], *, component: str = "boot") -> Tuple[str, ...]:
    """Names that are required but absent/empty. NEVER raises.

    Kept separate from ordering on purpose: "loaded too early" and "loaded but
    missing a value" are different faults with different fixes, and a single
    boolean would hide which one occurred.
    """
    missing: List[str] = []
    for name in names or ():
        try:
            if not (os.environ.get(str(name), "") or "").strip():
                missing.append(str(name))
        except Exception:  # noqa: BLE001
            missing.append(str(name))
    if missing:
        _record(component, f"missing required config: {', '.join(missing)}")
    return tuple(missing)


def assert_ready(
    *, required: Sequence[str] = (), component: str = "boot",
) -> None:
    """Refuse to continue an indeterminate boot. RAISES by design.

    The one fatal entry point. Use it where continuing would spawn execution
    threads against configuration nobody can vouch for; use
    :func:`require_hydrated` everywhere a safe degraded answer exists.
    """
    if not is_hydrated():
        fault = _record(component, "assert_ready before hydration")
        raise ConfigHydrationFault(fault.render())
    missing = validate_required(required, component=component)
    if missing:
        raise ConfigHydrationFault(
            f"ConfigHydrationFault component={component} "
            f"reason=missing required config: {', '.join(missing)}"
        )


def reset_for_tests() -> None:
    """Test seam — forget hydration and every recorded fault."""
    global _hydrated_at  # noqa: PLW0603
    with _lock:
        _hydrated_at = None
        _faults.clear()
