"""backend/core/ouroboros/governance/stdlib_self_health_oracle.py

Offline empirical anchor for the Production Oracle substrate.

Implements :class:`ProductionOracleProtocol` against the existing
``.ouroboros/sessions/<id>/summary.json`` artifacts produced by the
battle-test harness. No network, no external auth, no API tokens --
pure stdlib filesystem reads.

Why this oracle is load-bearing for the arc:
  * The substrate (``production_oracle.py``) is empirically
    validatable in this sandboxed env where Sentry/Datadog tokens
    aren't available. Without an offline anchor, the substrate
    would only be testable via mocks; this oracle proves the
    Protocol works against real artifacts.
  * Future Sentry/Datadog/Prometheus adapters drop in as additional
    Protocol implementers without touching this module.

Signals emitted (one per dimension, every query_signals call):
  * HEALTHCHECK -- harness completion ratio across the recent N
    sessions. HEALTHY when >=80% complete; DEGRADED 50-79%; FAILED
    <50%.
  * PERFORMANCE -- mean cost-per-session relative to env-tunable
    baseline. HEALTHY <=baseline*1.5; DEGRADED <=baseline*3.0;
    FAILED above. Catches runaway cost burn.
  * METRIC -- distribution of stop_reasons. Clean termination
    (idle_timeout / wall_clock_cap / cost_cap / shutdown_event) is
    HEALTHY; signal-driven kills (SIGKILL / SIGTERM / sighup /
    sigint) are abnormal.

Authority invariant: all OracleSignals carry verdict + severity that
the substrate aggregator combines into an advisory verdict. NEVER
mutates Iron Gate / risk tier / route / approval directly.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

from backend.core.ouroboros.governance.production_oracle import (
    OracleKind,
    OracleSignal,
    OracleVerdict,
)


logger = logging.getLogger(__name__)


_DEFAULT_LOOKBACK = 10
_DEFAULT_BASELINE_COST = 0.50
_DEFAULT_HEALTHY_COMPLETION = 0.80
_DEFAULT_DEGRADED_COMPLETION = 0.50
_DEFAULT_HEALTHY_COST_RATIO = 1.5
_DEFAULT_DEGRADED_COST_RATIO = 3.0
_ORACLE_NAME = "stdlib_self_health"

_SESSIONS_DIRNAME = ".ouroboros/sessions"


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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def stdlib_self_health_enabled() -> bool:
    """Per-adapter sub-gate. Master flag for the substrate is
    ``JARVIS_PRODUCTION_ORACLE_ENABLED`` (Slice D). When master is
    on but this is off, the oracle reports DISABLED signals."""
    return _env_bool("JARVIS_STDLIB_SELF_HEALTH_ORACLE_ENABLED", True)


def lookback_sessions() -> int:
    return _env_int(
        "JARVIS_STDLIB_SELF_HEALTH_LOOKBACK", _DEFAULT_LOOKBACK,
        minimum=1,
    )


def baseline_cost_usd() -> float:
    return _env_float(
        "JARVIS_STDLIB_SELF_HEALTH_BASELINE_COST_USD",
        _DEFAULT_BASELINE_COST, minimum=0.001,
    )


def healthy_completion_threshold() -> float:
    return _env_float(
        "JARVIS_STDLIB_SELF_HEALTH_COMPLETION_HEALTHY",
        _DEFAULT_HEALTHY_COMPLETION, minimum=0.0,
    )


def degraded_completion_threshold() -> float:
    return _env_float(
        "JARVIS_STDLIB_SELF_HEALTH_COMPLETION_DEGRADED",
        _DEFAULT_DEGRADED_COMPLETION, minimum=0.0,
    )


def _resolve_sessions_dir(project_root: Optional[Path]) -> Path:
    base = project_root if project_root else Path(os.getcwd())
    return Path(base) / _SESSIONS_DIRNAME


def _load_recent_summaries(
    project_root: Optional[Path], lookback: int,
) -> Tuple[dict, ...]:
    sessions_dir = _resolve_sessions_dir(project_root)
    if not sessions_dir.is_dir():
        return ()
    try:
        names = sorted(
            (p.name for p in sessions_dir.iterdir() if p.is_dir()),
            reverse=True,
        )
    except OSError:
        return ()
    summaries: list = []
    for name in names[:max(1, lookback)]:
        summary_path = sessions_dir / name / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            text = summary_path.read_text(encoding="utf-8")
            summaries.append(json.loads(text))
        except Exception:  # noqa: BLE001 -- defensive
            logger.debug(
                "[StdlibSelfHealthOracle] skip unparseable summary %s",
                name, exc_info=True,
            )
            continue
    return tuple(summaries)


def _completion_signal(
    summaries: Tuple[dict, ...], now_ts: float,
) -> OracleSignal:
    if not summaries:
        return OracleSignal(
            oracle_name=_ORACLE_NAME, kind=OracleKind.HEALTHCHECK,
            verdict=OracleVerdict.INSUFFICIENT_DATA,
            observed_at_ts=now_ts,
            summary="no recent sessions to score",
            payload={"sessions_examined": 0}, severity=0.0,
        )
    completed = sum(
        1 for s in summaries
        if str(s.get("session_outcome", "")).lower() == "complete"
    )
    ratio = completed / len(summaries)
    healthy_t = healthy_completion_threshold()
    degraded_t = degraded_completion_threshold()
    if ratio >= healthy_t:
        verdict, severity = OracleVerdict.HEALTHY, 0.2
    elif ratio >= degraded_t:
        verdict, severity = OracleVerdict.DEGRADED, 0.6
    else:
        verdict, severity = OracleVerdict.FAILED, 0.9
    return OracleSignal(
        oracle_name=_ORACLE_NAME, kind=OracleKind.HEALTHCHECK,
        verdict=verdict, observed_at_ts=now_ts,
        summary=(
            f"completion ratio {ratio:.2%} ({completed}/"
            f"{len(summaries)} sessions clean-terminated)"
        ),
        payload={
            "completion_ratio": round(ratio, 4),
            "sessions_complete": completed,
            "sessions_examined": len(summaries),
            "healthy_threshold": round(healthy_t, 4),
            "degraded_threshold": round(degraded_t, 4),
        },
        severity=severity,
    )


def _cost_signal(
    summaries: Tuple[dict, ...], now_ts: float,
) -> OracleSignal:
    if not summaries:
        return OracleSignal(
            oracle_name=_ORACLE_NAME, kind=OracleKind.PERFORMANCE,
            verdict=OracleVerdict.INSUFFICIENT_DATA,
            observed_at_ts=now_ts,
            summary="no recent sessions to score cost",
            payload={"sessions_examined": 0}, severity=0.0,
        )
    costs = []
    for s in summaries:
        try:
            costs.append(float(s.get("cost_total", 0) or 0))
        except (TypeError, ValueError):
            continue
    if not costs:
        return OracleSignal(
            oracle_name=_ORACLE_NAME, kind=OracleKind.PERFORMANCE,
            verdict=OracleVerdict.INSUFFICIENT_DATA,
            observed_at_ts=now_ts,
            summary="cost field unparseable in all summaries",
            payload={"sessions_examined": len(summaries)},
            severity=0.0,
        )
    mean_cost = sum(costs) / len(costs)
    baseline = baseline_cost_usd()
    healthy_ratio = _env_float(
        "JARVIS_STDLIB_SELF_HEALTH_COST_HEALTHY_RATIO",
        _DEFAULT_HEALTHY_COST_RATIO, minimum=1.0,
    )
    degraded_ratio = _env_float(
        "JARVIS_STDLIB_SELF_HEALTH_COST_DEGRADED_RATIO",
        _DEFAULT_DEGRADED_COST_RATIO, minimum=1.0,
    )
    cost_multiple = mean_cost / baseline if baseline > 0 else 0.0
    if cost_multiple <= healthy_ratio:
        verdict, severity = OracleVerdict.HEALTHY, 0.2
    elif cost_multiple <= degraded_ratio:
        verdict, severity = OracleVerdict.DEGRADED, 0.6
    else:
        verdict, severity = OracleVerdict.FAILED, 0.9
    return OracleSignal(
        oracle_name=_ORACLE_NAME, kind=OracleKind.PERFORMANCE,
        verdict=verdict, observed_at_ts=now_ts,
        summary=(
            f"mean cost ${mean_cost:.4f} ({cost_multiple:.2f}x "
            f"baseline ${baseline:.2f})"
        ),
        payload={
            "mean_cost_usd": round(mean_cost, 4),
            "baseline_cost_usd": round(baseline, 4),
            "cost_multiple": round(cost_multiple, 4),
            "sessions_examined": len(costs),
        },
        severity=severity,
    )


def _stop_reason_signal(
    summaries: Tuple[dict, ...], now_ts: float,
) -> OracleSignal:
    if not summaries:
        return OracleSignal(
            oracle_name=_ORACLE_NAME, kind=OracleKind.METRIC,
            verdict=OracleVerdict.INSUFFICIENT_DATA,
            observed_at_ts=now_ts,
            summary="no sessions to score termination reasons",
            payload={"sessions_examined": 0}, severity=0.0,
        )
    clean_reasons = {
        "idle_timeout", "wall_clock_cap", "cost_cap",
        "shutdown_event", "operator_quit",
    }
    abnormal_reasons = {"sigkill", "sigterm", "sighup", "sigint"}
    clean = abnormal = other = 0
    for s in summaries:
        raw_reason = str(s.get("stop_reason", "") or "")
        head = raw_reason.split("+", 1)[0].strip().lower()
        if head in clean_reasons:
            clean += 1
        elif head in abnormal_reasons:
            abnormal += 1
        else:
            other += 1
    abnormal_ratio = abnormal / len(summaries)
    if abnormal_ratio >= 0.5:
        verdict, severity = OracleVerdict.FAILED, 0.85
    elif abnormal_ratio > 0.0:
        verdict, severity = OracleVerdict.DEGRADED, 0.55
    else:
        verdict, severity = OracleVerdict.HEALTHY, 0.2
    return OracleSignal(
        oracle_name=_ORACLE_NAME, kind=OracleKind.METRIC,
        verdict=verdict, observed_at_ts=now_ts,
        summary=(
            f"stop_reasons clean={clean} abnormal={abnormal} "
            f"other={other} ({abnormal_ratio:.0%} abnormal)"
        ),
        payload={
            "clean_terminations": clean,
            "abnormal_terminations": abnormal,
            "other_terminations": other,
            "abnormal_ratio": round(abnormal_ratio, 4),
            "sessions_examined": len(summaries),
        },
        severity=severity,
    )


class StdlibSelfHealthOracle:
    """Offline oracle reading recent battle-test session summaries.

    Implements :class:`production_oracle.ProductionOracleProtocol`
    structurally (duck-typed; Protocol is ``@runtime_checkable``).
    """

    def __init__(
        self, *, project_root: Optional[Path] = None,
    ) -> None:
        self._project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(os.getcwd()).resolve()
        )

    @property
    def name(self) -> str:
        return _ORACLE_NAME

    @property
    def enabled(self) -> bool:
        return stdlib_self_health_enabled()

    async def query_signals(
        self, *, since_ts: float = 0.0,  # noqa: ARG002 -- offline
    ) -> Tuple[OracleSignal, ...]:
        try:
            if not self.enabled:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME,
                    kind=OracleKind.HEALTHCHECK,
                    verdict=OracleVerdict.DISABLED,
                    observed_at_ts=time.time(),
                    summary="stdlib_self_health_oracle disabled",
                    payload={}, severity=0.0,
                ),)
            summaries = _load_recent_summaries(
                self._project_root, lookback_sessions(),
            )
            now = time.time()
            return (
                _completion_signal(summaries, now),
                _cost_signal(summaries, now),
                _stop_reason_signal(summaries, now),
            )
        except Exception:  # noqa: BLE001 -- contract: never raise
            logger.debug(
                "[StdlibSelfHealthOracle] query_signals failed",
                exc_info=True,
            )
            return (OracleSignal(
                oracle_name=_ORACLE_NAME,
                kind=OracleKind.HEALTHCHECK,
                verdict=OracleVerdict.DISABLED,
                observed_at_ts=time.time(),
                summary="oracle internal failure",
                payload={"reason": "query_signals_exception"},
                severity=0.0,
            ),)


def register_shipped_invariants() -> list:
    """Pins required functions, classes, and tunable constants."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "stdlib_self_health_enabled",
        "lookback_sessions",
        "baseline_cost_usd",
        "_completion_signal",
        "_cost_signal",
        "_stop_reason_signal",
        "register_shipped_invariants",
    )
    REQUIRED_CLASSES = ("StdlibSelfHealthOracle",)
    REQUIRED_CONSTANTS = (
        "_DEFAULT_LOOKBACK",
        "_DEFAULT_BASELINE_COST",
        "_DEFAULT_HEALTHY_COMPLETION",
        "_DEFAULT_DEGRADED_COMPLETION",
        "_ORACLE_NAME",
    )

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        seen_classes: set = set()
        seen_constants: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.ClassDef):
                seen_classes.add(node.name)
            elif isinstance(node, _ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, _ast.Name):
                        seen_constants.add(tgt.id)
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"stdlib_self_health_oracle MUST NOT "
                            f"call {node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        for const in REQUIRED_CONSTANTS:
            if const not in seen_constants:
                violations.append(f"missing constant {const!r}")
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "stdlib_self_health_oracle.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="stdlib_self_health_oracle_substrate",
            target_file=target,
            description=(
                "Offline empirical anchor: 3 signal builders + "
                "StdlibSelfHealthOracle class + tunable constants "
                "all present; no dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "StdlibSelfHealthOracle",
    "stdlib_self_health_enabled",
    "lookback_sessions",
    "baseline_cost_usd",
    "register_shipped_invariants",
]
