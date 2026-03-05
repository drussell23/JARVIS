"""Base classes and shared types for immune-tier services.

These are copied from unified_supervisor.py so that each service module can
be imported independently without pulling in the 73K-line monolith.

The canonical definitions remain in unified_supervisor.py; these are
governance-layer duplicates kept in sync by convention.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Stub for SystemKernelConfig so service constructors that accept it can be
# instantiated without importing the full monolith.  At runtime the real
# config is passed; this stub satisfies static analysers & test harnesses.
# ---------------------------------------------------------------------------

class SystemKernelConfig:
    """Minimal stub matching the constructor-visible surface of the real config.

    The real class lives in unified_supervisor.py.  Immune-tier constructors
    only *store* the config reference — they never introspect it during
    __init__ — so a bare object is sufficient for import-time safety.
    """
    pass


# ---------------------------------------------------------------------------
# ServiceHealthReport
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceHealthReport:
    """Structured health report from a governed service organ."""
    alive: bool
    ready: bool
    degraded: bool = False
    draining: bool = False
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# CapabilityContract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityContract:
    """Formal declaration of what a service does."""
    name: str
    version: str
    inputs: List[str]
    outputs: List[str]
    side_effects: List[str]
    idempotent: bool = True
    cross_repo: bool = False


# ---------------------------------------------------------------------------
# SystemService ABC
# ---------------------------------------------------------------------------

class SystemService(ABC):
    """Uniform lifecycle contract for system services.

    Declared before service subclasses so class inheritance resolution is
    deterministic at import time.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Set up resources. Called once during activation."""

    @abstractmethod
    async def health_check(self) -> Tuple[bool, str]:
        """Return (healthy, message). Called periodically by registries."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Release resources. Called during shutdown."""

    # --- v2 lifecycle (new, with backward-compatible defaults) ---

    async def start(self) -> bool:
        """Begin active operation. Called after initialize succeeds.
        Default: returns True (no-op for legacy services)."""
        return True

    async def health(self) -> ServiceHealthReport:
        """Return structured health report.
        Default: wraps legacy health_check() into a ServiceHealthReport."""
        try:
            ok, msg = await self.health_check()
            return ServiceHealthReport(alive=True, ready=ok, message=msg)
        except Exception as exc:
            return ServiceHealthReport(alive=True, ready=False, message=str(exc))

    async def drain(self, deadline_s: float) -> bool:
        """Stop accepting new work, flush in-flight ops before deadline.
        Default: returns True (nothing to drain for legacy services)."""
        return True

    async def stop(self) -> None:
        """Release resources. Must be safe to call multiple times.
        Default: delegates to cleanup()."""
        await self.cleanup()

    # --- v2 capability declaration (new, with defaults) ---

    def capability_contract(self) -> CapabilityContract:
        """Declare inputs, outputs, side effects, idempotency.
        Default: returns a stub contract with the class name."""
        return CapabilityContract(
            name=type(self).__name__,
            version="0.0.0",
            inputs=[],
            outputs=[],
            side_effects=[],
        )

    def activation_triggers(self) -> List[str]:
        """Return list of event topics that should activate this service.
        Empty list = always_on (activated at boot).
        Default: returns [] (always_on behavior)."""
        return []
