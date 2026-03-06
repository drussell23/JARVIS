# backend/core/startup_config.py
"""Declarative startup configuration with schema and DAG validation.

Loads gate dependency graph, budget policy, and FSM transition timeouts
from compiled defaults with env-var overrides.  All numeric values are
range-validated so that bad operator input is caught at config-load time
rather than at runtime deep inside an async pipeline.

Env-var conventions
-------------------
* ``JARVIS_GATE_<name>_TIMEOUT`` — per-gate timeout override (float seconds)
* ``JARVIS_BUDGET_*`` — concurrency budget knobs
* ``JARVIS_HANDOFF_TIMEOUT_S``, ``JARVIS_DRAIN_WINDOW_S`` — FSM transition
* ``JARVIS_LEASE_*``, ``JARVIS_PROBE_*`` — lease / prober tuning
* ``JARVIS_GCP_*`` — GCP-specific knobs
* ``JARVIS_FSM_JOURNAL_PATH`` — override journal location

All public symbols are re-exported via ``__all__``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = [
    "GateConfig",
    "SoftGatePrecondition",
    "BudgetConfig",
    "StartupConfig",
    "ConfigValidationError",
    "load_startup_config",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigValidationError(Exception):
    """Raised when startup configuration fails validation."""


# ---------------------------------------------------------------------------
# Env-var helpers (range-checked)
# ---------------------------------------------------------------------------


def _env_float(
    name: str,
    default: float,
    min_val: float = 0.1,
    max_val: float = 3600.0,
) -> float:
    """Read an env var as a float with inclusive range enforcement.

    Parameters
    ----------
    name:
        Environment variable name.
    default:
        Fallback value when the variable is unset.
    min_val:
        Minimum acceptable value (inclusive).
    max_val:
        Maximum acceptable value (inclusive).

    Raises
    ------
    ConfigValidationError
        If the raw string cannot be parsed as a float or the parsed
        value falls outside ``[min_val, max_val]``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"{name}={raw!r} is not a valid float"
        ) from exc
    if value < min_val:
        raise ConfigValidationError(
            f"{name}={value} is below minimum {min_val}"
        )
    if value > max_val:
        raise ConfigValidationError(
            f"{name}={value} is above maximum {max_val}"
        )
    return value


def _env_int(
    name: str,
    default: int,
    min_val: int = 0,
    max_val: int = 100,
) -> int:
    """Read an env var as an int with inclusive range enforcement.

    Raises
    ------
    ConfigValidationError
        If the raw string cannot be parsed as an int or the parsed
        value falls outside ``[min_val, max_val]``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"{name}={raw!r} is not a valid int"
        ) from exc
    if value < min_val:
        raise ConfigValidationError(
            f"{name}={value} is below minimum {min_val}"
        )
    if value > max_val:
        raise ConfigValidationError(
            f"{name}={value} is above maximum {max_val}"
        )
    return value


def _env_bool(name: str, default: bool) -> bool:
    """Read an env var as a boolean.

    Truthy values: ``"1"``, ``"true"``, ``"yes"`` (case-insensitive).
    Falsy  values: ``"0"``, ``"false"``, ``"no"`` (case-insensitive).

    Anything else raises :class:`ConfigValidationError`.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalised = raw.strip().lower()
    if normalised in ("1", "true", "yes"):
        return True
    if normalised in ("0", "false", "no"):
        return False
    raise ConfigValidationError(
        f"{name}={raw!r} is not a valid boolean "
        f"(expected 1/0, true/false, yes/no)"
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GateConfig:
    """Configuration for a single startup gate (phase boundary).

    Attributes
    ----------
    dependencies:
        Gate names that must complete before this gate opens.
    timeout_s:
        Maximum seconds to wait for this gate's work to complete.
    on_timeout:
        Action when the gate times out — ``"skip"`` (non-critical)
        or ``"fail"`` (fatal).
    """

    dependencies: List[str]
    timeout_s: float
    on_timeout: str  # "skip" | "fail"


@dataclass
class SoftGatePrecondition:
    """Precondition for a soft-gate category to begin execution.

    Attributes
    ----------
    require_phase:
        The startup phase that must be reached first.
    require_memory_stable_s:
        Seconds of memory-pressure stability required.
    memory_slope_threshold_mb_s:
        Maximum RSS slope (MB/s) considered "stable".
    memory_sample_interval_s:
        How often to sample memory for slope calculation.
    """

    require_phase: str
    require_memory_stable_s: float = 10.0
    memory_slope_threshold_mb_s: float = 0.5
    memory_sample_interval_s: float = 1.0


@dataclass
class BudgetConfig:
    """Concurrency budget policy for startup operations.

    Hard gates are serialised (only ``max_hard_concurrent`` at a time);
    soft gates can overlap up to ``max_total_concurrent`` total.

    Attributes
    ----------
    max_hard_concurrent:
        Simultaneous hard-gate operations allowed.
    max_total_concurrent:
        Total simultaneous operations (hard + soft).
    hard_gate_categories:
        Operation categories classified as *hard* (RAM-intensive).
    soft_gate_categories:
        Operation categories classified as *soft* (lighter weight).
    soft_gate_preconditions:
        Per-category preconditions that must hold before a soft gate
        operation may start.
    gcp_parallel_allowed:
        Whether GCP provisioning may overlap with other soft work.
    max_wait_s:
        Maximum seconds any operation waits for a budget slot.
    """

    max_hard_concurrent: int = 1
    max_total_concurrent: int = 3
    hard_gate_categories: List[str] = field(
        default_factory=lambda: ["MODEL_LOAD", "REACTOR_LAUNCH", "SUBPROCESS_SPAWN"],
    )
    soft_gate_categories: List[str] = field(
        default_factory=lambda: ["ML_INIT", "GCP_PROVISION"],
    )
    soft_gate_preconditions: Dict[str, SoftGatePrecondition] = field(
        default_factory=dict,
    )
    gcp_parallel_allowed: bool = True
    max_wait_s: float = 60.0


@dataclass
class StartupConfig:
    """Top-level declarative startup configuration.

    Contains the gate dependency graph, budget policy, FSM transition
    timeouts, lease/prober tuning, GCP knobs, and persistence settings.
    """

    # Gate dependency graph
    gates: Dict[str, GateConfig] = field(default_factory=dict)

    # Concurrency budget
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    # FSM transition timeouts
    handoff_timeout_s: float = 10.0
    drain_window_s: float = 5.0

    # Lease / prober configuration
    lease_ttl_s: float = 120.0
    probe_timeout_s: float = 15.0
    probe_cache_ttl_s: float = 3.0
    lease_hysteresis_count: int = 3
    lease_reacquire_delay_s: float = 30.0

    # GCP
    gcp_deadline_s: float = 60.0
    cloud_fallback_enabled: bool = True

    # Recovery
    handoff_retry_enabled: bool = False

    # Persistence
    fsm_journal_path: str = ""

    # ------------------------------------------------------------------
    # DAG validation
    # ------------------------------------------------------------------

    def validate_dag(self) -> None:
        """Check the gate graph for unknown targets and cycles.

        Raises
        ------
        ConfigValidationError
            If any gate lists a dependency that is not a known gate name,
            or if the dependency graph contains a cycle (detected via DFS).
        """
        gate_names = set(self.gates.keys())

        # 1. Unknown dependency targets
        for name, gate in self.gates.items():
            for dep in gate.dependencies:
                if dep not in gate_names:
                    raise ConfigValidationError(
                        f"Gate {name!r} depends on unknown gate {dep!r}"
                    )

        # 2. Cycle detection via iterative DFS with 3-colour marking
        WHITE, GREY, BLACK = 0, 1, 2
        colour: Dict[str, int] = {n: WHITE for n in gate_names}

        for start in gate_names:
            if colour[start] != WHITE:
                continue
            stack: List[tuple] = [(start, False)]
            while stack:
                node, returning = stack.pop()
                if returning:
                    colour[node] = BLACK
                    continue
                if colour[node] == GREY:
                    # Already on the recursion stack — this means we
                    # revisited it without it turning BLACK, which is a
                    # cycle only if we are processing its successors.
                    # Since we push (node, True) *before* successors,
                    # hitting GREY here means we found it again via a
                    # descendant.
                    raise ConfigValidationError(
                        f"Gate dependency graph contains a cycle involving {node!r}"
                    )
                if colour[node] == BLACK:
                    continue
                colour[node] = GREY
                # Push the "return" marker first, then successors
                stack.append((node, True))
                for dep in self.gates[node].dependencies:
                    if colour[dep] == GREY:
                        raise ConfigValidationError(
                            f"Gate dependency graph contains a cycle "
                            f"involving {dep!r}"
                        )
                    if colour[dep] == WHITE:
                        stack.append((dep, False))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def load_startup_config() -> StartupConfig:
    """Build a :class:`StartupConfig` from compiled defaults + env overrides.

    This is the single entry-point for obtaining startup configuration.
    Callers should treat the returned object as immutable after creation
    (enforced by convention, not ``frozen=True``, to allow test mutation).

    Returns
    -------
    StartupConfig
        Fully validated configuration.

    Raises
    ------
    ConfigValidationError
        If any env-var value is out of range or the resulting DAG is
        invalid.
    """

    # -- Gate timeouts (env overrides) -------------------------------------
    prewarm_timeout = _env_float("JARVIS_GATE_PREWARM_TIMEOUT", 45.0)
    core_svc_timeout = _env_float("JARVIS_GATE_CORE_SERVICES_TIMEOUT", 120.0)
    core_ready_timeout = _env_float("JARVIS_GATE_CORE_READY_TIMEOUT", 60.0)
    deferred_timeout = _env_float("JARVIS_GATE_DEFERRED_TIMEOUT", 90.0)

    gates: Dict[str, GateConfig] = {
        "PREWARM_GCP": GateConfig(
            dependencies=[],
            timeout_s=prewarm_timeout,
            on_timeout="skip",
        ),
        "CORE_SERVICES": GateConfig(
            dependencies=["PREWARM_GCP"],
            timeout_s=core_svc_timeout,
            on_timeout="fail",
        ),
        "CORE_READY": GateConfig(
            dependencies=["CORE_SERVICES"],
            timeout_s=core_ready_timeout,
            on_timeout="fail",
        ),
        "DEFERRED_COMPONENTS": GateConfig(
            dependencies=["CORE_READY"],
            timeout_s=deferred_timeout,
            on_timeout="fail",
        ),
    }

    # -- Budget policy -----------------------------------------------------
    max_hard = _env_int("JARVIS_BUDGET_MAX_HARD", 1, min_val=1, max_val=10)
    max_total = _env_int("JARVIS_BUDGET_MAX_TOTAL", 3, min_val=1, max_val=50)
    max_wait = _env_float("JARVIS_BUDGET_MAX_WAIT_S", 60.0, min_val=1.0, max_val=600.0)
    memory_stable_s = _env_float("JARVIS_MEMORY_STABLE_S", 10.0, min_val=1.0, max_val=120.0)
    memory_slope = _env_float("JARVIS_MEMORY_SLOPE_THRESHOLD", 0.5, min_val=0.1, max_val=10.0)

    soft_preconditions: Dict[str, SoftGatePrecondition] = {
        "ML_INIT": SoftGatePrecondition(
            require_phase="CORE_READY",
            require_memory_stable_s=memory_stable_s,
            memory_slope_threshold_mb_s=memory_slope,
        ),
        "GCP_PROVISION": SoftGatePrecondition(
            require_phase="PREWARM_GCP",
            require_memory_stable_s=memory_stable_s,
            memory_slope_threshold_mb_s=memory_slope,
        ),
    }

    budget = BudgetConfig(
        max_hard_concurrent=max_hard,
        max_total_concurrent=max_total,
        hard_gate_categories=["MODEL_LOAD", "REACTOR_LAUNCH", "SUBPROCESS_SPAWN"],
        soft_gate_categories=["ML_INIT", "GCP_PROVISION"],
        soft_gate_preconditions=soft_preconditions,
        gcp_parallel_allowed=_env_bool("JARVIS_GCP_PARALLEL_ALLOWED", True),
        max_wait_s=max_wait,
    )

    # -- FSM transition timeouts -------------------------------------------
    handoff_timeout = _env_float("JARVIS_HANDOFF_TIMEOUT_S", 10.0, min_val=1.0, max_val=120.0)
    drain_window = _env_float("JARVIS_DRAIN_WINDOW_S", 5.0, min_val=0.5, max_val=60.0)

    # -- Lease / prober tuning ---------------------------------------------
    lease_ttl = _env_float("JARVIS_LEASE_TTL_S", 120.0, min_val=10.0, max_val=600.0)
    probe_timeout = _env_float("JARVIS_PROBE_TIMEOUT_S", 15.0, min_val=1.0, max_val=120.0)
    probe_cache_ttl = _env_float("JARVIS_PROBE_CACHE_TTL", 3.0, min_val=0.1, max_val=60.0)
    lease_hysteresis = _env_int("JARVIS_LEASE_HYSTERESIS_COUNT", 3, min_val=1, max_val=20)
    lease_reacquire_delay = _env_float(
        "JARVIS_LEASE_REACQUIRE_DELAY_S", 30.0, min_val=1.0, max_val=300.0,
    )

    # -- GCP ---------------------------------------------------------------
    gcp_deadline = _env_float("JARVIS_GCP_DEADLINE_S", 60.0, min_val=5.0, max_val=600.0)
    cloud_fallback = _env_bool("JARVIS_CLOUD_FALLBACK_ENABLED", True)

    # -- Recovery ----------------------------------------------------------
    handoff_retry = _env_bool("JARVIS_HANDOFF_RETRY_ENABLED", False)

    # -- Persistence -------------------------------------------------------
    default_state_dir = os.environ.get("JARVIS_STATE_DIR", "/tmp/jarvis")
    default_journal = os.path.join(default_state_dir, "startup_fsm_journal.jsonl")
    fsm_journal_path = os.environ.get("JARVIS_FSM_JOURNAL_PATH", default_journal)

    # -- Assemble ----------------------------------------------------------
    cfg = StartupConfig(
        gates=gates,
        budget=budget,
        handoff_timeout_s=handoff_timeout,
        drain_window_s=drain_window,
        lease_ttl_s=lease_ttl,
        probe_timeout_s=probe_timeout,
        probe_cache_ttl_s=probe_cache_ttl,
        lease_hysteresis_count=lease_hysteresis,
        lease_reacquire_delay_s=lease_reacquire_delay,
        gcp_deadline_s=gcp_deadline,
        cloud_fallback_enabled=cloud_fallback,
        handoff_retry_enabled=handoff_retry,
        fsm_journal_path=fsm_journal_path,
    )

    # Validate the DAG before returning
    cfg.validate_dag()

    return cfg
