"""backend/core/ouroboros/governance/production_oracle_observer.py

Periodic async observer for the Production Oracle substrate.

Composes registered :class:`ProductionOracleProtocol` adapters into a
single periodic poll loop, aggregates their signals into a verdict,
publishes SSE events, and maintains a bounded in-memory history ring
buffer that the GET ``/observability/production-oracle`` route reads
from.

Discipline:
  * Cadence is posture-aware (mirrors :class:`PostureObserver`):
    HARDEN -> 60s, MAINTAIN -> 300s, EXPLORE/CONSOLIDATE -> 180s.
  * Tick failure is fault-isolated -- one broken adapter never
    breaks the loop.
  * History ring buffer is bounded (default 64 most-recent verdicts);
    operators read via the GET route.
  * register_flags() exposes 4 env knobs (master + cadence overrides
    + history size + signal aggregator thresholds).
  * NEVER raises into the host event loop.

Authority invariant: this module ORCHESTRATES the substrate; it does
NOT carry authority. Verdicts feed the (downstream) auto_action_router
and operator REPL surfaces. Iron Gate / risk tier / route / approval
remain untouched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional, Tuple

from backend.core.ouroboros.governance.production_oracle import (
    OracleSignal,
    OracleVerdict,
    compute_aggregate_verdict,
    project_signal_for_observability,
)


logger = logging.getLogger(__name__)


# Cadence defaults -- mirror PostureObserver's posture-aware tick.
_CADENCE_HARDEN_S = 60.0
_CADENCE_MAINTAIN_S = 300.0
_CADENCE_EXPLORE_S = 180.0
_CADENCE_CONSOLIDATE_S = 180.0
_DEFAULT_HISTORY_SIZE = 64


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def production_oracle_enabled() -> bool:
    """Master switch. Graduated default-true 2026-05-03 (Slice D).

    Off -> the observer's ``run_periodic`` short-circuits to a sleep
    loop with no oracle queries. Operators flip explicit ``false``
    to silence the substrate end-to-end."""
    return _env_bool("JARVIS_PRODUCTION_ORACLE_ENABLED", True)


def history_ring_size() -> int:
    return _env_int(
        "JARVIS_PRODUCTION_ORACLE_HISTORY_SIZE",
        _DEFAULT_HISTORY_SIZE, minimum=4,
    )


def fail_threshold_severity() -> float:
    return _env_float(
        "JARVIS_PRODUCTION_ORACLE_FAIL_THRESHOLD", 0.8, minimum=0.0,
    )


def degrade_threshold_severity() -> float:
    return _env_float(
        "JARVIS_PRODUCTION_ORACLE_DEGRADE_THRESHOLD",
        0.5, minimum=0.0,
    )


def cadence_for_posture(posture: str) -> float:
    """Map a posture string (the 4-value StrategicPosture) to a tick
    cadence in seconds. Unknown postures -> the EXPLORE default
    (most conservative cadence). Env-tunable per-posture."""
    p = (posture or "").strip().upper()
    table = {
        "HARDEN": _env_float(
            "JARVIS_PRODUCTION_ORACLE_CADENCE_HARDEN_S",
            _CADENCE_HARDEN_S, minimum=10.0,
        ),
        "MAINTAIN": _env_float(
            "JARVIS_PRODUCTION_ORACLE_CADENCE_MAINTAIN_S",
            _CADENCE_MAINTAIN_S, minimum=10.0,
        ),
        "EXPLORE": _env_float(
            "JARVIS_PRODUCTION_ORACLE_CADENCE_EXPLORE_S",
            _CADENCE_EXPLORE_S, minimum=10.0,
        ),
        "CONSOLIDATE": _env_float(
            "JARVIS_PRODUCTION_ORACLE_CADENCE_CONSOLIDATE_S",
            _CADENCE_CONSOLIDATE_S, minimum=10.0,
        ),
    }
    return table.get(p, table["EXPLORE"])


@dataclass(frozen=True)
class OracleObservation:
    """One frozen tick result. Stored in the observer's ring buffer
    + projected by the GET route + emitted via SSE."""

    observed_at_ts: float
    aggregate_verdict: OracleVerdict
    signals: Tuple[OracleSignal, ...] = ()
    posture: str = ""
    tick_duration_ms: int = 0
    adapters_queried: int = 0
    adapters_failed: int = 0


def project_observation(obs: OracleObservation) -> dict:
    """Lightweight projection for SSE / GET payloads. Never raises."""
    try:
        return {
            "observed_at_ts": float(obs.observed_at_ts),
            "aggregate_verdict": obs.aggregate_verdict.value,
            "posture": str(obs.posture or "")[:40],
            "tick_duration_ms": int(obs.tick_duration_ms),
            "adapters_queried": int(obs.adapters_queried),
            "adapters_failed": int(obs.adapters_failed),
            "signals": [
                project_signal_for_observability(s)
                for s in (obs.signals or ())
            ],
        }
    except Exception:  # noqa: BLE001 -- defensive
        return {
            "observed_at_ts": float(obs.observed_at_ts),
            "aggregate_verdict": "disabled",
            "posture": "",
            "tick_duration_ms": 0,
            "adapters_queried": 0,
            "adapters_failed": 0,
            "signals": [],
        }


class ProductionOracleObserver:
    """Periodic observer composing N oracle adapters into one stream.

    Construction does NOT start the loop. Call :meth:`run_periodic`
    from an asyncio task; cancel the task to stop. The history ring
    buffer is populated on every tick + readable via
    :meth:`current` / :meth:`history`.
    """

    def __init__(
        self,
        adapters: Optional[List] = None,
        *,
        history_size: Optional[int] = None,
    ) -> None:
        self._adapters: List = list(adapters or [])
        self._history: Deque[OracleObservation] = deque(
            maxlen=history_size or history_ring_size(),
        )
        self._current: Optional[OracleObservation] = None
        self._lock = asyncio.Lock()
        self._tick_count: int = 0
        self._failure_count: int = 0

    @property
    def adapter_count(self) -> int:
        return len(self._adapters)

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def register(self, adapter) -> None:  # noqa: ANN001
        """Add an adapter implementing ProductionOracleProtocol."""
        self._adapters.append(adapter)

    def current(self) -> Optional[OracleObservation]:
        return self._current

    def history(self) -> Tuple[OracleObservation, ...]:
        return tuple(self._history)

    async def tick_once(
        self, *, posture: str = "EXPLORE", since_ts: float = 0.0,
    ) -> OracleObservation:
        """Query every registered adapter ONCE, aggregate, return.

        Each adapter call is wrapped in its own try/except; one
        broken adapter never breaks the tick. Tick start/end times
        bracket the gather() call so the duration metric reflects
        real wall-clock cost.
        """
        t_start = time.monotonic()
        all_signals: List[OracleSignal] = []
        adapters_failed = 0
        for adapter in self._adapters:
            try:
                if not getattr(adapter, "enabled", True):
                    continue
                sigs = await adapter.query_signals(since_ts=since_ts)
                if sigs:
                    all_signals.extend(sigs)
            except Exception:  # noqa: BLE001 -- adapter contract violated
                adapters_failed += 1
                self._failure_count += 1
                logger.debug(
                    "[ProductionOracle] adapter %r raised in tick",
                    getattr(adapter, "name", repr(adapter)),
                    exc_info=True,
                )
        verdict = compute_aggregate_verdict(
            all_signals,
            fail_threshold_severity=fail_threshold_severity(),
            degrade_threshold_severity=degrade_threshold_severity(),
        )
        duration_ms = int((time.monotonic() - t_start) * 1000)
        obs = OracleObservation(
            observed_at_ts=time.time(),
            aggregate_verdict=verdict,
            signals=tuple(all_signals),
            posture=str(posture or ""),
            tick_duration_ms=duration_ms,
            adapters_queried=len(self._adapters),
            adapters_failed=adapters_failed,
        )
        async with self._lock:
            self._current = obs
            self._history.append(obs)
            self._tick_count += 1
        # Best-effort SSE publish.
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_production_oracle_signal,
            )
            publish_production_oracle_signal(
                aggregate_verdict=verdict.value,
                signal_count=len(all_signals),
                adapters_queried=len(self._adapters),
                adapters_failed=adapters_failed,
                tick_duration_ms=duration_ms,
                posture=str(posture or ""),
            )
        except Exception:  # noqa: BLE001 -- best-effort
            logger.debug(
                "[ProductionOracle] SSE publish skipped",
                exc_info=True,
            )
        return obs

    async def run_periodic(
        self, *, posture_provider=None,  # noqa: ANN001
    ) -> None:
        """Async loop. Cancel the surrounding task to stop.

        ``posture_provider`` is an optional callable returning the
        current StrategicPosture string; when supplied the observer
        respects per-posture cadence. When None (default), uses the
        EXPLORE cadence -- the most conservative tick rate.
        """
        while True:
            if not production_oracle_enabled():
                # Master off -> sleep at conservative cadence and
                # re-check; allows hot-flip without restart.
                await asyncio.sleep(_CADENCE_EXPLORE_S)
                continue
            try:
                posture = (
                    posture_provider() if posture_provider else "EXPLORE"
                )
            except Exception:  # noqa: BLE001 -- defensive
                posture = "EXPLORE"
            try:
                await self.tick_once(posture=str(posture))
            except Exception:  # noqa: BLE001 -- never break the loop
                logger.debug(
                    "[ProductionOracle] tick_once raised",
                    exc_info=True,
                )
            await asyncio.sleep(cadence_for_posture(str(posture)))


# ---------------------------------------------------------------------------
# Module-owned default-instance singleton
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[ProductionOracleObserver] = None
_DEFAULT_OBSERVER_LOCK = asyncio.Lock()


def get_default_observer(
    *, project_root: Optional[Path] = None,
) -> ProductionOracleObserver:
    """Return the process-wide observer with the default adapter
    bundle pre-registered. First call constructs + bundles the
    adapters; subsequent calls return the cached instance.

    Default bundle (always safe to register):
      * StdlibSelfHealthOracle (offline; zero network)
      * HTTPHealthCheckOracle (env-config; reports DISABLED when
        no URL set -- safe to register unconditionally)
    """
    global _DEFAULT_OBSERVER
    if _DEFAULT_OBSERVER is not None:
        return _DEFAULT_OBSERVER
    from backend.core.ouroboros.governance.stdlib_self_health_oracle import (  # noqa: E501
        StdlibSelfHealthOracle,
    )
    from backend.core.ouroboros.governance.http_healthcheck_oracle import (  # noqa: E501
        HTTPHealthCheckOracle,
    )
    obs = ProductionOracleObserver(
        adapters=[
            StdlibSelfHealthOracle(project_root=project_root),
            HTTPHealthCheckOracle(),
        ],
    )
    _DEFAULT_OBSERVER = obs
    return obs


def reset_default_observer() -> None:
    """Test helper. Clears the singleton."""
    global _DEFAULT_OBSERVER
    _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[ProductionOracle] register_flags degraded: %s", exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/production_oracle_observer.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_PRODUCTION_ORACLE_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example="JARVIS_PRODUCTION_ORACLE_ENABLED=true",
            description=(
                "Master switch for the Production Oracle substrate. "
                "When on, the observer polls registered adapters "
                "(StdlibSelfHealthOracle + HTTPHealthCheckOracle by "
                "default) at posture-aware cadence and surfaces "
                "verdicts via /observability/production-oracle + "
                "SSE production_oracle_signal_observed. Authority-"
                "free: verdicts are advisory; never directly mutate "
                "Iron Gate / risk / route. Graduated default-true "
                "2026-05-03 (Slice D)."
            ),
        ),
        FlagSpec(
            name="JARVIS_PRODUCTION_ORACLE_HISTORY_SIZE",
            type=FlagType.INT, default=_DEFAULT_HISTORY_SIZE,
            category=Category.CAPACITY,
            source_file=target,
            example="JARVIS_PRODUCTION_ORACLE_HISTORY_SIZE=128",
            description=(
                "Bounded in-memory ring buffer size for recent "
                "OracleObservations. Floor 4. The /observability "
                "GET route reads the ring; SSE emits tick-by-tick. "
                "Larger -> longer history visible to operators; "
                "memory grows linearly with this knob × signal count."
            ),
        ),
        FlagSpec(
            name="JARVIS_PRODUCTION_ORACLE_FAIL_THRESHOLD",
            type=FlagType.FLOAT, default=0.8,
            category=Category.SAFETY,
            source_file=target,
            example="JARVIS_PRODUCTION_ORACLE_FAIL_THRESHOLD=0.9",
            description=(
                "Severity threshold above which a FAILED-verdict "
                "OracleSignal escalates the aggregate to FAILED. "
                "Lower = stricter (more aggregate FAILEDs). Floor "
                "0.0; clamp at 1.0."
            ),
        ),
        FlagSpec(
            name="JARVIS_PRODUCTION_ORACLE_DEGRADE_THRESHOLD",
            type=FlagType.FLOAT, default=0.5,
            category=Category.SAFETY,
            source_file=target,
            example="JARVIS_PRODUCTION_ORACLE_DEGRADE_THRESHOLD=0.3",
            description=(
                "Severity threshold above which a DEGRADED-verdict "
                "OracleSignal escalates the aggregate to DEGRADED. "
                "Lower = stricter. Floor 0.0; clamp at 1.0."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[ProductionOracle] spec %s skipped: %s",
                spec.name, exc,
            )
    return count


def register_shipped_invariants() -> list:
    """AST pin: observer + cadence + ring buffer + factory all
    present; OracleObservation stays frozen; no exec/eval/compile."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "production_oracle_enabled",
        "history_ring_size",
        "cadence_for_posture",
        "project_observation",
        "get_default_observer",
        "reset_default_observer",
        "register_flags",
        "register_shipped_invariants",
    )
    REQUIRED_CLASSES = (
        "OracleObservation",
        "ProductionOracleObserver",
    )

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        seen_classes: dict = {}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.ClassDef):
                seen_classes[node.name] = node
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"production_oracle_observer MUST NOT "
                            f"call {node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        # OracleObservation MUST stay frozen.
        obs_node = seen_classes.get("OracleObservation")
        if obs_node is not None:
            frozen = False
            for dec in obs_node.decorator_list:
                if isinstance(dec, _ast.Call):
                    for kw in dec.keywords:
                        if (
                            kw.arg == "frozen"
                            and isinstance(kw.value, _ast.Constant)
                            and kw.value.value is True
                        ):
                            frozen = True
                            break
            if not frozen:
                violations.append(
                    "OracleObservation MUST stay "
                    "@dataclass(frozen=True)"
                )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "production_oracle_observer.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="production_oracle_observer_substrate",
            target_file=target,
            description=(
                "Production oracle observer: cadence + ring buffer "
                "+ tick + factory + frozen OracleObservation; no "
                "dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "OracleObservation",
    "ProductionOracleObserver",
    "production_oracle_enabled",
    "history_ring_size",
    "cadence_for_posture",
    "project_observation",
    "get_default_observer",
    "reset_default_observer",
    "register_flags",
    "register_shipped_invariants",
]
