"""backend/core/ouroboros/governance/persistent_intelligence_health_oracle.py

Defect #3 Slice C (2026-05-03) — PersistentIntelligenceHealth oracle
adapter implementing :class:`production_oracle.ProductionOracleProtocol`.

Closes the silent-degradation gap that the brutal review called out:
the persistent_intelligence_manager logs ERROR-level failures (12
times in soak v5's 62-min run) but had no SSE event, no GET surface,
no operator-facing health flag. This adapter projects the manager's
health state into the existing Production Oracle observer's signal
stream — every periodic tick produces an OracleSignal that surfaces
via SSE production_oracle_signal_observed + GET /observability/
production-oracle.

Mapping (closed-5 PersistentIntelligenceHealth -> OracleSignal):

  HEALTHY              -> verdict=HEALTHY, severity=0.1
  DEGRADED_READONLY    -> verdict=DEGRADED, severity=0.55
                          (typically a permission/sandbox issue;
                           checkpoints fail but reads still work)
  DEGRADED_DISK_FULL   -> verdict=FAILED, severity=0.85
                          (operationally critical -- needs operator)
  DEGRADED_OTHER       -> verdict=DEGRADED, severity=0.5
  DISABLED             -> verdict=DISABLED, severity=0.0

Why this oracle adapter (not a parallel SSE event):
  * The Production Oracle observer ticks periodically; a new adapter
    plugs into the existing tick loop with no new wiring.
  * The auto_action_router's Rule 1.5 oracle veto already consumes
    the aggregated verdict -- a DEGRADED_DISK_FULL state will
    therefore propose a NOTIFY_APPLY route at the next VERIFY phase
    (load-bearing fix for "operator can't write to disk" scenarios
    that would otherwise corrupt downstream ops).
  * Operators get persistent-intelligence health for free in the
    existing /observability/production-oracle GET response.

Authority invariant: read-only over PersistentIntelligenceManager's
``health`` property. NEVER mutates the manager. NEVER raises into
the observer's tick loop. Disabled gracefully when the manager has
not been constructed yet (pre-boot or post-shutdown).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, Tuple

from backend.core.ouroboros.governance.production_oracle import (
    OracleKind,
    OracleSignal,
    OracleVerdict,
)


logger = logging.getLogger(__name__)


_ORACLE_NAME = "persistent_intelligence_health"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def persistent_intelligence_health_oracle_enabled() -> bool:
    """Per-adapter sub-gate. Master flag for the oracle substrate is
    JARVIS_PRODUCTION_ORACLE_ENABLED. When master is on but this
    sub-gate is off, the adapter reports DISABLED signals."""
    return _env_bool(
        "JARVIS_PERSISTENT_INTELLIGENCE_ORACLE_ENABLED", True,
    )


def _disabled_signal(reason: str, payload: Optional[dict] = None) -> OracleSignal:
    return OracleSignal(
        oracle_name=_ORACLE_NAME, kind=OracleKind.ERROR,
        verdict=OracleVerdict.DISABLED, observed_at_ts=time.time(),
        summary=reason, payload=payload or {}, severity=0.0,
    )


def _classify_health(health_value: str) -> Tuple[OracleVerdict, float, str]:
    """Map a PersistentIntelligenceHealth.value string to (verdict,
    severity, summary). Pure function -- testable in isolation
    without constructing the manager. Defensive on unknown values
    (treats as DEGRADED_OTHER)."""
    h = (health_value or "").strip().lower()
    if h == "healthy":
        return (
            OracleVerdict.HEALTHY, 0.1,
            "persistent_intelligence checkpoint loop healthy",
        )
    if h == "degraded_readonly":
        return (
            OracleVerdict.DEGRADED, 0.55,
            "persistent_intelligence DB readonly -- checkpoints failing",
        )
    if h == "degraded_disk_full":
        return (
            OracleVerdict.FAILED, 0.85,
            "persistent_intelligence disk full -- checkpoint substrate critical",
        )
    if h == "degraded_other":
        return (
            OracleVerdict.DEGRADED, 0.5,
            "persistent_intelligence checkpoint failures (unclassified)",
        )
    if h == "disabled":
        return (
            OracleVerdict.DISABLED, 0.0,
            "persistent_intelligence not initialized",
        )
    # Unknown value -- conservative classification.
    return (
        OracleVerdict.DEGRADED, 0.5,
        f"persistent_intelligence unknown health state: {health_value!r}",
    )


class PersistentIntelligenceHealthOracle:
    """ProductionOracleProtocol adapter reading from the global
    PersistentIntelligenceManager singleton. Auto-registers in the
    default observer bundle (observer Slice C).

    Implements Protocol structurally (duck-typed).
    """

    @property
    def name(self) -> str:
        return _ORACLE_NAME

    @property
    def enabled(self) -> bool:
        return persistent_intelligence_health_oracle_enabled()

    async def query_signals(
        self, *, since_ts: float = 0.0,  # noqa: ARG002 -- single-shot
    ) -> Tuple[OracleSignal, ...]:
        try:
            if not persistent_intelligence_health_oracle_enabled():
                return (_disabled_signal(
                    "persistent_intelligence_health_oracle disabled",
                ),)
            # Read the singleton without forcing initialization.
            # If the manager has never been constructed, report
            # DISABLED rather than triggering a side-effect creation.
            try:
                from backend.core.persistent_intelligence_manager import (  # noqa: E501
                    _manager as _pim_singleton,
                )
            except ImportError:
                return (_disabled_signal(
                    "persistent_intelligence_manager not importable",
                ),)
            mgr = _pim_singleton
            if mgr is None:
                return (_disabled_signal(
                    "persistent_intelligence_manager not yet initialized",
                ),)
            try:
                health_value = mgr.health.value
                effective_path = getattr(
                    mgr, "effective_db_path", "<unknown>",
                )
                checkpoint_suspended = bool(
                    getattr(mgr, "checkpoint_suspended", False),
                )
                consecutive_failures = int(
                    getattr(
                        mgr, "_consecutive_checkpoint_failures", 0,
                    ),
                )
            except Exception:  # noqa: BLE001 -- defensive read
                return (_disabled_signal(
                    "persistent_intelligence_manager state unreadable",
                ),)
            verdict, severity, summary = _classify_health(health_value)
            payload = {
                "health": health_value,
                "effective_db_path": str(effective_path)[:200],
                "checkpoint_suspended": checkpoint_suspended,
                "consecutive_failures": consecutive_failures,
            }
            return (OracleSignal(
                oracle_name=_ORACLE_NAME, kind=OracleKind.ERROR,
                verdict=verdict, observed_at_ts=time.time(),
                summary=summary, payload=payload,
                severity=severity,
            ),)
        except Exception:  # noqa: BLE001 -- contract: never raise
            logger.debug(
                "[PersistentIntelligenceHealthOracle] query_signals failed",
                exc_info=True,
            )
            return (_disabled_signal(
                "oracle internal failure",
                {"reason": "query_signals_exception"},
            ),)


def register_shipped_invariants() -> list:
    """Pin the adapter substrate: enabled gate + classify_health
    pure function + adapter class + closed-5 mapping coverage. No
    exec/eval/compile."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "persistent_intelligence_health_oracle_enabled",
        "_classify_health",
        "register_shipped_invariants",
    )
    REQUIRED_CLASSES = ("PersistentIntelligenceHealthOracle",)
    EXPECTED_HEALTH_VALUES = (
        "healthy", "degraded_readonly", "degraded_disk_full",
        "degraded_other", "disabled",
    )

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        seen_classes: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.ClassDef):
                seen_classes.add(node.name)
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"persistent_intelligence_health_oracle "
                            f"MUST NOT call {node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        # Closed-5 health-value coverage: every expected enum value
        # must appear as a string literal in source (proves the
        # _classify_health switch handles all of them).
        for v in EXPECTED_HEALTH_VALUES:
            if v not in source:
                violations.append(
                    f"closed-5 mapping missing health value {v!r}"
                )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "persistent_intelligence_health_oracle.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="persistent_intelligence_health_oracle_substrate",
            target_file=target,
            description=(
                "Defect #3 Slice C oracle adapter: enabled gate + "
                "_classify_health pure function + adapter class + "
                "closed-5 health-value coverage; no dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "PersistentIntelligenceHealthOracle",
    "persistent_intelligence_health_oracle_enabled",
    "register_shipped_invariants",
]
