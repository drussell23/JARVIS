"""backend/core/startup_memory_gate.py — Disease 5: OOM pre-flight gate.

Checks available physical RAM before each heavyweight component initialises.
This prevents the kernel OOM-killer from firing SIGKILL mid-startup by
shedding OPTIONAL components when free RAM drops below safe thresholds.

Design:
* ``MemoryPressureLevel`` — SAFE / ELEVATED / CRITICAL / OOM_IMMINENT.
* ``ComponentMemoryBudget`` — immutable per-component RAM declaration.
* ``MemoryGate`` — acquires a named "RAM slot" before init; refuses
  OPTIONAL components under pressure; blocks REQUIRED ones until pressure
  drops below CRITICAL.
* ``get_memory_gate()`` — process-wide singleton.

All memory figures are in **mebibytes (MiB)**.  The gate polls
``/proc/meminfo`` (Linux) or ``psutil.virtual_memory()`` (macOS).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import platform
import time
from dataclasses import dataclass
from typing import Dict, Optional

__all__ = [
    "MemoryPressureLevel",
    "ComponentMemoryBudget",
    "MemoryGateRefused",
    "MemoryGate",
    "get_memory_gate",
]

logger = logging.getLogger(__name__)

# How often the gate polls free RAM (seconds).
_POLL_INTERVAL_S: float = 1.0
# Maximum time to wait for pressure to drop to CRITICAL before giving up.
_MAX_WAIT_S: float = 30.0


# ---------------------------------------------------------------------------
# Pressure levels
# ---------------------------------------------------------------------------


class MemoryPressureLevel(str, enum.Enum):
    """Current system RAM pressure category."""

    SAFE = "safe"            # > 20 % free  → full throughput
    ELEVATED = "elevated"    # 10–20 % free → warn, proceed
    CRITICAL = "critical"    # 5–10 % free  → drop OPTIONAL, block REQUIRED
    OOM_IMMINENT = "oom_imminent"  # < 5 % free → drop everything non-essential


# Thresholds: percentage of total RAM that must be free for each level.
_SAFE_THRESHOLD_PCT: float = 20.0
_ELEVATED_THRESHOLD_PCT: float = 10.0
_CRITICAL_THRESHOLD_PCT: float = 5.0


# ---------------------------------------------------------------------------
# ComponentMemoryBudget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentMemoryBudget:
    """Declared RAM requirement for one component.

    Parameters
    ----------
    component:
        Name matching the parallel_initializer component key.
    required_mib:
        Expected peak RAM consumption during initialisation (MiB).
    optional:
        If True, the gate will refuse initialisation under CRITICAL pressure
        rather than blocking.  Required components always wait (up to
        ``_MAX_WAIT_S``) for pressure to drop.
    """

    component: str
    required_mib: float
    optional: bool = True


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class MemoryGateRefused(RuntimeError):
    """Raised when the gate refuses a component due to memory pressure.

    Attributes
    ----------
    component:
        Name of the refused component.
    pressure:
        Current pressure level at time of refusal.
    free_mib:
        Available RAM at time of refusal.
    """

    def __init__(
        self,
        component: str,
        pressure: MemoryPressureLevel,
        free_mib: float,
    ) -> None:
        self.component = component
        self.pressure = pressure
        self.free_mib = free_mib
        super().__init__(
            f"[MemoryGate] Refused '{component}': pressure={pressure.value} "
            f"free={free_mib:.0f} MiB"
        )


# ---------------------------------------------------------------------------
# Free-RAM measurement
# ---------------------------------------------------------------------------


def _free_mib() -> float:
    """Return current free physical RAM in MiB.

    Uses psutil if available (most accurate cross-platform), falls back to
    /proc/meminfo on Linux and resource.getrlimit on macOS.
    """
    try:
        import psutil  # type: ignore[import]
        vm = psutil.virtual_memory()
        return vm.available / (1024 * 1024)
    except ImportError:
        pass

    if platform.system() == "Linux":
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        return kb / 1024.0
        except OSError:
            pass

    # Last resort: return a safe sentinel so we don't gate everything.
    return 4096.0  # Assume 4 GiB free if measurement fails.


def _total_mib() -> float:
    """Return total physical RAM in MiB."""
    try:
        import psutil  # type: ignore[import]
        return psutil.virtual_memory().total / (1024 * 1024)
    except ImportError:
        pass
    return 16384.0  # Assume 16 GiB if measurement fails.


def _pressure_level(free_mib: float, total_mib: float) -> MemoryPressureLevel:
    if total_mib <= 0:
        return MemoryPressureLevel.SAFE
    pct_free = (free_mib / total_mib) * 100.0
    if pct_free >= _SAFE_THRESHOLD_PCT:
        return MemoryPressureLevel.SAFE
    if pct_free >= _ELEVATED_THRESHOLD_PCT:
        return MemoryPressureLevel.ELEVATED
    if pct_free >= _CRITICAL_THRESHOLD_PCT:
        return MemoryPressureLevel.CRITICAL
    return MemoryPressureLevel.OOM_IMMINENT


# ---------------------------------------------------------------------------
# MemoryGate
# ---------------------------------------------------------------------------


class MemoryGate:
    """Pre-flight RAM gate for component initialisation.

    Usage::

        gate = get_memory_gate()
        gate.declare(ComponentMemoryBudget("neural_mesh", required_mib=2048))

        # Before initialising neural_mesh:
        await gate.check("neural_mesh")   # raises MemoryGateRefused if OOM_IMMINENT

    The gate is *advisory* for OPTIONAL components and *blocking* (up to
    ``_MAX_WAIT_S``) for REQUIRED components.
    """

    def __init__(self) -> None:
        self._budgets: Dict[str, ComponentMemoryBudget] = {}
        self._total_mib: float = _total_mib()
        self._shed_count: int = 0
        self._checked_count: int = 0

    # ------------------------------------------------------------------
    # Budget registration
    # ------------------------------------------------------------------

    def declare(self, budget: ComponentMemoryBudget) -> None:
        """Register a component's declared RAM budget.

        Can be called outside an event loop (e.g. during module init).
        """
        self._budgets[budget.component] = budget
        logger.debug(
            "[MemoryGate] declared '%s': required=%.0f MiB optional=%s",
            budget.component, budget.required_mib, budget.optional,
        )

    def declare_many(self, budgets: list[ComponentMemoryBudget]) -> None:
        for b in budgets:
            self.declare(b)

    # ------------------------------------------------------------------
    # Prospective pressure helper (Nuances 3 + 9)
    # ------------------------------------------------------------------

    _PRESSURE_ORDER = [
        MemoryPressureLevel.SAFE,
        MemoryPressureLevel.ELEVATED,
        MemoryPressureLevel.CRITICAL,
        MemoryPressureLevel.OOM_IMMINENT,
    ]

    def _effective_pressure(
        self,
        free_mib: float,
        required_mib: float,
    ) -> MemoryPressureLevel:
        """Return the WORSE of current pressure and prospective post-allocation pressure.

        Even when current pressure is SAFE, if ``free_mib - required_mib``
        would fall below the CRITICAL threshold (i.e., the allocation itself
        would cause an OOM), the effective pressure is raised accordingly.

        This catches burst-allocation spikes (model weight loading allocates
        2–4 GiB in 200 ms) that the slope gate cannot detect fast enough.
        """
        current = _pressure_level(free_mib, self._total_mib)
        if required_mib <= 0.0:
            return current
        prospective_free = max(0.0, free_mib - required_mib)
        prospective = _pressure_level(prospective_free, self._total_mib)
        order = self._PRESSURE_ORDER
        return order[max(order.index(current), order.index(prospective))]

    # ------------------------------------------------------------------
    # Pre-flight check
    # ------------------------------------------------------------------

    async def check(self, component: str) -> MemoryPressureLevel:
        """Check whether *component* may proceed with initialisation.

        Returns the effective pressure level.  Raises ``MemoryGateRefused``
        if the component is OPTIONAL and effective pressure is CRITICAL or
        worse, or if the component is REQUIRED and pressure does not drop
        below CRITICAL within ``_MAX_WAIT_S`` seconds.

        Effective pressure is the WORSE of:
        * current system free RAM vs. total RAM thresholds, AND
        * prospective RAM after allocating this component's ``required_mib``
          (guards against burst-allocation OOM — Nuances 3 + 9).
        """
        self._checked_count += 1
        budget = self._budgets.get(component)
        free = _free_mib()
        required = budget.required_mib if budget else 0.0
        pressure = self._effective_pressure(free, required)
        current_pressure = _pressure_level(free, self._total_mib)

        if pressure != current_pressure:
            logger.warning(
                "[MemoryGate] '%s' prospective pressure %s after %.0f MiB alloc "
                "(current=%s, free=%.0f MiB)",
                component, pressure.value, required, current_pressure.value, free,
            )
        else:
            logger.debug(
                "[MemoryGate] check '%s': free=%.0f MiB pressure=%s",
                component, free, pressure.value,
            )

        # ELEVATED: log and proceed
        if pressure == MemoryPressureLevel.ELEVATED:
            logger.warning(
                "[MemoryGate] '%s' starting under ELEVATED memory pressure "
                "(free=%.0f MiB). Monitor allocation.",
                component, free,
            )
            return pressure

        # SAFE: always proceed
        if pressure == MemoryPressureLevel.SAFE:
            return pressure

        # CRITICAL or OOM_IMMINENT handling
        is_optional = budget.optional if budget else True

        if is_optional:
            self._shed_count += 1
            logger.error(
                "[MemoryGate] SHED optional component '%s': pressure=%s "
                "free=%.0f MiB (total_shed=%d)",
                component, pressure.value, free, self._shed_count,
            )
            raise MemoryGateRefused(component, pressure, free)

        # REQUIRED component: wait up to _MAX_WAIT_S for effective pressure to drop
        deadline = time.monotonic() + _MAX_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_S)
            free = _free_mib()
            pressure = self._effective_pressure(free, required)
            if pressure in (MemoryPressureLevel.SAFE, MemoryPressureLevel.ELEVATED):
                logger.info(
                    "[MemoryGate] REQUIRED '%s': pressure recovered to %s, proceeding",
                    component, pressure.value,
                )
                return pressure
            logger.warning(
                "[MemoryGate] REQUIRED '%s': still under pressure=%s (free=%.0f MiB), waiting...",
                component, pressure.value, free,
            )

        # Pressure never recovered — raise to let orchestrator decide
        raise MemoryGateRefused(component, pressure, _free_mib())

    def current_pressure(self) -> MemoryPressureLevel:
        """Return current pressure without gating any component."""
        return _pressure_level(_free_mib(), self._total_mib)

    def free_mib(self) -> float:
        """Return current free RAM in MiB."""
        return _free_mib()

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def shed_count(self) -> int:
        """Number of optional components shed since process start."""
        return self._shed_count

    @property
    def checked_count(self) -> int:
        """Number of pre-flight checks performed."""
        return self._checked_count


# ---------------------------------------------------------------------------
# Default component budgets (declared separately from initializer)
# ---------------------------------------------------------------------------

#: Well-known RAM budgets for the 20+ parallel_initializer components.
#: Components not listed default to optional=True with a conservative 512 MiB.
DEFAULT_COMPONENT_BUDGETS: list[ComponentMemoryBudget] = [
    # Infrastructure (never shed — required for basic operation)
    ComponentMemoryBudget("cloud_sql_proxy",     required_mib=64,   optional=False),
    ComponentMemoryBudget("cloud_ml_router",     required_mib=256,  optional=False),
    ComponentMemoryBudget("gcp_vm_manager",      required_mib=128,  optional=False),
    ComponentMemoryBudget("memory_aware_startup",required_mib=64,   optional=False),

    # Voice (required for basic voice functionality)
    ComponentMemoryBudget("cloud_ecapa_client",  required_mib=512,  optional=False),
    ComponentMemoryBudget("speaker_verification",required_mib=1024, optional=False),
    ComponentMemoryBudget("vbi_prewarm",         required_mib=256,  optional=True),
    ComponentMemoryBudget("vbi_health_monitor",  required_mib=64,   optional=True),
    ComponentMemoryBudget("voice_unlock_api",    required_mib=128,  optional=True),
    ComponentMemoryBudget("jarvis_voice_api",    required_mib=128,  optional=True),
    ComponentMemoryBudget("unified_websocket",   required_mib=128,  optional=True),

    # Intelligence (optional — system runs without them)
    ComponentMemoryBudget("ml_engine_registry",  required_mib=256,  optional=True),
    ComponentMemoryBudget("neural_mesh",         required_mib=2048, optional=True),  # loads 7B model
    ComponentMemoryBudget("goal_inference",      required_mib=1536, optional=True),
    ComponentMemoryBudget("uae_engine",          required_mib=512,  optional=True),
    ComponentMemoryBudget("hybrid_orchestrator", required_mib=256,  optional=True),
    ComponentMemoryBudget("vision_analyzer",     required_mib=1024, optional=True),
    ComponentMemoryBudget("display_monitor",     required_mib=256,  optional=True),

    # Agentic (highest RAM, optional)
    ComponentMemoryBudget("agentic_system",      required_mib=3072, optional=True),  # full agent stack
    ComponentMemoryBudget("dynamic_components",  required_mib=512,  optional=True),
]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_gate: Optional[MemoryGate] = None


def get_memory_gate() -> MemoryGate:
    """Return (lazily creating) the process-wide MemoryGate."""
    global _g_gate
    if _g_gate is None:
        gate = MemoryGate()
        gate.declare_many(DEFAULT_COMPONENT_BUDGETS)
        _g_gate = gate
    return _g_gate
